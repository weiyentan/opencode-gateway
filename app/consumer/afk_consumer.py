"""Live AFK outcome consumer (issue #451).

Consumes the existing, config-driven provider-events topic in its OWN
Kafka consumer group (``opencode-outcomes`` — never the usage consumer's
``opencode-gateway`` group), mapping each message type to a canonical
engineering event/entity and writing it to Postgres in a single database
transaction (``delivery_log`` + ``engineering_events`` through the #448
``AsyncpgOutcomeRepository``).  The Kafka offset is committed only after
that transaction succeeds; a crash after the DB commit but before the
offset commit re-delivers the message, which the dedup layers
(``delivery_log`` UNIQUE(provider, delivery_id) + ``engineering_events``
identity UNIQUE) absorb harmlessly.

Terminal states the topic does not carry (merged/closed) are converged by
a scheduled reconciliation loop reusing the #449 backfill engine
(``scripts.afk_backfill.run_backfill``) over a bounded, config-driven
window.

Operational pattern mirrors :mod:`app.consumer.consumer`: no auto-commit,
``earliest`` reset, DLQ for poison messages, exponential backoff with DLQ
and commit on exhaustion, graceful shutdown with in-flight drain.

This module deliberately imports ``afk_outcomes`` (pure domain) and the
backfill engine; it never re-implements normalization or correlation logic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord, OffsetAndMetadata, TopicPartition
from pydantic import BaseModel

from afk_outcomes.models import (
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)
from afk_outcomes.providers.github_http import GitHubHttpApi
from afk_outcomes.repository import AsyncpgOutcomeRepository
from app.core.metrics import DEFAULT_REGISTRY, MetricsRegistry
from scripts.afk_backfill import PrefetchedWindow, run_backfill

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

_DEFAULT_TOPIC = "afk.events"
_DEFAULT_DLQ_TOPIC = "afk.events-dlq"
_DEFAULT_CONSUMER_GROUP_ID = "opencode-outcomes"

_DEFAULT_RECONCILE_CADENCE_SECONDS = 3600.0
_DEFAULT_RECONCILE_WINDOW_SECONDS = 86400.0

_MAX_RETRIES = 5
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0

# DLQ operational max (issue #483): the afk.events-dlq topic is retained until
# resolved, but never unbounded — messages older than this many days are
# escalated/expired by the DLQ sweep.  Mirrors GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS.
_DEFAULT_DLQ_MAX_AGE_DAYS = 30
_DEFAULT_DLQ_ESCALATION_TOPIC = "afk.events-dlq-expired"
_DEFAULT_DLQ_SWEEP_GROUP_ID = "opencode-outcomes-dlq-sweep"

# Backoff jitter multiplier bounds (issue #482).  The retry delay is
# ``base * uniform(_JITTER_LOW, _JITTER_HIGH)`` so concurrent consumers that
# fail together do not retry in lockstep.
_JITTER_LOW = 0.5
_JITTER_HIGH = 1.5

# ── Metric names (stable — do not rename; downstream dashboards depend) ─────
#
# Per-state counters, a retry histogram, a DLQ-depth gauge, and per-partition
# lag/committed-offset gauges.  Partition-scoped gauges append the integer
# partition id (e.g. ``afk_consumer.lag.0``).  All are process-local values
# registered on :mod:`app.core.metrics`; there is no Prometheus server yet.

METRIC_MESSAGES_TOTAL = "afk_consumer.messages.total"
METRIC_MESSAGES_ACCEPTED = "afk_consumer.messages.accepted"
METRIC_MESSAGES_DLQ = "afk_consumer.messages.dlq"
METRIC_MESSAGES_POISON = "afk_consumer.messages.poison"
METRIC_RETRIES = "afk_consumer.retries"
METRIC_RETRIES_PER_MESSAGE = "afk_consumer.retries.per_message"
METRIC_DB_ERRORS = "afk_consumer.db_errors"
METRIC_DLQ_DEPTH = "afk_consumer.dlq.depth"
METRIC_COMMITTED_OFFSET = "afk_consumer.committed_offset"
METRIC_LAG = "afk_consumer.lag"

# ── Canonical event type vocabulary ──────────────────────────────────────────
#
# The locked set of canonical event types the outcome layer recognises.
# ``map_normalized_event`` validates its bridged ``event_type`` against this
# vocabulary; an unknown resource type or action that does not resolve to a
# canonical event type returns ``None`` (unmappable → DLQ, never persisted).

_CANONICAL_EVENT_TYPES = frozenset(
    {
        "issue.opened",
        "issue.closed",
        "change_request.opened",
        "change_request.review_requested",
        "change_request.changes_requested",
        "change_request.approved",
        "change_request.merged",
        "change_request.closed",
        "pipeline.failed",
        "pipeline.succeeded",
    }
)


class _NormalizedResource(BaseModel):
    """The nested ``resource`` object in a v1 normalized event.

    Carries the producer's native resource vocabulary (``pull_request`` /
    ``merge_request`` / ``issue``) — never the outcome layer's canonical
    ``change_request`` vocabulary.
    """

    resource_type: str
    resource_id: str
    repository: str
    action: str


class _RedactedPayload(BaseModel):
    """The nested ``redacted_payload`` object in a v1 normalized event.

    ``reference`` is a *reference* to the redacted payload, never the
    payload itself.  ``provider`` and ``delivery_id`` must equal the
    envelope's ``provider`` and ``delivery_id`` (reference mismatch → DLQ).
    """

    reference: str | None = None
    provider: str | None = None
    delivery_id: str | None = None


class NormalizedProviderEvent(BaseModel):
    """One normalized provider event (fast-api-eda-gateway #97-#102).

    The Stage-2 mapping bridge input: a schema-versioned, provider-agnostic
    shape that carries the provider's native ``pull_request`` / ``merge_request``
    / ``issue`` ``resource_type`` — never the outcome layer's canonical
    ``change_request`` vocabulary.  ``resource_id`` is the stable
    provider-scoped resource identity (the value behind ``entity_id``);
    ``payload_ref`` is a *reference* to the redacted payload, never the
    payload itself; ``ingested_at`` is the producer's ingest timestamp.

    The v1 nested envelope (issue #495) wraps resource fields in a
    ``resource`` object and the payload reference in a ``redacted_payload``
    object.  The flat shape (issue #482) is also accepted.
    """

    model_config = {"ser_json_exclude_none": True}

    schema_version: str
    provider: Provider
    delivery_id: str
    resource_type: str = ""
    resource_id: str = ""
    repository: str = ""
    action: str = ""
    occurred_at: datetime
    ingested_at: datetime | None = None
    actor: str | None = None
    payload_ref: str | None = None
    resource: _NormalizedResource | None = None
    redacted_payload: _RedactedPayload | None = None

    @property
    def effective_resource_type(self) -> str:
        """Return ``resource_type`` from the nested object when present."""
        if self.resource is not None:
            return self.resource.resource_type
        return self.resource_type

    @property
    def effective_resource_id(self) -> str:
        """Return ``resource_id`` from the nested object when present."""
        if self.resource is not None:
            return self.resource.resource_id
        return self.resource_id

    @property
    def effective_repository(self) -> str:
        """Return ``repository`` from the nested object when present."""
        if self.resource is not None:
            return self.resource.repository
        return self.repository

    @property
    def effective_action(self) -> str:
        """Return ``action`` from the nested object when present."""
        if self.resource is not None:
            return self.resource.action
        return self.action

    @property
    def effective_payload_ref(self) -> str | None:
        """Return ``payload_ref`` from the nested redacted_payload when present."""
        if self.redacted_payload is not None:
            return self.redacted_payload.reference
        return self.payload_ref


# ── Normalized resource-type → canonical entity-type bridge (issue #482) ────
#
# The producer's resource types are provider-specific: ``pull_request``
# (GitHub) and ``merge_request`` (GitLab) are the *same* outcome-layer
# concept, ``change_request`` (CONTEXT.md / ADR 0020).  ``issue`` is
# unchanged.

_RESOURCE_TYPE_TO_ENTITY_TYPE: dict[str, EntityType] = {
    "issue": EntityType.ISSUE,
    "pull_request": EntityType.CHANGE_REQUEST,
    "merge_request": EntityType.CHANGE_REQUEST,
}


def _coerce_resource_number(resource_id: str) -> int | None:
    """Return ``resource_id`` as an int when it is a plain number, else None."""
    try:
        return int(resource_id)
    except (TypeError, ValueError):
        return None


# ── Repository URL normalization (issue #495) ────────────────────────────────
#
# Repository identity is derived strictly from the producer repository URL.
# The normalization rules are deterministic and reject invalid identity rather
# than falling back to a display label:
#
# 1. Require absolute HTTP(S) — reject non-HTTP schemes and relative URLs.
# 2. Lowercase the hostname.
# 3. Remove credentials (userinfo), query strings, and fragments.
# 4. Strip default ports (80 for http, 443 for https).
# 5. Strip a trailing slash.
# 6. Strip a terminal ``.git`` suffix.
# 7. Preserve path spelling (case-sensitive).
# 8. Reject empty or invalid identity.

import re as _re
from urllib.parse import urlparse as _urlparse

_REPO_URL_RE = _re.compile(r"^https?://", _re.IGNORECASE)


def normalize_repository_url(raw: str) -> str | None:
    """Normalize a repository URL into a deterministic identity string.

    Returns the normalized identity, or ``None`` when the URL is invalid
    (not an absolute HTTP(S) URL, empty, or unparseable).
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not _REPO_URL_RE.match(raw):
        return None

    try:
        parsed = _urlparse(raw)
    except Exception:
        return None

    if not parsed.hostname:
        return None

    # Lowercase the hostname.
    host = parsed.hostname.lower()

    # Strip default ports; preserve non-default ports.
    port = ""
    if parsed.port is not None:
        is_default = (parsed.scheme == "http" and parsed.port == 80) or (
            parsed.scheme == "https" and parsed.port == 443
        )
        if not is_default:
            port = f":{parsed.port}"

    # Build the normalized URL: scheme + host + optional non-default port + path.
    path = parsed.path or ""

    # Strip trailing slash.
    path = path.rstrip("/")

    # Strip terminal .git suffix.
    if path.endswith(".git"):
        path = path[:-4]

    # Remove leading slash for the canonical owner/repo form.
    path = path.lstrip("/")

    if not path:
        return None

    return f"{host}{port}/{path}"


# ── Normalized event validation (issue #495) ─────────────────────────────────
#
# The validation boundary distinguishes valid v1 lifecycle observations from
# malformed data and unsupported versions before mapping or persistence.
# Each violation class produces a distinct DLQ reason string.

_VALID_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})


class NormalizedEventValidationError(ValueError):
    """A normalized event failed validation with a distinct reason.

    The ``reason`` is a stable string suitable for DLQ routing; it is
    distinct per violation class so operators can triage without inspecting
    the payload.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_normalized_event(message: NormalizedProviderEvent) -> None:
    """Validate a normalized event before mapping or persistence.

    Raises :class:`NormalizedEventValidationError` with a distinct reason
    for each violation class:

    * **Unsupported schema version** — ``schema_version`` is not ``"1.0"``.
    * **Invalid repository identity** — the repository URL cannot be
      normalized to a valid identity.
    * **Reference mismatch** — the ``redacted_payload`` provider or
      delivery_id does not equal the envelope's provider or delivery_id.

    The caller routes the message to the DLQ with the reason string.
    """
    # ── Schema version ──────────────────────────────────────────────
    if message.schema_version not in _VALID_SCHEMA_VERSIONS:
        raise NormalizedEventValidationError(
            f"Unsupported schema version: {message.schema_version!r} "
            f"(supported: {sorted(_VALID_SCHEMA_VERSIONS)})"
        )

    # ── Repository identity ─────────────────────────────────────────
    # Only validate repository identity for the nested v1 shape (where
    # the repository is always a URL).  The flat shape uses plain
    # owner/repo strings that are not URLs.
    repo = message.effective_repository
    if message.resource is not None:
        normalized = normalize_repository_url(repo)
        if normalized is None:
            raise NormalizedEventValidationError(
                f"Invalid repository identity: {repo!r} — "
                f"must be an absolute HTTP(S) URL with a valid hostname and path"
            )

    # ── Reference mismatch (nested redacted_payload only) ───────────
    if message.redacted_payload is not None:
        rp = message.redacted_payload
        if rp.provider is not None and rp.provider != message.provider.value:
            raise NormalizedEventValidationError(
                f"Reference mismatch: redacted_payload.provider={rp.provider!r} "
                f"!= envelope.provider={message.provider.value!r}"
            )
        if rp.delivery_id is not None and rp.delivery_id != message.delivery_id:
            raise NormalizedEventValidationError(
                f"Reference mismatch: redacted_payload.delivery_id={rp.delivery_id!r} "
                f"!= envelope.delivery_id={message.delivery_id!r}"
            )


def map_normalized_event(
    message: NormalizedProviderEvent,
) -> tuple[EngineeringEntity, EngineeringEvent] | None:
    """Bridge a normalized event into the outcome layer's canonical vocabulary.

    ``resource_type`` selects the canonical entity type; ``action`` becomes
    the canonical event-type suffix.  The resulting ``event_type`` is then
    validated against the locked canonical vocabulary: a resource type or
    action that does not produce a canonical event type returns ``None`` (the
    caller routes it to the DLQ as unmappable — never persisted, never
    conflated with the legacy types).

    Returns ``None`` when ``resource_type`` is unknown or ``action`` does not
    resolve to a locked canonical event type.
    """
    resource_type = message.effective_resource_type
    resource_id = message.effective_resource_id
    repository = message.effective_repository
    action = message.effective_action
    payload_ref = message.effective_payload_ref

    entity_type = _RESOURCE_TYPE_TO_ENTITY_TYPE.get(resource_type)
    if entity_type is None:
        return None
    event_type = f"{entity_type.value}.{action}"
    if event_type not in _CANONICAL_EVENT_TYPES:
        return None
    entity_id = f"{entity_type.value}:{resource_id}"
    entity = EngineeringEntity(
        entity_id=entity_id,
        entity_type=entity_type,
        provider=message.provider,
        repository=repository,
        number=_coerce_resource_number(resource_id),
    )
    payload: dict[str, Any] = {}
    if payload_ref is not None:
        payload["payload_ref"] = payload_ref
    event = EngineeringEvent(
        event_id=f"{entity_id}:{action}",
        event_type=event_type,
        provider=message.provider,
        entity_id=entity_id,
        occurred_at=message.occurred_at,
        actor=message.actor,
        payload=payload,
    )
    return entity, event


def map_provider_event(
    message: NormalizedProviderEvent,
) -> tuple[EngineeringEntity, EngineeringEvent] | None:
    """Map a normalized provider event to the canonical entity + event (or None).

    Bridges the producer's native resource vocabulary into the outcome layer's
    canonical vocabulary via :func:`map_normalized_event`.  Returns ``None``
    when the message cannot be mapped — the caller treats that as a poison
    message (DLQ, no DB write).
    """
    return map_normalized_event(message)


class AFKOutcomeConsumer:
    """Consumes provider-events messages and writes them to Postgres.

    Error-handling behaviour:

    * **valid message** — persist in a single transaction, then commit the
      offset (commit only after the transaction succeeds).
    * **invalid JSON / invalid shape / unmappable type** — send to DLQ, then
      commit (poison messages must not block the group).
    * **DB error** — retry with exponential backoff; on success, commit the
      offset.  On exhaustion (max retries reached), send the message to the
      DLQ and commit, so it is not silently lost and does not block the
      group.
    """

    def __init__(
        self,
        *,
        kafka_brokers: str,
        pool: asyncpg.Pool,
        provider: Provider,
        repository: str,
        adapter: Any,
        topic: str = _DEFAULT_TOPIC,
        dlq_topic: str = _DEFAULT_DLQ_TOPIC,
        consumer_group_id: str = _DEFAULT_CONSUMER_GROUP_ID,
        reconcile_cadence_seconds: float = _DEFAULT_RECONCILE_CADENCE_SECONDS,
        reconcile_window_seconds: float = _DEFAULT_RECONCILE_WINDOW_SECONDS,
        max_retries: int = _MAX_RETRIES,
        initial_backoff: float = _INITIAL_BACKOFF_SECONDS,
        max_backoff: float = _MAX_BACKOFF_SECONDS,
        dlq_max_age_days: int = _DEFAULT_DLQ_MAX_AGE_DAYS,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._kafka_brokers = kafka_brokers
        self._pool = pool
        self._provider = provider
        self._repository = repository
        self._adapter = adapter
        self._topic = topic
        self._dlq_topic = dlq_topic
        self._consumer_group_id = consumer_group_id
        self._reconcile_cadence_seconds = reconcile_cadence_seconds
        self._reconcile_window_seconds = reconcile_window_seconds
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._dlq_max_age_days = dlq_max_age_days
        self._metrics = metrics if metrics is not None else DEFAULT_REGISTRY

        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._in_flight: asyncio.Task[Any] | None = None
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._owns_pool = False
        self._adapter_client: Any = None

        # Per-partition commit frontier (issue #459, PR #458 finding 1).  The
        # Kafka consumer's in-memory position advances past a fetched message
        # the moment it is returned, so an argless ``commit()`` would commit
        # past a message that failed to persist/DLQ and permanently skip it.
        # We track the highest *consecutive* offset we may safely commit, plus
        # any offset that failed and must be redelivered before the frontier
        # may advance (a "gap").  See ``_mark_committable`` / ``_mark_blocked``.
        self._committable: dict[TopicPartition, int] = {}
        self._blocked: dict[TopicPartition, int] = {}

        # Highest offset observed per partition (lag/backlog metric source).
        # Reset alongside the commit frontier when the consumer is recreated.
        self._last_seen: dict[TopicPartition, int] = {}

        # Eagerly register the process-local metrics so the full per-state
        # surface exists from startup with zero defaults (a snapshot never
        # omits an expected counter).  Partition-scoped lag gauges are created
        # lazily on first commit.
        self._metrics.counter(METRIC_MESSAGES_TOTAL)
        self._metrics.counter(METRIC_MESSAGES_ACCEPTED)
        self._metrics.counter(METRIC_MESSAGES_DLQ)
        self._metrics.counter(METRIC_MESSAGES_POISON)
        self._metrics.counter(METRIC_RETRIES)
        self._metrics.counter(METRIC_DB_ERRORS)
        self._metrics.gauge(METRIC_DLQ_DEPTH)
        self._metrics.histogram(METRIC_RETRIES_PER_MESSAGE)

    # ── Factory ────────────────────────────────────────────────────────

    @classmethod
    async def from_env(cls) -> AFKOutcomeConsumer:
        """Build a consumer from environment variables and application settings.

        Required: ``GATEWAY_KAFKA_BROKERS``, a reachable Postgres (via
        ``GATEWAY_DATABASE_*``), ``GATEWAY_AFK_OUTCOMES_REPOSITORY`` (the
        owner/repo to reconcile), and provider credentials in the adapters'
        standard env vars (``GITHUB_TOKEN`` / ``GITLAB_TOKEN``).

        Fails fast with :class:`ValueError` when
        ``GATEWAY_AFK_OUTCOMES_REPOSITORY`` is empty/absent: the consumer
        always reconciles against this repository, so an empty value would
        otherwise start a reconcile loop that silently retries forever
        against an adapter error.
        """
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.afk_outcomes_repository.strip():
            raise ValueError(
                "GATEWAY_AFK_OUTCOMES_REPOSITORY must be set: the AFK outcome "
                "consumer reconciles a bounded window against this repository; "
                "without it the reconcile loop would silently retry forever."
            )
        provider = Provider(settings.afk_outcomes_provider)
        pool = await asyncpg.create_pool(
            host=settings.database_host,
            port=settings.database_port,
            database=settings.database_name,
            user=settings.database_user,
            password=settings.database_password,
            min_size=1,
            max_size=2,
        )
        adapter, client = _build_adapter(provider)

        consumer = cls(
            kafka_brokers=settings.kafka_brokers,
            pool=pool,
            provider=provider,
            repository=settings.afk_outcomes_repository,
            adapter=adapter,
            topic=settings.afk_outcomes_topic,
            dlq_topic=settings.afk_outcomes_dlq_topic,
            consumer_group_id=settings.afk_outcomes_consumer_group_id,
            reconcile_cadence_seconds=settings.afk_outcomes_reconcile_cadence_seconds,
            reconcile_window_seconds=settings.afk_outcomes_reconcile_window_seconds,
            max_retries=settings.afk_outcomes_max_retries,
            initial_backoff=settings.afk_outcomes_initial_backoff_seconds,
            max_backoff=settings.afk_outcomes_max_backoff_seconds,
            dlq_max_age_days=settings.retention_dlq_max_age_days,
        )
        consumer._owns_pool = True
        consumer._adapter_client = client
        return consumer

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise Kafka consumer/producer and begin polling + reconciliation."""
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._kafka_brokers,
            group_id=self._consumer_group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=None,  # Deserialization is done in _process_message
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._kafka_brokers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )

        await self._consumer.start()
        await self._producer.start()
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._signal_handler)
            except NotImplementedError:
                # Signal handlers not available on this platform
                pass

        self._reconcile_task = asyncio.ensure_future(self._reconcile_loop())

        logger.info(
            "AFK outcome consumer started: topic=%s group=%s",
            self._topic,
            self._consumer_group_id,
        )

    async def run(self) -> None:
        """Run the main poll loop (mirrors the usage consumer's run)."""
        if self._consumer is None:
            raise RuntimeError("Consumer not started — call start() first")

        while self._running:
            try:
                iterator = self._consumer.__aiter__()
                while self._running:
                    try:
                        msg = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=1.0,
                        )
                    except TimeoutError:
                        continue  # re-check _running
                    except StopAsyncIteration:
                        break

                    try:
                        self._in_flight = asyncio.ensure_future(
                            self._process_message(msg)
                        )
                        await self._in_flight
                    except KafkaError:
                        # A Kafka commit or DLQ-publish failure means the broker
                        # connection is unreliable.  Recreating the consumer
                        # re-reads from the last committed offset, so an
                        # already-persisted (or DLQ'd) message is redelivered
                        # idempotently via the delivery_log/engineering_events
                        # dedup, and an un-persisted message is retried.  Never
                        # mark an already-persisted message as a blocked
                        # processing gap (issue #473).
                        logger.exception(
                            "Kafka error processing message at offset %d — "
                            "recreating consumer for safe redelivery",
                            msg.offset,
                        )
                        if self._running:
                            await self._recreate_consumer()
                        break  # re-establish a fresh iterator from the new consumer
                    except Exception:
                        # A message that failed to be processed for a non-Kafka
                        # reason must not be committed past: open a gap so the
                        # committed offset never skips it, and it is redelivered
                        # on the next rebalance/restart.
                        self._mark_blocked(msg)
                        logger.exception(
                            "Unhandled exception processing message "
                            "at offset %d — blocking commit (redeliver after "
                            "rebalance)",
                            msg.offset,
                        )
                    finally:
                        self._in_flight = None
            except KafkaError:
                if self._running:
                    logger.exception(
                        "Kafka error in poll loop — re-creating consumer"
                    )
                    await self._recreate_consumer()
            except asyncio.CancelledError:
                logger.info("AFK outcome consumer poll loop cancelled")
                break

    async def stop(self) -> None:
        """Gracefully shut down the consumer (drain in-flight, stop reconciler)."""
        logger.info("Stopping AFK outcome consumer …")
        self._running = False
        self._shutdown_event.set()

        # Stop the reconciliation loop.
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
        self._reconcile_task = None

        # Wait for in-flight processing to finish.
        if self._in_flight is not None and not self._in_flight.done():
            try:
                await asyncio.wait_for(self._in_flight, timeout=30.0)
            except TimeoutError:
                logger.warning("In-flight message timed out during shutdown")
                self._in_flight.cancel()

        if self._consumer:
            await self._consumer.stop()
        if self._producer:
            await self._producer.stop()
        if self._adapter_client is not None:
            with contextlib.suppress(Exception):
                await self._adapter_client.aclose()
        if self._owns_pool:
            await self._pool.close()

        logger.info("AFK outcome consumer stopped")

    # ── Signal handling ────────────────────────────────────────────────

    def _signal_handler(self) -> None:
        """Handle SIGTERM / SIGINT — trigger graceful shutdown."""
        logger.info("Shutdown signal received — draining …")
        self._running = False
        self._shutdown_event.set()

    # ── Internal helpers ────────────────────────────────────────────────

    async def _recreate_consumer(self) -> None:
        """Stop and recreate the Kafka consumer after a connection error."""
        if self._consumer:
            await self._consumer.stop()

        # A fresh consumer re-reads from the last committed offset, so reset the
        # in-memory commit frontier and block set: stale positions must never
        # leak across a recreation (they would commit past a gap or leave a
        # block that never clears — issue #473).
        self._committable = {}
        self._blocked = {}
        self._last_seen = {}

        delay = self._initial_backoff
        max_attempts = max(1, self._max_retries * 2)
        for attempt in range(max_attempts):
            if not self._running:
                return
            try:
                self._consumer = AIOKafkaConsumer(
                    self._topic,
                    bootstrap_servers=self._kafka_brokers,
                    group_id=self._consumer_group_id,
                    enable_auto_commit=False,
                    auto_offset_reset="earliest",
                    value_deserializer=None,
                )
                await self._consumer.start()
                return
            except KafkaError:
                if attempt < max_attempts - 1 and self._running:
                    jitter = random.uniform(_JITTER_LOW, _JITTER_HIGH)
                    await asyncio.sleep(min(delay * jitter, self._max_backoff))
                    delay = min(delay * 2, self._max_backoff)
                    continue
                raise

    async def _process_message(self, msg: ConsumerRecord) -> None:
        """Process one message: deserialize → validate → map → persist → commit."""
        self._metrics.counter(METRIC_MESSAGES_TOTAL).inc()
        self._record_last_seen(msg)

        # ── Deserialise JSON ────────────────────────────────────────
        try:
            raw_value = json.loads(msg.value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            logger.warning(
                "Unparseable message (key=%s offset=%d) — sending to DLQ: %s",
                msg.key,
                msg.offset,
                exc,
            )
            await self._send_to_dlq(
                {"raw": msg.value.decode("utf-8", errors="replace")},
                reason=f"JSON decode failure: {exc}",
            )
            self._metrics.counter(METRIC_MESSAGES_POISON).inc()
            self._mark_committable(msg)
            await self._commit()
            return

        # ── Validate the payload ────────────────────────────────────
        try:
            message = self._parse_message(raw_value)
        except Exception:
            logger.warning(
                "Invalid message shape — sending to DLQ (key=%s offset=%d)",
                msg.key,
                msg.offset,
            )
            await self._send_to_dlq(
                raw_value if isinstance(raw_value, dict) else {},
                reason="Invalid message shape — failed Pydantic validation",
            )
            self._metrics.counter(METRIC_MESSAGES_POISON).inc()
            self._mark_committable(msg)
            await self._commit()
            return

        # ── Validate normalized events (issue #495) ─────────────────
        try:
            validate_normalized_event(message)
        except NormalizedEventValidationError as exc:
            logger.warning(
                "Normalized event validation failed — sending to DLQ "
                "(key=%s offset=%d): %s",
                msg.key,
                msg.offset,
                exc.reason,
            )
            await self._send_to_dlq(
                raw_value if isinstance(raw_value, dict) else {},
                reason=exc.reason,
            )
            self._metrics.counter(METRIC_MESSAGES_POISON).inc()
            self._mark_committable(msg)
            await self._commit()
            return

        # ── Map message type → canonical entity/event ───────────────
        mapped = map_provider_event(message)
        if mapped is None:
            logger.warning(
                "Unmappable message type %r — sending to DLQ (offset=%d)",
                self._message_type_label(message),
                msg.offset,
            )
            await self._send_to_dlq(
                raw_value if isinstance(raw_value, dict) else {},
                reason=f"Unmappable message type: {self._message_type_label(message)!r}",
            )
            self._metrics.counter(METRIC_MESSAGES_POISON).inc()
            self._mark_committable(msg)
            await self._commit()
            return
        entity, event = mapped

        # ── Persist in a single transaction, then commit ────────────
        # ``max(1, ...)`` is defense-in-depth: programmatic construction with
        # ``max_retries=0`` must never produce an empty loop that silently
        # drops the message without persisting, DLQ'ing, or committing.
        retries = 0
        for attempt in range(max(1, self._max_retries)):
            try:
                await self._persist(message, entity, event)
            except Exception:
                self._metrics.counter(METRIC_DB_ERRORS).inc()
                if attempt < self._max_retries - 1:
                    retries += 1
                    self._metrics.counter(METRIC_RETRIES).inc()
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "DB error (attempt %d/%d) — retrying in %.1fs",
                        attempt + 1,
                        self._max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Max retries (%d) exhausted after DB errors — "
                    "sending to DLQ",
                    self._max_retries,
                )
                self._metrics.histogram(METRIC_RETRIES_PER_MESSAGE).observe(retries)
                await self._send_to_dlq(
                    raw_value if isinstance(raw_value, dict) else {},
                    reason=f"DB persist failed after {self._max_retries} retries",
                )
                self._mark_committable(msg)
                await self._commit()
                return

            self._metrics.histogram(METRIC_RETRIES_PER_MESSAGE).observe(retries)
            self._metrics.counter(METRIC_MESSAGES_ACCEPTED).inc()
            self._mark_committable(msg)
            await self._commit()
            return

    @staticmethod
    def _parse_message(
        raw_value: Any,
    ) -> NormalizedProviderEvent:
        """Parse a raw payload into a normalized provider event model.

        Only producer-owned normalized-event versions are accepted.  The
        nested v1 shape is identified by its ``resource`` object; the flat
        shape is identified by its ``resource_type`` field.  Raises
        :class:`pydantic.ValidationError` when the payload matches neither
        shape.
        """
        return NormalizedProviderEvent.model_validate(raw_value)

    @staticmethod
    def _message_type_label(
        message: NormalizedProviderEvent,
    ) -> str:
        """Return the human-readable message type for logs/DLQ reasons."""
        return f"{message.effective_resource_type}.{message.effective_action}"

    def _retry_delay(self, attempt: int) -> float:
        """Bounded exponential backoff with jitter (issue #482).

        ``base = min(initial * 2^attempt, max)``, then scaled by a uniform
        jitter factor in ``[0.5, 1.5]`` so concurrent consumers that fail
        together do not retry in lockstep.
        """
        base = min(self._initial_backoff * (2.0**attempt), self._max_backoff)
        return base * random.uniform(_JITTER_LOW, _JITTER_HIGH)

    def _record_last_seen(self, msg: ConsumerRecord) -> None:
        """Track the highest offset observed per partition (lag metric source)."""
        tp = TopicPartition(msg.topic, msg.partition)
        prev = self._last_seen.get(tp)
        if prev is None or msg.offset > prev:
            self._last_seen[tp] = msg.offset

    async def _persist(
        self,
        message: NormalizedProviderEvent,
        entity: EngineeringEntity,
        event: EngineeringEvent,
    ) -> None:
        """Write delivery_log + event in a single transaction (via repository)."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                repo = AsyncpgOutcomeRepository(conn)
                await repo.record_event(
                    provider=message.provider,
                    delivery_id=message.delivery_id,
                    entity=entity,
                    event=event,
                )

    async def _commit(self) -> None:
        """Commit the highest consecutive offset per partition, never past a gap.

        Only the *consecutive* frontier is committed; a failed message opens a
        gap (see ``_mark_blocked``) that the frontier must not cross, so the
        failed offset stays redeliverable on rebalance.  The blocked offset is
        itself excluded from the committed range: the commit is capped at
        ``blocked - 1`` so a later message can never commit past an uncommitted
        gap, even if the in-memory frontier was advanced before a commit
        failure (issue #473).
        """
        if not self._consumer:
            return
        offsets: dict[TopicPartition, OffsetAndMetadata] = {}
        for tp, committable in self._committable.items():
            blocked = self._blocked.get(tp)
            if blocked is not None and committable >= blocked:
                # Never commit past the blocked offset: cap at blocked - 1 so
                # the failed message stays redeliverable.
                committable = blocked - 1
                if committable < 0:
                    continue  # the very first offset is blocked — nothing safe
            offsets[tp] = OffsetAndMetadata(committable + 1, "")
        if offsets:
            await self._consumer.commit(offsets)
            self._record_committed_offsets(offsets)

    def _record_committed_offsets(
        self, offsets: dict[TopicPartition, OffsetAndMetadata]
    ) -> None:
        """Record committed-offset and consumer-lag gauges after a commit.

        Lag is measured as the observed-minus-committed backlog for each
        partition — the highest offset seen so far (``_last_seen``) minus the
        just-committed offset — a lower-bound proxy for broker lag that needs
        no live ``highwater()`` round-trip.
        """
        for tp, om in offsets.items():
            committed = om.offset - 1
            self._metrics.gauge(f"{METRIC_COMMITTED_OFFSET}.{tp.partition}").set(committed)
            last_seen = self._last_seen.get(tp, committed)
            self._metrics.gauge(f"{METRIC_LAG}.{tp.partition}").set(
                max(0, last_seen - committed)
            )

    def _mark_committable(self, msg: ConsumerRecord) -> None:
        """Advance a partition's consecutive commit frontier past ``msg``.

        A message that previously failed opens a gap (``_blocked``); the
        frontier must not advance past that gap until the failed offset is
        itself redelivered and succeeds.
        """
        tp = TopicPartition(msg.topic, msg.partition)
        prev = self._committable.get(tp)
        blocked = self._blocked.get(tp)
        if blocked is not None:
            if msg.offset < blocked:
                return  # redelivery of an earlier message — does not clear the block
            if msg.offset > blocked:
                return  # still a gap — never advance past the failed offset
            # msg.offset == blocked: the failed message succeeded on redelivery.
            del self._blocked[tp]
        if prev is None or msg.offset == prev + 1:
            self._committable[tp] = msg.offset

    def _mark_blocked(self, msg: ConsumerRecord) -> None:
        """Record a failed message offset that must be reprocessed before this
        partition's commit frontier may advance. Keeps the earliest (lowest)
        failed offset: the frontier can only advance once the first gap is
        closed, and later failures cannot move the block forward."""
        tp = TopicPartition(msg.topic, msg.partition)
        current = self._blocked.get(tp)
        if current is None or msg.offset < current:
            self._blocked[tp] = msg.offset

    async def _send_to_dlq(self, payload: dict[str, Any], *, reason: str) -> None:
        """Send a message to the DLQ topic with context about the failure.

        The DLQ record is stamped with ``dead_lettered_at`` and
        ``max_age_days`` (the operational max) so its age is measurable by the
        DLQ sweep (issue #483) — the producer-path enforcement of the
        operational max.  Shape: ``{"original_topic", "reason", "payload",
        "dead_lettered_at", "max_age_days"}``.
        """
        if self._producer is None:
            return
        dlq_payload = build_dlq_payload(
            self._topic,
            reason,
            payload,
            max_age_days=self._dlq_max_age_days,
        )
        try:
            await self._producer.send_and_wait(self._dlq_topic, dlq_payload)
        except KafkaError:
            logger.exception("Failed to send message to DLQ topic %s", self._dlq_topic)
            raise
        # Record only after a successful publish (a failed send is not on the
        # DLQ).  ``dlq.depth`` is a depth proxy: this consumer only publishes
        # to the DLQ, so the gauge rises with each publish and is decremented
        # by a future DLQ-drainer.
        self._metrics.counter(METRIC_MESSAGES_DLQ).inc()
        self._metrics.gauge(METRIC_DLQ_DEPTH).inc()

    # ── Scheduled reconciliation ───────────────────────────────────────

    async def _reconcile_loop(self) -> None:
        """Run bounded-window reconciliation on a config-driven cadence.

        Reuses the #449 backfill engine (pull → correlate → persist) to
        converge terminal states (merged/closed) the topic does not carry.
        Runs independently of the consume loop and never touches Kafka
        offsets.
        """
        while self._running:
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciliation window failed — will retry next cycle")
            try:
                await asyncio.sleep(self._reconcile_cadence_seconds)
            except asyncio.CancelledError:
                raise

    async def _reconcile_once(self) -> None:
        """Reconcile one bounded window through the backfill engine.

        The provider network fetches run *before* the pooled connection is
        acquired, so the slow adapter calls never hold a pool connection
        open (which would contend with the consume-path ``_persist`` acquires
        on the shared ``max_size=2`` pool).
        """
        now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
        since = now - timedelta(seconds=self._reconcile_window_seconds)
        entities = await self._adapter.fetch_entities(
            self._repository, since=since, until=now
        )
        events = await self._adapter.fetch_events(
            self._repository, since=since, until=now
        )
        async with self._pool.acquire() as conn:
            await run_backfill(
                conn,
                adapter=self._adapter,
                repository=self._repository,
                since=since,
                until=now,
                dry_run=False,
                prefetched=PrefetchedWindow(entities=entities, events=events),
            )


# ── DLQ operational max (issue #483) ─────────────────────────────────────────
#
# The ``afk.events-dlq`` topic is retained until resolved but must never grow
# unbounded.  Every DLQ record is stamped with ``dead_lettered_at`` and
# ``max_age_days`` (see ``build_dlq_payload``), and an operator-run sweep
# (``sweep_dlq`` / ``python -m app.consumer.afk_consumer --dlq-sweep``)
# escalates messages strictly older than the operational max to an escalation
# topic, preserving their payload + reason for manual resolution.  Physical
# removal from the DLQ is enforced by the topic's Kafka retention configured
# to the same max age (documented in ADR 0022); the escalation topic is the
# durable operator record, so nothing is ever silently lost.  Mirror
# ``scripts/retention_transcripts.py``: dry-run + bounded batches + a config
# driven window.


def build_dlq_payload(
    original_topic: str,
    reason: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int | None = None,
) -> dict[str, Any]:
    """Build the canonical DLQ record, stamped with its age metadata.

    ``dead_lettered_at`` makes the message age measurable; ``max_age_days``
    records the operational max in effect at DLQ time.  Both are read by
    :func:`is_dlq_expired` / :func:`sweep_dlq`.
    """
    now = now if now is not None else datetime.now(timezone.utc)  # noqa: UP017
    return {
        "original_topic": original_topic,
        "reason": reason,
        "payload": payload,
        "dead_lettered_at": now.isoformat(),
        "max_age_days": (
            max_age_days if max_age_days is not None else _DEFAULT_DLQ_MAX_AGE_DAYS
        ),
    }


def dlq_message_age(dlq_payload: dict[str, Any], now: datetime) -> timedelta | None:
    """Return the age of a DLQ record, or ``None`` when it is unknowable.

    A missing or unparseable ``dead_lettered_at`` yields ``None`` — unknown
    age is retained, never prematurely expired.
    """
    raw = dlq_payload.get("dead_lettered_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dead_lettered_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dead_lettered_at.tzinfo is None:
        dead_lettered_at = dead_lettered_at.replace(tzinfo=timezone.utc)  # noqa: UP017
    return now - dead_lettered_at


def is_dlq_expired(
    dlq_payload: dict[str, Any], now: datetime, max_age_days: int
) -> bool:
    """True when a DLQ record is strictly older than the operational max.

    Boundary semantics mirror the transcript retention job: a record exactly
    at the max-age edge is retained (strict ``>``); only strictly older
    records are expired.  Unknown age is retained.
    """
    age = dlq_message_age(dlq_payload, now)
    if age is None:
        return False
    return age > timedelta(days=max_age_days)


def classify_dlq_message(
    dlq_payload: dict[str, Any], now: datetime, max_age_days: int
) -> str:
    """Classify one DLQ record: ``"expired"`` (escalate) or ``"retain"``."""
    return "expired" if is_dlq_expired(dlq_payload, now, max_age_days) else "retain"


def build_escalation_payload(
    dlq_payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int | None = None,
) -> dict[str, Any]:
    """Build the escalation record for an expired DLQ message.

    Preserves the original payload + reason (so the operator can resolve it)
    and stamps a machine-readable ``escalation_reason``.  The record is
    **content-stable**: it carries no volatile ``now``-derived timestamp, and
    ``escalation_key`` is a deterministic SHA-256 over the DLQ record's own
    stable identity (``original_topic``, ``dead_lettered_at``, ``reason``,
    ``payload``), so re-escalating the same record on a later sweep produces
    an identical record (idempotent by content / natural key).
    """
    effective_max = (
        max_age_days
        if max_age_days is not None
        else dlq_payload.get("max_age_days", _DEFAULT_DLQ_MAX_AGE_DAYS)
    )
    escalation_key = hashlib.sha256(
        json.dumps(
            {
                "original_topic": dlq_payload.get("original_topic"),
                "dead_lettered_at": dlq_payload.get("dead_lettered_at"),
                "reason": dlq_payload.get("reason"),
                "payload": dlq_payload.get("payload"),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "original_topic": dlq_payload.get("original_topic"),
        "reason": dlq_payload.get("reason"),
        "payload": dlq_payload.get("payload"),
        "dead_lettered_at": dlq_payload.get("dead_lettered_at"),
        "escalation_key": escalation_key,
        "escalation_reason": (
            f"exceeded DLQ operational max of {effective_max} day(s)"
        ),
    }


@dataclass
class DLQSweepReport:
    """The result of one DLQ sweep (mirrors the transcript RetentionReport)."""

    now: datetime
    dry_run: bool
    max_age_days: int
    scanned: int = 0
    expired: int = 0
    retained: int = 0
    escalated: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.scanned


def run_dlq_sweep(
    messages: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_age_days: int,
    limit: int | None = None,
    dry_run: bool = False,
) -> DLQSweepReport:
    """Classify a list of DLQ records into expired vs retained.

    ``limit`` caps how many records are scanned (bounded runs).  In a dry run
    the would-be-expired count is reported but no escalation payloads are
    collected (nothing is escalated).  Strictly-older-than-the-max records are
    expired; unknown-age records are retained.
    """
    now = now if now is not None else datetime.now(timezone.utc)  # noqa: UP017
    report = DLQSweepReport(now=now, dry_run=dry_run, max_age_days=max_age_days)
    remaining = limit
    for message in messages:
        if remaining is not None and remaining <= 0:
            break
        report.scanned += 1
        if remaining is not None:
            remaining -= 1
        if classify_dlq_message(message, now, max_age_days) == "expired":
            report.expired += 1
            if not dry_run:
                report.escalated.append(
                    build_escalation_payload(message, now=now, max_age_days=max_age_days)
                )
        else:
            report.retained += 1
    return report


def format_dlq_report(report: DLQSweepReport) -> str:
    """Render the DLQ sweep report (dry-run and write runs share the form)."""
    lines = [
        "DLQ operational-max sweep report",
        f"as-of: {report.now.isoformat()}",
        f"mode: {'dry-run' if report.dry_run else 'write'}",
        f"max-age: {report.max_age_days} day(s)",
        f"scanned: {report.scanned} record(s)",
        f"expired (escalated): {report.expired} record(s)",
        f"retained: {report.retained} record(s)",
    ]
    if report.dry_run:
        lines.append(
            "dry-run: no messages were escalated; re-run without --dry-run to apply."
        )
    return "\n".join(lines)


def _lenient_dlq_deserializer(raw: bytes) -> Any:
    """Decode one DLQ record value; corrupt values decode to ``None``.

    A malformed JSON body or undecodable bytes must not crash the sweep
    consumer: the value is dropped to a ``None`` sentinel so
    :func:`_collect_dlq_batch` can skip it with a warning instead of raising
    inside the consumer.
    """
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def _collect_dlq_batch(
    consumer: Any, batch_size: int
) -> tuple[list[tuple[dict[str, Any], TopicPartition, int]], dict[TopicPartition, int], int]:
    """Collect up to ``batch_size`` DLQ records (bounded).

    Returns a ``(records, max_consumed_offsets, consumed)`` triple:

      * ``records`` — well-formed ``(payload, topic_partition, offset)``
        tuples.  Corrupt (non-object) records are skipped with a warning.
      * ``max_consumed_offsets`` — ``{topic_partition: highest offset}`` over
        *every* consumed message, corrupt included, so the sweep's commit
        position never advances past a dropped record.
      * ``consumed`` — the total number of messages consumed this batch
        (well-formed + corrupt), so the caller can detect stream exhaustion.
    """
    records: list[tuple[dict[str, Any], TopicPartition, int]] = []
    max_consumed: dict[TopicPartition, int] = {}
    consumed = 0
    iterator = consumer.__aiter__()
    for _ in range(batch_size):
        try:
            msg = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
        except (TimeoutError, StopAsyncIteration):
            break
        consumed += 1
        value = msg.value
        tp = TopicPartition(msg.topic, msg.partition)
        prev = max_consumed.get(tp)
        if prev is None or msg.offset > prev:
            max_consumed[tp] = msg.offset
        if not isinstance(value, dict):
            logger.warning(
                "Skipping corrupt DLQ record (partition=%d offset=%d): "
                "deserialized value is not a JSON object",
                msg.partition,
                msg.offset,
            )
            continue
        records.append((value, tp, msg.offset))
    return records, max_consumed, consumed


def _compute_dlq_commit_offsets(
    scanned_records: list[tuple[dict[str, Any], TopicPartition, int]],
    max_consumed_offsets: dict[TopicPartition, int],
    *,
    now: datetime,
    max_age_days: int,
) -> dict[TopicPartition, int]:
    """Compute the per-partition offsets to commit after one sweep chunk.

    For each partition, if any scanned record is retained (not yet expired),
    the commit offset is that partition's FIRST retained offset so the next
    run re-reads from there and re-examines it once it ages past the max.
    Otherwise every consumed record in that partition is done, so the commit
    offset is ``max_consumed + 1`` (already-escalated records are never
    re-read).  Returns a ``{TopicPartition: offset}`` mapping (empty when
    there is nothing to commit).
    """
    first_retained: dict[TopicPartition, int] = {}
    for payload, tp, offset in scanned_records:
        if is_dlq_expired(payload, now, max_age_days):
            continue
        prev = first_retained.get(tp)
        if prev is None or offset < prev:
            first_retained[tp] = offset

    offsets: dict[TopicPartition, int] = {}
    for tp, max_offset in max_consumed_offsets.items():
        retained = first_retained.get(tp)
        if retained is not None:
            offsets[tp] = retained
        else:
            offsets[tp] = max_offset + 1
    return offsets


async def sweep_dlq(
    kafka_brokers: str,
    dlq_topic: str,
    escalation_topic: str,
    max_age_days: int,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int = 100,
    consumer_group_id: str = _DEFAULT_DLQ_SWEEP_GROUP_ID,
) -> DLQSweepReport:
    """Escalate DLQ records older than the operational max (bounded batches).

    Consumes the DLQ topic in ``batch_size``-bounded chunks (up to ``limit``
    total), classifies each record, and — in write mode — publishes an
    escalation record for each expired message to ``escalation_topic`` so the
    message is never silently lost.  A dry run reports the would-be-expired
    counts and publishes nothing.

    Offsets are committed per chunk in write mode: a partition with a
    retained (not-yet-expired) record commits at its first retained offset so
    the next run re-reads from there and re-examines it once it ages past the
    max, while a partition whose scanned records are all expired commits at
    ``max consumed + 1`` (already-escalated records are never re-read).
    Corrupt (non-object) records are skipped with a warning but still counted
    in the per-partition commit position.  Dry runs never commit.
    """
    now = now if now is not None else datetime.now(timezone.utc)  # noqa: UP017
    consumer = AIOKafkaConsumer(
        dlq_topic,
        bootstrap_servers=kafka_brokers,
        group_id=consumer_group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=_lenient_dlq_deserializer,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=kafka_brokers,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await consumer.start()
    await producer.start()
    try:
        report = DLQSweepReport(now=now, dry_run=dry_run, max_age_days=max_age_days)
        remaining = limit
        while remaining is None or remaining > 0:
            take = batch_size if remaining is None else min(batch_size, remaining)
            records, max_consumed, consumed = await _collect_dlq_batch(consumer, take)
            if not records and consumed == 0:
                break
            chunk = [payload for payload, _, _ in records]
            chunk_report = run_dlq_sweep(
                chunk,
                now=now,
                max_age_days=max_age_days,
                limit=remaining,
                dry_run=dry_run,
            )
            report.scanned += chunk_report.scanned
            report.expired += chunk_report.expired
            report.retained += chunk_report.retained
            report.escalated.extend(chunk_report.escalated)
            if not dry_run:
                for escalation in chunk_report.escalated:
                    await producer.send_and_wait(escalation_topic, escalation)
                commit_offsets = _compute_dlq_commit_offsets(
                    records[: chunk_report.scanned],
                    max_consumed,
                    now=now,
                    max_age_days=max_age_days,
                )
                if commit_offsets:
                    await consumer.commit(commit_offsets)
            if remaining is not None:
                remaining -= chunk_report.scanned
            if consumed < take:
                break
        return report
    finally:
        await consumer.stop()
        await producer.stop()


async def _main_dlq_sweep(argv: list[str] | None = None) -> int:
    """Entry point for the operator DLQ sweep (``--dlq-sweep``)."""
    parser = argparse.ArgumentParser(
        description=(
            "Escalate DLQ records older than the operational max "
            "(GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS) to the escalation topic."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the would-be-escalated records without publishing anything.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N records (bounded runs).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Records per consume batch (default 100).",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from app.core.config import get_settings

    settings = get_settings()
    report = await sweep_dlq(
        settings.kafka_brokers,
        settings.afk_outcomes_dlq_topic,
        _DEFAULT_DLQ_ESCALATION_TOPIC,
        settings.retention_dlq_max_age_days,
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
    )
    print(format_dlq_report(report))
    return 0


# ── Provider adapter wiring (env-driven, no token storage) ──────────────────


def _build_adapter(provider: Provider) -> tuple[Any, Any]:
    """Build the provider adapter plus its API client (closed by the caller).

    Credentials come from the environment via the adapters' injectable
    API-client seam (``GITHUB_TOKEN`` / ``GITLAB_TOKEN``) — no token handling
    or storage is implemented here.  The GitHub path uses the shared
    :class:`afk_outcomes.providers.github_http.GitHubHttpApi` parsed-JSON
    seam.
    """
    if provider is Provider.GITHUB:
        from afk_outcomes.providers.github import GitHubAdapter

        github_client = GitHubHttpApi(os.environ.get("GITHUB_TOKEN", ""))
        return GitHubAdapter(github_client), github_client

    from afk_outcomes.providers.gitlab import GitLabAdapter

    gitlab_headers: dict[str, str] = {}
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        gitlab_headers["PRIVATE-TOKEN"] = token
    client = httpx.AsyncClient(headers=gitlab_headers, timeout=30.0)
    return GitLabAdapter(client=client), client


# ── Entry point ───────────────────────────────────────────────────────────


async def _main() -> None:
    """Entry point when run as ``python -m app.consumer.afk_consumer``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    consumer = await AFKOutcomeConsumer.from_env()
    await consumer.start()
    try:
        await consumer.run()
    finally:
        await consumer.stop()


# Known no-value (boolean) flags.  A ``--dlq-sweep`` token immediately
# following one of these is a genuine mode switch, never a value.
_NO_VALUE_FLAGS = frozenset({"--dry-run"})


def _looks_like_value_taking_flag(token: str) -> bool:
    """True when ``token`` is plausibly a flag that consumes a following value.

    Used to avoid mistaking a literal ``--dlq-sweep`` that is actually the
    *value* of some other option (``--future-option --dlq-sweep``) for the mode
    switch.  Any token that starts with ``-`` and is not a known no-value flag
    is assumed to take a value.
    """
    return token.startswith("-") and token not in _NO_VALUE_FLAGS


def _parse_cli(argv: list[str]) -> tuple[bool, list[str]]:
    """Dispatch the CLI between consumer mode and ``--dlq-sweep`` mode.

    Returns ``(is_sweep, remaining)``: ``is_sweep`` is True when the
    ``--dlq-sweep`` flag was given, and ``remaining`` is the argument list the
    chosen mode handler should see — the original ``argv`` minus ``--dlq-sweep``
    itself, with every other argument (including the sweep's own
    ``--batch-size`` / ``--limit`` / ``--dry-run`` flags) preserved verbatim for
    the handler to re-parse.

    ``--dlq-sweep`` is parsed as a genuine argparse flag (``allow_abbrev=False``)
    so it can never be silently consumed as the value of another option.
    Because ``parse_known_args`` cannot know an *unknown* option's arity, a
    literal ``--dlq-sweep`` that is actually the value of a preceding option
    (``["--future-option", "--dlq-sweep"]``) is still recognized as a flag.  We
    therefore honor it as the mode switch only when it is NOT plausibly a value:
    i.e. it is the first argument, or it is preceded by a known no-value flag
    (``--dry-run``) or by a plain value.  Otherwise the whole ``argv`` is
    treated as consumer-mode arguments, unchanged.
    """
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--dlq-sweep", action="store_true", dest="dlq_sweep")
    args, remaining = parser.parse_known_args(argv)
    if not args.dlq_sweep:
        return False, remaining
    for i, token in enumerate(argv):
        if token == "--dlq-sweep":
            if i == 0 or not _looks_like_value_taking_flag(argv[i - 1]):
                return True, remaining
            break
    return False, argv


if __name__ == "__main__":
    is_sweep, remaining = _parse_cli(sys.argv[1:])
    sys.argv = [sys.argv[0], *remaining]
    if is_sweep:
        sys.exit(asyncio.run(_main_dlq_sweep()))
    asyncio.run(_main())
