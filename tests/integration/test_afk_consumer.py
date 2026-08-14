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
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from app.consumer.afk_consumer import AFKOutcomeConsumer

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
REPOSITORY = "weiyentan/opencode-gateway"


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
