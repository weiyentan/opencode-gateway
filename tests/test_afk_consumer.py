"""Tests for the live AFK outcome consumer (``app.consumer.afk_consumer``).

Covers the message-type → canonical-event mapping (all ten locked types),
the single-transaction write path with offset-commit-after-transaction
ordering, poison-message DLQ handling, DB-error backoff with DLQ + commit
on exhaustion, consumer-group separation, and the scheduled reconciliation
loop reusing the backfill engine.  Kafka and asyncpg are mocked.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord, TopicPartition

from afk_outcomes.models import EntityType, Provider
from afk_outcomes.providers.github import GitHubAdapter
from afk_outcomes.providers.github_http import GitHubHttpApi
from afk_outcomes.providers.gitlab import GitLabAdapter
from app.consumer.afk_consumer import (
    _CANONICAL_EVENT_TYPES,
    METRIC_COMMITTED_OFFSET,
    METRIC_DB_ERRORS,
    METRIC_DLQ_DEPTH,
    METRIC_LAG,
    METRIC_MESSAGES_ACCEPTED,
    METRIC_MESSAGES_DLQ,
    METRIC_MESSAGES_POISON,
    METRIC_MESSAGES_TOTAL,
    METRIC_RETRIES,
    METRIC_RETRIES_PER_MESSAGE,
    AFKOutcomeConsumer,
    NormalizedEventValidationError,
    NormalizedProviderEvent,
    _build_adapter,
    _lenient_dlq_deserializer,
    _parse_cli,
    build_dlq_payload,
    build_escalation_payload,
    classify_dlq_message,
    dlq_message_age,
    is_dlq_expired,
    map_normalized_event,
    map_provider_event,
    normalize_repository_url,
    run_dlq_sweep,
    sweep_dlq,
    validate_normalized_event,
)
from app.core.metrics import MetricsRegistry

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
DELIVERY_ID = "11111111-2222-3333-4444-555555555555"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeTransaction:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def __aenter__(self) -> _FakeTransaction:
        self._order.append("tx_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._order.append("tx_exit")
        return False


class _FakeConn:
    def __init__(self, order: list[str], *, execute: AsyncMock | None = None) -> None:
        self._order = order
        self.execute = execute if execute is not None else AsyncMock(return_value="OK")

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._order)


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


class _FakeAdapter:
    provider = Provider.GITHUB

    async def fetch_entities(self, repository, *, since=None, until=None):
        return []

    async def fetch_events(self, repository, *, since=None, until=None):
        return []


class _FakeHttpxResponse:
    """A stand-in for ``httpx.Response`` carrying a pre-parsed body."""

    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._body


class _PathServingHttpxClient:
    """A fake ``httpx.AsyncClient`` that serves parsed JSON keyed by exact path."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> _FakeHttpxResponse:
        self.calls.append((path, params or {}))
        return _FakeHttpxResponse(self._payloads[path])

    async def aclose(self) -> None:
        return None


def _github_rest_payloads() -> dict[str, object]:
    """Realistic GitHub REST-shaped payloads covering issues + a merged PR."""
    return {
        "/repos/owner/repo/issues": [
            {
                "number": 100,
                "title": "An issue",
                "state": "open",
                "user": {"login": "alice"},
                "created_at": "2026-08-01T10:00:00Z",
                "updated_at": "2026-08-01T10:00:00Z",
                "html_url": "https://github.com/owner/repo/issues/100",
            }
        ],
        "/repos/owner/repo/pulls": [
            {
                "number": 200,
                "title": "A merged pull request",
                "state": "closed",
                "user": {"login": "alice"},
                "created_at": "2026-08-01T10:00:00Z",
                "updated_at": "2026-08-01T12:00:00Z",
                "closed_at": "2026-08-01T12:00:00Z",
                "merged_at": "2026-08-01T12:00:00Z",
                "merge_commit_sha": "merge-200",
                "merged_by": {"login": "carol"},
                "head": {"ref": "feature/x", "sha": "sha200"},
                "base": {"ref": "main"},
                "requested_reviewers": [],
                "html_url": "https://github.com/owner/repo/pulls/200",
            }
        ],
        "/repos/owner/repo/pulls/200/reviews": [
            {
                "id": 1001,
                "state": "approved",
                "user": {"login": "bob"},
                "submitted_at": "2026-08-01T11:00:00Z",
                "commit_id": "sha200",
            }
        ],
        "/repos/owner/repo/pulls/200/commits": [
            {
                "sha": "commit-a",
                "commit": {"author": {"name": "alice"}, "message": "feat: x"},
                "html_url": "https://github.com/owner/repo/commit/commit-a",
            }
        ],
        "/repos/owner/repo/commits/sha200/check-runs": {
            "check_runs": [
                {
                    "id": 300,
                    "name": "ci",
                    "conclusion": "success",
                    "completed_at": "2026-08-01T12:30:00Z",
                }
            ]
        },
    }


# ── Builders ─────────────────────────────────────────────────────────────────


def _mk_msg(
    value: dict, *, offset: int = 42, partition: int = 0
) -> MagicMock:
    """Build a MagicMock that quacks like an aiokafka ConsumerRecord."""
    msg = MagicMock(spec=ConsumerRecord)
    msg.value = json.dumps(value).encode("utf-8")
    msg.offset = offset
    msg.partition = partition
    msg.topic = "afk.events"
    msg.key = None
    msg.headers = ()
    return msg


def _valid_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": DELIVERY_ID,
        "resource_type": "pull_request",
        "resource_id": "442",
        "repository": "owner/repo",
        "action": "merged",
        "occurred_at": "2026-08-01T10:30:00Z",
        "actor": "carol",
        "payload_ref": "redacted-payload-ref-442",
    }
    payload.update(overrides)
    return payload


def _make_consumer(
    *,
    pool: _FakePool,
    order: list[str] | None = None,
    consumer_group_id: str = "opencode-outcomes",
    reconcile_window_seconds: float = 3600.0,
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
    metrics: MetricsRegistry | None = None,
) -> AFKOutcomeConsumer:
    return AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=pool,  # type: ignore[arg-type]
        provider=Provider.GITHUB,
        repository="owner/repo",
        adapter=_FakeAdapter(),
        consumer_group_id=consumer_group_id,
        reconcile_window_seconds=reconcile_window_seconds,
        max_retries=max_retries,
        initial_backoff=initial_backoff,
        max_backoff=max_backoff,
        metrics=metrics,
    )


# ── Message-type → canonical-event mapping ───────────────────────────────────


def test_canonical_event_types_is_the_locked_ten() -> None:
    assert _CANONICAL_EVENT_TYPES == {
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


# ── Valid message → single transaction → commit after commit ────────────────


@pytest.mark.asyncio
async def test_valid_message_persists_and_commits_after_transaction() -> None:
    order: list[str] = []
    conn = _FakeConn(order)
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock(
        side_effect=lambda *args, **kwargs: order.append("commit")
    )
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())
    await consumer._process_message(msg)

    # The offset is committed only after the transaction context exits.
    assert order == ["tx_enter", "tx_exit", "commit"]
    consumer._producer.send_and_wait.assert_not_called()

    # delivery_log + engineering_events written with the delivery UUID.
    sqls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("INSERT INTO delivery_log" in s for s in sqls)
    assert any("INSERT INTO engineering_events" in s for s in sqls)

    delivery_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO delivery_log" in c.args[0]
    )
    assert delivery_call.args[1] == "github"
    assert delivery_call.args[2] == DELIVERY_ID

    event_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO engineering_events" in c.args[0]
    )
    assert event_call.args[1] == "github"  # provider
    assert event_call.args[2] == "owner/repo"  # repository
    assert event_call.args[3] == "change_request"  # entity_type
    assert event_call.args[4] == "442"  # external_id
    assert event_call.args[5] == "change_request.merged"  # event_type


# ── Poison messages → DLQ + commit (offset advances) ─────────────────────────


@pytest.mark.asyncio
async def test_unparseable_json_sends_to_dlq_and_commits() -> None:
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = MagicMock(spec=ConsumerRecord)
    msg.value = b"not valid json at all"
    msg.offset = 101
    msg.partition = 0
    msg.topic = "afk.events"
    msg.key = None
    msg.headers = ()

    await consumer._process_message(msg)

    conn.execute.assert_not_called()  # no DB write
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "JSON decode failure" in dlq_payload["reason"]


@pytest.mark.asyncio
async def test_invalid_shape_sends_to_dlq_and_commits() -> None:
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg({"not": "valid"})
    await consumer._process_message(msg)

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Invalid message shape" in dlq_payload["reason"]


@pytest.mark.asyncio
async def test_unmappable_type_sends_to_dlq_and_commits() -> None:
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg(_valid_payload(action="assigned"))
    await consumer._process_message(msg)

    conn.execute.assert_not_called()  # unmappable → no DB write
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Unmappable message type" in dlq_payload["reason"]


# ── DB error → backoff, DLQ + commit on exhaustion ───────────────────────────


@pytest.mark.asyncio
async def test_db_error_retries_then_commits_on_recovery() -> None:
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[
                RuntimeError("db down"),
                RuntimeError("db down"),
                "OK",  # delivery_log on the third attempt
                "OK",  # engineering_events on the third attempt
            ]
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())
    with patch(
        "app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert mock_sleep.call_count == 2
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_db_error_max_retries_exhausted_sends_to_dlq_and_commits() -> None:
    conn = _FakeConn(
        [],
        execute=AsyncMock(side_effect=[RuntimeError("db down")] * 3),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg(_valid_payload())
    with patch(
        "app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert conn.execute.call_count == 3  # one failing attempt per retry
    assert mock_sleep.call_count == 2
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "DB persist failed after 3 retries" in dlq_payload["reason"]
    assert dlq_payload["payload"] == _valid_payload()


@pytest.mark.asyncio
async def test_zero_max_retries_does_not_silently_drop_message() -> None:
    """Defense-in-depth: a consumer programmatically built with
    ``max_retries=0`` must still attempt persistence and, on a persistent DB
    failure, DLQ the message — never silently return without persisting,
    DLQ'ing, or committing (which would drop the message forever)."""
    metrics = MetricsRegistry()
    conn = _FakeConn(
        [],
        execute=AsyncMock(side_effect=[RuntimeError("db down")]),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=0, metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(_mk_msg(_valid_payload()))

    # One persistence attempt, then the DLQ path (not silently dropped).
    assert conn.execute.call_count == 1
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    snap = _snap(metrics)
    assert snap[METRIC_MESSAGES_ACCEPTED] == 0
    assert snap[METRIC_MESSAGES_DLQ] == 1
    assert snap[METRIC_DB_ERRORS] == 1


@pytest.mark.asyncio
async def test_failing_then_successful_message_does_not_lose_failed_message() -> None:
    """Regression: a max-retries-exhausted message followed by a successful one
    must DLQ the failed message, not silently skip it via a later commit."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[
                RuntimeError("db down"),  # failing message attempt 1
                RuntimeError("db down"),  # failing message attempt 2
                RuntimeError("db down"),  # failing message attempt 3
                "OK",  # delivery_log for the successful message
                "OK",  # engineering_events for the successful message
            ]
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    failing_payload = _valid_payload(delivery_id="failing-delivery")
    failing_msg = _mk_msg(failing_payload)
    with patch(
        "app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock
    ):
        await consumer._process_message(failing_msg)

    # The failed message was DLQ'd and its offset committed — not lost.
    consumer._producer.send_and_wait.assert_called_once()
    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "DB persist failed after 3 retries" in dlq_payload["reason"]
    assert dlq_payload["payload"] == failing_payload
    consumer._consumer.commit.assert_called_once()

    # A subsequent successful message commits its own offset without
    # swallowing the failed message (already DLQ'd + committed above).
    await consumer._process_message(_mk_msg(_valid_payload()))

    # Total: one DLQ (the failed message) and two commits (failed + good).
    consumer._producer.send_and_wait.assert_called_once()
    assert consumer._consumer.commit.call_count == 2


# ── DLQ publish failure must not swallow the message (no commit) ──────────────


def _mk_invalid_json_msg() -> MagicMock:
    """A ConsumerRecord whose value is not decodable JSON."""
    msg = MagicMock(spec=ConsumerRecord)
    msg.value = b"not valid json at all"
    msg.offset = 101
    msg.partition = 0
    msg.topic = "afk.events"
    msg.key = None
    msg.headers = ()
    return msg


@pytest.mark.asyncio
async def test_db_failure_dlq_success_commits_exactly_once() -> None:
    """DB exhausts retries; DLQ publish succeeds → exactly one offset commit."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(side_effect=[RuntimeError("db down")] * 3),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_msg(_valid_payload())
    with patch(
        "app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await consumer._process_message(msg)

    assert mock_sleep.call_count == 2
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_called_once()


@pytest.mark.asyncio
async def test_db_failure_dlq_failure_does_not_commit() -> None:
    """DB exhausts retries AND DLQ publish fails → no commit, error propagates."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(side_effect=[RuntimeError("db down")] * 3),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    msg = _mk_msg(_valid_payload())
    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KafkaError):
            await consumer._process_message(msg)

    consumer._consumer.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_msg",
    [
        _mk_invalid_json_msg,
        lambda: _mk_msg({"not": "valid"}),
            lambda: _mk_msg(_valid_payload(action="assigned")),
    ],
    ids=["invalid-json", "invalid-shape", "unmappable-type"],
)
async def test_poison_message_dlq_failure_does_not_commit(make_msg) -> None:
    """A poison message whose DLQ publish fails must not be committed away."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    msg = make_msg()
    with pytest.raises(KafkaError):
        await consumer._process_message(msg)

    consumer._consumer.commit.assert_not_called()
    conn.execute.assert_not_called()


# ── Commit frontier (no permanent message loss — PR #459 finding 1) ──────────


def _tp(partition: int = 0) -> TopicPartition:
    return TopicPartition("afk.events", partition)


def _last_commit_offsets(consumer: AFKOutcomeConsumer) -> dict[TopicPartition, int]:
    """Extract {TopicPartition: committed_offset} from the last commit call."""
    offsets = consumer._consumer.commit.call_args.args[0]
    return {tp: om.offset for tp, om in offsets.items()}


@pytest.mark.asyncio
async def test_dlq_success_commits_explicit_offset_covering_message() -> None:
    """DLQ publish success → commit carries explicit offsets covering the msg."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    msg = _mk_invalid_json_msg()  # offset 101
    await consumer._process_message(msg)

    consumer._consumer.commit.assert_called_once()
    assert _last_commit_offsets(consumer) == {_tp(): 102}


@pytest.mark.asyncio
async def test_consecutive_successes_advance_commit_frontier() -> None:
    """Two consecutive successful messages advance the committed offset to N+2."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(_valid_payload(), offset=10))
    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))

    assert consumer._consumer.commit.call_count == 2
    assert _last_commit_offsets(consumer) == {_tp(): 12}


@pytest.mark.asyncio
async def test_failed_then_successful_message_does_not_commit_past_failure() -> None:
    """A later success must not commit past an earlier DLQ-failed offset."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[
                "OK",  # delivery_log for offset 9
                "OK",  # engineering_events for offset 9
                RuntimeError("db down"),  # offset 10, attempt 1
                RuntimeError("db down"),  # offset 10, attempt 2
                RuntimeError("db down"),  # offset 10, attempt 3
                "OK",  # delivery_log for offset 11
                "OK",  # engineering_events for offset 11
            ]
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    # A prior successful message establishes the frontier at offset 10.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=9))
    assert _last_commit_offsets(consumer) == {_tp(): 10}

    failing_msg = _mk_msg(_valid_payload(delivery_id="failing"), offset=10)
    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KafkaError):
            await consumer._process_message(failing_msg)
    consumer._mark_blocked(failing_msg)  # what run()'s except handler does

    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))

    # The good message is processed, but its commit must not pass offset 10.
    assert consumer._consumer.commit.call_count == 2
    assert _last_commit_offsets(consumer) == {_tp(): 10}


@pytest.mark.asyncio
async def test_first_message_failure_does_not_advance_past_it() -> None:
    """Edge case: the FIRST message for a partition fails → frontier stays put."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    poison = _mk_invalid_json_msg()  # offset 101, the first message for partition 0
    with pytest.raises(KafkaError):
        await consumer._process_message(poison)
    consumer._mark_blocked(poison)

    await consumer._process_message(_mk_msg(_valid_payload(), offset=102))

    # No commit: the frontier cannot advance past the blocked first offset.
    consumer._consumer.commit.assert_not_called()


@pytest.mark.asyncio
async def test_redelivered_failed_message_clears_block_and_advances() -> None:
    """Redelivery of the failed offset clears the block; the frontier advances."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[RuntimeError("db down")] * 3 + ["OK"] * 6
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    failing_msg = _mk_msg(_valid_payload(delivery_id="failing"), offset=10)
    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KafkaError):
            await consumer._process_message(failing_msg)
    consumer._mark_blocked(failing_msg)

    # A later message processed while blocked must not advance the frontier.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))
    consumer._consumer.commit.assert_not_called()

    # Redelivery of the failed offset succeeds → block cleared, frontier = N.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=10))
    assert _last_commit_offsets(consumer) == {_tp(): 11}

    # The redelivered N+1 then advances the frontier to N+2.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))
    assert _last_commit_offsets(consumer) == {_tp(): 12}


@pytest.mark.asyncio
async def test_multiple_failures_keep_earliest_blocked_offset() -> None:
    """Consecutive failures must not move the block past the earliest gap.

    Regression: a second failing message on the same partition must not
    overwrite the first failed offset — otherwise the block would move to a
    later offset and never clear when the first failed offset is redelivered.
    """
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[RuntimeError("db down")] * 6 + ["OK"] * 8
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock(side_effect=KafkaError("dlq down"))

    # offset 10 (N) fails, then offset 11 (N+1) also fails.
    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(KafkaError):
            await consumer._process_message(_mk_msg(_valid_payload(), offset=10))
        with pytest.raises(KafkaError):
            await consumer._process_message(_mk_msg(_valid_payload(), offset=11))
    consumer._mark_blocked(_mk_msg(_valid_payload(), offset=10))
    consumer._mark_blocked(_mk_msg(_valid_payload(), offset=11))

    # The block must remain at the earliest failed offset (10), not 11.
    assert consumer._blocked == {_tp(): 10}

    # offset 12 (N+2) succeeds — but its commit must not advance past N=10.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=12))
    consumer._consumer.commit.assert_not_called()

    # Redelivery: N succeeds → block cleared, frontier = N.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=10))
    assert _last_commit_offsets(consumer) == {_tp(): 11}

    # N+1 redelivered → frontier = N+1.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))
    assert _last_commit_offsets(consumer) == {_tp(): 12}

    # N+2 redelivered → frontier = N+2, committed offset = N+3.
    await consumer._process_message(_mk_msg(_valid_payload(), offset=12))
    assert _last_commit_offsets(consumer) == {_tp(): 13}


@pytest.mark.asyncio
async def test_mark_committable_does_not_advance_past_blocked_first_offset() -> None:
    """Edge case: the FIRST message for a partition fails; a later success
    must not mark itself committable (which would commit past the failure)."""
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])))
    consumer._mark_blocked(_mk_msg(_valid_payload(), offset=10))
    consumer._mark_committable(_mk_msg(_valid_payload(), offset=11))
    assert consumer._committable == {}
    assert consumer._blocked == {_tp(): 10}


@pytest.mark.asyncio
async def test_mark_committable_clears_block_on_redelivery() -> None:
    """Redelivery of the blocked offset clears the block and re-advances."""
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])))
    consumer._mark_blocked(_mk_msg(_valid_payload(), offset=10))
    consumer._mark_committable(_mk_msg(_valid_payload(), offset=11))
    assert consumer._committable == {}
    consumer._mark_committable(_mk_msg(_valid_payload(), offset=10))  # redelivered
    assert consumer._committable == {_tp(): 10}
    assert consumer._blocked == {}


# ── Consumer group separation ────────────────────────────────────────────────


def test_default_consumer_group_is_separate_from_usage_consumer() -> None:
    from app.consumer.afk_consumer import _DEFAULT_CONSUMER_GROUP_ID

    assert _DEFAULT_CONSUMER_GROUP_ID == "opencode-outcomes"
    assert _DEFAULT_CONSUMER_GROUP_ID != "opencode-gateway"


@pytest.mark.asyncio
async def test_constructed_consumer_uses_separate_group() -> None:
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])))
    assert consumer._consumer_group_id == "opencode-outcomes"
    assert consumer._consumer_group_id != "opencode-gateway"


@pytest.mark.asyncio
async def test_start_uses_separate_group_no_autocommit_earliest_reset() -> None:
    consumer = _make_consumer(
        pool=_FakePool(_FakeConn([])), consumer_group_id="opencode-outcomes"
    )
    with (
        patch(
            "app.consumer.afk_consumer.AFKOutcomeConsumer._reconcile_loop",
            new_callable=AsyncMock,
        ),
        patch("app.consumer.afk_consumer.AIOKafkaConsumer") as mock_kafka_consumer,
        patch("app.consumer.afk_consumer.AIOKafkaProducer") as mock_kafka_producer,
    ):
        mock_kafka_consumer.return_value.start = AsyncMock()
        mock_kafka_producer.return_value.start = AsyncMock()
        await consumer.start()

    assert mock_kafka_consumer.call_args.kwargs["group_id"] == "opencode-outcomes"
    assert mock_kafka_consumer.call_args.kwargs["enable_auto_commit"] is False
    assert mock_kafka_consumer.call_args.kwargs["auto_offset_reset"] == "earliest"
    assert mock_kafka_consumer.call_args.args[0] == "afk.events"


# ── Scheduled reconciliation reuses the backfill engine ──────────────────────


@pytest.mark.asyncio
async def test_reconcile_once_reuses_backfill_engine_over_bounded_window() -> None:
    conn = _FakeConn([])
    consumer = _make_consumer(
        pool=_FakePool(conn), reconcile_window_seconds=3600.0
    )
    with patch(
        "app.consumer.afk_consumer.run_backfill", new_callable=AsyncMock
    ) as mock_backfill:
        await consumer._reconcile_once()

    mock_backfill.assert_awaited_once()
    kwargs = mock_backfill.call_args.kwargs
    assert kwargs["repository"] == "owner/repo"
    assert kwargs["dry_run"] is False
    assert kwargs["adapter"] is consumer._adapter

    since = kwargs["since"]
    until = kwargs["until"]
    assert isinstance(since, datetime)
    assert isinstance(until, datetime)
    assert until - since == timedelta(seconds=3600.0)


@pytest.mark.asyncio
async def test_reconcile_once_prefetches_provider_before_acquiring_conn() -> None:
    """Network fetches run *before* the pooled connection is acquired.

    The reconcile loop must not hold a pooled connection open across the slow
    provider fetches (which would contend with the consume-path ``_persist``
    acquires on the shared pool).
    """
    order: list[str] = []
    conn = _FakeConn(order)

    class _RecordingAdapter(_FakeAdapter):
        async def fetch_entities(self, repository, *, since=None, until=None):
            order.append("fetch_entities")
            return []

        async def fetch_events(self, repository, *, since=None, until=None):
            order.append("fetch_events")
            return []

    class _RecordingPool(_FakePool):
        def acquire(self) -> _AcquireCtx:
            order.append("acquire")
            return _AcquireCtx(self._conn)

    consumer = _make_consumer(
        pool=_RecordingPool(conn), reconcile_window_seconds=3600.0
    )
    consumer._adapter = _RecordingAdapter()  # type: ignore[assignment]

    with patch(
        "app.consumer.afk_consumer.run_backfill", new_callable=AsyncMock
    ) as mock_backfill:
        await consumer._reconcile_once()

    assert order == ["fetch_entities", "fetch_events", "acquire"]
    mock_backfill.assert_awaited_once()
    # The fetched window is handed to run_backfill so it does not re-fetch.
    prefetched = mock_backfill.call_args.kwargs["prefetched"]
    assert prefetched.entities == []
    assert prefetched.events == []


@pytest.mark.asyncio
async def test_reconcile_loop_runs_windows_until_stopped() -> None:
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])))
    consumer._running = True

    calls = 0

    async def fake_reconcile_once() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            consumer._running = False  # stop after two windows

    with patch(
        "app.consumer.afk_consumer.AFKOutcomeConsumer._reconcile_once",
        side_effect=fake_reconcile_once,
    ), patch(
        "app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock
    ):
        await consumer._reconcile_loop()

    assert calls == 2


# ── from_env factory ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_from_env_reads_afk_settings() -> None:
    env_vars = {
        "GATEWAY_ENV": "development",
        "GATEWAY_KAFKA_BROKERS": "broker1:9092",
        "GATEWAY_AFK_OUTCOMES_TOPIC": "afk.events",
        "GATEWAY_AFK_OUTCOMES_DLQ_TOPIC": "afk.events-dlq",
        "GATEWAY_AFK_OUTCOMES_CONSUMER_GROUP_ID": "opencode-outcomes",
        "GATEWAY_AFK_OUTCOMES_PROVIDER": "github",
        "GATEWAY_AFK_OUTCOMES_REPOSITORY": "owner/repo",
        "GATEWAY_AFK_OUTCOMES_RECONCILE_CADENCE_SECONDS": "600",
        "GATEWAY_AFK_OUTCOMES_RECONCILE_WINDOW_SECONDS": "3600",
    }
    with (
        patch.dict(os.environ, env_vars, clear=True),
        patch("app.consumer.afk_consumer.asyncpg.create_pool", new_callable=AsyncMock),
        patch(
            "app.consumer.afk_consumer._build_adapter",
            return_value=(_FakeAdapter(), None),
        ),
    ):
        consumer = await AFKOutcomeConsumer.from_env()

    assert consumer._topic == "afk.events"
    assert consumer._dlq_topic == "afk.events-dlq"
    assert consumer._consumer_group_id == "opencode-outcomes"
    assert consumer._repository == "owner/repo"
    assert consumer._reconcile_cadence_seconds == 600.0
    assert consumer._reconcile_window_seconds == 3600.0


@pytest.mark.asyncio
async def test_from_env_fails_fast_when_repository_empty() -> None:
    """``from_env`` fails fast (before any pool/adapter work) on an empty repository.

    Without this guard the consumer would start with an empty
    ``GATEWAY_AFK_OUTCOMES_REPOSITORY`` and the reconcile loop would retry
    forever against an adapter error (caught and logged).
    """
    env_vars = {
        "GATEWAY_ENV": "development",
        "GATEWAY_KAFKA_BROKERS": "broker1:9092",
        "GATEWAY_AFK_OUTCOMES_TOPIC": "afk.events",
        "GATEWAY_AFK_OUTCOMES_DLQ_TOPIC": "afk.events-dlq",
        "GATEWAY_AFK_OUTCOMES_CONSUMER_GROUP_ID": "opencode-outcomes",
        "GATEWAY_AFK_OUTCOMES_PROVIDER": "github",
        "GATEWAY_AFK_OUTCOMES_RECONCILE_CADENCE_SECONDS": "600",
        "GATEWAY_AFK_OUTCOMES_RECONCILE_WINDOW_SECONDS": "3600",
    }
    with (
        patch.dict(os.environ, env_vars, clear=True),
        patch(
            "app.consumer.afk_consumer.asyncpg.create_pool",
            new_callable=AsyncMock,
        ) as mock_pool,
        patch(
            "app.consumer.afk_consumer._build_adapter",
            return_value=(_FakeAdapter(), None),
        ),
    ):
        with pytest.raises(ValueError, match="GATEWAY_AFK_OUTCOMES_REPOSITORY"):
            await AFKOutcomeConsumer.from_env()

    # The guard fires before any DB pool connection is attempted.
    assert mock_pool.await_count == 0


# ── Provider adapter wiring (GitHub parsed-JSON seam) ─────────────────────────


@pytest.mark.asyncio
async def test_build_adapter_github_injects_parsed_json_seam() -> None:
    """``_build_adapter(GITHUB)`` wraps httpx so ``get()`` returns parsed JSON."""
    fake_client = _PathServingHttpxClient(
        {"/repos/owner/repo/issues": [{"number": 1}]}
    )
    with patch(
        "afk_outcomes.providers.github_http.httpx.AsyncClient",
        return_value=fake_client,
    ) as mock_http:
        adapter, client = _build_adapter(Provider.GITHUB)

    assert isinstance(adapter, GitHubAdapter)
    # The injected client is the parsed-JSON seam, not a raw httpx.AsyncClient.
    assert isinstance(client, GitHubHttpApi)

    body = await client.get("/repos/owner/repo/issues")
    assert body == [{"number": 1}]  # parsed JSON body, not an httpx.Response

    # The underlying httpx client was built with the GitHub base URL + headers.
    assert mock_http.call_args.kwargs["base_url"] == "https://api.github.com"
    assert (
        mock_http.call_args.kwargs["headers"]["Accept"]
        == "application/vnd.github+json"
    )


@pytest.mark.asyncio
async def test_build_adapter_github_reconciles_nonempty_via_real_adapter() -> None:
    """The real GitHubAdapter fed through ``_build_adapter`` yields entities/events."""
    payloads = _github_rest_payloads()
    with patch(
        "afk_outcomes.providers.github_http.httpx.AsyncClient",
        return_value=_PathServingHttpxClient(payloads),
    ):
        adapter, _client = _build_adapter(Provider.GITHUB)

    entities = await adapter.fetch_entities("owner/repo")
    events = await adapter.fetch_events("owner/repo")

    assert entities, "expected non-empty entities from the parsed-JSON seam"
    assert events, "expected non-empty events from the parsed-JSON seam"
    assert any(e.entity_type is EntityType.CHANGE_REQUEST for e in entities)
    assert any(e.event_type == "change_request.merged" for e in events)


@pytest.mark.asyncio
async def test_build_adapter_gitlab_passes_raw_httpx_client() -> None:
    """The GitLab path keeps passing a raw httpx.AsyncClient (unchanged)."""
    fake_client = _PathServingHttpxClient({})
    with patch(
        "app.consumer.afk_consumer.httpx.AsyncClient",
        return_value=fake_client,
    ) as mock_http:
        adapter, client = _build_adapter(Provider.GITLAB)

    assert isinstance(adapter, GitLabAdapter)
    assert client is fake_client  # raw client, no JSON-parsing wrapper
    assert "timeout" in mock_http.call_args.kwargs


# ── Redelivery produces no duplicate rows (unit-level, mocked asyncpg) ───────


@pytest.mark.asyncio
async def test_redelivery_writes_are_conflict_ignored() -> None:
    """Re-processing the same message re-issues conflict-ignore SQL (no dup)."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    msg = _mk_msg(_valid_payload())
    await consumer._process_message(msg)
    await consumer._process_message(msg)  # redelivery

    delivery_log_calls = [
        c for c in conn.execute.call_args_list if "INSERT INTO delivery_log" in c.args[0]
    ]
    assert len(delivery_log_calls) == 2  # re-issued; ON CONFLICT DO NOTHING dedups
    for call in delivery_log_calls:
        assert "ON CONFLICT (provider, delivery_id) DO NOTHING" in call.args[0]
    assert consumer._consumer.commit.call_count == 2


# ── Commit-failure recovery (issue #473) ────────────────────────────────────
# A DB-success/Kafka-commit-failure sequence must never mark the persisted
# message as a blocked processing gap, nor let a later commit advance past the
# uncommitted offset.  The production run() path recreates the consumer on a
# Kafka error (rebalance) so the message is redelivered idempotently — the
# delivery_log/engineering_events dedup absorbs the replay.


class _FakeAsyncIterator:
    """Async iterator over a fixed message list (drives run()'s __anext__)."""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        self._i = 0

    async def __anext__(self) -> object:
        if self._i >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._i]
        self._i += 1
        return msg


class _FakeKafkaConsumer:
    """Stand-in for AIOKafkaConsumer: start/stop/commit/__aiter__ only."""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.commit = AsyncMock(return_value=None)
        self.stop = AsyncMock(return_value=None)
        self.start = AsyncMock(return_value=None)

    def __aiter__(self) -> _FakeAsyncIterator:
        return _FakeAsyncIterator(self._messages)


@pytest.mark.asyncio
async def test_commit_never_advances_past_blocked_frontier() -> None:
    """Regression (issue #473): _commit caps the committed offset at the
    blocked offset even when the in-memory frontier was advanced before the
    commit failure — the stale state a DB-success/commit-failure used to leave,
    which a later commit would otherwise use to skip the uncommitted gap."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()

    # Persist succeeded (frontier advanced to 10), then commit failed and the
    # offset was marked blocked — exactly the state the old code left behind.
    consumer._mark_committable(_mk_msg(_valid_payload(), offset=10))
    consumer._mark_blocked(_mk_msg(_valid_payload(), offset=10))

    await consumer._commit()

    # The commit is capped at blocked (10), never advanced to 11 (which would
    # skip the redelivery of offset 10).
    assert _last_commit_offsets(consumer) == {_tp(): 10}


@pytest.mark.asyncio
async def test_run_commit_failure_recreates_and_redelivers_idempotently() -> None:
    """Regression (issue #473): drive the production run() path.

    A DB-success/Kafka-commit-failure sequence must not mark the persisted
    message as a blocked gap: the first delivery persists, the offset commit
    fails, the consumer recreates (rebalance) so it re-reads from the last
    committed offset, and the message is redelivered idempotently — the
    delivery_log conflict-ignore INSERT is re-issued, never double-counted.
    """
    persisted_sqls: list[str] = []

    async def execute(*args: object, **kwargs: object) -> str:
        if args and "INSERT INTO delivery_log" in str(args[0]):
            persisted_sqls.append(str(args[0]))
        return "OK"

    conn = _FakeConn([], execute=execute)
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._running = True

    msg = _mk_msg(_valid_payload(), offset=10)
    state = {"commit_failures_left": 1}
    instances: list[_FakeKafkaConsumer] = []

    def factory(*args: object, **kwargs: object) -> _FakeKafkaConsumer:
        inst = _FakeKafkaConsumer([msg])

        async def commit(offsets: object) -> None:
            if state["commit_failures_left"] > 0:
                state["commit_failures_left"] -= 1
                raise KafkaError("offset commit failed")
            consumer._running = False  # stop run() after the successful commit

        inst.commit = AsyncMock(side_effect=commit)
        instances.append(inst)
        return inst

    # Spy: a persisted message must never be marked as a blocked processing gap.
    blocked_calls: list[object] = []
    real_mark_blocked = consumer._mark_blocked

    def spy_mark_blocked(m: object) -> None:
        blocked_calls.append(m)
        real_mark_blocked(m)

    consumer._mark_blocked = spy_mark_blocked  # type: ignore[method-assign]

    consumer._consumer = factory()
    with patch("app.consumer.afk_consumer.AIOKafkaConsumer", side_effect=factory):
        await consumer.run()

    # The commit failure recreated the consumer (rebalance), not a blocked gap.
    assert len(instances) == 2
    assert blocked_calls == []
    assert consumer._blocked == {}
    # Redelivery re-persisted idempotently (conflict-ignore, no double-count).
    assert len(persisted_sqls) == 2
    for sql in persisted_sqls:
        assert "ON CONFLICT (provider, delivery_id) DO NOTHING" in sql
    # The redelivered message's offset was committed.
    assert _last_commit_offsets(consumer) == {_tp(): 11}


def test_import_from_afk_consumer_module() -> None:
    """The consumer class is importable and does not import app into afk_outcomes."""
    import afk_outcomes  # noqa: F401

    assert AFKOutcomeConsumer.__name__ == "AFKOutcomeConsumer"


# ══════════════════════════════════════════════════════════════════════════
#  Stage-2 mapping bridge (normalized contract → canonical vocabulary) #482
# ══════════════════════════════════════════════════════════════════════════


def _normalized_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": DELIVERY_ID,
        "resource_type": "pull_request",
        "resource_id": "442",
        "repository": "owner/repo",
        "action": "merged",
        "occurred_at": "2026-08-01T10:30:00Z",
        "ingested_at": "2026-08-01T10:31:00Z",
        "actor": "carol",
        "payload_ref": "redacted-payload-ref-123",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("resource_type", "action", "expected_entity_type", "expected_event_type"),
    [
        ("pull_request", "merged", EntityType.CHANGE_REQUEST, "change_request.merged"),
        ("merge_request", "merged", EntityType.CHANGE_REQUEST, "change_request.merged"),
        ("pull_request", "opened", EntityType.CHANGE_REQUEST, "change_request.opened"),
        ("merge_request", "approved", EntityType.CHANGE_REQUEST, "change_request.approved"),
        (
            "merge_request",
            "review_requested",
            EntityType.CHANGE_REQUEST,
            "change_request.review_requested",
        ),
        ("issue", "opened", EntityType.ISSUE, "issue.opened"),
        ("issue", "closed", EntityType.ISSUE, "issue.closed"),
    ],
)
def test_map_normalized_event_bridges_resource_type_to_canonical(
    resource_type: str,
    action: str,
    expected_entity_type: EntityType,
    expected_event_type: str,
) -> None:
    message = NormalizedProviderEvent.model_validate(
        _normalized_payload(resource_type=resource_type, action=action)
    )
    mapped = map_normalized_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is expected_entity_type
    assert entity.entity_id == f"{expected_entity_type.value}:442"
    assert event.event_type == expected_event_type
    assert event.entity_id == entity.entity_id


def test_map_provider_event_dispatches_normalized_shape() -> None:
    """map_provider_event routes the normalized shape through the bridge."""
    message = NormalizedProviderEvent.model_validate(_normalized_payload())
    mapped = map_provider_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert event.event_type == "change_request.merged"


def test_map_normalized_event_carries_payload_ref() -> None:
    """The redacted payload reference is forwarded, never the payload itself."""
    message = NormalizedProviderEvent.model_validate(_normalized_payload())
    mapped = map_normalized_event(message)
    assert mapped is not None
    _entity, event = mapped
    assert event.payload == {"payload_ref": "redacted-payload-ref-123"}


def test_map_normalized_event_unknown_resource_type_returns_none() -> None:
    message = NormalizedProviderEvent.model_validate(
        _normalized_payload(resource_type="commit")
    )
    assert map_normalized_event(message) is None


def test_map_normalized_event_unmappable_action_returns_none() -> None:
    message = NormalizedProviderEvent.model_validate(
        _normalized_payload(action="assigned")
    )
    assert map_normalized_event(message) is None


@pytest.mark.asyncio
async def test_normalized_message_persists_as_canonical_event() -> None:
    """A normalized ``pull_request.merged`` persists as ``change_request.merged``."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(_normalized_payload()))

    event_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO engineering_events" in c.args[0]
    )
    assert event_call.args[3] == "change_request"  # entity_type
    assert event_call.args[4] == "442"  # external_id
    assert event_call.args[5] == "change_request.merged"  # event_type
    consumer._consumer.commit.assert_called_once()
    consumer._producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_unmappable_normalized_action_routes_to_dlq() -> None:
    """A normalized event with an unmappable action is poison → DLQ, no DB write."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(_mk_msg(_normalized_payload(action="assigned")))

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()
    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Unmappable message type" in dlq_payload["reason"]
    assert "pull_request.assigned" in dlq_payload["reason"]


# ══════════════════════════════════════════════════════════════════════════
#  Bounded exponential retry with jitter (#482)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_delay_applies_jitter_bounds() -> None:
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])))
    with patch("app.consumer.afk_consumer.random.uniform", return_value=0.5):
        assert consumer._retry_delay(0) == pytest.approx(0.5)  # 1.0 * 0.5
    with patch("app.consumer.afk_consumer.random.uniform", return_value=1.5):
        assert consumer._retry_delay(1) == pytest.approx(3.0)  # 1.0*2 * 1.5


@pytest.mark.asyncio
async def test_retry_delay_respects_max_backoff_cap() -> None:
    consumer = _make_consumer(pool=_FakePool(_FakeConn([])), max_backoff=10.0)
    with patch("app.consumer.afk_consumer.random.uniform", return_value=1.5):
        # 2**50 far exceeds the cap; the jitter applies to the capped 10.0.
        assert consumer._retry_delay(50) == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_retry_path_sleeps_with_jittered_delay() -> None:
    """The DB-retry loop sleeps a jittered (bounded) delay between attempts."""
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[RuntimeError("db down"), RuntimeError("db down"), "OK", "OK"]
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    msg = _mk_msg(_valid_payload())
    with (
        patch("app.consumer.afk_consumer.random.uniform", return_value=1.0),
        patch("app.consumer.afk_consumer.asyncio.sleep", side_effect=_fake_sleep),
    ):
        await consumer._process_message(msg)

    # Two retries happened: 1.0*2^0 and 1.0*2^1 (jitter pinned to 1.0).
    assert delays == [1.0, 2.0]
    assert consumer._consumer.commit.call_count == 1


# ══════════════════════════════════════════════════════════════════════════
#  Consumer metrics: per-state counters, retry histogram, DLQ depth, lag (#482)
# ══════════════════════════════════════════════════════════════════════════


def _snap(metrics: MetricsRegistry) -> dict:
    return metrics.snapshot()


@pytest.mark.asyncio
async def test_metrics_accepted_and_total_counters() -> None:
    metrics = MetricsRegistry()
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn), metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(_valid_payload(), offset=10))

    snap = _snap(metrics)
    assert snap[METRIC_MESSAGES_TOTAL] == 1
    assert snap[METRIC_MESSAGES_ACCEPTED] == 1
    assert snap[METRIC_MESSAGES_DLQ] == 0
    assert snap[METRIC_MESSAGES_POISON] == 0


@pytest.mark.asyncio
async def test_metrics_poison_dlq_and_depth() -> None:
    metrics = MetricsRegistry()
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn), metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(_mk_msg(_valid_payload(action="assigned")))

    snap = _snap(metrics)
    assert snap[METRIC_MESSAGES_POISON] == 1
    assert snap[METRIC_MESSAGES_DLQ] == 1
    assert snap[METRIC_DLQ_DEPTH] == 1
    assert snap[METRIC_MESSAGES_ACCEPTED] == 0


@pytest.mark.asyncio
async def test_metrics_retry_distribution_and_db_errors() -> None:
    metrics = MetricsRegistry()
    conn = _FakeConn(
        [],
        execute=AsyncMock(
            side_effect=[RuntimeError("db down"), RuntimeError("db down"), "OK", "OK"]
        ),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3, metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        await consumer._process_message(_mk_msg(_valid_payload()))

    snap = _snap(metrics)
    assert snap[METRIC_DB_ERRORS] == 2
    assert snap[METRIC_RETRIES] == 2
    hist = snap[METRIC_RETRIES_PER_MESSAGE]
    assert hist["count"] == 1
    assert hist["buckets"]["2"] == 1  # the message needed 2 retries before success


@pytest.mark.asyncio
async def test_metrics_retries_exhausted_records_actual_retry_count() -> None:
    """On exhaustion the histogram records the actual retry count.

    ``retries`` counts only non-final failed attempts, so for ``max_retries=3``
    the message actually retried 2 times (attempts 1 and 2) before the final
    attempt failed and was DLQ'd — consistent with the success path, which
    observes ``retries`` rather than ``max_retries``.
    """
    metrics = MetricsRegistry()
    conn = _FakeConn(
        [],
        execute=AsyncMock(side_effect=[RuntimeError("db down")] * 3),
    )
    consumer = _make_consumer(pool=_FakePool(conn), max_retries=3, metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    with patch("app.consumer.afk_consumer.asyncio.sleep", new_callable=AsyncMock):
        await consumer._process_message(_mk_msg(_valid_payload()))

    snap = _snap(metrics)
    hist = snap[METRIC_RETRIES_PER_MESSAGE]
    assert hist["buckets"]["2"] == 1  # actual retries = 2 for max_retries=3


@pytest.mark.asyncio
async def test_metrics_committed_offset_and_lag() -> None:
    metrics = MetricsRegistry()
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn), metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(_valid_payload(), offset=10))
    await consumer._process_message(_mk_msg(_valid_payload(), offset=11))

    snap = _snap(metrics)
    assert snap[f"{METRIC_COMMITTED_OFFSET}.0"] == 11
    assert snap[f"{METRIC_LAG}.0"] == 0  # every seen offset has been committed


@pytest.mark.asyncio
async def test_metrics_lag_reflects_uncommitted_backlog() -> None:
    metrics = MetricsRegistry()
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn), metrics=metrics)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    # Observe offsets 10 and 11, but commit only through 10.
    consumer._record_last_seen(_mk_msg(_valid_payload(), offset=10))
    consumer._record_last_seen(_mk_msg(_valid_payload(), offset=11))
    consumer._mark_committable(_mk_msg(_valid_payload(), offset=10))
    await consumer._commit()

    snap = _snap(metrics)
    assert snap[f"{METRIC_COMMITTED_OFFSET}.0"] == 10
    assert snap[f"{METRIC_LAG}.0"] == 1  # offset 11 seen but not yet committed


# ══════════════════════════════════════════════════════════════════════════
#  MetricsRegistry basics (app.core.metrics) — histogram, snapshot, reset
# ══════════════════════════════════════════════════════════════════════════


def test_registry_counter_gauge_histogram_and_snapshot() -> None:
    registry = MetricsRegistry()
    registry.counter("c").inc()
    registry.counter("c").inc(2)
    registry.gauge("g").set(1.5)
    registry.gauge("g").inc(0.5)
    registry.histogram("h").observe(2)

    snap = registry.snapshot()
    assert snap["c"] == 3
    assert snap["g"] == 2.0
    assert snap["h"]["count"] == 1
    assert snap["h"]["sum"] == 2.0
    assert snap["h"]["buckets"]["2"] == 1


def test_registry_rejects_type_conflict() -> None:
    registry = MetricsRegistry()
    registry.counter("x")
    with pytest.raises(ValueError):
        registry.gauge("x")


def test_registry_reset_clears_all_metrics() -> None:
    registry = MetricsRegistry()
    registry.counter("c").inc()
    registry.reset()
    assert registry.snapshot() == {}


@pytest.mark.asyncio
async def test_from_env_reads_afk_retry_settings() -> None:
    env_vars = {
        "GATEWAY_ENV": "development",
        "GATEWAY_KAFKA_BROKERS": "broker1:9092",
        "GATEWAY_AFK_OUTCOMES_TOPIC": "afk.events",
        "GATEWAY_AFK_OUTCOMES_DLQ_TOPIC": "afk.events-dlq",
        "GATEWAY_AFK_OUTCOMES_CONSUMER_GROUP_ID": "opencode-outcomes",
        "GATEWAY_AFK_OUTCOMES_PROVIDER": "github",
        "GATEWAY_AFK_OUTCOMES_REPOSITORY": "owner/repo",
        "GATEWAY_AFK_OUTCOMES_MAX_RETRIES": "7",
        "GATEWAY_AFK_OUTCOMES_INITIAL_BACKOFF_SECONDS": "2.0",
        "GATEWAY_AFK_OUTCOMES_MAX_BACKOFF_SECONDS": "90",
    }
    with (
        patch.dict(os.environ, env_vars, clear=True),
        patch("app.consumer.afk_consumer.asyncpg.create_pool", new_callable=AsyncMock),
        patch(
            "app.consumer.afk_consumer._build_adapter",
            return_value=(_FakeAdapter(), None),
        ),
    ):
        consumer = await AFKOutcomeConsumer.from_env()

    assert consumer._max_retries == 7
    assert consumer._initial_backoff == 2.0
    assert consumer._max_backoff == 90.0


# ══════════════════════════════════════════════════════════════════════════
#  DLQ operational max (issue #483) — stamping, classification, escalation
# ══════════════════════════════════════════════════════════════════════════


DLQ_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
MAX_AGE = 30


def _dlq_payload(
    *,
    dead_lettered_at: str | None = "2026-08-01T12:00:00+00:00",
    max_age_days: int = MAX_AGE,
) -> dict:
    """A DLQ message as produced by ``_send_to_dlq`` (with age metadata)."""
    return {
        "original_topic": "afk.events",
        "reason": "DB persist failed after 3 retries",
        "payload": {"delivery_id": DELIVERY_ID},
        "dead_lettered_at": dead_lettered_at,
        "max_age_days": max_age_days,
    }


def test_build_dlq_payload_stamps_age_metadata() -> None:
    """Every DLQ message carries dead_lettered_at + max_age_days so its age and
    the operational max are self-describing (measurable by the sweep)."""
    payload = build_dlq_payload(
        "afk.events", "boom", {"x": 1}, now=DLQ_NOW, max_age_days=MAX_AGE
    )

    assert payload["original_topic"] == "afk.events"
    assert payload["reason"] == "boom"
    assert payload["payload"] == {"x": 1}
    assert payload["dead_lettered_at"] == DLQ_NOW.isoformat()
    assert payload["max_age_days"] == MAX_AGE


def test_build_dlq_payload_defaults_max_age() -> None:
    """When no max age is given, the default operational max is stamped."""
    payload = build_dlq_payload("afk.events", "boom", {}, now=DLQ_NOW)
    assert payload["max_age_days"] == 30


def test_dlq_message_age_parses_dead_lettered_at() -> None:
    payload = _dlq_payload(dead_lettered_at="2026-08-01T12:00:00+00:00")
    assert dlq_message_age(payload, DLQ_NOW) == timedelta(days=15)


def test_dlq_message_age_missing_timestamp_returns_none() -> None:
    assert dlq_message_age({"reason": "no timestamp"}, DLQ_NOW) is None


def test_dlq_message_age_unparseable_timestamp_returns_none() -> None:
    assert dlq_message_age(_dlq_payload(dead_lettered_at="not-a-time"), DLQ_NOW) is None


def test_is_dlq_expired_boundary_strictly_older() -> None:
    """A message exactly at the max-age edge is retained (strict ``>``); only
    strictly older messages are expired — the same boundary semantics as the
    transcript retention job."""
    at_edge = _dlq_payload(dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE)).isoformat())
    past_edge = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE) - timedelta(seconds=1)).isoformat()
    )
    within = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE - 1)).isoformat()
    )

    assert is_dlq_expired(at_edge, DLQ_NOW, MAX_AGE) is False
    assert is_dlq_expired(past_edge, DLQ_NOW, MAX_AGE) is True
    assert is_dlq_expired(within, DLQ_NOW, MAX_AGE) is False


def test_is_dlq_expired_unknown_age_retained() -> None:
    """A message without a usable dead_lettered_at has unknown age and is
    retained (never prematurely expired)."""
    assert is_dlq_expired({"reason": "no timestamp"}, DLQ_NOW, MAX_AGE) is False


def test_classify_dlq_message() -> None:
    past = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 1)).isoformat()
    )
    within = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=1)).isoformat()
    )
    assert classify_dlq_message(past, DLQ_NOW, MAX_AGE) == "expired"
    assert classify_dlq_message(within, DLQ_NOW, MAX_AGE) == "retain"


def test_build_escalation_payload_preserves_original_and_reason() -> None:
    """Escalation preserves the original payload + reason and stamps a
    deterministic ``escalation_key`` + ``escalation_reason``, so the operator
    can resolve it later and re-escalations are content-stable."""
    payload = _dlq_payload()
    escalated = build_escalation_payload(payload, now=DLQ_NOW, max_age_days=MAX_AGE)

    assert escalated["original_topic"] == "afk.events"
    assert escalated["reason"] == payload["reason"]
    assert escalated["payload"] == payload["payload"]
    assert escalated["dead_lettered_at"] == payload["dead_lettered_at"]
    assert "escalation_key" in escalated
    assert "escalated_at" not in escalated
    assert f"{MAX_AGE}" in escalated["escalation_reason"]


def test_build_escalation_payload_is_content_stable() -> None:
    """Same inputs → byte-identical output (no volatile ``now``-derived field).

    Two escalations of the same DLQ record (even at different ``now``) must be
    identical, so a later sweep re-escalating the same record is idempotent by
    content (``escalation_key`` is a deterministic natural key over the DLQ
    record's own stable identity).
    """
    payload = _dlq_payload()
    first = build_escalation_payload(payload, now=DLQ_NOW, max_age_days=MAX_AGE)
    later = build_escalation_payload(
        payload, now=DLQ_NOW + timedelta(days=10), max_age_days=MAX_AGE
    )

    assert first == later
    assert "escalation_key" in first
    assert "escalated_at" not in first
    assert len(first["escalation_key"]) == 64  # sha256 hex digest


def test_run_dlq_sweep_classifies_and_builds_escalations() -> None:
    expired = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 5)).isoformat()
    )
    retained = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=1)).isoformat()
    )
    unknown = {"reason": "no timestamp"}

    report = run_dlq_sweep(
        [expired, retained, unknown], now=DLQ_NOW, max_age_days=MAX_AGE
    )

    assert report.scanned == 3
    assert report.expired == 1
    assert report.retained == 2
    assert len(report.escalated) == 1
    assert report.escalated[0]["payload"] == expired["payload"]


def test_run_dlq_sweep_dry_run_reports_without_collecting_escalations() -> None:
    expired = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 5)).isoformat()
    )
    report = run_dlq_sweep([expired], now=DLQ_NOW, max_age_days=MAX_AGE, dry_run=True)

    assert report.dry_run is True
    assert report.expired == 1
    assert report.escalated == []  # nothing is escalated in a dry run


def test_run_dlq_sweep_limit_caps_scanned_messages() -> None:
    messages = [
        _dlq_payload(dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + i)).isoformat())
        for i in range(1, 6)
    ]
    report = run_dlq_sweep(messages, now=DLQ_NOW, max_age_days=MAX_AGE, limit=2)

    assert report.scanned == 2
    assert report.expired == 2
    assert len(report.escalated) == 2


@pytest.mark.asyncio
async def test_send_to_dlq_stamps_operational_max() -> None:
    """The producer path stamps dead_lettered_at + max_age_days into the DLQ
    message (the enforcement surface for the operational max)."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    with patch(
        "app.consumer.afk_consumer.datetime", wraps=datetime
    ) as mock_dt:
        mock_dt.now.return_value = DLQ_NOW
        await consumer._process_message(_mk_msg(_valid_payload(action="assigned")))

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert dlq_payload["dead_lettered_at"] == DLQ_NOW.isoformat()
    assert dlq_payload["max_age_days"] == 30


@pytest.mark.asyncio
async def test_from_env_reads_dlq_max_age() -> None:
    env_vars = {
        "GATEWAY_ENV": "development",
        "GATEWAY_KAFKA_BROKERS": "broker1:9092",
        "GATEWAY_AFK_OUTCOMES_TOPIC": "afk.events",
        "GATEWAY_AFK_OUTCOMES_DLQ_TOPIC": "afk.events-dlq",
        "GATEWAY_AFK_OUTCOMES_CONSUMER_GROUP_ID": "opencode-outcomes",
        "GATEWAY_AFK_OUTCOMES_PROVIDER": "github",
        "GATEWAY_AFK_OUTCOMES_REPOSITORY": "owner/repo",
        "GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS": "14",
    }
    with (
        patch.dict(os.environ, env_vars, clear=True),
        patch("app.consumer.afk_consumer.asyncpg.create_pool", new_callable=AsyncMock),
        patch(
            "app.consumer.afk_consumer._build_adapter",
            return_value=(_FakeAdapter(), None),
        ),
    ):
        consumer = await AFKOutcomeConsumer.from_env()

    assert consumer._dlq_max_age_days == 14


# ── DLQ sweep offset commits + lenient deserialization (PR #492 findings) ──


class _DLQRecord:
    """A minimal record quacking like an aiokafka ConsumerRecord for the sweep."""

    def __init__(self, topic: str, partition: int, offset: int, value: object) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class _FakeSweepConsumer:
    """Stand-in for AIOKafkaConsumer in the sweep: __aiter__/start/stop/commit."""

    def __init__(self, messages: list[_DLQRecord]) -> None:
        self._messages = messages
        self.commit = AsyncMock(return_value=None)
        self.start = AsyncMock(return_value=None)
        self.stop = AsyncMock(return_value=None)

    def __aiter__(self) -> _FakeAsyncIterator:
        return _FakeAsyncIterator(self._messages)


class _FakeSweepProducer:
    """Stand-in for AIOKafkaProducer in the sweep: start/stop/send_and_wait."""

    def __init__(self) -> None:
        self.start = AsyncMock(return_value=None)
        self.stop = AsyncMock(return_value=None)
        self.send_and_wait = AsyncMock(return_value=None)


def test_lenient_dlq_deserializer_returns_none_on_corrupt() -> None:
    """Corrupt bytes decode to ``None`` instead of raising inside the consumer."""
    assert _lenient_dlq_deserializer(b"not json") is None
    assert _lenient_dlq_deserializer(b"\xff\xfe invalid utf8") is None
    assert _lenient_dlq_deserializer(json.dumps({"a": 1}).encode("utf-8")) == {"a": 1}


@pytest.mark.asyncio
async def test_sweep_dlq_commits_offsets_in_write_mode() -> None:
    """Write-mode sweep commits per-partition offsets: a partition with a
    retained record commits at its first retained offset (re-examined later);
    an all-expired partition commits at max-consumed + 1 (never re-read)."""
    dlq_topic = "afk.events-dlq"
    escalation_topic = "afk.events-dlq-expired"
    expired = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 5)).isoformat()
    )
    retained = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=1)).isoformat()
    )
    messages = [
        _DLQRecord(dlq_topic, 0, 5, expired),
        _DLQRecord(dlq_topic, 0, 6, retained),
        _DLQRecord(dlq_topic, 1, 10, expired),
    ]
    consumer = _FakeSweepConsumer(messages)
    producer = _FakeSweepProducer()

    with (
        patch("app.consumer.afk_consumer.AIOKafkaConsumer", return_value=consumer),
        patch("app.consumer.afk_consumer.AIOKafkaProducer", return_value=producer),
    ):
        report = await sweep_dlq(
            "broker:9092",
            dlq_topic,
            escalation_topic,
            MAX_AGE,
            now=DLQ_NOW,
        )

    assert report.scanned == 3
    assert report.expired == 2
    assert report.retained == 1

    # Two escalations published, both to the escalation topic.
    assert producer.send_and_wait.call_count == 2
    assert [c.args[0] for c in producer.send_and_wait.call_args_list] == [
        escalation_topic,
        escalation_topic,
    ]

    # Commit: partition 0 → first retained (6); partition 1 → max+1 (11).
    consumer.commit.assert_called_once()
    assert consumer.commit.call_args.args[0] == {
        TopicPartition(dlq_topic, 0): 6,
        TopicPartition(dlq_topic, 1): 11,
    }


@pytest.mark.asyncio
async def test_sweep_dlq_dry_run_does_not_commit_or_publish() -> None:
    """Dry-run sweep publishes nothing and commits nothing."""
    dlq_topic = "afk.events-dlq"
    expired = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 5)).isoformat()
    )
    consumer = _FakeSweepConsumer([_DLQRecord(dlq_topic, 0, 5, expired)])
    producer = _FakeSweepProducer()

    with (
        patch("app.consumer.afk_consumer.AIOKafkaConsumer", return_value=consumer),
        patch("app.consumer.afk_consumer.AIOKafkaProducer", return_value=producer),
    ):
        report = await sweep_dlq(
            "broker:9092",
            dlq_topic,
            "afk.events-dlq-expired",
            MAX_AGE,
            now=DLQ_NOW,
            dry_run=True,
        )

    assert report.expired == 1
    assert report.escalated == []
    producer.send_and_wait.assert_not_called()
    consumer.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_dlq_skips_corrupt_record_with_warning() -> None:
    """A corrupt (non-object) record is skipped with a warning, does not crash
    the sweep, and its offset is still committed past so the commit position
    is not wrong; the well-formed records are still processed."""
    dlq_topic = "afk.events-dlq"
    expired = _dlq_payload(
        dead_lettered_at=(DLQ_NOW - timedelta(days=MAX_AGE + 5)).isoformat()
    )
    messages = [
        _DLQRecord(dlq_topic, 0, 3, None),  # corrupt (lenient deserializer sentinel)
        _DLQRecord(dlq_topic, 0, 4, expired),
    ]
    consumer = _FakeSweepConsumer(messages)
    producer = _FakeSweepProducer()

    with (
        patch("app.consumer.afk_consumer.AIOKafkaConsumer", return_value=consumer),
        patch("app.consumer.afk_consumer.AIOKafkaProducer", return_value=producer),
        patch("app.consumer.afk_consumer.logger") as mock_logger,
    ):
        report = await sweep_dlq(
            "broker:9092", dlq_topic, "afk.events-dlq-expired", MAX_AGE, now=DLQ_NOW
        )

    assert report.scanned == 1  # only the well-formed record is scanned
    assert report.expired == 1
    mock_logger.warning.assert_called_once()

    # The corrupt offset (3) is committed past together with the expired
    # record (4) — commit at max-consumed+1 = 5.
    consumer.commit.assert_called_once()
    assert consumer.commit.call_args.args[0] == {TopicPartition(dlq_topic, 0): 5}
    assert producer.send_and_wait.call_count == 1


# ══════════════════════════════════════════════════════════════════════════
#  Producer→consumer contract pinning (#485)
# ══════════════════════════════════════════════════════════════════════════
#
# The producer (fast-api-eda-gateway #97–#102, PRD #478) emits a normalized,
# schema-versioned event on ``afk.events``.  These tests pin the exact
# producer→consumer contract: a fixture event matching the producer contract
# must map through ``map_provider_event`` to the canonical outcome-layer
# vocabulary WITHOUT DLQ routing, and every contract field must be carried
# through (never dropped).  A payload that violates the contract routes to
# the DLQ with the original payload plus a reason (documented in
# ``docs/afk-outcome-contract-validation.md``).

# The pinned producer contract: provider, forwarded ``delivery_id``, stable
# ``resource_id``, ``resource_type``, ``action``, ``occurred_at``,
# ``ingested_at``, ``actor``, redacted ``payload_ref``, and ``schema_version``.
PRODUCER_CONTRACT_EVENT: dict[str, object] = {
    "schema_version": "1.0",
    "provider": "github",
    "delivery_id": "22222222-3333-4444-5555-666666666666",
    "resource_type": "pull_request",
    "resource_id": "442",
    "repository": "owner/repo",
    "action": "merged",
    "occurred_at": "2026-08-13T10:10:29Z",
    "ingested_at": "2026-08-13T10:10:30Z",
    "actor": "carol",
    "payload_ref": "redacted-payload-ref-442",
}


def test_producer_contract_event_has_exact_fields() -> None:
    """The pinned producer contract carries exactly the agreed field set.

    Adding or removing a field here is a contract change and must fail this
    test (and be reconciled with ADR 0020 / PRD #478) rather than drift
    silently against the producer.
    """
    assert set(PRODUCER_CONTRACT_EVENT) == {
        "schema_version",
        "provider",
        "delivery_id",
        "resource_type",
        "resource_id",
        "repository",
        "action",
        "occurred_at",
        "ingested_at",
        "actor",
        "payload_ref",
    }


def test_producer_contract_maps_to_canonical_change_request() -> None:
    """The contract fixture maps to the canonical change_request vocabulary.

    Every contract field is carried through the bridge: ``resource_type``
    selects the canonical entity type, ``resource_id`` becomes the stable
    identity, ``action`` the event suffix, and ``payload_ref`` the redacted
    reference (never the payload itself).
    """
    message = NormalizedProviderEvent.model_validate(PRODUCER_CONTRACT_EVENT)
    mapped = map_provider_event(message)

    assert mapped is not None
    entity, event = mapped

    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert entity.entity_id == "change_request:442"
    assert entity.provider is Provider.GITHUB
    assert entity.repository == "owner/repo"
    assert entity.number == 442

    assert event.event_type == "change_request.merged"
    assert event.entity_id == "change_request:442"
    assert event.provider is Provider.GITHUB
    assert event.occurred_at == datetime(2026, 8, 13, 10, 10, 29, tzinfo=UTC)
    assert event.actor == "carol"
    assert event.payload == {"payload_ref": "redacted-payload-ref-442"}


@pytest.mark.asyncio
async def test_producer_contract_event_is_not_routed_to_dlq() -> None:
    """The contract-conforming event persists — never routed to the DLQ."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(PRODUCER_CONTRACT_EVENT))

    consumer._producer.send_and_wait.assert_not_called()
    consumer._consumer.commit.assert_called_once()

    event_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO engineering_events" in c.args[0]
    )
    assert event_call.args[3] == "change_request"  # entity_type
    assert event_call.args[4] == "442"  # external_id
    assert event_call.args[5] == "change_request.merged"  # event_type


def test_producer_contract_gitlab_merge_request_maps_to_change_request() -> None:
    """The same contract holds cross-provider: GitLab ``merge_request`` → change_request."""
    message = NormalizedProviderEvent.model_validate(
        {**PRODUCER_CONTRACT_EVENT, "provider": "gitlab", "resource_type": "merge_request"}
    )
    mapped = map_provider_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert entity.provider is Provider.GITLAB
    assert event.event_type == "change_request.merged"


@pytest.mark.asyncio
async def test_contract_violation_routes_to_dlq_with_payload_and_reason() -> None:
    """A normalized event violating the contract (unmappable action) → DLQ.

    The documented contract-violation behavior: the full original payload is
    forwarded to ``afk.events-dlq`` with ``original_topic`` and a ``reason``
    string, and nothing is persisted.
    """
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    violating = {**PRODUCER_CONTRACT_EVENT, "action": "synchronize"}  # not canonical
    await consumer._process_message(_mk_msg(violating))

    conn.execute.assert_not_called()  # nothing persisted
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert topic == "afk.events-dlq"
    assert dlq_payload["original_topic"] == "afk.events"
    assert "Unmappable message type" in dlq_payload["reason"]
    assert "pull_request.synchronize" in dlq_payload["reason"]
    assert dlq_payload["payload"] == violating  # original payload preserved verbatim


@pytest.mark.asyncio
async def test_contract_violation_bad_json_routes_to_dlq_with_raw_payload() -> None:
    """A non-JSON body is DLQ'd carrying the raw bytes plus a reason."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(_mk_invalid_json_msg())

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert dlq_payload["reason"]
    assert dlq_payload["payload"] == {"raw": "not valid json at all"}


# ══════════════════════════════════════════════════════════════════════════
#  Producer: executable normalized-event v1 contract artifacts (#494)
# ══════════════════════════════════════════════════════════════════════════
#
# The producer (fast-api-eda-gateway) owns the executable normalized-event
# contract published in docs/contracts/normalized-event-v1/.  These tests:
#
# 1. Build records through the real NormalizedProviderEvent serializer and
#    validate every fixture against the published JSON Schema.
# 2. Verify payload references in generated fixtures identify the same
#    provider and delivery_id as their containing envelope.
# 3. Verify contract artifacts contain no raw webhook payload or secret data.
# 4. Prove that every allowed v1 (resource_type, action) pair is covered.


import json as _json_mod
from pathlib import Path as _Path


_CONTRACTS_DIR = _Path(__file__).resolve().parent.parent / "docs" / "contracts" / "normalized-event-v1"
_SCHEMA_PATH = _CONTRACTS_DIR / "schema.json"
_FIXTURES_DIR = _CONTRACTS_DIR / "fixtures"


def _load_schema() -> dict:
    return _json_mod.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_fixture(filename: str) -> dict:
    return _json_mod.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# The complete set of allowed v1 (resource_type, action) pairs.
_ALLOWED_PAIRS: set[tuple[str, str]] = {
    ("issue", "opened"),
    ("issue", "closed"),
    ("pull_request", "opened"),
    ("pull_request", "review_requested"),
    ("pull_request", "changes_requested"),
    ("pull_request", "approved"),
    ("pull_request", "merged"),
    ("pull_request", "closed"),
    ("merge_request", "opened"),
    ("merge_request", "review_requested"),
    ("merge_request", "changes_requested"),
    ("merge_request", "approved"),
    ("merge_request", "merged"),
    ("merge_request", "closed"),
}


def _expected_fixture_filename(resource_type: str, action: str) -> str:
    return f"{resource_type}.{action}.json"


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_fixture_exists_for_every_allowed_pair(
    resource_type: str, action: str
) -> None:
    """Every allowed v1 (resource_type, action) pair has a fixture file."""
    filename = _expected_fixture_filename(resource_type, action)
    assert (_FIXTURES_DIR / filename).is_file(), (
        f"Missing fixture: {filename}"
    )


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_fixture_built_through_real_serializer(
    resource_type: str, action: str
) -> None:
    """Every fixture round-trips through the real NormalizedProviderEvent serializer.

    The fixture is loaded, validated through Pydantic (the real serializer),
    and the re-serialized output matches the fixture byte-for-byte — proving
    the fixture is real serializer output, not hand-crafted JSON.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    re_serialized = _json_mod.loads(message.model_dump_json(exclude_none=True))
    assert re_serialized == fixture, (
        f"Fixture {resource_type}.{action} does not round-trip through the "
        f"real serializer — the fixture may be hand-crafted rather than "
        f"serializer-generated."
    )


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_fixture_validates_against_published_schema(
    resource_type: str, action: str
) -> None:
    """Every fixture validates against the published JSON Schema."""
    from jsonschema import validate

    schema = _load_schema()
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    # Should not raise ValidationError.
    validate(instance=fixture, schema=schema)


def test_all_fixtures_share_same_provider_and_delivery_id() -> None:
    """Payload references in generated fixtures identify the same provider and
    delivery_id as their containing envelope (acceptance criterion)."""
    provider = None
    delivery_id = None
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
        if provider is None:
            provider = fixture["provider"]
            delivery_id = fixture["delivery_id"]
        else:
            assert fixture["provider"] == provider, (
                f"Fixture {resource_type}.{action} has provider={fixture['provider']!r}, "
                f"expected {provider!r}"
            )
            assert fixture["delivery_id"] == delivery_id, (
                f"Fixture {resource_type}.{action} has delivery_id={fixture['delivery_id']!r}, "
                f"expected {delivery_id!r}"
            )


def test_fixture_payload_ref_matches_resource_id() -> None:
    """Every fixture's payload_ref identifies the same resource as its
    resource_id (the redacted payload reference is scoped to the resource)."""
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
        resource_id = fixture["resource_id"]
        payload_ref = fixture.get("payload_ref")
        assert payload_ref is not None, (
            f"Fixture {resource_type}.{action} has no payload_ref"
        )
        assert resource_id in payload_ref, (
            f"Fixture {resource_type}.{action} payload_ref={payload_ref!r} "
            f"does not reference resource_id={resource_id!r}"
        )


def test_contract_artifacts_contain_no_raw_webhook_payload_or_secrets() -> None:
    """Contract artifacts (schema + fixtures) contain no raw webhook payload
    or secret data (acceptance criterion)."""
    suspicious_keys = {
        "token", "secret", "password", "api_key", "webhook",
        "raw_payload", "hook", "signature", "authorization",
    }

    # Check the schema.
    schema = _load_schema()
    _assert_no_suspicious_keys(schema, f"{_SCHEMA_PATH}", suspicious_keys)

    # Check every fixture.
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
        _assert_no_suspicious_keys(
            fixture,
            f"fixture {resource_type}.{action}",
            suspicious_keys,
        )
        # No nested payload object — only payload_ref.
        assert "payload" not in fixture or not isinstance(fixture.get("payload"), dict), (
            f"Fixture {resource_type}.{action} contains a nested payload object "
            f"(raw webhook payload) — only payload_ref is allowed."
        )


def _assert_no_suspicious_keys(
    obj: dict, label: str, suspicious: set[str]
) -> None:
    """Assert that no key in ``obj`` (recursively) matches a suspicious pattern."""
    for key in obj:
        key_lower = key.lower()
        for s in suspicious:
            assert s not in key_lower, (
                f"{label}: suspicious key {key!r} (matches {s!r})"
            )
        if isinstance(obj[key], dict):
            _assert_no_suspicious_keys(obj[key], f"{label}.{key}", suspicious)


def test_schema_is_valid_json_schema() -> None:
    """The published schema is a valid JSON Schema document."""
    from jsonschema.validators import validator_for

    schema = _load_schema()
    # Check that jsonschema can construct a validator for it (validates
    # the meta-schema implicitly).
    cls = validator_for(schema)
    cls.check_schema(schema)


def test_schema_declares_additional_properties_false() -> None:
    """The schema rejects unknown fields (additionalProperties: false)."""
    schema = _load_schema()
    assert schema.get("additionalProperties") is False, (
        "Schema must set additionalProperties: false to reject unknown fields"
    )


def test_schema_rejects_unknown_resource_type() -> None:
    """The schema rejects a resource_type not in the allowed enum."""
    from jsonschema import validate, ValidationError

    schema = _load_schema()
    invalid = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": "00000000-0000-0000-0000-000000000001",
        "resource_type": "commit",
        "resource_id": "abc123",
        "repository": "owner/repo",
        "action": "opened",
        "occurred_at": "2026-08-15T10:00:00Z",
    }
    with pytest.raises(ValidationError):
        validate(instance=invalid, schema=schema)


def test_schema_rejects_issue_with_invalid_action() -> None:
    """The schema rejects an issue with a non-issue action (e.g. merged)."""
    from jsonschema import validate, ValidationError

    schema = _load_schema()
    invalid = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": "00000000-0000-0000-0000-000000000001",
        "resource_type": "issue",
        "resource_id": "100",
        "repository": "owner/repo",
        "action": "merged",
        "occurred_at": "2026-08-15T10:00:00Z",
    }
    with pytest.raises(ValidationError):
        validate(instance=invalid, schema=schema)


def test_schema_rejects_unknown_field() -> None:
    """The schema rejects a message with an unknown field."""
    from jsonschema import validate, ValidationError

    schema = _load_schema()
    invalid = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": "00000000-0000-0000-0000-000000000001",
        "resource_type": "issue",
        "resource_id": "100",
        "repository": "owner/repo",
        "action": "opened",
        "occurred_at": "2026-08-15T10:00:00Z",
        "extra_field": "should be rejected",
    }
    with pytest.raises(ValidationError):
        validate(instance=invalid, schema=schema)


def test_schema_accepts_null_optional_fields() -> None:
    """The schema accepts null for optional fields (actor, ingested_at, payload_ref)."""
    from jsonschema import validate

    schema = _load_schema()
    valid = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": "00000000-0000-0000-0000-000000000001",
        "resource_type": "issue",
        "resource_id": "100",
        "repository": "owner/repo",
        "action": "opened",
        "occurred_at": "2026-08-15T10:00:00Z",
        "ingested_at": None,
        "actor": None,
        "payload_ref": None,
    }
    validate(instance=valid, schema=schema)


def test_fixture_count_matches_allowed_pairs() -> None:
    """The number of fixture files matches the number of allowed pairs."""
    fixture_files = sorted(
        f.name for f in _FIXTURES_DIR.glob("*.json")
    )
    expected_files = sorted(
        _expected_fixture_filename(rt, a) for rt, a in _ALLOWED_PAIRS
    )
    assert fixture_files == expected_files, (
        f"Fixture files {fixture_files} do not match expected {expected_files}"
    )


# ══════════════════════════════════════════════════════════════════════════
#  CLI dispatch (PR #492 review) — _parse_cli
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--dlq-sweep"], (True, [])),
        (["--dlq-sweep", "--batch-size", "50"], (True, ["--batch-size", "50"])),
        (["--batch-size", "50", "--dlq-sweep"], (True, ["--batch-size", "50"])),
        (["--dlq-sweep", "--dry-run", "--limit", "5"], (True, ["--dry-run", "--limit", "5"])),
        ([], (False, [])),
        (["--some-future-flag", "x"], (False, ["--some-future-flag", "x"])),
        # A literal "--dlq-sweep" that is the VALUE of another option must not
        # trigger sweep mode (the reviewer's fragility scenario).
        (
            ["--future-option", "--dlq-sweep"],
            (False, ["--future-option", "--dlq-sweep"]),
        ),
    ],
)
def test_parse_cli(argv: list[str], expected: tuple[bool, list[str]]) -> None:
    """``_parse_cli`` detects ``--dlq-sweep`` as a flag, never as a value.

    The sweep's own flags flow through ``remaining`` unchanged; a literal
    ``--dlq-sweep`` positioned as another option's value does not switch mode.
    """
    assert _parse_cli(argv) == expected


# ══════════════════════════════════════════════════════════════════════════
#  Consumer: pin v1 contract and validate nested normalized events (#495)
# ══════════════════════════════════════════════════════════════════════════
#
# The consumer pins the producer-owned normalized-event v1 schema and
# fixture artifacts with immutable source provenance, then validates the
# shipped nested envelope.  The validation boundary distinguishes valid v1
# lifecycle observations from malformed data and unsupported versions before
# mapping or persistence.  Repository identity is derived strictly from the
# producer repository URL.


# ── Nested v1 envelope shape ────────────────────────────────────────────────


def _nested_v1_payload(**overrides: object) -> dict:
    """A v1 nested-envelope normalized event (resource + redacted_payload objects)."""
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "provider": "github",
        "delivery_id": "33333333-4444-5555-6666-777777777777",
        "occurred_at": "2026-08-15T10:00:00Z",
        "ingested_at": "2026-08-15T10:00:01Z",
        "actor": "test-user",
        "resource": {
            "resource_type": "pull_request",
            "resource_id": "200",
            "repository": "https://github.com/owner/repo",
            "action": "merged",
        },
        "redacted_payload": {
            "reference": "redacted-payload-ref-200",
            "provider": "github",
            "delivery_id": "33333333-4444-5555-6666-777777777777",
        },
    }
    payload.update(overrides)  # type: ignore[arg-type]
    return payload


def test_nested_v1_envelope_parses_through_normalized_provider_event() -> None:
    """The nested v1 shape (resource + redacted_payload objects) parses through
    the real NormalizedProviderEvent serializer."""
    message = NormalizedProviderEvent.model_validate(_nested_v1_payload())

    assert message.schema_version == "1.0"
    assert message.provider is Provider.GITHUB
    assert message.delivery_id == "33333333-4444-5555-6666-777777777777"
    assert message.resource is not None
    assert message.resource.resource_type == "pull_request"
    assert message.resource.resource_id == "200"
    assert message.resource.repository == "https://github.com/owner/repo"
    assert message.resource.action == "merged"
    assert message.redacted_payload is not None
    assert message.redacted_payload.reference == "redacted-payload-ref-200"
    assert message.redacted_payload.provider == "github"
    assert message.redacted_payload.delivery_id == "33333333-4444-5555-6666-777777777777"


def test_nested_v1_envelope_effective_properties() -> None:
    """Effective properties resolve from the nested objects when present."""
    message = NormalizedProviderEvent.model_validate(_nested_v1_payload())

    assert message.effective_resource_type == "pull_request"
    assert message.effective_resource_id == "200"
    assert message.effective_repository == "https://github.com/owner/repo"
    assert message.effective_action == "merged"
    assert message.effective_payload_ref == "redacted-payload-ref-200"


def test_nested_v1_envelope_maps_to_canonical() -> None:
    """The nested v1 envelope maps through map_normalized_event to the canonical
    outcome-layer vocabulary."""
    message = NormalizedProviderEvent.model_validate(_nested_v1_payload())
    mapped = map_normalized_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert entity.entity_id == "change_request:200"
    assert entity.repository == "https://github.com/owner/repo"
    assert event.event_type == "change_request.merged"
    assert event.payload == {"payload_ref": "redacted-payload-ref-200"}


# ── Validation: schema version ──────────────────────────────────────────────


def test_validate_accepts_v1_schema_version() -> None:
    """Schema version "1.0" passes validation."""
    message = NormalizedProviderEvent.model_validate(_nested_v1_payload())
    validate_normalized_event(message)  # does not raise


def test_validate_rejects_unsupported_schema_version() -> None:
    """An unsupported schema version raises NormalizedEventValidationError."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(schema_version="2.0")
    )
    with pytest.raises(NormalizedEventValidationError, match="Unsupported schema version"):
        validate_normalized_event(message)


def test_validate_rejects_unknown_schema_version() -> None:
    """An unknown schema version (e.g. "0.9") raises NormalizedEventValidationError."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(schema_version="0.9")
    )
    with pytest.raises(NormalizedEventValidationError, match="Unsupported schema version"):
        validate_normalized_event(message)


# ── Validation: reference mismatch ──────────────────────────────────────────


def test_validate_rejects_provider_mismatch() -> None:
    """redacted_payload.provider != envelope.provider → DLQ."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            redacted_payload={
                "reference": "ref-200",
                "provider": "gitlab",
                "delivery_id": "33333333-4444-5555-6666-777777777777",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError, match="Reference mismatch.*provider"):
        validate_normalized_event(message)


def test_validate_rejects_delivery_id_mismatch() -> None:
    """redacted_payload.delivery_id != envelope.delivery_id → DLQ."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            redacted_payload={
                "reference": "ref-200",
                "provider": "github",
                "delivery_id": "wrong-delivery-id",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError, match="Reference mismatch.*delivery_id"):
        validate_normalized_event(message)


def test_validate_accepts_null_redacted_payload_fields() -> None:
    """Null provider/delivery_id in redacted_payload are not checked (no mismatch)."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            redacted_payload={
                "reference": "ref-200",
                "provider": None,
                "delivery_id": None,
            }
        )
    )
    validate_normalized_event(message)  # does not raise


# ── Validation: repository identity ─────────────────────────────────────────


def test_validate_rejects_invalid_repository_identity() -> None:
    """A non-HTTP(S) repository URL raises NormalizedEventValidationError."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            resource={
                "resource_type": "pull_request",
                "resource_id": "200",
                "repository": "ftp://github.com/owner/repo",
                "action": "merged",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError, match="Invalid repository identity"):
        validate_normalized_event(message)


def test_validate_rejects_empty_repository() -> None:
    """An empty repository URL raises NormalizedEventValidationError."""
    message = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            resource={
                "resource_type": "pull_request",
                "resource_id": "200",
                "repository": "https:///owner/repo",
                "action": "merged",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError, match="Invalid repository identity"):
        validate_normalized_event(message)


# ── Repository URL normalization ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Basic normalization.
        ("https://github.com/owner/repo", "github.com/owner/repo"),
        ("https://github.com/Owner/Repo", "github.com/Owner/Repo"),  # path case preserved
        ("https://GITHUB.COM/owner/repo", "github.com/owner/repo"),  # hostname lowercased
        # Trailing slash.
        ("https://github.com/owner/repo/", "github.com/owner/repo"),
        # Terminal .git.
        ("https://github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://github.com/owner/repo.git/", "github.com/owner/repo"),
        # Default ports stripped.
        ("https://github.com:443/owner/repo", "github.com/owner/repo"),
        ("http://github.com:80/owner/repo", "github.com/owner/repo"),
        # Non-default ports preserved.
        ("https://github.com:8443/owner/repo", "github.com:8443/owner/repo"),
        ("http://github.com:8080/owner/repo", "github.com:8080/owner/repo"),
        # Credentials removed.
        ("https://user:pass@github.com/owner/repo", "github.com/owner/repo"),
        # Query string removed.
        ("https://github.com/owner/repo?ref=main", "github.com/owner/repo"),
        # Fragment removed.
        ("https://github.com/owner/repo#readme", "github.com/owner/repo"),
        # GitLab-style.
        ("https://gitlab.com/group/subgroup/project", "gitlab.com/group/subgroup/project"),
        # Self-hosted.
        ("https://git.example.com/org/repo", "git.example.com/org/repo"),
    ],
)
def test_normalize_repository_url(raw: str, expected: str) -> None:
    assert normalize_repository_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-url",
        "ftp://github.com/owner/repo",
        "file:///etc/passwd",
        "github.com/owner/repo",  # no scheme
        "//github.com/owner/repo",  # protocol-relative
        "/owner/repo",  # relative path
        "https:///owner/repo",  # empty hostname
        "https:///",  # no path
    ],
)
def test_normalize_repository_url_rejects_invalid(raw: str) -> None:
    assert normalize_repository_url(raw) is None


def test_normalize_repository_url_credentials_not_in_identity() -> None:
    """Credentials, query strings, and fragments cannot become part of repository identity."""
    result = normalize_repository_url(
        "https://admin:secret@github.com/owner/repo.git?ref=main#section"
    )
    assert result == "github.com/owner/repo"
    assert "admin" not in result
    assert "secret" not in result
    assert "ref=main" not in result
    assert "section" not in result


# ── Validation: distinct DLQ reasons per violation class ────────────────────


@pytest.mark.parametrize(
    ("payload_builder", "expected_reason_substring"),
    [
        (
            lambda: _nested_v1_payload(schema_version="2.0"),
            "Unsupported schema version",
        ),
        (
            lambda: _nested_v1_payload(
                resource={
                    "resource_type": "pull_request",
                    "resource_id": "200",
                    "repository": "ftp://github.com/owner/repo",
                    "action": "merged",
                }
            ),
            "Invalid repository identity",
        ),
        (
            lambda: _nested_v1_payload(
                redacted_payload={
                    "reference": "ref-200",
                    "provider": "gitlab",
                    "delivery_id": "33333333-4444-5555-6666-777777777777",
                }
            ),
            "Reference mismatch",
        ),
    ],
    ids=["unsupported-version", "invalid-repo-identity", "reference-mismatch"],
)
def test_each_violation_class_produces_distinct_dlq_reason(
    payload_builder, expected_reason_substring: str
) -> None:
    """Each violation class produces a distinct DLQ reason string."""
    message = NormalizedProviderEvent.model_validate(payload_builder())
    with pytest.raises(NormalizedEventValidationError) as exc_info:
        validate_normalized_event(message)
    assert expected_reason_substring in exc_info.value.reason


# ── Validation: consumer path integration ───────────────────────────────────


@pytest.mark.asyncio
async def test_nested_v1_event_passes_validation_and_persists() -> None:
    """A valid nested v1 event passes validation and persists — never DLQ'd."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(_nested_v1_payload()))

    consumer._producer.send_and_wait.assert_not_called()
    consumer._consumer.commit.assert_called_once()

    event_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO engineering_events" in c.args[0]
    )
    assert event_call.args[3] == "change_request"  # entity_type
    assert event_call.args[4] == "200"  # external_id
    assert event_call.args[5] == "change_request.merged"  # event_type


@pytest.mark.asyncio
async def test_unsupported_schema_version_routes_to_dlq() -> None:
    """An unsupported schema version is DLQ'd with a distinct reason."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(_mk_msg(_nested_v1_payload(schema_version="2.0")))

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Unsupported schema version" in dlq_payload["reason"]
    assert "2.0" in dlq_payload["reason"]


@pytest.mark.asyncio
async def test_invalid_repository_identity_routes_to_dlq() -> None:
    """An invalid repository identity is DLQ'd with a distinct reason."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(
        _mk_msg(
            _nested_v1_payload(
                resource={
                    "resource_type": "pull_request",
                    "resource_id": "200",
                    "repository": "ftp://github.com/owner/repo",
                    "action": "merged",
                }
            )
        )
    )

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Invalid repository identity" in dlq_payload["reason"]


@pytest.mark.asyncio
async def test_reference_mismatch_routes_to_dlq() -> None:
    """A reference mismatch is DLQ'd with a distinct reason."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    await consumer._process_message(
        _mk_msg(
            _nested_v1_payload(
                redacted_payload={
                    "reference": "ref-200",
                    "provider": "gitlab",
                    "delivery_id": "33333333-4444-5555-6666-777777777777",
                }
            )
        )
    )

    conn.execute.assert_not_called()
    consumer._producer.send_and_wait.assert_called_once()
    consumer._consumer.commit.assert_called_once()

    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Reference mismatch" in dlq_payload["reason"]


# ── Contract pinning: fixtures with source provenance ───────────────────────


def test_pinned_schema_has_source_provenance() -> None:
    """The pinned schema file exists and is valid JSON Schema."""
    assert _SCHEMA_PATH.is_file(), f"Schema not found at {_SCHEMA_PATH}"
    schema = _load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Normalized Provider Event v1"


def test_pinned_fixtures_cover_all_allowed_pairs() -> None:
    """Every allowed v1 (resource_type, action) pair has a pinned fixture."""
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        filename = _expected_fixture_filename(resource_type, action)
        assert (_FIXTURES_DIR / filename).is_file(), f"Missing fixture: {filename}"


def test_every_pinned_fixture_passes_validation() -> None:
    """Every pinned valid v1 fixture passes validate_normalized_event and is
    not classified as poison."""
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
        message = NormalizedProviderEvent.model_validate(fixture)
        # Must not raise.
        validate_normalized_event(message)


def test_every_pinned_fixture_maps_without_dlq() -> None:
    """Every pinned valid v1 fixture maps through map_normalized_event without
    returning None (would be DLQ'd)."""
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
        message = NormalizedProviderEvent.model_validate(fixture)
        mapped = map_normalized_event(message)
        assert mapped is not None, (
            f"Fixture {resource_type}.{action} returned None from "
            f"map_normalized_event — would be DLQ'd"
        )


# ── Validation: distinct DLQ reasons across all violation classes ───────────


def test_all_violation_classes_have_distinct_reason_prefixes() -> None:
    """Every violation class produces a reason with a distinct prefix so
    operators can triage without inspecting the payload."""
    reasons: set[str] = set()

    # Unsupported version.
    msg_v = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(schema_version="2.0")
    )
    with pytest.raises(NormalizedEventValidationError) as exc:
        validate_normalized_event(msg_v)
    reasons.add(exc.value.reason.split(":")[0])

    # Invalid repository identity.
    msg_r = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            resource={
                "resource_type": "pull_request",
                "resource_id": "200",
                "repository": "ftp://github.com/owner/repo",
                "action": "merged",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError) as exc:
        validate_normalized_event(msg_r)
    reasons.add(exc.value.reason.split(":")[0])

    # Reference mismatch (provider).
    msg_p = NormalizedProviderEvent.model_validate(
        _nested_v1_payload(
            redacted_payload={
                "reference": "ref-200",
                "provider": "gitlab",
                "delivery_id": "33333333-4444-5555-6666-777777777777",
            }
        )
    )
    with pytest.raises(NormalizedEventValidationError) as exc:
        validate_normalized_event(msg_p)
    reasons.add(exc.value.reason.split(":")[0])

    # All three violation classes produce distinct reason prefixes.
    assert len(reasons) == 3, (
        f"Expected 3 distinct reason prefixes, got {len(reasons)}: {reasons}"
    )
