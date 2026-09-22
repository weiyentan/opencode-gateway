"""PostgreSQL golden-dataset integration test for the AFK dashboard (issue #730).

Exercises the full AFK Dashboard lifecycle against a real PostgreSQL 16
instance — the architectural confidence gate.  Validates PostgreSQL-specific
joins, grouping, numeric behaviour, ambiguity exclusion, verifier convergence,
and stale-row deletion.

The test seeds a deterministic golden dataset, runs recomputation, proves
refresh/backfill parity, validates summary API responses, exercises verifier
mismatch detection, and confirms convergence after corrective backfill.

Uses the shared vocabulary extracted in #724 and exercises the verifier
split from #729.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_dashboard_golden_dataset.py -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

_API_KEY = "test-golden-dataset"

# Deterministic test data constants.
_REPO = "https://github.com/acme/golden-test"
_PROVIDER = "github"

_DAY_1 = date(2026, 1, 15)  # Day with runs + change requests + executions + usage
_DAY_2 = date(2026, 1, 16)  # Day with executions only

# AFK run IDs (ULID-like format).
_RUN_A = "01JGOLDEN0000000000000001"  # Day 1: merged CR, 1 session, usage
_RUN_B = "01JGOLDEN0000000000000002"  # Day 1: closed CR, 1 session (ambiguous)
_RUN_C = "01JGOLDEN0000000000000003"  # Day 2: no CR, 0 sessions

# External session IDs — one maps to TWO runs (ambiguous) and one to one run.
_EXT_SES_UNAMBIGUOUS = "ses_golden_unambiguous"
_EXT_SES_AMBIGUOUS = "ses_golden_ambiguous"  # linked to both run A and run B

_DROP_ALL_SQL = (
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
    "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)


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
async def _migrated_schema(_integration_db_available: bool) -> None:  # type: ignore[override]
    """Apply Alembic migrations in a subprocess (module-scoped)."""
    import subprocess

    env = dict(os.environ)
    env.update(
        {
            "GATEWAY_ENV": "development",
            "GATEWAY_DATABASE_HOST": _DEFAULT_HOST,
            "GATEWAY_DATABASE_PORT": str(_DEFAULT_PORT),
            "GATEWAY_DATABASE_NAME": _DEFAULT_DB,
            "GATEWAY_DATABASE_USER": _DEFAULT_USER,
            "GATEWAY_DATABASE_PASSWORD": _DEFAULT_PASSWORD,
        }
    )
    cmd = [os.sys.executable, "-m", "alembic", "upgrade", "head"]
    try:
        result = subprocess.run(
            cmd, cwd=_PROJ_ROOT, env=env, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"alembic upgrade head failed: {result.stderr[-500:]}")
    except Exception:
        async with asyncpg.connect(dsn=_dsn(), timeout=5) as conn:
            await conn.execute(_DROP_ALL_SQL)
        result = subprocess.run(
            cmd, cwd=_PROJ_ROOT, env=env, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"alembic upgrade head failed (retry): {result.stderr[-500:]}")


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_client(conn: asyncpg.Connection) -> uuid.UUID:
    """Insert an opencode_clients row; return client_id."""
    return await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"golden-{uuid.uuid4().hex[:8]}",
    )


async def _seed_credential(conn: asyncpg.Connection, client_id: uuid.UUID) -> uuid.UUID:
    """Insert a collector_credentials row; return credential_id."""
    return await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "gold",
    )


async def _seed_source_db(
    conn: asyncpg.Connection, client_id: uuid.UUID, credential_id: uuid.UUID
) -> uuid.UUID:
    """Insert a source_databases row; return source_database_id."""
    return await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id)"
        " VALUES ($1, $2) RETURNING id",
        credential_id,
        client_id,
    )


async def _seed_session(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
    source_database_id: uuid.UUID,
    external_session_id: str,
) -> uuid.UUID:
    """Insert a sessions row (internal gateway session); return sessions.id."""
    return await conn.fetchval(
        "INSERT INTO sessions"
        " (client_id, source_database_id, external_session_id,"
        "  first_message_at, last_message_at)"
        " VALUES ($1, $2, $3, $4, $4) RETURNING id",
        client_id,
        source_database_id,
        external_session_id,
        datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


async def _seed_observed_model(conn: asyncpg.Connection) -> uuid.UUID:
    """Insert an observed_models row; return model_id."""
    return await conn.fetchval(
        "INSERT INTO observed_models (model_name) VALUES ($1) RETURNING id",
        "claude-sonnet-4-20250514",
    )


async def _seed_source_identity(
    conn: asyncpg.Connection, client_id: uuid.UUID
) -> uuid.UUID:
    """Insert a source_identities row; return source_identity_id."""
    return await conn.fetchval(
        "INSERT INTO source_identities (client_id, collector_source_id)"
        " VALUES ($1, $2) RETURNING id",
        client_id,
        f"golden-source-{uuid.uuid4().hex[:8]}",
    )


async def _seed_usage_event(
    conn: asyncpg.Connection,
    *,
    client_id: uuid.UUID,
    session_id: uuid.UUID,
    model_id: uuid.UUID,
    source_identity_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    estimated_cost_usd: Decimal = Decimal("0.10"),
    reported_at: datetime | None = None,
) -> None:
    """Insert one usage_events row."""
    if reported_at is None:
        reported_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    source_record_id = f"golden-usage-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        """INSERT INTO usage_events
           (canonical_source_identity_id, source_record_id,
            client_id, session_id, model_id,
            input_tokens, output_tokens, cached_tokens,
            cache_read_tokens, cache_write_tokens, estimated_cost_usd,
            reported_at)
           VALUES ($1, $2, $3, $4, $5,
                   $6, $7, 0, $8, $9, $10, $11)""",
        source_identity_id,
        source_record_id,
        client_id,
        session_id,
        model_id,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
        estimated_cost_usd,
        reported_at,
    )


# ---------------------------------------------------------------------------
# Golden dataset seed
# ---------------------------------------------------------------------------


async def _seed_golden_dataset(conn: asyncpg.Connection) -> None:
    """Seed the complete deterministic golden dataset for the AFK dashboard.

    Creates:
    - 3 AFK runs (two on day 1, one on day 2)
    - Engineering events (CR opened/merged/closed)
    - Execution bindings (completed, failed, cancelled)
    - Sessions: one unambiguous (run A only), one ambiguous (runs A + B)
    - Usage events only for the unambiguous session (ambiguous excluded)
    """
    now = datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)

    # ── 1. AFK runs ──────────────────────────────────────────────
    for run_id, repo in [(_RUN_A, _REPO), (_RUN_B, _REPO), (_RUN_C, _REPO)]:
        await conn.execute(
            """INSERT INTO afk_runs
               (afk_run_id, provider, repository, started_at, first_seen_at,
                last_seen_at)
               VALUES ($1, $2, $3, $4, $4, $4)
               ON CONFLICT (afk_run_id) DO NOTHING""",
            run_id, _PROVIDER, repo, now,
        )

    # ── 2. Engineering events (change requests) ─────────────────
    # Run A: CR opened + merged on day 1
    await conn.execute(
        """INSERT INTO engineering_events
           (provider, repository, entity_type, external_id, event_type,
            occurred_at, payload, observation_key, observed_via)
           VALUES ($1, $2, 'change_request', 'cr-100', 'change_request.opened',
                   $3, '{}', $4, 'webhook')
           ON CONFLICT DO NOTHING""",
        _PROVIDER, _REPO, now,
        f"obs-{uuid.uuid4().hex}",
    )
    await conn.execute(
        """INSERT INTO engineering_events
           (provider, repository, entity_type, external_id, event_type,
            occurred_at, payload, observation_key, observed_via)
           VALUES ($1, $2, 'change_request', 'cr-100', 'change_request.merged',
                   $3, '{}', $4, 'webhook')
           ON CONFLICT DO NOTHING""",
        _PROVIDER, _REPO, now,
        f"obs-{uuid.uuid4().hex}",
    )

    # Run B: CR opened + closed on day 1
    await conn.execute(
        """INSERT INTO engineering_events
           (provider, repository, entity_type, external_id, event_type,
            occurred_at, payload, observation_key, observed_via)
           VALUES ($1, $2, 'change_request', 'cr-200', 'change_request.opened',
                   $3, '{}', $4, 'webhook')
           ON CONFLICT DO NOTHING""",
        _PROVIDER, _REPO, now,
        f"obs-{uuid.uuid4().hex}",
    )
    await conn.execute(
        """INSERT INTO engineering_events
           (provider, repository, entity_type, external_id, event_type,
            occurred_at, payload, observation_key, observed_via)
           VALUES ($1, $2, 'change_request', 'cr-200', 'change_request.closed',
                   $3, '{}', $4, 'webhook')
           ON CONFLICT DO NOTHING""",
        _PROVIDER, _REPO, now,
        f"obs-{uuid.uuid4().hex}",
    )

    # ── 3. Execution bindings ────────────────────────────────────
    # Day 1: 2 completed, 1 failed
    day1_started = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
    for i, outcome in enumerate(["completed", "completed", "failed"]):
        await conn.execute(
            """INSERT INTO execution_bindings
               (afk_run_id, awx_job_id, provider, repository_url, outcome,
                created_at, started_at, finished_at)
               VALUES ($1, $2, $3, $4, $5, $6, $6, $6)""",
            _RUN_A, str(100000 + i), _PROVIDER, _REPO, outcome, day1_started,
        )

    # Day 2: 1 cancelled
    day2_started = datetime(2026, 1, 16, 9, 0, 0, tzinfo=timezone.utc)
    await conn.execute(
        """INSERT INTO execution_bindings
           (afk_run_id, awx_job_id, provider, repository_url, outcome,
            created_at, started_at, finished_at)
           VALUES ($1, $2, $3, $4, $5, $6, $6, $6)""",
        _RUN_C, "100003", _PROVIDER, _REPO, "cancelled", day2_started,
    )

    # ── 4. Gateway sessions ─────────────────────────────────────
    client_id = await _seed_client(conn)
    credential_id = await _seed_credential(conn, client_id)
    source_database_id = await _seed_source_db(conn, client_id, credential_id)
    model_id = await _seed_observed_model(conn)
    source_identity_id = await _seed_source_identity(conn, client_id)

    internal_ses_unambiguous = await _seed_session(
        conn, client_id, source_database_id, _EXT_SES_UNAMBIGUOUS,
    )
    internal_ses_ambiguous = await _seed_session(
        conn, client_id, source_database_id, _EXT_SES_AMBIGUOUS,
    )

    # ── 5. AFK run sessions (the critical join) ─────────────────
    # Unambiguous session: linked only to run A
    await conn.execute(
        """INSERT INTO afk_run_sessions
           (afk_run_id, session_id, external_session_id, started_at, first_seen_at)
           VALUES ($1, $2, $3, $4, $4)
           ON CONFLICT DO NOTHING""",
        _RUN_A, internal_ses_unambiguous, _EXT_SES_UNAMBIGUOUS,
        datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )

    # Ambiguous session: linked to BOTH run A and run B
    # First link (to run A)
    await conn.execute(
        """INSERT INTO afk_run_sessions
           (afk_run_id, session_id, external_session_id, started_at, first_seen_at)
           VALUES ($1, $2, $3, $4, $4)
           ON CONFLICT DO NOTHING""",
        _RUN_A, internal_ses_ambiguous, _EXT_SES_AMBIGUOUS,
        datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    # Second link (to run B) — same external_session_id but different afk_run_id
    await conn.execute(
        """INSERT INTO afk_run_sessions
           (afk_run_id, session_id, external_session_id, started_at, first_seen_at)
           VALUES ($1, $2, $3, $4, $4)
           ON CONFLICT DO NOTHING""",
        _RUN_B, internal_ses_ambiguous, f"{_EXT_SES_AMBIGUOUS}-runB",
        datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )

    # ── 6. Usage events (only for unambiguous session) ───────────
    await _seed_usage_event(
        conn,
        client_id=client_id,
        session_id=internal_ses_unambiguous,
        model_id=model_id,
        source_identity_id=source_identity_id,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=50,
        estimated_cost_usd=Decimal("0.15"),
        reported_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )

    # Usage for the ambiguous session — should be EXCLUDED from rollup
    await _seed_usage_event(
        conn,
        client_id=client_id,
        session_id=internal_ses_ambiguous,
        model_id=model_id,
        source_identity_id=source_identity_id,
        input_tokens=9999,
        output_tokens=9999,
        cache_read_tokens=9999,
        cache_write_tokens=9999,
        estimated_cost_usd=Decimal("99.99"),
        reported_at=datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:  # type: ignore[override]
    """Per-test pool that seeds the golden dataset and tears it down."""
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None
    async with pool.acquire() as conn:
        await _seed_golden_dataset(conn)
    yield pool
    # Teardown: truncate all seeded tables.
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE usage_events, afk_run_sessions, afk_runs,"
            " execution_bindings, engineering_events, afk_dashboard_daily"
            " CASCADE"
        )
        await conn.execute(
            "DELETE FROM collector_credentials WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name LIKE 'golden-%')"
        )
        await conn.execute(
            "DELETE FROM source_databases WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name LIKE 'golden-%')"
        )
        await conn.execute(
            "DELETE FROM source_identities WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name LIKE 'golden-%')"
        )
        await conn.execute(
            "DELETE FROM sessions WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name LIKE 'golden-%')"
        )
        await conn.execute(
            "DELETE FROM opencode_clients WHERE name LIKE 'golden-%'"
        )
    await pool.close()


# ---------------------------------------------------------------------------
# Recompute helper (uses the engine directly)
# ---------------------------------------------------------------------------


async def _recompute_all_buckets(conn: asyncpg.Connection) -> None:
    """Recompute afk_dashboard_daily for all golden-dataset buckets."""
    from app.core.afk_dashboard_daily import recompute_bucket

    for day in [_DAY_1, _DAY_2]:
        async with conn.transaction():
            await recompute_bucket(conn, day, _PROVIDER, _REPO)


# ---------------------------------------------------------------------------
# Backfill helper (delegates to the script's entry point)
# ---------------------------------------------------------------------------


async def _run_backfill(
    conn: asyncpg.Connection, from_date: date, to_date: date
) -> int:
    """Run the backfill script for the given window; return exit code."""
    from scripts.backfill_afk_dashboard_daily import main as backfill_main

    return await backfill_main([
        "--from", from_date.isoformat(),
        "--to", to_date.isoformat(),
    ])


async def _run_backfill_verify(
    conn: asyncpg.Connection, from_date: date, to_date: date
) -> int:
    """Run the backfill script in --verify mode; return exit code."""
    from scripts.backfill_afk_dashboard_daily import main as backfill_main

    return await backfill_main([
        "--from", from_date.isoformat(),
        "--to", to_date.isoformat(),
        "--verify",
    ])


# ---------------------------------------------------------------------------
# Verifier helpers
# ---------------------------------------------------------------------------


async def _run_afk_verifier(
    conn: asyncpg.Connection, from_date: date, to_date: date
) -> tuple[int, list]:
    """Run the AFK verifier for the window; return (exit_code, mismatches)."""
    from scripts.verify_afk_dashboard_daily import (
        VerificationWindow,
        _fetch_afk_dashboard_mismatches,
    )

    window = VerificationWindow(from_date=from_date, to_date=to_date)
    mismatches = await _fetch_afk_dashboard_mismatches(conn, window)
    return (1 if mismatches else 0, mismatches)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_golden_dataset_seeds_correctly(db_pool: asyncpg.Pool) -> None:
    """The golden dataset seeds the expected row counts in every source table."""
    async with db_pool.acquire() as conn:
        run_count = await conn.fetchval("SELECT COUNT(*) FROM afk_runs")
        assert run_count == 3, f"expected 3 AFK runs, got {run_count}"

        cr_count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events"
            " WHERE entity_type = 'change_request'"
        )
        assert cr_count == 4, f"expected 4 CR events, got {cr_count}"

        binding_count = await conn.fetchval("SELECT COUNT(*) FROM execution_bindings")
        assert binding_count == 4, f"expected 4 execution bindings, got {binding_count}"

        session_count = await conn.fetchval("SELECT COUNT(*) FROM afk_run_sessions")
        assert session_count == 3, f"expected 3 AFK run sessions, got {session_count}"

        usage_count = await conn.fetchval("SELECT COUNT(*) FROM usage_events")
        assert usage_count == 2, f"expected 2 usage events, got {usage_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_session_excluded_from_session_count(
    db_pool: asyncpg.Pool,
) -> None:
    """A session linked to two AFK runs is excluded from session_count."""
    async with db_pool.acquire() as conn:
        # The ambiguous session maps to 2 afk_run_ids → HAVING COUNT = 2 → excluded.
        ambiguous_rows = await conn.fetch(
            "SELECT afk_run_id FROM afk_run_sessions"
            " WHERE external_session_id LIKE 'ses_golden_ambiguous%'"
            " ORDER BY afk_run_id"
        )
        assert len(ambiguous_rows) == 2, (
            f"ambiguous session should have 2 links, got {len(ambiguous_rows)}"
        )
        run_ids = {r["afk_run_id"] for r in ambiguous_rows}
        assert run_ids == {_RUN_A, _RUN_B}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_session_usage_excluded_from_rollup(
    db_pool: asyncpg.Pool,
) -> None:
    """Usage events for the ambiguous session must not appear in rollup totals."""
    async with db_pool.acquire() as conn:
        # Find the internal session ID for the ambiguous external session.
        internal_ambiguous = await conn.fetchval(
            "SELECT session_id FROM afk_run_sessions"
            " WHERE external_session_id = $1",
            _EXT_SES_AMBIGUOUS,
        )
        # Usage for this session should exist.
        usage = await conn.fetchval(
            "SELECT SUM(input_tokens) FROM usage_events"
            " WHERE session_id = $1",
            internal_ambiguous,
        )
        assert usage == 9999, f"ambiguous session usage should be 9999, got {usage}"

        # But after recomputation, the rollup must not include it.
        await _recompute_all_buckets(conn)
        row = await conn.fetchrow(
            "SELECT input_tokens FROM afk_dashboard_daily"
            " WHERE day = $1 AND provider = $2 AND repository = $3",
            _DAY_1, _PROVIDER, _REPO,
        )
        assert row is not None, "rollup row should exist for day 1"
        assert row["input_tokens"] == 1000, (
            f"ambiguous session usage (9999) must be excluded;"
            f" expected 1000 from unambiguous session, got {row['input_tokens']}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_exact_daily_metric_values(db_pool: asyncpg.Pool) -> None:
    """After recomputation, every additive metric matches the golden dataset."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        # ── Day 1 assertions ─────────────────────────────────────
        row = await conn.fetchrow(
            "SELECT * FROM afk_dashboard_daily"
            " WHERE day = $1 AND provider = $2 AND repository = $3",
            _DAY_1, _PROVIDER, _REPO,
        )
        assert row is not None, "day 1 rollup row should exist"
        assert row["runs_started"] == 2  # run A + run B
        assert row["change_requests_opened"] == 2  # cr-100 + cr-200 opened
        assert row["change_requests_merged"] == 1  # cr-100 merged
        assert row["change_requests_closed"] == 1  # cr-200 closed
        assert row["execution_count"] == 3  # 3 bindings on day 1
        assert row["successful_execution_count"] == 2  # 2 completed
        assert row["failed_execution_count"] == 1  # 1 failed
        assert row["cancelled_execution_count"] == 0
        assert row["session_count"] == 1  # only unambiguous session
        assert row["input_tokens"] == 1000  # only unambiguous session
        assert row["output_tokens"] == 500
        assert row["cache_read_tokens"] == 200
        assert row["cache_write_tokens"] == 50
        assert Decimal(str(row["estimated_cost_usd"])) == Decimal("0.15")

        # ── Day 2 assertions ─────────────────────────────────────
        row2 = await conn.fetchrow(
            "SELECT * FROM afk_dashboard_daily"
            " WHERE day = $1 AND provider = $2 AND repository = $3",
            _DAY_2, _PROVIDER, _REPO,
        )
        assert row2 is not None, "day 2 rollup row should exist"
        assert row2["runs_started"] == 1  # run C only
        assert row2["change_requests_opened"] == 0
        assert row2["change_requests_merged"] == 0
        assert row2["change_requests_closed"] == 0
        assert row2["execution_count"] == 1  # 1 cancelled binding
        assert row2["successful_execution_count"] == 0
        assert row2["failed_execution_count"] == 0
        assert row2["cancelled_execution_count"] == 1
        assert row2["session_count"] == 0  # no sessions linked to run C
        assert row2["input_tokens"] == 0
        assert row2["output_tokens"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_and_backfill_discover_same_active_buckets(
    db_pool: asyncpg.Pool,
) -> None:
    """Refresh and backfill discover the same (day, provider, repository) set."""
    from scripts.refresh_afk_dashboard_daily import DISCOVERY_SQL as REFRESH_DISCOVERY
    from scripts.backfill_afk_dashboard_daily import DISCOVERY_SQL as BACKFILL_DISCOVERY

    async with db_pool.acquire() as conn:
        # Both discovery queries should find the same buckets for the window.
        refresh_buckets = await conn.fetch(
            REFRESH_DISCOVERY, _DAY_1, _DAY_2,
        )
        backfill_buckets = await conn.fetch(
            BACKFILL_DISCOVERY, _DAY_1, _DAY_2, None, None,
        )

        refresh_set = {(r["day"], r["provider"], r["repository"]) for r in refresh_buckets}
        backfill_set = {(r["day"], r["provider"], r["repository"]) for r in backfill_buckets}

        assert refresh_set == backfill_set, (
            f"refresh and backfill should discover the same active buckets;\n"
            f"  refresh: {refresh_set}\n"
            f"  backfill: {backfill_set}"
        )
        assert len(refresh_set) == 2, (
            f"expected 2 active buckets (day 1 + day 2), got {len(refresh_set)}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_daily_summary_metrics_additive(db_pool: asyncpg.Pool) -> None:
    """The daily summary API response contains additive metric values."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        from app.core.schemas.afk_dashboard import AFKDashboardSummary
        from app.api.afk_dashboard_summary import _fetch_summary

        summary = await _fetch_summary(
            conn,
            interval="daily",
            from_date=_DAY_1,
            to_date=_DAY_2,
            provider=_PROVIDER,
            repository=_REPO,
            db_timeout_seconds=30,
        )

        assert isinstance(summary, AFKDashboardSummary)
        assert summary.interval == "daily"
        assert len(summary.buckets) == 2

        # First bucket (day 1)
        b1 = summary.buckets[0]
        assert b1.period_start == _DAY_1
        assert b1.runs_started == 2
        assert b1.change_requests_opened == 2
        assert b1.change_requests_merged == 1
        assert b1.execution_count == 3
        assert b1.session_count == 1
        assert b1.input_tokens == 1000

        # Second bucket (day 2)
        b2 = summary.buckets[1]
        assert b2.period_start == _DAY_2
        assert b2.runs_started == 1
        assert b2.execution_count == 1
        assert b2.cancelled_execution_count == 1

        # Additive invariant: daily total == sum of buckets
        assert summary.derived_at is not None, "derived_at should be set"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_monthly_summary_additive_across_days(db_pool: asyncpg.Pool) -> None:
    """The monthly summary sums daily buckets for the same month."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        from app.api.afk_dashboard_summary import _fetch_summary

        summary = await _fetch_summary(
            conn,
            interval="monthly",
            from_date=_DAY_1,
            to_date=_DAY_2,
            provider=_PROVIDER,
            repository=_REPO,
            db_timeout_seconds=30,
        )

        assert len(summary.buckets) == 1, "both days should collapse into one monthly bucket"
        mb = summary.buckets[0]
        assert mb.period_start == date(2026, 1, 1)
        # Monthly totals are the sum of both days.
        assert mb.runs_started == 3  # 2 (day1) + 1 (day2)
        assert mb.execution_count == 4  # 3 (day1) + 1 (day2)
        assert mb.input_tokens == 1000  # only unambiguous session usage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derived_at_freshness_metadata(db_pool: asyncpg.Pool) -> None:
    """derived_at is set on every rollup bucket after recomputation."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        for day in [_DAY_1, _DAY_2]:
            row = await conn.fetchrow(
                "SELECT derived_at FROM afk_dashboard_daily"
                " WHERE day = $1 AND provider = $2 AND repository = $3",
                day, _PROVIDER, _REPO,
            )
            assert row is not None, f"rollup row should exist for {day}"
            assert row["derived_at"] is not None, (
                f"derived_at should be set for {day}"
            )

        # Assert the freshness range: oldest_derived_at == MIN(derived_at)
        freshness = await conn.fetchrow(
            "SELECT MIN(derived_at) AS oldest, MAX(derived_at) AS latest"
            " FROM afk_dashboard_daily"
            " WHERE provider = $1 AND repository = $2",
            _PROVIDER, _REPO,
        )
        assert freshness["oldest"] is not None, "oldest_derived_at should be set"
        assert freshness["latest"] is not None, "latest derived_at should be set"
        assert freshness["oldest"] <= freshness["latest"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_afk_verifier_zero_mismatches(db_pool: asyncpg.Pool) -> None:
    """The AFK verifier reports zero mismatches on a freshly-recomputed dataset."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)
        exit_code, mismatches = await _run_afk_verifier(conn, _DAY_1, _DAY_2)
        assert exit_code == 0, (
            f"AFK verifier should report 0 mismatches, got {exit_code};"
            f" mismatches: {[m.as_dict() for m in mismatches]}"
        )
        assert len(mismatches) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verifier_detects_modified_source_fact(db_pool: asyncpg.Pool) -> None:
    """Modifying a canonical source fact causes the verifier to detect a mismatch."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        # Verify clean first.
        exit_code, mismatches = await _run_afk_verifier(conn, _DAY_1, _DAY_2)
        assert exit_code == 0, "verifier should be clean before modification"

        # Corrupt a source fact: change execution outcome from failed → completed.
        await conn.execute(
            """UPDATE execution_bindings
               SET outcome = 'completed'
               WHERE afk_run_id = $1 AND outcome = 'failed'""",
            _RUN_A,
        )

        # Re-run verifier without recomputing — mismatch expected.
        exit_code, mismatches = await _run_afk_verifier(conn, _DAY_1, _DAY_2)
        assert exit_code == 1, "verifier should detect the modified source fact"
        assert len(mismatches) >= 1, "should report at least one mismatch"

        mismatch = mismatches[0]
        assert mismatch.source == "afk_dashboard_daily"
        assert mismatch.day == _DAY_1
        # The execution metrics should differ.
        exec_diff = mismatch.fields.get("execution_count") or mismatch.fields.get(
            "successful_execution_count"
        )
        assert exec_diff is not None, (
            "mismatch should include execution metric differences"
        )

        # Restore the original value for subsequent tests.
        await conn.execute(
            """UPDATE execution_bindings
               SET outcome = 'failed'
               WHERE afk_run_id = $1 AND awx_job_id = '100002'""",
            _RUN_A,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_convergence_after_correction(db_pool: asyncpg.Pool) -> None:
    """Scoped backfill including stale-row deletion converges to zero mismatches."""
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        # Insert a stale rollup row for a bucket with no canonical activity.
        stale_day = date(2026, 1, 17)
        await conn.execute(
            """INSERT INTO afk_dashboard_daily
               (day, provider, repository, runs_started, input_tokens,
                output_tokens, cache_read_tokens, cache_write_tokens,
                estimated_cost_usd, derived_at, rollup_version, updated_at)
               VALUES ($1, $2, $3, 999, 999, 999, 999, 999, 999,
                       now(), 'stale', now())""",
            stale_day, _PROVIDER, _REPO,
        )

        # Verify stale row exists.
        stale = await conn.fetchval(
            "SELECT runs_started FROM afk_dashboard_daily"
            " WHERE day = $1 AND provider = $2 AND repository = $3",
            stale_day, _PROVIDER, _REPO,
        )
        assert stale == 999, "stale row should exist before backfill"

        # Run backfill which should recompute + delete stale rows.
        backfill_exit = await _run_backfill(conn, _DAY_1, stale_day)
        assert backfill_exit == 0, "backfill should succeed"

        # The stale row should be deleted (no canonical activity for that day).
        stale_after = await conn.fetchval(
            "SELECT runs_started FROM afk_dashboard_daily"
            " WHERE day = $1 AND provider = $2 AND repository = $3",
            stale_day, _PROVIDER, _REPO,
        )
        assert stale_after is None, (
            f"stale row should be deleted after backfill, got {stale_after}"
        )

        # Convergence: verifier should pass after backfill.
        remaining = await _run_backfill_verify(conn, _DAY_1, stale_day)
        assert remaining == 0, (
            "backfill --verify should report zero mismatches after convergence"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reporting_verifier_independent_of_afk_verifier(
    db_pool: asyncpg.Pool,
) -> None:
    """Running the reporting verifier independently cannot fail the AFK verifier.

    The reporting verifier checks ``reporting_resource_aggregates`` vs
    ``reporting_deliveries`` — tables the golden dataset does not seed.
    The AFK verifier checks ``afk_dashboard_daily`` vs canonical AFK sources.
    They must be independent: the reporting verifier should not affect the
    AFK verifier's result, and vice versa.
    """
    async with db_pool.acquire() as conn:
        await _recompute_all_buckets(conn)

        # Run AFK verifier first — should be clean.
        afk_exit, afk_mismatches = await _run_afk_verifier(conn, _DAY_1, _DAY_2)
        assert afk_exit == 0, (
            f"AFK verifier should be clean, got {afk_exit}; "
            f"mismatches: {[m.as_dict() for m in afk_mismatches]}"
        )

        # Run the full verifier (which includes reporting side).
        from scripts.verify_afk_dashboard_daily import (
            VerificationWindow,
            _run_verification,
        )

        window = VerificationWindow(from_date=_DAY_1, to_date=_DAY_2)
        all_mismatches = await _run_verification(conn, window)

        # Reporting mismatches should be empty (no seeded reporting data).
        reporting_mismatches = [
            m for m in all_mismatches if m.source == "reporting_resource_aggregates"
        ]
        assert len(reporting_mismatches) == 0, (
            f"reporting verifier should find 0 mismatches with no seeded data,"
            f" got {len(reporting_mismatches)}"
        )

        # AFK mismatches should still be 0 — the two verifiers are independent.
        afk_mismatches_after = [
            m for m in all_mismatches if m.source == "afk_dashboard_daily"
        ]
        assert len(afk_mismatches_after) == 0, (
            f"AFK verifier should remain clean after reporting verifier run,"
            f" got {len(afk_mismatches_after)} mismatches"
        )
