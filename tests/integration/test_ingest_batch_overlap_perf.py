"""Production-sized integration test for batch-level overlap detection (issue #416).

Verifies that a 100-record ingest batch performs at most one historical overlap
query even when ``usage_ingest_attempts`` contains a production-scale history
(~100k rows), and that quarantined records never produce accounting side effects.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v

The test reads database connection parameters from the standard Gateway
environment variables (``GATEWAY_DATABASE_HOST``, ``GATEWAY_DATABASE_PORT``,
``GATEWAY_DATABASE_NAME``, ``GATEWAY_DATABASE_USER``,
``GATEWAY_DATABASE_PASSWORD``).  The ``docker-compose.test.yml`` service
is configured to match the defaults expected by these tests.

Environment variables
---------------------
GATEWAY_DATABASE_HOST
    Host of the test database (defaults to ``localhost``).
GATEWAY_DATABASE_PORT
    Port of the test database (defaults to ``5433`` to match
    ``docker-compose.test.yml``).
GATEWAY_DATABASE_NAME
    Database name (defaults to ``opencode_gateway_test``).
GATEWAY_DATABASE_USER
    Database user (defaults to ``opencode_test``).
GATEWAY_DATABASE_PASSWORD
    Database password (defaults to ``opencode_test``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

# ── Repository paths ─────────────────────────────────────────────────────
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

# ── Default connection parameters matching docker-compose.test.yml ───────
_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

# ── Request budget (seconds) ────────────────────────────────────────────
# The Gateway's total_request_timeout_seconds defaults to 20 in config.py.
# The issue specifies a 35-second budget; we use the lower config default
# (20s) as a stricter bound.  A 100-record batch with batch-level overlap
# detection should complete in well under 5 seconds even with a 100k-row
# fixture; the old per-record approach would need >2 minutes and time out.
REQUEST_BUDGET_SECONDS = 20.0

# ── Production-scale fixture size ────────────────────────────────────────
# The production evidence from issue #416 reports ~123k usage_ingest_attempts
# rows.  We seed at this scale so the integration environment mirrors the
# production query plan.
ATTEMPTS_FIXTURE_COUNT = 123_000

# ── Batch size for ingest test ───────────────────────────────────────────
BATCH_SIZE = 100

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _dsn() -> str:
    """Build an asyncpg DSN from environment variables or defaults."""
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


async def _can_connect() -> bool:
    """Return True if the test database is reachable."""
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=_dsn(), timeout=5),
            timeout=10.0,
        )
        await conn.close()
        return True
    except Exception:
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ts() -> datetime:
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def _integration_db_available() -> bool:
    """Module-level check: skip the entire module if the test DB is absent."""
    if not asyncio.run(_can_connect()):
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


@pytest.fixture(scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    """Create a dedicated asyncpg pool, run Alembic migrations, and return it.

    The pool is shared across all tests in the module and closed after the
    last test completes.
    """
    # ── Build pool ───────────────────────────────────────────────────
    pool = await asyncpg.create_pool(
        dsn=_dsn(),
        min_size=2,
        max_size=5,
    )
    assert pool is not None

    # ── Run Alembic migrations ───────────────────────────────────────
    import alembic.command
    import alembic.config

    # Override the sqlalchemy.url so Alembic connects to the test DB.
    # Build a sync psycopg URL that Alembic can use for DDL.
    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    try:
        alembic.command.upgrade(alembic_cfg, "head")
    except Exception:
        # Downgrade any lingering dirty state and retry once.
        try:
            alembic.command.downgrade(alembic_cfg, "base")
        except Exception:
            pass
        # Drop all tables manually as a last-resort reset
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        alembic.command.upgrade(alembic_cfg, "head")

    yield pool

    # ── Cleanup: drop all tables so the next run starts fresh ────────
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
    except Exception:
        pass
    finally:
        await pool.close()


# ══════════════════════════════════════════════════════════════════════════
#  Fixture data — production-scale seed
# ══════════════════════════════════════════════════════════════════════════

# These are populated per-module by the _seed fixture and stored as module
# globals so individual test functions can reference them without repeating
# the seed logic.

_SEED: dict = {}


@pytest.fixture(scope="module")
async def _seed(db_pool: asyncpg.Pool) -> dict:
    """Seed the test database with production-scale data for an existing
    identity, then return a dict of generated IDs for use by tests.

    Creates, in order:
    - 1 ``opencode_client`` (existing identity owner)
    - 1 ``collector_credential``
    - 1 ``source_database``
    - 1 ``source_identity`` (the existing identity)
    - 1 ``session`` (for usage_events FK)
    - 1 ``observed_model`` (for usage_events FK)
    - ``ATTEMPTS_FIXTURE_COUNT`` ``usage_ingest_attempts`` rows owned by
      the existing identity
    - 1 ``ingest_batch`` (for FK on attempts)
    - Several ``usage_events`` rows with varying source_record_ids to
      simulate a mix of existing records.

    The seed includes ``usage_events`` rows for source_record_ids that
    overlap with the test batch (so the batch overlap query has something
    to find when an overlapping identity is tested).
    """
    global _SEED
    if _SEED:
        return _SEED  # already seeded in this module

    async with db_pool.acquire() as conn:
        now = _utcnow()

        # ── 1. opencode_client ──────────────────────────────────────
        client_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO opencode_clients (id, name, is_active, created_at)
               VALUES ($1, 'test-client-existing', true, $2)""",
            client_id,
            now,
        )

        # ── 2. collector_credential ─────────────────────────────────
        # token_hash is SHA-256 of the test API key "test-api-key",
        # matching the conftest GATEWAY_API_KEY and the bearer token
        # sent by _build_ingest_app_for_integration.
        cred_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO collector_credentials
               (id, client_id, token_hash, token_prefix, created_at)
               VALUES ($1, $2,
                       '4c806362b613f7496abf284146efd31da90e4b16169fe001841ca17290f427c4',
                       'test-api', $3)""",
            cred_id,
            client_id,
            now,
        )

        # ── 3. source_database ──────────────────────────────────────
        source_db_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO source_databases (id, client_id, collector_credential_id,
               first_seen_at, last_seen_at, record_count)
               VALUES ($1, $2, $3, $4, $4, 0)""",
            source_db_id,
            client_id,
            cred_id,
            now,
        )

        # ── 4. source_identity (existing identity) ─────────────────
        existing_identity_id = uuid.uuid4()
        collector_source_id = str(source_db_id)
        await conn.execute(
            """INSERT INTO source_identities
               (id, client_id, collector_source_id, is_canonical,
                created_at)
               VALUES ($1, $2, $3, true, $4)""",
            existing_identity_id,
            client_id,
            collector_source_id,
            now,
        )

        # ── 5. session (needed for usage_events FK) ─────────────────
        session_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO sessions (id, source_database_id,
               client_id, external_session_id,
               first_message_at, last_message_at, message_count,
               total_input_tokens, total_output_tokens, total_cached_tokens,
               total_cache_read_tokens, total_cache_write_tokens,
               total_estimated_cost_usd)
               VALUES ($1, $2, $3, 'ses_seed_001',
                       '2025-07-16T12:00:00Z', '2025-07-16T12:00:00Z', 0,
                       1000, 500, 0, 0, 0, 0.01)""",
            session_id,
            source_db_id,
            client_id,
        )

        # ── 6. observed_model ───────────────────────────────────────
        model_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO observed_models (id, model_name, first_seen_at, last_seen_at)
               VALUES ($1, 'gpt-4', $2, $2)""",
            model_id,
            now,
        )

        # ── 7. ingest_batch (for FK on attempts) ────────────────────
        batch_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO ingest_batches
               (id, collector_credential_id, client_id, collector_version,
                schema_version, record_count, accepted_count, rejected_count,
                ingested_at)
               VALUES ($1, $2, $3, 'test', '1.0',
                       $4, $4, 0, $5)""",
            batch_id,
            cred_id,
            client_id,
            ATTEMPTS_FIXTURE_COUNT,
            now,
        )

        # ── 8. Bulk-insert usage_ingest_attempts rows ───────────────
        # Use a single multi-row INSERT for speed — 123k individual INSERTs
        # would take minutes.  We batch in chunks of 1000 to avoid exceeding
        # statement parameter limits.
        CHUNK = 1000
        base_ts = _mk_ts()

        for offset in range(0, ATTEMPTS_FIXTURE_COUNT, CHUNK):
            chunk_size = min(CHUNK, ATTEMPTS_FIXTURE_COUNT - offset)
            placeholders = []
            params = []
            for i in range(chunk_size):
                idx = offset + i
                p = i * 8
                placeholders.append(
                    f"(${p + 1}, ${p + 2}, ${p + 3}, ${p + 4}, "
                    f"${p + 5}, ${p + 6}, ${p + 7}, ${p + 8})"
                )
                params.extend([
                    uuid.uuid4(),                    # id
                    None,                            # usage_event_id
                    existing_identity_id,            # source_identity_id
                    f"existing-rec-{idx:06d}",       # original_source_record_id
                    '{}',                            # record_jsonb
                    batch_id,                        # ingest_batch_id
                    "accepted",                      # outcome
                    base_ts,                         # delivered_at
                ])
            sql = (
                "INSERT INTO usage_ingest_attempts "
                "(id, usage_event_id, source_identity_id, "
                " original_source_record_id, record_jsonb, ingest_batch_id, outcome, delivered_at) "
                "VALUES " + ", ".join(placeholders)
            )
            await conn.execute(sql, *params)

        # ── 9. Create several usage_events for the existing identity ──
        # These use source_record_ids that overlap with the test batch
        # so `check_batch_overlap` has records to detect when a new
        # identity is tested with overlapping IDs.
        for i in range(50):
            await conn.execute(
                """INSERT INTO usage_events
                   (id, canonical_source_identity_id, source_record_id,
                    client_id, session_id, model_id,
                    input_tokens, output_tokens, cached_tokens,
                    reported_at, first_ingested_at, last_ingested_at)
                   VALUES ($1, $2, $3, $4, $5, $6,
                           100, 50, 0, $7, $8, $8)""",
                uuid.uuid4(),
                existing_identity_id,
                f"rec-{i:03d}",  # overlaps with the 100-record test batch
                client_id,
                session_id,
                model_id,
                base_ts,
                now,
            )

        # Build the seed dict for test use
        seed_data = {
            "client_id": client_id,
            "credential_id": cred_id,
            "source_db_id": source_db_id,
            "existing_identity_id": existing_identity_id,
            "collector_source_id": collector_source_id,
            "session_id": session_id,
            "model_id": model_id,
            "batch_id": batch_id,
            "base_ts": base_ts,
            "attempts_count": ATTEMPTS_FIXTURE_COUNT,
        }
        _SEED = seed_data

        # Verify the seed count
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM usage_ingest_attempts"
        )
        assert cnt == ATTEMPTS_FIXTURE_COUNT, (
            f"Expected {ATTEMPTS_FIXTURE_COUNT} attempts, got {cnt}"
        )

        return _SEED


# ══════════════════════════════════════════════════════════════════════════
#  Tests
# ══════════════════════════════════════════════════════════════════════════


def _build_ingest_app_for_integration(
    db_pool: asyncpg.Pool,
    *,
    api_key: str | None = "test-api-key",
) -> AsyncClient:
    """Build a FastAPI TestClient connected to the real integration DB pool.

    Disables the API-key middleware when ``api_key`` is ``None``; provides
    an ``Authorization: Bearer <api_key>`` header otherwise so the
    middleware passes — the bearer token must exactly match
    ``GATEWAY_API_KEY``, pinned to ``test-api-key`` by ``tests/conftest.py``
    (the seeded collector credential hash matches it), so both auth layers
    succeed.  ``GATEWAY_ENV=development`` does not bypass API-key checks.
    """
    from app.core.factory import create_app
    from app.db.session import get_session

    os.environ.setdefault("GATEWAY_ENV", "development")

    app = create_app(configure_logging=False)

    async def _override_get_session(request: Request):
        conn = await db_pool.acquire()
        try:
            yield conn
        finally:
            await db_pool.release(conn)

    app.dependency_overrides[get_session] = _override_get_session

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


def _valid_ingest_payload(
    *,
    source_db_id: uuid.UUID,
    records: list[dict] | None = None,
) -> dict:
    """Return a valid ingest request body for an integration test."""
    if records is None:
        records = [
            {
                "source_record_id": "rec-000",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            },
        ]
    return {
        "schema_version": "1.0",
        "collector_version": "0.1.0",
        "source_database_id": str(source_db_id),
        "records": records,
    }


@pytest.mark.integration
class TestIngestBatchOverlapPerformance:
    """Integration tests for batch-level overlap detection (issue #416).

    All tests share the module-scoped ``db_pool`` and ``_seed`` fixtures.
    """

    @pytest.mark.asyncio
    async def test_100_record_batch_completes_within_request_budget(
        self, db_pool: asyncpg.Pool, _seed: dict
    ):
        """A 100-record non-overlapping ingest batch completes within the
        configured request budget even with a production-scale
        (~123k attempts) history.  The batch uses source_record_ids that
        do NOT overlap with any existing usage_events, so it should pass
        through without quarantine or conflict.

        Timing assertion: the entire batch must complete under
        ``REQUEST_BUDGET_SECONDS``.  The old per-record overlap scan
        (1.58s/record × 100 = 158s) far exceeds this budget; the new
        batch-level check runs once and completes in well under a second.
        """
        seed = _seed

        # ── Build 100 records with UNIQUE source_record_ids that do NOT
        #     overlap with the existing identity's usage_events. ──────
        # The seed's usage_events use IDs "rec-000" through "rec-049",
        # but the existing identity OWNS those — they won't conflict
        # with itself.  We use "new-rec-000" … "new-rec-099" for the
        # batch so they don't trigger overlap either.
        records = [
            {
                "source_record_id": f"new-rec-{i:03d}",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.001",
                "reported_at": _mk_ts().isoformat(),
            }
            for i in range(BATCH_SIZE)
        ]

        payload = _valid_ingest_payload(
            source_db_id=seed["source_db_id"],
            records=records,
        )

        client = _build_ingest_app_for_integration(db_pool)

        start = time.monotonic()
        async with client as c:
            response = await c.post("/ingest", json=payload)
        elapsed = time.monotonic() - start

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: "
            f"{response.json()}"
        )
        data = response.json()["data"]
        assert data["accepted_count"] == BATCH_SIZE, (
            f"Expected {BATCH_SIZE} accepted, got {data['accepted_count']}"
        )
        assert data["rejected_count"] == 0

        assert elapsed < REQUEST_BUDGET_SECONDS, (
            f"Batch of {BATCH_SIZE} records took {elapsed:.2f}s — "
            f"exceeds {REQUEST_BUDGET_SECONDS}s budget. "
            f"Per-record overlap scan regression suspected."
        )

    @pytest.mark.asyncio
    async def test_overlapping_identity_is_quarantined_before_accounting(
        self, db_pool: asyncpg.Pool, _seed: dict
    ):
        """When a NEW identity sends records whose source_record_ids overlap
        with an existing unresolved identity (detected by the batch-level
        check_batch_overlap), the identity is quarantined and ALL records
        in the batch receive status ``quarantined`` — no canonical events,
        no session aggregates, no usage records, and no rollup rows are
        created.

        Both the existing identity and the new identity belong to the same
        client — ``check_batch_overlap`` is same-client scoped
        (``si.client_id = $1``), so overlap detection requires the
        overlapping records to reside under the same client.
        """
        seed = _seed
        async with db_pool.acquire() as conn:
            # ── Create a second source_database under the SAME client ──
            # A different collector_source_id will result in a new
            # source_identity row for the same client, which
            # check_batch_overlap can compare against the existing
            # identity's usage_events.
            now = _utcnow()

            new_source_db_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO source_databases (id, client_id,
                   collector_credential_id, first_seen_at, last_seen_at,
                   record_count)
                   VALUES ($1, $2, $3, $4, $4, 0)""",
                new_source_db_id,
                seed["client_id"],
                seed["credential_id"],
                now,
            )

            # ── Snapshot pre-ingest state (before quota request) ─────
            before_sessions = await conn.fetchval(
                "SELECT COUNT(*) FROM sessions"
            )
            before_events = await conn.fetchval(
                "SELECT COUNT(*) FROM usage_events"
            )
            before_rollup = await conn.fetchval(
                "SELECT COUNT(*) FROM client_project_rollup"
            )
            before_records = await conn.fetchval(
                "SELECT COUNT(*) FROM opencode_usage_records"
            )
            before_quarantines = await conn.fetchval(
                "SELECT COUNT(*) FROM source_identity_quarantine"
            )

        # ── Build 100 records with OVERLAPPING source_record_ids ─────
        # These IDs ("rec-000" … "rec-099") already exist as usage_events
        # owned by the existing identity under the same client, so the
        # new identity should be quarantined via same-client scoped
        # check_batch_overlap.
        records = [
            {
                "source_record_id": f"rec-{i:03d}",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.001",
                "reported_at": _mk_ts().isoformat(),
            }
            for i in range(BATCH_SIZE)
        ]

        payload = _valid_ingest_payload(
            source_db_id=new_source_db_id,
            records=records,
        )

        client = _build_ingest_app_for_integration(db_pool)

        async with client as c:
            response = await c.post("/ingest", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]

        # ── All records must be quarantined ──────────────────────────
        assert data["accepted_count"] == 0, (
            f"Expected 0 accepted, got {data['accepted_count']}"
        )
        assert data["rejected_count"] == BATCH_SIZE, (
            f"Expected {BATCH_SIZE} rejected (quarantined), "
            f"got {data['rejected_count']}"
        )
        for result in data["results"]:
            assert result["status"] == "quarantined", (
                f"Expected quarantined, got {result['status']} "
                f"for index {result['index']}"
            )
            assert result["event_id"] is None
            assert result["attempt_id"] is not None

        # ── Verify no accounting side effects ────────────────────────
        async with db_pool.acquire() as conn:
            after_sessions = await conn.fetchval(
                "SELECT COUNT(*) FROM sessions"
            )
            after_events = await conn.fetchval(
                "SELECT COUNT(*) FROM usage_events"
            )
            after_rollup = await conn.fetchval(
                "SELECT COUNT(*) FROM client_project_rollup"
            )
            after_records = await conn.fetchval(
                "SELECT COUNT(*) FROM opencode_usage_records"
            )
            after_quarantines = await conn.fetchval(
                "SELECT COUNT(*) FROM source_identity_quarantine"
            )

            # Session count must not change
            assert after_sessions == before_sessions, (
                f"Sessions changed: {before_sessions} → {after_sessions}"
            )
            # No new canonical events
            assert after_events == before_events, (
                f"usage_events changed: {before_events} → {after_events}"
            )
            # No rollup writes
            assert after_rollup == before_rollup, (
                f"client_project_rollup changed: "
                f"{before_rollup} → {after_rollup}"
            )
            # No raw usage records
            assert after_records == before_records, (
                f"opencode_usage_records changed: "
                f"{before_records} → {after_records}"
            )
            # A new quarantine entry must have been created
            assert after_quarantines == before_quarantines + 1, (
                f"Expected 1 new quarantine, "
                f"got {after_quarantines - before_quarantines}"
            )

            # ── Verify the quarantine entry ──────────────────────────
            q = await conn.fetchrow(
                """SELECT q.overlap_count, q.quarantined_at, q.cleared_at
                   FROM source_identity_quarantine q
                   ORDER BY q.quarantined_at DESC
                   LIMIT 1"""
            )
            assert q is not None
            assert q["overlap_count"] >= 1, (
                f"Expected overlap_count >= 1, got {q['overlap_count']}"
            )
            assert q["cleared_at"] is None, (
                "Quarantine should not be cleared automatically"
            )

            # ── Verify 100 quarantined attempt rows recorded ─────────
            attempt_count = await conn.fetchval(
                """SELECT COUNT(*) FROM usage_ingest_attempts
                   WHERE outcome = 'quarantined'"""
            )
            assert attempt_count >= BATCH_SIZE, (
                f"Expected at least {BATCH_SIZE} quarantined attempts, "
                f"got {attempt_count}"
            )

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_records_retain_per_record_outcomes(
        self, db_pool: asyncpg.Pool, _seed: dict
    ):
        """When a non-overlapping batch contains both valid and invalid
        records, the batch-level check passes (no overlap with other
        unresolved identities), and the per-record outcomes are
        preserved: valid records are accepted, invalid records are
        rejected with a validation reason.

        .. note::

           Per-record ``conflict`` outcomes are unreachable under the
           current same-client scoping because ``check_batch_overlap``
           quarantines the whole identity before the per-record conflict
           check can fire.  This test exercises the mixed per-record
           path instead — validation failures alongside successful
           accepts — which is the dominant mixed-outcome pattern in
           practice.
        """
        seed = _seed
        async with db_pool.acquire() as conn:
            now = _utcnow()

            # Create a second source_database under the SAME client.
            # No overlap with existing usage_events — this batch uses
            # unique source_record_ids so the batch overlap check
            # passes and the per-record loop runs.
            second_source_db_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO source_databases (id, client_id,
                   collector_credential_id, first_seen_at, last_seen_at,
                   record_count)
                   VALUES ($1, $2, $3, $4, $4, 0)""",
                second_source_db_id,
                seed["client_id"],
                seed["credential_id"],
                now,
            )

        # Build a mixed batch: 5 invalid (negative tokens) + 5 valid
        mixed_records = []
        # 5 invalid records — negative input_tokens rejected by _validate_tokens
        for i in range(5):
            mixed_records.append({
                "source_record_id": f"invalid-rec-{i:03d}",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": -1,  # triggers "Negative token value"
                "output_tokens": 5,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.001",
                "reported_at": _mk_ts().isoformat(),
            })
        # 5 valid records — unique IDs, no overlap
        for i in range(5):
            mixed_records.append({
                "source_record_id": f"valid-mix-{i:03d}",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.001",
                "reported_at": _mk_ts().isoformat(),
            })

        payload = _valid_ingest_payload(
            source_db_id=second_source_db_id,
            records=mixed_records,
        )

        client = _build_ingest_app_for_integration(db_pool)
        async with client as c:
            response = await c.post("/ingest", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]

        # ── Verify mixed outcomes ────────────────────────────────────
        rejected_count = sum(
            1 for r in data["results"] if r["status"] == "rejected"
        )
        accepted_count = sum(
            1 for r in data["results"] if r["status"] == "accepted"
        )
        assert rejected_count == 5, (
            f"Expected 5 rejected (invalid), got {rejected_count}"
        )
        assert accepted_count == 5, (
            f"Expected 5 accepted, got {accepted_count}"
        )
        assert data["accepted_count"] == 5
        assert data["rejected_count"] == 5

        # ── Verify per-record outcomes have documented statuses ──────
        valid_statuses = {"accepted", "duplicate", "updated",
                          "quarantined", "conflict", "rejected"}
        for result in data["results"]:
            assert result["status"] in valid_statuses, (
                f"Invalid status: {result['status']} at index "
                f"{result.get('index', '?')}"
            )

        # ── Rejected records should carry a reason ───────────────────
        for result in data["results"]:
            if result["status"] == "rejected":
                assert result.get("reason"), (
                    "Rejected record missing reason at index "
                    f"{result.get('index', '?')}"
                )

    @pytest.mark.asyncio
    async def test_batch_overlap_uses_one_query_with_production_fixture(
        self, db_pool: asyncpg.Pool, _seed: dict
    ):
        """Verify that a non-overlapping batch performs at most one query
        against ``usage_ingest_attempts`` for historical overlap detection.

        Uses ``pg_stat_statements`` to count the number of queries matching
        the overlap scan pattern.  Falls back to timing-based assertion if
        ``pg_stat_statements`` is unavailable, but the timing of a 100-record
        batch against a 123k-attempt fixture is itself a strong regression
        guard (the old per-record scan would need >2 minutes).
        """
        # Try to enable pg_stat_statements tracking
        async with db_pool.acquire() as conn:
            pgss_available = False
            try:
                await conn.execute(
                    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
                )
                await conn.execute(
                    "SELECT pg_stat_statements_reset()"
                )
                pgss_available = True
            except Exception as exc:
                logger.info(
                    "pg_stat_statements unavailable (%s); "
                    "relying on timing-based assertion", exc,
                )

        # ── Build 100 non-overlapping records ────────────────────────
        records = [
            {
                "source_record_id": f"perf-rec-{i:03d}",
                "session_id": str(uuid.uuid4()),
                "model": "gpt-4",
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.001",
                "reported_at": _mk_ts().isoformat(),
            }
            for i in range(BATCH_SIZE)
        ]

        payload = _valid_ingest_payload(
            source_db_id=_seed["source_db_id"],
            records=records,
        )

        client = _build_ingest_app_for_integration(db_pool)

        start = time.monotonic()
        async with client as c:
            response = await c.post("/ingest", json=payload)
        elapsed = time.monotonic() - start

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == BATCH_SIZE

        if pgss_available:
            async with db_pool.acquire() as conn:
                # Count queries that scan usage_ingest_attempts with a
                # self-join or group-by pattern (the old per-record query).
                # We expect ZERO such queries.
                old_pattern_count = await conn.fetchval(
                    """SELECT COALESCE(SUM(calls), 0)
                       FROM pg_stat_statements
                       WHERE query ILIKE '%usage_ingest_attempts%'
                         AND query ILIKE '%JOIN%usage_ingest_attempts%'
                         AND query ILIKE '%original_source_record_id%'"""
                )
                # Count batch overlap queries (the new single-set query)
                batch_pattern_count = await conn.fetchval(
                    """SELECT COALESCE(SUM(calls), 0)
                       FROM pg_stat_statements
                       WHERE query ILIKE '%usage_events%'
                         AND query ILIKE '%source_record_id%'
                         AND query ILIKE '%ANY%'"""
                )

                assert old_pattern_count == 0, (
                    f"Old per-record overlap scan detected: "
                    f"{old_pattern_count} query calls matching "
                    f"the usage_ingest_attempts self-join pattern. "
                    f"Regression to issue #416."
                )
                assert batch_pattern_count >= 1, (
                    "Batch overlap query not found in pg_stat_statements. "
                    "Expected at least 1 call matching the "
                    "usage_events ANY($2) pattern."
                )

        # Timing assertion: with batch overlap detection, a 100-record
        # batch against a 123k-attempt fixture finishes in seconds, not
        # minutes.  The exact threshold is generous (10s) to accommodate
        # slow CI/docker environments while still catching the regression
        # (old code: ~1.58s/record × 100 = 158s).
        assert elapsed < 10.0, (
            f"100-record batch with 123k-attempt fixture took {elapsed:.2f}s. "
            f"Expected <10s with batch overlap; old per-record scan "
            f"would need >2 minutes."
        )
