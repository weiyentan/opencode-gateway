"""DLQ operational-max module (issue #483, extracted for operator discoverability).

Everything an operator needs to reason about the AFK outcome DLQ lives here:
the canonical DLQ record builder (``build_dlq_payload``), age/expiry
classification, the escalation record builder, and the operator sweep
(``sweep_dlq`` / ``python -m app.consumer.afk_consumer --dlq-sweep``).

The ``afk.events-dlq`` topic is retained until resolved but must never grow
unbounded.  Every DLQ record is stamped with ``dead_lettered_at`` and
``max_age_days`` (see ``build_dlq_payload``), and an operator-run sweep
escalates messages strictly older than the operational max to an escalation
topic, preserving their payload + reason for manual resolution.  Physical
removal from the DLQ is enforced by the topic's Kafka retention configured
to the same max age (documented in ADR 0022); the escalation topic is the
durable operator record, so nothing is ever silently lost.  Mirror
``scripts/retention_transcripts.py``: dry-run + bounded batches + a config
driven window.

Imported back into :mod:`app.consumer.afk_consumer` so the existing public
import surface (and the CLI entry point) is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition

logger = logging.getLogger(__name__)

# DLQ operational max (issue #483): the afk.events-dlq topic is retained until
# resolved, but never unbounded — messages older than this many days are
# escalated/expired by the DLQ sweep.  Mirrors GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS.
_DEFAULT_DLQ_MAX_AGE_DAYS = 30
_DEFAULT_DLQ_ESCALATION_TOPIC = "engineering.events.normalized.dlq-expired"
_DEFAULT_DLQ_SWEEP_GROUP_ID = "opencode-outcomes-dlq-sweep"

# Metric names (stable — do not rename; downstream dashboards depend).
METRIC_MESSAGES_DLQ = "afk_consumer.messages.dlq"
METRIC_DLQ_DEPTH = "afk_consumer.dlq.depth"


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
        settings.normalized_events_dlq_topic,
        _DEFAULT_DLQ_ESCALATION_TOPIC,
        settings.retention_dlq_max_age_days,
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
    )
    print(format_dlq_report(report))
    return 0
