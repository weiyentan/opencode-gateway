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

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord
from pydantic import BaseModel, Field

from afk_outcomes.models import (
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)
from afk_outcomes.repository import AsyncpgOutcomeRepository
from scripts.afk_backfill import run_backfill

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

# ── Message-type → canonical-event mapping ──────────────────────────────────
#
# The provider-events topic is external and its payload contract is defined
# here.  ``type`` carries one of the ten locked canonical event types (PRD
# decision #7), shared with the provider adapters' normalization vocabulary.
# The entity a message maps onto is derived from the event-type prefix:
# ``issue.*`` maps to an ``issue`` entity; ``change_request.*`` and
# ``pipeline.*`` map to a ``change_request`` entity (pipeline events attach
# to the owning change request, matching the provider adapters).

_MAPPED_EVENT_TYPES = frozenset(
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


class ProviderEventMessage(BaseModel):
    """One message on the provider-events topic.

    ``delivery_id`` is the provider's delivery UUID (``X-GitHub-Delivery`` /
    ``X-GitLab-Event-UUID``) forwarded in the payload (PRD decision #8); it
    is the key ``delivery_log`` dedups on, so a redelivered message no-ops.
    """

    provider: Provider
    delivery_id: str
    type: str
    repository: str
    number: int
    occurred_at: datetime
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _entity_type_for(event_type: str) -> EntityType:
    """Derive the canonical entity type a message type maps onto."""
    if event_type.startswith("issue."):
        return EntityType.ISSUE
    return EntityType.CHANGE_REQUEST


def map_provider_event(
    message: ProviderEventMessage,
) -> tuple[EngineeringEntity, EngineeringEvent] | None:
    """Map a provider message to the canonical entity + event (or None).

    Returns ``None`` when ``message.type`` is not in the locked vocabulary —
    the caller treats that as a poison message (DLQ, no DB write).
    """
    if message.type not in _MAPPED_EVENT_TYPES:
        return None
    entity_type = _entity_type_for(message.type)
    entity_id = f"{entity_type.value}:{message.number}"
    suffix = message.type.split(".", 1)[1]
    entity = EngineeringEntity(
        entity_id=entity_id,
        entity_type=entity_type,
        provider=message.provider,
        repository=message.repository,
        number=message.number,
    )
    event = EngineeringEvent(
        event_id=f"{entity_id}:{suffix}",
        event_type=message.type,
        provider=message.provider,
        entity_id=entity_id,
        occurred_at=message.occurred_at,
        actor=message.actor,
        payload=message.payload,
    )
    return entity, event


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

        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._in_flight: asyncio.Task[Any] | None = None
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._owns_pool = False
        self._adapter_client: Any = None

    # ── Factory ────────────────────────────────────────────────────────

    @classmethod
    async def from_env(cls) -> AFKOutcomeConsumer:
        """Build a consumer from environment variables and application settings.

        Required: ``GATEWAY_KAFKA_BROKERS``, a reachable Postgres (via
        ``GATEWAY_DATABASE_*``), ``GATEWAY_AFK_OUTCOMES_REPOSITORY`` (the
        owner/repo to reconcile), and provider credentials in the adapters'
        standard env vars (``GITHUB_TOKEN`` / ``GITLAB_TOKEN``).
        """
        from app.core.config import get_settings

        settings = get_settings()
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
                    except Exception:
                        logger.exception(
                            "Unhandled exception processing message "
                            "at offset %d — skipping",
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
                    jitter = random.uniform(0.5, 1.5)
                    await asyncio.sleep(min(delay * jitter, self._max_backoff))
                    delay = min(delay * 2, self._max_backoff)
                    continue
                raise

    async def _process_message(self, msg: ConsumerRecord) -> None:
        """Process one message: deserialize → validate → map → persist → commit."""
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
            await self._commit()
            return

        # ── Validate the payload ────────────────────────────────────
        try:
            message = ProviderEventMessage.model_validate(raw_value)
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
            await self._commit()
            return

        # ── Map message type → canonical entity/event ───────────────
        mapped = map_provider_event(message)
        if mapped is None:
            logger.warning(
                "Unmappable message type %r — sending to DLQ (offset=%d)",
                message.type,
                msg.offset,
            )
            await self._send_to_dlq(
                raw_value if isinstance(raw_value, dict) else {},
                reason=f"Unmappable message type: {message.type!r}",
            )
            await self._commit()
            return
        entity, event = mapped

        # ── Persist in a single transaction, then commit ────────────
        for attempt in range(self._max_retries):
            try:
                await self._persist(message, entity, event)
            except Exception:
                if attempt < self._max_retries - 1:
                    delay = min(
                        self._initial_backoff * (2**attempt),
                        self._max_backoff,
                    )
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
                await self._send_to_dlq(
                    raw_value if isinstance(raw_value, dict) else {},
                    reason=f"DB persist failed after {self._max_retries} retries",
                )
                await self._commit()
                return

            await self._commit()
            return

    async def _persist(
        self,
        message: ProviderEventMessage,
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
        """Commit the current Kafka offset."""
        if self._consumer:
            await self._consumer.commit()

    async def _send_to_dlq(self, payload: dict[str, Any], *, reason: str) -> None:
        """Send a message to the DLQ topic with context about the failure."""
        if self._producer is None:
            return
        dlq_payload: dict[str, Any] = {
            "original_topic": self._topic,
            "reason": reason,
            "payload": payload,
        }
        try:
            await self._producer.send_and_wait(self._dlq_topic, dlq_payload)
        except KafkaError:
            logger.exception("Failed to send message to DLQ topic %s", self._dlq_topic)

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
        """Reconcile one bounded window through the backfill engine."""
        now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
        since = now - timedelta(seconds=self._reconcile_window_seconds)
        async with self._pool.acquire() as conn:
            await run_backfill(
                conn,
                adapter=self._adapter,
                repository=self._repository,
                since=since,
                until=now,
                dry_run=False,
            )


# ── Provider adapter wiring (env-driven, no token storage) ──────────────────


class _GitHubHttpApi:
    """A :class:`afk_outcomes.providers.github.GitHubApi` over httpx.

    The :class:`GitHubAdapter` seam expects ``get()`` to return the parsed
    JSON body (a list or dict), not an ``httpx.Response``.  A raw
    ``httpx.AsyncClient.get()`` returns ``httpx.Response``, so the adapter's
    ``_items()`` / ``isinstance(body, dict)`` checks would silently yield zero
    entities/events.  This wraps the httpx client so ``get()`` returns
    ``response.json()`` — the same seam ``scripts.afk_backfill._GitHubHttpApi``
    provides.
    """

    def __init__(self, token: str) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30.0
        )

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        response = await self._client.get(path, params=params or {})
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _build_adapter(provider: Provider) -> tuple[Any, Any]:
    """Build the provider adapter plus its API client (closed by the caller).

    Credentials come from the environment via the adapters' injectable
    API-client seam (``GITHUB_TOKEN`` / ``GITLAB_TOKEN``) — no token handling
    or storage is implemented here.
    """
    if provider is Provider.GITHUB:
        from afk_outcomes.providers.github import GitHubAdapter

        github_client = _GitHubHttpApi(os.environ.get("GITHUB_TOKEN", ""))
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


if __name__ == "__main__":
    asyncio.run(_main())
