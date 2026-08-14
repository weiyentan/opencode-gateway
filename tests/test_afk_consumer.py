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
    _MAPPED_EVENT_TYPES,
    AFKOutcomeConsumer,
    ProviderEventMessage,
    _build_adapter,
    map_provider_event,
)

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
        "provider": "github",
        "delivery_id": DELIVERY_ID,
        "type": "change_request.merged",
        "repository": "owner/repo",
        "number": 442,
        "occurred_at": "2026-08-01T10:30:00Z",
        "actor": "carol",
        "payload": {"merge_commit_sha": "abc123"},
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
    )


# ── Message-type → canonical-event mapping ───────────────────────────────────


@pytest.mark.parametrize(
    ("event_type", "expected_entity_type"),
    [
        ("issue.opened", EntityType.ISSUE),
        ("issue.closed", EntityType.ISSUE),
        ("change_request.opened", EntityType.CHANGE_REQUEST),
        ("change_request.review_requested", EntityType.CHANGE_REQUEST),
        ("change_request.changes_requested", EntityType.CHANGE_REQUEST),
        ("change_request.approved", EntityType.CHANGE_REQUEST),
        ("change_request.merged", EntityType.CHANGE_REQUEST),
        ("change_request.closed", EntityType.CHANGE_REQUEST),
        ("pipeline.failed", EntityType.CHANGE_REQUEST),
        ("pipeline.succeeded", EntityType.CHANGE_REQUEST),
    ],
)
def test_map_each_event_type_to_canonical_event(
    event_type: str, expected_entity_type: EntityType
) -> None:
    message = ProviderEventMessage(
        provider=Provider.GITHUB,
        delivery_id=DELIVERY_ID,
        type=event_type,
        repository="owner/repo",
        number=442,
        occurred_at=datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC),
        actor="carol",
    )
    mapped = map_provider_event(message)

    assert mapped is not None
    entity, event = mapped

    assert event.event_type == event_type
    assert event.provider is Provider.GITHUB
    assert event.occurred_at == datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)
    assert event.actor == "carol"

    assert entity.entity_type is expected_entity_type
    assert entity.entity_id == f"{expected_entity_type.value}:442"
    assert event.entity_id == entity.entity_id
    assert entity.repository == "owner/repo"
    assert entity.number == 442


def test_mapping_derives_entity_id_from_number_and_type() -> None:
    message = ProviderEventMessage(
        provider=Provider.GITLAB,
        delivery_id=DELIVERY_ID,
        type="issue.closed",
        repository="group/project",
        number=99,
        occurred_at=datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC),
    )
    mapped = map_provider_event(message)
    assert mapped is not None
    entity, event = mapped

    assert entity.entity_id == "issue:99"
    assert entity.entity_type is EntityType.ISSUE
    assert event.entity_id == "issue:99"
    assert event.event_id == "issue:99:closed"
    assert event.provider is Provider.GITLAB


def test_unmappable_type_returns_none() -> None:
    message = ProviderEventMessage(
        provider=Provider.GITHUB,
        delivery_id=DELIVERY_ID,
        type="pull_request.assigned",  # not in the locked vocabulary
        repository="owner/repo",
        number=442,
        occurred_at=datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC),
    )
    assert map_provider_event(message) is None


def test_mapped_event_types_is_the_locked_ten() -> None:
    assert _MAPPED_EVENT_TYPES == {
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


def test_provider_event_message_rejects_unknown_provider() -> None:
    with pytest.raises(Exception):
        ProviderEventMessage.model_validate(
            {
                "provider": "bitbucket",
                "delivery_id": DELIVERY_ID,
                "type": "issue.opened",
                "repository": "owner/repo",
                "number": 1,
                "occurred_at": "2026-08-01T10:30:00Z",
            }
        )


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

    msg = _mk_msg(_valid_payload(type="pull_request.assigned"))
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
        lambda: _mk_msg(_valid_payload(type="pull_request.assigned")),
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
