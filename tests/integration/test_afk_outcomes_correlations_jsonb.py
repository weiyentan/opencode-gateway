"""Real-PostgreSQL integration tests for the unresolved-correlation read path.

Issue #769: plain asyncpg returns JSONB columns as JSON-encoded ``str`` (no
type codec is registered — see ``app/api/closure_relationships.py``), so a
real ``unresolved_correlations`` row arrives with ``candidates`` as ``'[]'``
and ``evidence`` as a serialized JSON string.  The AFK Outcomes REST API must
decode those values before response validation, or the endpoint returns
HTTP 500 for unmatched rows and silently drops stored evidence.

This module inserts real rows (JSONB values written as JSON strings, exactly
like the production writer) into a live PostgreSQL database and reads them
back through the real HTTP endpoint and the normal asyncpg pool path.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_outcomes_correlations_jsonb.py -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(``GATEWAY_DATABASE_*``), defaulting to the ``docker-compose.test.yml``
service.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Request

from tests.integration._disposable_postgres import DatabaseTarget, verify_disposable_target

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_TARGET = DatabaseTarget.from_env()

_API_KEY = "test-api-key"
_ENDPOINT = "/api/v1/afk-outcomes/correlations"

_DROP_ALL_SQL = (
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
    "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)


def _dsn() -> str:
    return _TARGET.dsn()


@pytest.fixture(scope="module")
def _verified_disposable_database() -> DatabaseTarget:
    """Fail closed unless the target is an explicitly disposable PostgreSQL 16 DB."""
    return asyncio.run(verify_disposable_target(_TARGET))


@pytest.fixture(scope="module")
def _migrated_schema(_verified_disposable_database: DatabaseTarget) -> None:
    """Migrate the test database to ``head`` using the unmodified revision chain."""

    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        alembic.command.upgrade(alembic_cfg, "head")

    async def _drop_all() -> None:
        conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
        try:
            await conn.execute(_DROP_ALL_SQL)
        finally:
            await conn.close()

    try:
        asyncio.run(asyncio.to_thread(_upgrade))
    except Exception:
        asyncio.run(_drop_all())
        asyncio.run(asyncio.to_thread(_upgrade))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(_DROP_ALL_SQL)
    await pool.close()


def _build_client(db_pool: asyncpg.Pool):
    """Build a FastAPI app connected to the real integration DB pool.

    ``Request`` must be imported at module level (never locally): FastAPI
    resolves the string annotation of the ``get_session`` override through
    the defining module's globals, and a local import would make ``request``
    fall back to a query parameter (422 on every route).
    """
    from httpx import ASGITransport, AsyncClient

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

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )


def _run_id(n: int) -> str:
    """Return a 26-char run identifier in the ``afk_runs.afk_run_id`` shape."""
    return f"01J7690{n:019d}"


async def _insert_unresolved(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    repository: str,
    reason: str,
    candidates: list[str],
    evidence: list[dict],
) -> None:
    """Persist one run-level unresolved correlation, as the real writer does.

    The JSONB parameters are JSON-encoded strings — plain asyncpg reads them
    back as strings, which is the representation under test.
    """
    await conn.execute(
        "INSERT INTO unresolved_correlations"
        " (provider, repository, entity_type, external_id, afk_run_id, method,"
        "  reason, correlation_confidence, candidates, evidence,"
        "  resolver_version, created_at)"
        " VALUES ('github', $1, 'afk_run', $2, $2, $3, $4, 0.0,"
        "         $5::jsonb, $6::jsonb, '2', now())",
        repository,
        run_id,
        reason,
        reason,
        json.dumps(candidates),
        json.dumps(evidence),
    )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_unresolved_correlations_jsonb_read_through_rest(
    db_pool: asyncpg.Pool,
) -> None:
    """JSONB candidates/evidence persisted in PostgreSQL read back correctly.

    Covers the two row kinds through the real asyncpg pool path:

    * unmatched — ``candidates`` stored as ``'[]'`` must decode to ``[]``
      (previously HTTP 500), with evidence provenance preserved;
    * ambiguous — a populated candidate array must decode to ``list[str]``
      retaining every value.
    """
    repository = f"it769/{uuid.uuid4().hex[:8]}"
    unmatched_run = _run_id(1)
    ambiguous_run = _run_id(2)

    unmatched_evidence = [
        {
            "kind": "title_match",
            "source_entity_id": "change_request:300",
            "detail": "title=Fix caching bug",
            "weight": 0.25,
        }
    ]
    ambiguous_evidence = [
        {
            "kind": "branch_name",
            "source_entity_id": "issue:42",
            "detail": "afk-42",
            "weight": 0.5,
        }
    ]

    async with db_pool.acquire() as conn:
        await _insert_unresolved(
            conn,
            run_id=unmatched_run,
            repository=repository,
            reason="unmatched",
            candidates=[],
            evidence=unmatched_evidence,
        )
        await _insert_unresolved(
            conn,
            run_id=ambiguous_run,
            repository=repository,
            reason="ambiguous",
            candidates=["change_request:300", "change_request:310"],
            evidence=ambiguous_evidence,
        )

        # Guard: the stored JSONB values arrive through plain asyncpg as
        # JSON-encoded strings — the exact production representation this
        # regression covers.
        raw = await conn.fetchrow(
            "SELECT candidates, evidence FROM unresolved_correlations"
            " WHERE afk_run_id = $1",
            unmatched_run,
        )
        assert raw is not None
        assert raw["candidates"] == "[]"
        assert json.loads(raw["evidence"]) == unmatched_evidence

    try:
        async with _build_client(db_pool) as client:
            # ── filtered: unmatched rows are readable, not HTTP 500 ──────
            filtered = await client.get(
                _ENDPOINT, params={"reason": "unmatched", "limit": 1000}
            )
            assert filtered.status_code == 200
            filtered_data = filtered.json()["data"]
            assert filtered_data["limit"] == 1000
            assert filtered_data["offset"] == 0
            assert filtered_data["total"] >= 1

            unmatched_item = next(
                item
                for item in filtered_data["items"]
                if item["entity_id"] == f"afk_run:{unmatched_run}"
            )
            assert unmatched_item["reason"] == "unmatched"
            assert unmatched_item["candidates"] == []
            assert unmatched_item["evidence"] == unmatched_evidence
            # Migration 0027 run identity: persisted rows always carry their run.
            assert unmatched_item["afk_run_id"] == unmatched_run
            assert unmatched_item["provisional"] is True

            # ── unfiltered: populated candidates retain every value ──────
            unfiltered = await client.get(_ENDPOINT, params={"limit": 1000})
            assert unfiltered.status_code == 200
            unfiltered_data = unfiltered.json()["data"]
            assert unfiltered_data["limit"] == 1000
            assert unfiltered_data["offset"] == 0
            assert unfiltered_data["total"] >= 2

            ambiguous_item = next(
                item
                for item in unfiltered_data["items"]
                if item["entity_id"] == f"afk_run:{ambiguous_run}"
            )
            assert ambiguous_item["reason"] == "ambiguous"
            assert ambiguous_item["candidates"] == [
                "change_request:300",
                "change_request:310",
            ]
            assert ambiguous_item["evidence"] == ambiguous_evidence
            assert ambiguous_item["afk_run_id"] == ambiguous_run
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM unresolved_correlations WHERE repository = $1",
                repository,
            )
