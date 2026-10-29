"""Opt-in production-scale overlap benchmark for canonical ingest (issue #835).

Classification
--------------
This module is an **optional performance benchmark**, not a required pull-request
test.  Ordinary PR CI does **not** exercise it: the module carries the
``profiling`` marker (excluded by the default ``pytest`` addopts and by the
``-m "not integration"`` unit run) and is deliberately absent from every
required CI job in ``.github/workflows/ci.yml``.  It runs only when explicitly
requested, either through the ``workflow_dispatch``-only
``.github/workflows/benchmark-overlap.yml`` workflow or manually as documented
below.

What it proves
--------------
Against a production-shaped ``usage_ingest_attempts`` fixture of
``ATTEMPTS_FIXTURE_COUNT`` (~123k) rows, a ``BATCH_SIZE`` (100) record ingest
batch must:

* complete within ``REQUEST_BUDGET_SECONDS`` (20s, the Gateway request timeout);
* execute **exactly one** set-based historical overlap query over
  ``usage_events`` (never the old per-record ``usage_ingest_attempts``
  self-join);
* leave session, canonical-event, rollup, and raw-record accounting untouched
  when the batch is quarantined for identity overlap.

Query counts are measured deterministically at the connection boundary
(:class:`_CountingConnection`), so the benchmark does not depend on the
``pg_stat_statements`` extension or on server preload configuration.  Every
test prints ``[overlap-benchmark]`` metric lines; run with ``-s`` to see them
in CI logs.

Manual entrypoint
-----------------
Start a disposable PostgreSQL 16 container outside Kubernetes, then run::

    docker run --rm -d --name opencode-gateway-test-db \\
        -e POSTGRES_DB=opencode_gateway_benchmark_test \\
        -e POSTGRES_USER=test \\
        -e POSTGRES_PASSWORD=test \\
        -p 5433:5432 postgres:16-alpine

    GATEWAY_ENV=development \\
    GATEWAY_TEST_DISPOSABLE_DB=1 \\
    GATEWAY_RUN_OVERLAP_BENCHMARK=1 \\
    GATEWAY_DATABASE_HOST=localhost \\
    GATEWAY_DATABASE_PORT=5433 \\
    GATEWAY_DATABASE_NAME=opencode_gateway_benchmark_test \\
    GATEWAY_DATABASE_USER=test \\
    GATEWAY_DATABASE_PASSWORD=test \\
        pytest tests/integration/test_ingest_batch_overlap_perf.py \\
            -v -m profiling -s -p no:cacheprovider

    docker stop opencode-gateway-test-db

Safety
------
This benchmark seeds ~123k rows, migrates the schema, and drops every table in
the ``public`` schema on teardown.  ``verify_disposable_target`` therefore runs
**before** any write and fails closed unless the target is positively attested
as disposable: ``GATEWAY_TEST_DISPOSABLE_DB=1``, an approved ``_test``
database name (``opencode_gateway_benchmark_test`` or
``opencode_gateway_disposable_test``), a non-production ``GATEWAY_ENV``, a
non-Kubernetes process/host, and PostgreSQL 16.  When the benchmark is
explicitly requested, an unreachable or non-disposable database raises
:class:`DisposableDatabaseError` and surfaces as a test error — it is never
converted into a green skip.

Without ``GATEWAY_RUN_OVERLAP_BENCHMARK=1`` the module skips with an explicit
"not requested" reason; that is the only skip path and it never masks a
requested benchmark run.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from tests.integration._disposable_postgres import (
    DatabaseTarget,
    verify_disposable_target,
)

# ── Repository paths ─────────────────────────────────────────────────────
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent

# Resolved once at import (never connects); the safety preflight verifies it
# before any migration, fixture insert, or teardown drop.
_TARGET = DatabaseTarget.from_env()

# ── Request budget (seconds) ────────────────────────────────────────────
# The Gateway's total_request_timeout_seconds defaults to 20 in config.py.
# A 100-record batch with batch-level overlap detection should complete in
# well under 5 seconds even with a 123k-row fixture; the old per-record
# approach would need >2 minutes and time out.
REQUEST_BUDGET_SECONDS = 20.0

# ── Production-scale fixture size ────────────────────────────────────────
# The production evidence from issue #416 reports ~123k usage_ingest_attempts
# rows.  We seed at this scale so the benchmark mirrors the production query
# plan.
ATTEMPTS_FIXTURE_COUNT = 123_000

# ── Batch size for ingest test ───────────────────────────────────────────
BATCH_SIZE = 100

# ── Opt-in switch ────────────────────────────────────────────────────────
# The benchmark seeds ~123k rows and resets the public schema; it must never
# run incidentally (e.g. from a broad ``pytest tests/integration`` invocation).
BENCHMARK_OPT_IN_ENV = "GATEWAY_RUN_OVERLAP_BENCHMARK"

# ── Query-count budget ───────────────────────────────────────────────────
# Exactly one batch-level overlap query is expected per 100-record batch.
OVERLAP_QUERY_BUDGET = 1

_DROP_ALL_SQL = (
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
    "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)

# Applied to every test in this module: ``integration`` for the shared marker
# vocabulary and ``profiling`` so the default test run excludes the benchmark.
pytestmark = [pytest.mark.integration, pytest.mark.profiling]


# ══════════════════════════════════════════════════════════════════════════
#  Query instrumentation
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class OverlapQueryCounter:
    """Counts overlap-related SQL executed for one ingest request."""

    overlap_queries: int = 0
    legacy_overlap_queries: int = 0
    total_queries: int = 0


def _normalize_query(query: Any) -> str:
    return " ".join(str(query).split())


def _record_query(counter: OverlapQueryCounter, query: Any) -> None:
    text = _normalize_query(query)
    counter.total_queries += 1
    # The batch-level ``check_batch_overlap`` query compares the whole batch
    # with ``source_record_id = ANY($n::text[])`` in one statement.
    if "usage_events" in text and "= ANY(" in text and "source_record_id" in text:
        counter.overlap_queries += 1
    # The removed per-record scan self-joined usage_ingest_attempts on
    # original_source_record_id.  Any match is a regression to issue #416.
    if (
        "usage_ingest_attempts" in text
        and "JOIN" in text.upper()
        and "original_source_record_id" in text
    ):
        counter.legacy_overlap_queries += 1


class _CountingConnection:
    """Transparent asyncpg connection proxy that records overlap queries.

    FastAPI caches ``get_session`` per request, so the auth dependency and the
    ingest handler share this proxy; every query the request issues passes
    through :meth:`_record` exactly once.
    """

    def __init__(self, conn: asyncpg.Connection, counter: OverlapQueryCounter) -> None:
        self._conn = conn
        self._counter = counter

    async def fetch(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        _record_query(self._counter, query)
        return await self._conn.fetch(query, *args, **kwargs)

    async def fetchrow(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        _record_query(self._counter, query)
        return await self._conn.fetchrow(query, *args, **kwargs)

    async def fetchval(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        _record_query(self._counter, query)
        return await self._conn.fetchval(query, *args, **kwargs)

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        _record_query(self._counter, query)
        return await self._conn.execute(query, *args, **kwargs)

    async def executemany(self, query: Any, args: Any, **kwargs: Any) -> Any:
        _record_query(self._counter, query)
        return await self._conn.executemany(query, args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # transaction(), copy_records_to_table(), is_closed(), etc.
        return getattr(self._conn, name)


def _emit_metrics(label: str, **values: Any) -> None:
    """Print one visible benchmark metric line (requires pytest ``-s``)."""
    rendered = ", ".join(f"{key}={value}" for key, value in values.items())
    print(f"[overlap-benchmark] {label}: {rendered}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _dsn() -> str:
    """Build an asyncpg DSN from the verified disposable target."""
    return _TARGET.dsn()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ts() -> datetime:
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


async def _drop_all_tables() -> None:
    """Drop every public table — the retry path for a corrupt schema state."""
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        await conn.execute(_DROP_ALL_SQL)
    finally:
        await conn.close()


def _migration_env() -> dict[str, str]:
    """Environment for the Alembic subprocess — point app settings at the target."""
    env = dict(os.environ)
    env.update(
        {
            "GATEWAY_ENV": "development",
            "GATEWAY_DATABASE_HOST": _TARGET.host,
            "GATEWAY_DATABASE_PORT": str(_TARGET.port),
            "GATEWAY_DATABASE_NAME": _TARGET.name,
            "GATEWAY_DATABASE_USER": _TARGET.user,
            "GATEWAY_DATABASE_PASSWORD": _TARGET.password,
        }
    )
    return env


def _run_alembic_upgrade() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_PROJ_ROOT,
        env=_migration_env(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def _benchmark_opt_in() -> None:
    """Require the explicit benchmark opt-in before touching the database."""
    if os.environ.get(BENCHMARK_OPT_IN_ENV) != "1":
        pytest.skip(
            "Optional production-scale overlap benchmark not requested. "
            f"Set {BENCHMARK_OPT_IN_ENV}=1 (plus the disposable-Postgres "
            "environment) to run it explicitly. This skip never applies to a "
            "requested benchmark run."
        )


@pytest.fixture(scope="module")
def _verified_disposable_database(_benchmark_opt_in: None) -> DatabaseTarget:
    """Fail closed unless the target is an explicitly disposable PostgreSQL 16 DB.

    Runs before ``_migrated_schema``, ``_seed``, and the per-test pools, so no
    migration, fixture insert, or teardown drop can execute against an
    unverified target.  A missing attestation, unreachable server, non-test
    database name, Kubernetes target, or wrong server version raises an error
    — never a skip, so a requested benchmark cannot go green without a real
    disposable database.
    """
    return asyncio.run(verify_disposable_target(_TARGET))


@pytest.fixture(scope="module")
def _migrated_schema(_verified_disposable_database: DatabaseTarget) -> None:
    """Migrate the disposable database to ``head`` once per benchmark run.

    Deliberately synchronous: ``alembic/env.py`` calls ``asyncio.run``, which
    cannot run inside the pytest-asyncio event loop, so the migration runs in
    a subprocess.  A failed first attempt (e.g. leftover partial tables) drops
    every public table and retries from scratch.
    """
    result = _run_alembic_upgrade()
    if result.returncode != 0:
        asyncio.run(_drop_all_tables())
        retry = _run_alembic_upgrade()
        if retry.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade head failed against {_TARGET.describe()}\n"
                f"first attempt:\n{result.stdout}\n{result.stderr}\n"
                f"retry after drop:\n{retry.stdout}\n{retry.stderr}"
            )


# ══════════════════════════════════════════════════════════════════════════
#  Fixture data — production-scale seed
# ══════════════════════════════════════════════════════════════════════════


async def _seed_database() -> dict:
    """Seed production-scale data for an existing identity.

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
    conn = await asyncpg.connect(dsn=_dsn(), timeout=30)
    try:
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
        chunk_size_limit = 1000
        base_ts = _mk_ts()

        for offset in range(0, ATTEMPTS_FIXTURE_COUNT, chunk_size_limit):
            chunk_size = min(chunk_size_limit, ATTEMPTS_FIXTURE_COUNT - offset)
            placeholders = []
            params = []
            for i in range(chunk_size):
                index = offset + i
                p = i * 8
                placeholders.append(
                    f"(${p + 1}, ${p + 2}, ${p + 3}, ${p + 4}, "
                    f"${p + 5}, ${p + 6}, ${p + 7}, ${p + 8})"
                )
                params.extend([
                    uuid.uuid4(),                    # id
                    None,                            # usage_event_id
                    existing_identity_id,            # source_identity_id
                    f"existing-rec-{index:06d}",     # original_source_record_id
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

        # Verify the seed count
        attempts_count = await conn.fetchval(
            "SELECT COUNT(*) FROM usage_ingest_attempts"
        )
        assert attempts_count == ATTEMPTS_FIXTURE_COUNT, (
            f"Expected {ATTEMPTS_FIXTURE_COUNT} attempts, got {attempts_count}"
        )

        return {
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
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def _seed(_migrated_schema: None) -> dict:
    """Seed the production-scale fixture once, then drop it on teardown."""
    seed_data = asyncio.run(_seed_database())
    try:
        yield seed_data
    finally:
        # The disposable target was verified before the seed; teardown can
        # safely reset the public schema for the next run.
        asyncio.run(_drop_all_tables())


@pytest.fixture
async def db_pool(_verified_disposable_database: DatabaseTarget) -> asyncpg.Pool:
    """Create a function-scoped asyncpg pool bound to the current test loop."""
    pool = await asyncpg.create_pool(
        dsn=_dsn(),
        min_size=2,
        max_size=5,
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


# ══════════════════════════════════════════════════════════════════════════
#  Tests
# ══════════════════════════════════════════════════════════════════════════


def _build_ingest_app_for_integration(
    db_pool: asyncpg.Pool,
    counter: OverlapQueryCounter,
    *,
    api_key: str | None = "test-api-key",
) -> AsyncClient:
    """Build a FastAPI AsyncClient connected to the real benchmark DB pool.

    The ``get_session`` override yields an instrumented connection so every
    query issued by the request is classified for the overlap-query metric.
    Disables the API-key middleware when ``api_key`` is ``None``; provides an
    ``Authorization: Bearer <api_key>`` header otherwise so the middleware
    passes — the bearer token must exactly match ``GATEWAY_API_KEY``, pinned
    to ``test-api-key`` by ``tests/conftest.py`` (the seeded collector
    credential hash matches it), so both auth layers succeed.
    ``GATEWAY_ENV=development`` does not bypass API-key checks.
    """
    from app.core.factory import create_app
    from app.db.session import get_session

    os.environ.setdefault("GATEWAY_ENV", "development")

    app = create_app(configure_logging=False)

    async def _override_get_session(request: Request):
        conn = await db_pool.acquire()
        try:
            yield _CountingConnection(conn, counter)
        finally:
            await db_pool.release(conn)

    app.dependency_overrides[get_session] = _override_get_session

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def _insert_source_identity(
    conn: asyncpg.Connection,
    *,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
) -> uuid.UUID:
    """Register the already-known identity for a second source database.

    The ingest path's ``resolve_canonical_identity`` SELECT expects
    ``canonical_parent_id`` on the row it returns; pre-registering the
    identity keeps this benchmark focused on the batch-overlap and quarantine
    accounting contract rather than on first-contact identity discovery.
    """
    identity_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO source_identities
           (id, client_id, collector_source_id, is_canonical, created_at)
           VALUES ($1, $2, $3, true, $4)""",
        identity_id,
        client_id,
        str(source_db_id),
        _utcnow(),
    )
    return identity_id


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


@pytest.mark.asyncio
class TestIngestBatchOverlapPerformance:
    """Opt-in benchmark for batch-level overlap detection (issue #416)."""

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
        counter = OverlapQueryCounter()

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

        client = _build_ingest_app_for_integration(db_pool, counter)

        start = time.monotonic()
        async with client as c:
            response = await c.post("/ingest", json=payload)
        elapsed = time.monotonic() - start

        data = response.json().get("data", {}) if response.status_code == 200 else {}
        _emit_metrics(
            "non_overlapping_batch_passed",
            fixture_attempt_rows=ATTEMPTS_FIXTURE_COUNT,
            batch_size=BATCH_SIZE,
            elapsed_seconds=f"{elapsed:.3f}",
            request_budget_seconds=REQUEST_BUDGET_SECONDS,
            overlap_queries=counter.overlap_queries,
            overlap_query_budget=OVERLAP_QUERY_BUDGET,
            legacy_overlap_queries=counter.legacy_overlap_queries,
            accepted=data.get("accepted_count"),
            rejected=data.get("rejected_count"),
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: "
            f"{response.json()}"
        )
        assert data["accepted_count"] == BATCH_SIZE, (
            f"Expected {BATCH_SIZE} accepted, got {data['accepted_count']}"
        )
        assert data["rejected_count"] == 0

        assert counter.overlap_queries == OVERLAP_QUERY_BUDGET, (
            f"Expected exactly {OVERLAP_QUERY_BUDGET} batch-level overlap query "
            f"for {BATCH_SIZE} records, counted {counter.overlap_queries}"
        )
        assert counter.legacy_overlap_queries == 0, (
            f"Legacy per-record overlap scan detected: "
            f"{counter.legacy_overlap_queries} queries"
        )

        assert elapsed < REQUEST_BUDGET_SECONDS, (
            f"Batch of {BATCH_SIZE} records took {elapsed:.2f}s — "
            f"exceeds {REQUEST_BUDGET_SECONDS}s budget. "
            f"Per-record overlap scan regression suspected."
        )

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
        counter = OverlapQueryCounter()
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
            await _insert_source_identity(
                conn,
                client_id=seed["client_id"],
                source_db_id=new_source_db_id,
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

        client = _build_ingest_app_for_integration(db_pool, counter)

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

        _emit_metrics(
            "quarantined_batch_accounting_effects",
            fixture_attempt_rows=ATTEMPTS_FIXTURE_COUNT,
            batch_size=BATCH_SIZE,
            overlap_queries=counter.overlap_queries,
            overlap_query_budget=OVERLAP_QUERY_BUDGET,
            legacy_overlap_queries=counter.legacy_overlap_queries,
            sessions_delta=after_sessions - before_sessions,
            usage_events_delta=after_events - before_events,
            client_project_rollup_delta=after_rollup - before_rollup,
            opencode_usage_records_delta=after_records - before_records,
            quarantines_delta=after_quarantines - before_quarantines,
            quarantined_attempts=attempt_count,
        )

        assert counter.overlap_queries == OVERLAP_QUERY_BUDGET, (
            f"Expected exactly {OVERLAP_QUERY_BUDGET} batch-level overlap query "
            f"for {BATCH_SIZE} records, counted {counter.overlap_queries}"
        )
        assert counter.legacy_overlap_queries == 0, (
            f"Legacy per-record overlap scan detected: "
            f"{counter.legacy_overlap_queries} queries"
        )
        assert after_sessions == before_sessions
        assert after_events == before_events
        assert after_rollup == before_rollup
        assert after_records == before_records

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
        counter = OverlapQueryCounter()
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
            await _insert_source_identity(
                conn,
                client_id=seed["client_id"],
                source_db_id=second_source_db_id,
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

        client = _build_ingest_app_for_integration(db_pool, counter)
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

        _emit_metrics(
            "mixed_batch_outcomes",
            batch_size=len(mixed_records),
            overlap_queries=counter.overlap_queries,
            overlap_query_budget=OVERLAP_QUERY_BUDGET,
            legacy_overlap_queries=counter.legacy_overlap_queries,
            accepted=accepted_count,
            rejected=rejected_count,
        )

        assert counter.overlap_queries == OVERLAP_QUERY_BUDGET

    async def test_batch_overlap_uses_one_query_with_production_fixture(
        self, db_pool: asyncpg.Pool, _seed: dict
    ):
        """Verify that a non-overlapping 100-record batch performs exactly one
        query against ``usage_events`` for historical overlap detection.

        Query counting happens at the connection boundary: the ingest request's
        ``get_session`` dependency yields an instrumented connection, so the
        metric is independent of ``pg_stat_statements`` availability.  The
        timing assertion remains a second regression guard — the old
        per-record scan would need >2 minutes against the 123k-attempt fixture.
        """
        counter = OverlapQueryCounter()

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

        client = _build_ingest_app_for_integration(db_pool, counter)

        start = time.monotonic()
        async with client as c:
            response = await c.post("/ingest", json=payload)
        elapsed = time.monotonic() - start

        data = response.json().get("data", {}) if response.status_code == 200 else {}
        _emit_metrics(
            "overlap_query_count",
            fixture_attempt_rows=ATTEMPTS_FIXTURE_COUNT,
            batch_size=BATCH_SIZE,
            elapsed_seconds=f"{elapsed:.3f}",
            overlap_queries=counter.overlap_queries,
            overlap_query_budget=OVERLAP_QUERY_BUDGET,
            legacy_overlap_queries=counter.legacy_overlap_queries,
            total_queries=counter.total_queries,
            accepted=data.get("accepted_count"),
        )

        assert response.status_code == 200
        assert data["accepted_count"] == BATCH_SIZE

        assert counter.overlap_queries == OVERLAP_QUERY_BUDGET, (
            f"Expected exactly {OVERLAP_QUERY_BUDGET} batch-level overlap query "
            f"for {BATCH_SIZE} records, counted {counter.overlap_queries}"
        )
        assert counter.legacy_overlap_queries == 0, (
            f"Old per-record overlap scan detected: "
            f"{counter.legacy_overlap_queries} query calls matching "
            f"the usage_ingest_attempts self-join pattern. "
            f"Regression to issue #416."
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
