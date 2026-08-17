"""Integration tests for the live AFK outcome consumer (issue #451).

Runs against the docker-compose Postgres (port 5433) and verifies the
database-enforced guarantees of the consumer's write path:

* ``record_event`` redelivery is absorbed by ``delivery_log`` UNIQUE and the
  ``engineering_events`` identity UNIQUE (no duplicate rows);
* a scheduled reconciliation window (``AFKOutcomeConsumer._reconcile_once``)
  reusing the backfill engine converges a merged terminal outcome for a run
  even when no merge event was delivered on the topic.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from afk_outcomes.models import (
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)
from afk_outcomes.providers.github import GitHubAdapter
from afk_outcomes.repository import AsyncpgOutcomeRepository
from app.consumer.afk_consumer import (
    AFKOutcomeConsumer,
    NormalizedProviderEvent,
    map_normalized_event,
    map_provider_event,
)

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
REPOSITORY = "weiyentan/opencode-gateway"
NORMALIZED_REPOSITORY = "github.com/weiyentan/opencode-gateway"


def _dsn() -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


async def _can_connect() -> bool:
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=_dsn(), timeout=5), timeout=10.0
        )
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def _integration_db_available() -> bool:
    if not asyncio.run(_can_connect()):
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


@pytest.fixture(scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    try:
        alembic.command.upgrade(alembic_cfg, "head")
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        alembic.command.upgrade(alembic_cfg, "head")

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        )
    await pool.close()


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _merged_payloads(now: datetime) -> dict:
    """One merged pull (no merge event delivered on the topic), no issues."""
    return {
        "repository": REPOSITORY,
        "issues": [],
        "pulls": [
            {
                "number": 300,
                "title": "Fix caching bug",
                "state": "closed",
                "merged": True,
                "user": {"login": "alice"},
                "created_at": _iso(now - timedelta(days=1)),
                "updated_at": _iso(now - timedelta(hours=12)),
                "closed_at": _iso(now - timedelta(hours=12)),
                "merged_at": _iso(now - timedelta(hours=12)),
                "merge_commit_sha": "merge-300",
                "merged_by": {"login": "carol"},
                "head": {"ref": "feature/caching", "sha": "sha300"},
                "base": {"ref": "main"},
                "requested_reviewers": [],
                "html_url": f"https://github.com/{REPOSITORY}/pulls/300",
            }
        ],
        "reviews": {},
        "commits": {},
        "check_runs": {"sha300": {"check_runs": []}},
    }


class FakeGitHubApi:
    """Serves GitHub REST-shaped fixture payloads by path."""

    def __init__(self, payloads: dict) -> None:
        self._payloads = payloads

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        repo = self._payloads["repository"]
        if path == f"/repos/{repo}/issues":
            return self._payloads["issues"]
        if path == f"/repos/{repo}/pulls":
            return self._payloads["pulls"]
        match = re.fullmatch(rf"/repos/{repo}/pulls/(\d+)/reviews", path)
        if match:
            return self._payloads["reviews"].get(match.group(1), [])
        match = re.fullmatch(rf"/repos/{repo}/pulls/(\d+)/commits", path)
        if match:
            return self._payloads["commits"].get(match.group(1), [])
        match = re.fullmatch(rf"/repos/{repo}/commits/([^/]+)/check-runs", path)
        if match:
            return self._payloads["check_runs"].get(match.group(1), {"check_runs": []})
        raise AssertionError(f"unexpected GitHub API path: {path}")


async def _seed_session(
    conn: asyncpg.Connection, *, title: str, now: datetime
) -> None:
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"cli-{uuid.uuid4()}",
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        "dummy-token-hash",
        "pref",
    )
    database_id = await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id)"
        " VALUES ($1, $2) RETURNING id",
        credential_id,
        client_id,
    )
    external_session_id = f"ses_{uuid.uuid4()}"
    session_id = await conn.fetchval(
        "INSERT INTO sessions"
        " (client_id, source_database_id, external_session_id,"
        "  first_message_at, last_message_at)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        client_id,
        database_id,
        external_session_id,
        now - timedelta(days=1),
        now - timedelta(hours=12),
    )
    await conn.execute(
        "INSERT INTO opencode_session_contexts"
        " (client_id, source_database_id, external_session_id, session_id, title)"
        " VALUES ($1, $2, $3, $4, $5)",
        client_id,
        database_id,
        external_session_id,
        session_id,
        title,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normalized_bridge_persists_canonical_change_request(db_pool: asyncpg.Pool) -> None:
    """A normalized ``pull_request.merged`` bridges to ``change_request.merged``.

    The Stage-2 mapping bridge maps the producer's ``pull_request``
    ``resource_type`` into the outcome layer's canonical ``change_request``
    vocabulary, and the resulting event persists through ``record_event``
    with the canonical ``change_request`` entity type — never the
    producer-specific resource type.
    """
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    merged_at = now - timedelta(hours=12)

    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "provider": "github",
            "delivery_id": "delivery-normalized-bridge",
            "resource_type": "pull_request",
            "resource_id": "441",
            "repository": REPOSITORY,
            "action": "merged",
            "occurred_at": _iso(merged_at),
            "ingested_at": _iso(now),
            "actor": "carol",
            "payload_ref": "redacted-payload-ref-441",
        }
    )
    mapped = map_normalized_event(message)
    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert event.event_type == "change_request.merged"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-normalized-bridge",
            entity=entity,
            event=event,
        )

        entity_type = await conn.fetchval(
            "SELECT entity_type FROM engineering_events "
            "WHERE event_type = 'change_request.merged' AND external_id = '441'"
        )
        assert entity_type == "change_request", (
            f"expected canonical change_request entity_type, got {entity_type!r}"
        )
        payload_ref = await conn.fetchval(
            "SELECT payload->>'payload_ref' FROM engineering_events "
            "WHERE event_type = 'change_request.merged' AND external_id = '441'"
        )
        assert payload_ref == "redacted-payload-ref-441"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_event_redelivery_produces_no_duplicate_rows(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        entity = EngineeringEntity(
            entity_id="change_request:442",
            entity_type=EntityType.CHANGE_REQUEST,
            provider=Provider.GITHUB,
            repository=REPOSITORY,
            number=442,
        )
        event = EngineeringEvent(
            event_id="change_request:442:merged",
            event_type="change_request.merged",
            provider=Provider.GITHUB,
            entity_id="change_request:442",
            occurred_at=datetime(2026, 8, 13, 10, 30, 0, tzinfo=UTC),
            actor="carol",
            payload={},
        )

        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-uuid-123",
            entity=entity,
            event=event,
        )
        await repo.record_event(  # redelivery of the same delivery UUID
            provider=Provider.GITHUB,
            delivery_id="delivery-uuid-123",
            entity=entity,
            event=event,
        )

        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = 'delivery-uuid-123'"
        )
        assert delivery_count == 1, f"expected 1 delivery-log row, got {delivery_count}"

        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events WHERE event_type = 'change_request.merged'"
        )
        assert event_count == 1, f"expected 1 event after redelivery, got {event_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_window_converges_merged_outcome(db_pool: asyncpg.Pool) -> None:
    """A merged outcome appears for a run even with no merge event on the topic."""
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+

    async with db_pool.acquire() as conn:
        await _seed_session(conn, title="Fix caching bug", now=now)

        # No topic-delivered events exist yet.
        assert await conn.fetchval("SELECT COUNT(*) FROM afk_runs") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM engineering_events") == 0

    consumer = AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=db_pool,  # type: ignore[arg-type]
        provider=Provider.GITHUB,
        repository=REPOSITORY,
        adapter=GitHubAdapter(FakeGitHubApi(_merged_payloads(now))),
        reconcile_window_seconds=30 * 86400,
    )
    await consumer._reconcile_once()

    async with db_pool.acquire() as conn:
        outcome_status = await conn.fetchval(
            "SELECT outcome_status FROM afk_runs ORDER BY first_seen_at LIMIT 1"
        )
        assert outcome_status == "merged", f"expected merged outcome, got {outcome_status}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_path_event_dedup_when_occurred_at_agrees(db_pool: asyncpg.Pool) -> None:
    """A live-consumed and backfill-fetched event for the same logical event dedup.

    The ``engineering_events`` identity key is
    ``(provider, repository, entity_type, external_id, event_type, occurred_at)``.
    When a live topic delivery (producer-forwarded ``occurred_at``) and a
    backfill fetch (adapter-derived ``occurred_at``) agree on the timestamp for
    the same logical merge event, the second write is a conflict-ignored no-op.
    """
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    merged_at = now - timedelta(hours=12)

    # Live path: a producer-forwarded merge event for change_request:300.
    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-cross-path-live",
            "resource": {
                "type": "pull_request",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 300,
            },
            "action": "merged",
            "occurred_at": _iso(merged_at),
            "ingested_at": _iso(now),
            "actor": "carol",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-cross-path-live",
                }
            },
        }
    )
    mapped = map_provider_event(message)
    assert mapped is not None
    entity, event = mapped

    async with db_pool.acquire() as conn:
        await _seed_session(conn, title="Fix caching bug", now=now)
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-cross-path-live",
            entity=entity,
            event=event,
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM engineering_events "
                "WHERE event_type = 'change_request.merged'"
            )
            == 1
        )

    # Backfill path: the adapter derives the same merge event with the same
    # occurred_at (the pull's merged_at equals the live delivery timestamp).
    payloads = _merged_payloads(now)
    payloads["pulls"][0]["merged_at"] = _iso(merged_at)
    consumer = AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=db_pool,  # type: ignore[arg-type]
        provider=Provider.GITHUB,
        repository=NORMALIZED_REPOSITORY,
        adapter=GitHubAdapter(FakeGitHubApi(payloads)),
        reconcile_window_seconds=30 * 86400,
    )
    await consumer._reconcile_once()

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events "
            "WHERE event_type = 'change_request.merged'"
        )
        assert count == 1, f"expected cross-path dedup to keep 1 row, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_path_event_distinct_when_occurred_at_differs(db_pool: asyncpg.Pool) -> None:
    """A timestamp mismatch between live and backfill yields distinct events.

    Documented behavior (finding B): because ``occurred_at`` is part of the
    ``engineering_events`` identity, a live event (producer-forwarded
    timestamp) and a backfill event (adapter-derived timestamp) for the same
    logical merge event are treated as *distinct* when their timestamps differ
    — the dedup key does not collapse them.
    """
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    live_at = now - timedelta(hours=12)
    backfill_at = now - timedelta(hours=12, seconds=5)  # 5 seconds later

    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-cross-path-live",
            "resource": {
                "type": "pull_request",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 300,
            },
            "action": "merged",
            "occurred_at": _iso(live_at),
            "ingested_at": _iso(now),
            "actor": "carol",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-cross-path-live",
                }
            },
        }
    )
    mapped = map_provider_event(message)
    assert mapped is not None
    entity, event = mapped

    async with db_pool.acquire() as conn:
        await _seed_session(conn, title="Fix caching bug", now=now)
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-cross-path-live",
            entity=entity,
            event=event,
        )

    payloads = _merged_payloads(now)
    payloads["pulls"][0]["merged_at"] = _iso(backfill_at)
    consumer = AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=db_pool,  # type: ignore[arg-type]
        provider=Provider.GITHUB,
        repository=NORMALIZED_REPOSITORY,
        adapter=GitHubAdapter(FakeGitHubApi(payloads)),
        reconcile_window_seconds=30 * 86400,
    )
    await consumer._reconcile_once()

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events "
            "WHERE event_type = 'change_request.merged'"
        )
        assert count == 2, f"expected distinct events for differing timestamps, got {count}"


# ── Replay convergence / redelivery through the full consumer path (#485) ────
#
# These tests drive the live consumer's ``_process_message`` (transport record
# → parse → map → transactional write → offset commit) against the real
# Postgres, asserting that re-delivering the same producer event converges on
# identical delivery/event rows regardless of order (live-then-replay and
# replay-then-live), with no duplicate rows.


def _kafka_record(value: dict[str, object], *, offset: int = 0, partition: int = 0) -> MagicMock:
    """A realistic Kafka ``ConsumerRecord`` stand-in (mirrors the unit-test shape)."""
    msg = MagicMock()
    msg.value = json.dumps(value).encode("utf-8")
    msg.offset = offset
    msg.partition = partition
    msg.topic = "afk.events"
    msg.key = None
    msg.headers = ()
    return msg


def _consumer_over_pool(db_pool: asyncpg.Pool) -> AFKOutcomeConsumer:
    """A consumer wired to the real pool with mocked Kafka commit/produce seams."""
    consumer = AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=db_pool,
        provider=Provider.GITHUB,
        repository=REPOSITORY,
        adapter=GitHubAdapter(
            FakeGitHubApi(_merged_payloads(datetime.now(timezone.utc)))  # noqa: UP017
        ),
    )
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    return consumer


def _normalized_contract_event(delivery_id: str, resource_id: str) -> dict[str, object]:
    """A producer-contract-conforming nested ``pull_request.merged`` event."""
    return {
        "schema_version": "1.0",
        "event_type": "normalized",
        "provider": "github",
        "delivery_id": delivery_id,
        "resource": {
            "type": "pull_request",
            "repository_url": f"https://github.com/{REPOSITORY}",
            "number": int(resource_id),
        },
        "action": "merged",
        "occurred_at": "2026-08-13T10:10:29Z",
        "ingested_at": "2026-08-13T10:10:30Z",
        "actor": "carol",
        "redacted_payload": {
            "reference": {"provider": "github", "delivery_id": delivery_id}
        },
    }


def _converged_event(delivery_id: str, resource_id: str) -> tuple[str, str, str, str, str]:
    """The canonical engineering_events row every ordering must converge on."""
    return (
        "change_request",
        resource_id,
        "change_request.merged",
        "carol",
        delivery_id,
    )


async def _event_snapshot(
    conn: asyncpg.Connection, external_id: str
) -> tuple[str, str, str, str, str] | None:
    """The canonical (non-volatile) event row for ``external_id``, or None."""
    row = await conn.fetchrow(
        "SELECT entity_type, external_id, event_type, actor, "
        "payload->'payload_ref'->>'delivery_id' AS payload_ref "
        "FROM engineering_events WHERE external_id = $1",
        external_id,
    )
    if row is None:
        return None
    return (
        row["entity_type"],
        row["external_id"],
        row["event_type"],
        row["actor"],
        row["payload_ref"],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_convergence_live_then_replay(db_pool: asyncpg.Pool) -> None:
    """Live delivery then a replay of the same event converge on identical rows.

    The full consumer path is driven twice with the same producer contract
    event; the replay must leave ``delivery_log`` and ``engineering_events``
    byte-identical to the post-live state (one delivery row + one event row).
    """
    delivery_id = "delivery-live-then-replay"
    resource_id = "900"
    event = _normalized_contract_event(delivery_id, resource_id)
    consumer = _consumer_over_pool(db_pool)

    await consumer._process_message(_kafka_record(event))  # live
    async with db_pool.acquire() as conn:
        after_live = await _event_snapshot(conn, resource_id)
        delivery_after_live = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = $1", delivery_id
        )
    assert after_live == _converged_event(delivery_id, resource_id)
    assert delivery_after_live == 1

    await consumer._process_message(_kafka_record(event))  # replay
    async with db_pool.acquire() as conn:
        after_replay = await _event_snapshot(conn, resource_id)
        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = $1", delivery_id
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events WHERE external_id = $1", resource_id
        )

    # Convergence: replay leaves the rows identical to the live state.
    assert after_replay == after_live == _converged_event(delivery_id, resource_id)
    assert delivery_count == 1
    assert event_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_convergence_replay_then_live(db_pool: asyncpg.Pool) -> None:
    """A duplicate delivery arriving before the "live" copy still converges.

    Whether the replay precedes or follows the live delivery, the final
    delivery/event rows are identical (one delivery row + one event row).
    """
    delivery_id = "delivery-replay-then-live"
    resource_id = "901"
    event = _normalized_contract_event(delivery_id, resource_id)
    consumer = _consumer_over_pool(db_pool)

    await consumer._process_message(_kafka_record(event))  # replay arrives first
    async with db_pool.acquire() as conn:
        after_replay = await _event_snapshot(conn, resource_id)
    assert after_replay == _converged_event(delivery_id, resource_id)

    await consumer._process_message(_kafka_record(event))  # then the live copy
    async with db_pool.acquire() as conn:
        after_live = await _event_snapshot(conn, resource_id)
        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = $1", delivery_id
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events WHERE external_id = $1", resource_id
        )

    assert after_live == after_replay == _converged_event(delivery_id, resource_id)
    assert delivery_count == 1
    assert event_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redelivery_creates_no_duplicate_rows_across_full_path(
    db_pool: asyncpg.Pool,
) -> None:
    """Two deliveries of the same provider + delivery_id create no duplicate rows.

    Dedup is enforced end to end: the ``delivery_log`` UNIQUE(provider,
    delivery_id) constraint absorbs the second delivery row, and the
    ``engineering_events`` identity UNIQUE absorbs the second event row.
    """
    delivery_id = "delivery-dedup-full-path"
    resource_id = "902"
    event = _normalized_contract_event(delivery_id, resource_id)
    consumer = _consumer_over_pool(db_pool)

    await consumer._process_message(_kafka_record(event, offset=10))
    await consumer._process_message(_kafka_record(event, offset=10))  # redelivery

    async with db_pool.acquire() as conn:
        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = $1", delivery_id
        )
        delivery_provider = await conn.fetchval(
            "SELECT provider FROM delivery_log WHERE delivery_id = $1", delivery_id
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events WHERE external_id = $1", resource_id
        )

    assert delivery_count == 1
    assert delivery_provider == "github"
    assert event_count == 1


# ══════════════════════════════════════════════════════════════════════════
#  Consumer: map and persist every valid normalized lifecycle event (#496)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normalized_edited_persists_as_canonical_updated(db_pool: asyncpg.Pool) -> None:
    """A normalized ``pull_request.edited`` bridges to ``change_request.updated``
    with source provenance retained."""
    now = datetime.now(timezone.utc)  # noqa: UP017
    edited_at = now - timedelta(hours=6)

    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-normalized-edited",
            "resource": {
                "type": "pull_request",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 500,
            },
            "action": "edited",
            "occurred_at": _iso(edited_at),
            "ingested_at": _iso(now),
            "actor": "alice",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-normalized-edited",
                }
            },
        }
    )
    mapped = map_normalized_event(message)
    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert event.event_type == "change_request.updated"
    assert event.payload.get("source_resource_type") == "pull_request"
    assert event.payload.get("source_action") == "edited"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-normalized-edited",
            entity=entity,
            event=event,
        )

        row = await conn.fetchrow(
            "SELECT entity_type, event_type, external_id, payload "
            "FROM engineering_events "
            "WHERE event_type = 'change_request.updated' AND external_id = '500'"
        )
        assert row is not None
        assert row["entity_type"] == "change_request"
        assert row["event_type"] == "change_request.updated"
        assert row["external_id"] == "500"
        payload = row["payload"]
        assert payload.get("source_resource_type") == "pull_request"
        assert payload.get("source_action") == "edited"
        assert payload.get("payload_ref") == {
            "provider": "github",
            "delivery_id": "delivery-normalized-edited",
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normalized_reopened_persists_as_canonical_reopened(db_pool: asyncpg.Pool) -> None:
    """A normalized ``issue.reopened`` bridges to ``issue.reopened``."""
    now = datetime.now(timezone.utc)  # noqa: UP017
    reopened_at = now - timedelta(hours=3)

    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-normalized-reopened",
            "resource": {
                "type": "issue",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 501,
            },
            "action": "reopened",
            "occurred_at": _iso(reopened_at),
            "ingested_at": _iso(now),
            "actor": "bob",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-normalized-reopened",
                }
            },
        }
    )
    mapped = map_normalized_event(message)
    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.ISSUE
    assert event.event_type == "issue.reopened"
    assert event.payload.get("source_resource_type") == "issue"
    assert event.payload.get("source_action") == "reopened"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-normalized-reopened",
            entity=entity,
            event=event,
        )

        row = await conn.fetchrow(
            "SELECT entity_type, event_type, external_id "
            "FROM engineering_events "
            "WHERE event_type = 'issue.reopened' AND external_id = '501'"
        )
        assert row is not None
        assert row["entity_type"] == "issue"
        assert row["event_type"] == "issue.reopened"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normalized_edited_redelivery_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """Redelivery of a normalized ``pull_request.edited`` does not duplicate
    engineering events."""
    now = datetime.now(timezone.utc)  # noqa: UP017
    updated_at = now - timedelta(hours=4)

    message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-updated-redelivery",
            "resource": {
                "type": "pull_request",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 502,
            },
            "action": "edited",
            "occurred_at": _iso(updated_at),
            "ingested_at": _iso(now),
            "actor": "carol",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-updated-redelivery",
                }
            },
        }
    )
    mapped = map_normalized_event(message)
    assert mapped is not None
    entity, event = mapped

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        # First delivery.
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-updated-redelivery",
            entity=entity,
            event=event,
        )
        # Redelivery — same delivery_id.
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-updated-redelivery",
            entity=entity,
            event=event,
        )

        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log "
            "WHERE delivery_id = 'delivery-updated-redelivery'"
        )
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events "
            "WHERE event_type = 'change_request.updated' AND external_id = '502'"
        )
        assert delivery_count == 1
        assert event_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_event_with_older_occurred_at_enriches_trail(db_pool: asyncpg.Pool) -> None:
    """A late event with an older ``occurred_at`` enriches the trail without
    regressing newer lifecycle state.

    The ``engineering_events`` identity key includes ``occurred_at``, so a
    late event with a different (older) timestamp is a distinct row — it
    enriches the trail rather than overwriting the newer event.
    """
    now = datetime.now(timezone.utc)  # noqa: UP017
    newer_at = now - timedelta(hours=2)
    older_at = now - timedelta(hours=6)

    # First: a newer updated event arrives.
    newer_message = NormalizedProviderEvent.model_validate(
        {
            "schema_version": "1.0",
            "event_type": "normalized",
            "provider": "github",
            "delivery_id": "delivery-late-newer",
            "resource": {
                "type": "pull_request",
                "repository_url": f"https://github.com/{REPOSITORY}",
                "number": 503,
            },
            "action": "edited",
            "occurred_at": _iso(newer_at),
            "ingested_at": _iso(now),
            "actor": "carol",
            "redacted_payload": {
                "reference": {
                    "provider": "github",
                    "delivery_id": "delivery-late-newer",
                }
            },
        }
    )
    mapped_newer = map_normalized_event(newer_message)
    assert mapped_newer is not None
    entity_newer, event_newer = mapped_newer

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-late-newer",
            entity=entity_newer,
            event=event_newer,
        )

        # Second: a late event with an older occurred_at arrives.
        older_message = NormalizedProviderEvent.model_validate(
            {
                "schema_version": "1.0",
                "event_type": "normalized",
                "provider": "github",
                "delivery_id": "delivery-late-older",
                "resource": {
                    "type": "pull_request",
                    "repository_url": f"https://github.com/{REPOSITORY}",
                    "number": 503,
                },
                "action": "edited",
                "occurred_at": _iso(older_at),
                "ingested_at": _iso(now),
                "actor": "alice",
                "redacted_payload": {
                    "reference": {
                        "provider": "github",
                        "delivery_id": "delivery-late-older",
                    }
                },
            }
        )
        mapped_older = map_normalized_event(older_message)
        assert mapped_older is not None
        entity_older, event_older = mapped_older

        await repo.record_event(
            provider=Provider.GITHUB,
            delivery_id="delivery-late-older",
            entity=entity_older,
            event=event_older,
        )

        # Both events exist — the late event enriched the trail.
        rows = await conn.fetch(
            "SELECT occurred_at, actor, "
            "payload->'payload_ref'->>'delivery_id' AS payload_ref "
            "FROM engineering_events "
            "WHERE external_id = '503' "
            "ORDER BY occurred_at"
        )
        assert len(rows) == 2
        # Older event is first.
        assert rows[0]["occurred_at"] == older_at
        assert rows[0]["actor"] == "alice"
        assert rows[0]["payload_ref"] == "delivery-late-older"
        # Newer event is second.
        assert rows[1]["occurred_at"] == newer_at
        assert rows[1]["actor"] == "carol"
        assert rows[1]["payload_ref"] == "delivery-late-newer"
