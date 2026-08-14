"""Integration tests for the AFK outcome backfill CLI (issue #449).

Runs ``scripts.afk_backfill.run_backfill`` against the docker-compose
Postgres (port 5433) with a fake GitHub API client, and verifies:

* a real backfill run over a window, followed by a re-run of the same
  window, converges with no duplicate rows (deterministic session-keyed
  run ids + entity-mapping uniqueness + conflict-ignored events);
* the dry-run report writes nothing and its counts match the resolution a
  real write stores for the same window (``afk_run_entities`` buckets).

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(defaulting to the ``docker-compose.test.yml`` service).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from afk_outcomes.providers.github import GitHubAdapter
from scripts.afk_backfill import BackfillReport, run_backfill

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
REPOSITORY = "weiyentan/opencode-gateway"
SINCE = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 8, 1, 23, 59, 59, tzinfo=UTC)
RUN_START = datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC)
RUN_FINISH = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)


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


# ── Fixture: GitHub REST-shaped payloads over the locked vocabulary ─────────


def _payloads() -> dict:
    """One owning CR, one commit-referenced issue, one temporal-only issue."""
    return {
        "repository": REPOSITORY,
        "issues": [
            {
                "number": 301,
                "title": "Fix caching bug",
                "state": "open",
                "user": {"login": "alice"},
                "created_at": "2026-08-01T09:00:00Z",
                "updated_at": "2026-08-01T10:00:00Z",
                "html_url": f"https://github.com/{REPOSITORY}/issues/301",
            },
            {
                "number": 302,
                "title": "Unrelated refactor",
                "state": "open",
                "user": {"login": "bob"},
                "created_at": "2026-08-01T08:30:00Z",
                "updated_at": "2026-08-01T09:00:00Z",
                "html_url": f"https://github.com/{REPOSITORY}/issues/302",
            },
        ],
        "pulls": [
            {
                "number": 300,
                "title": "Fix caching bug",
                "state": "closed",
                "merged": True,
                "user": {"login": "alice"},
                "created_at": "2026-08-01T08:00:00Z",
                "updated_at": "2026-08-01T10:00:00Z",
                "closed_at": "2026-08-01T10:30:00Z",
                "merged_at": "2026-08-01T10:30:00Z",
                "merge_commit_sha": "merge-300",
                "merged_by": {"login": "carol"},
                "head": {"ref": "feature/caching", "sha": "sha300"},
                "base": {"ref": "main"},
                "requested_reviewers": [],
                "html_url": f"https://github.com/{REPOSITORY}/pulls/300",
            },
        ],
        "reviews": {
            "300": [
                {
                    "id": 5001,
                    "user": {"login": "bob"},
                    "state": "APPROVED",
                    "submitted_at": "2026-08-01T09:30:00Z",
                    "commit_id": "c1",
                    "html_url": f"https://github.com/{REPOSITORY}/pull/300#review-5001",
                }
            ]
        },
        "commits": {
            "300": [
                {
                    "sha": "c1",
                    "commit": {
                        "message": "fix #301 caching bug",
                        "author": {"name": "alice", "date": "2026-08-01T09:00:00Z"},
                    },
                    "html_url": f"https://github.com/{REPOSITORY}/commit/c1",
                }
            ]
        },
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


# ── Seeding and counting helpers ────────────────────────────────────────────


async def _seed_session(conn: asyncpg.Connection, *, title: str) -> None:
    """Insert one session (plus client/credential/source-database/context)."""
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
        RUN_START,
        RUN_FINISH,
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


_TABLE_COUNTS_SQL = """
    SELECT
        (SELECT COUNT(*) FROM afk_runs) AS afk_runs,
        (SELECT COUNT(*) FROM afk_run_entities) AS afk_run_entities,
        (SELECT COUNT(*) FROM afk_run_sessions) AS afk_run_sessions,
        (SELECT COUNT(*) FROM engineering_events) AS engineering_events,
        (SELECT COUNT(*) FROM delivery_log) AS delivery_log,
        (SELECT COUNT(*) FROM unresolved_correlations) AS unresolved_correlations
"""


async def _table_counts(conn: asyncpg.Connection) -> dict[str, int]:
    return dict(await conn.fetchrow(_TABLE_COUNTS_SQL))


async def _stored_match_buckets(conn: asyncpg.Connection) -> tuple[int, int, int]:
    """(explicit, high, inferred) counts as the stored entity links would say.

    Mirrors the report's bucket rule: explicit = ``explicit_run_id`` method;
    high = confidence >= 0.5; inferred = 0 < confidence < 0.5.  Noise links
    (confidence 0) are excluded.
    """
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE correlation_confidence > 0
                  AND correlation_method = 'explicit_run_id') AS explicit,
            COUNT(*) FILTER (
                WHERE correlation_confidence >= 0.5
                  AND correlation_method <> 'explicit_run_id') AS high,
            COUNT(*) FILTER (
                WHERE correlation_confidence > 0
                  AND correlation_confidence < 0.5) AS inferred
        FROM afk_run_entities
        """
    )
    return (row["explicit"], row["high"], row["inferred"])


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_backfill_rerun_converges_and_dry_run_matches_stored(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        await _seed_session(conn, title="Fix caching bug")
        adapter = GitHubAdapter(FakeGitHubApi(_payloads()))

        # Dry-run: full report, nothing persisted.
        dry = await run_backfill(
            conn,
            adapter=adapter,
            repository=REPOSITORY,
            since=SINCE,
            until=UNTIL,
            dry_run=True,
        )
        assert dry.dry_run is True
        assert dry.sessions_considered == 1
        assert dry.change_requests_scanned == 1
        assert dry.issues_scanned == 2
        assert (dry.explicit_matches, dry.high_matches, dry.inferred_matches) == (
            0,
            2,
            1,
        )
        assert (dry.ambiguous, dry.unmatched) == (0, 0)
        assert await _table_counts(conn) == {
            "afk_runs": 0,
            "afk_run_entities": 0,
            "afk_run_sessions": 0,
            "engineering_events": 0,
            "delivery_log": 0,
            "unresolved_correlations": 0,
        }

        # Real run: same resolution, rows written once.
        real = await run_backfill(
            conn,
            adapter=adapter,
            repository=REPOSITORY,
            since=SINCE,
            until=UNTIL,
        )
        for name in (
            "change_requests_scanned",
            "issues_scanned",
            "sessions_considered",
            "explicit_matches",
            "high_matches",
            "inferred_matches",
            "ambiguous",
            "unmatched",
        ):
            assert getattr(real, name) == getattr(dry, name), name
        after_first = await _table_counts(conn)
        assert after_first["afk_runs"] == 1
        assert after_first["afk_run_entities"] == 4  # CR + 2 issues + noise commit
        assert after_first["delivery_log"] == 1

        # Re-run of the same window converges — no duplicate rows anywhere.
        rerun = await run_backfill(
            conn,
            adapter=adapter,
            repository=REPOSITORY,
            since=SINCE,
            until=UNTIL,
        )
        for name in (
            "explicit_matches",
            "high_matches",
            "inferred_matches",
            "ambiguous",
            "unmatched",
        ):
            assert getattr(rerun, name) == getattr(dry, name), name
        assert await _table_counts(conn) == after_first

        # Dry-run counts are consistent with what the stored data says.
        assert await _stored_match_buckets(conn) == (
            real.explicit_matches,
            real.high_matches,
            real.inferred_matches,
        )


@pytest.mark.integration
async def test_bounded_window_reconciliation_on_second_run(db_pool: asyncpg.Pool) -> None:
    """A later bounded re-run of the same window converges with the first."""
    async with db_pool.acquire() as conn:
        await _seed_session(conn, title="Fix caching bug")
        adapter = GitHubAdapter(FakeGitHubApi(_payloads()))

        first = await run_backfill(
            conn,
            adapter=adapter,
            repository=REPOSITORY,
            since=SINCE,
            until=UNTIL,
        )
        assert first.high_matches == 2

        # Re-apply the pull -> correlate -> persist path over the same bounds.
        report: BackfillReport = await run_backfill(
            conn,
            adapter=adapter,
            repository=REPOSITORY,
            since=SINCE,
            until=UNTIL,
            dry_run=True,
        )
        counts = await _table_counts(conn)
        assert report.sessions_considered == 1
        assert report.high_matches == 2
        assert counts["afk_runs"] == 1  # still exactly one run row
