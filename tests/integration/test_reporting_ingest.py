"""Integration tests for the reporting-ingestion write path (issue #479).

Runs against an isolated, disposable PostgreSQL 16 database and verifies the
actual database-enforced guarantees of
``POST /api/v1/reporting/ingest/deliveries``:

* the ``reporting_deliveries`` UNIQUE (provider, delivery_id) constraint plus
  ``ON CONFLICT DO NOTHING`` absorb a redelivery — exactly one row;
* the ``delivery_state_trails`` UNIQUE
  (provider, delivery_id, state, occurred_at) constraint plus
  ``ON CONFLICT DO NOTHING`` keep the trail to one entry per first delivery;
* the UNIQUE constraints are enforced at the raw-SQL level (a second direct
  INSERT of the same key is rejected).

Prerequisites
-------------
This suite migrates the schema and can drop every table in the ``public``
schema, so it refuses to run unless the target is positively verified as a
disposable PostgreSQL 16 database.  Start one outside Kubernetes before
running::

    docker run --rm -d --name opencode-gateway-test-db \\
        -e POSTGRES_DB=opencode_gateway_test \\
        -e POSTGRES_USER=opencode_test \\
        -e POSTGRES_PASSWORD=opencode_test \\
        -p 5433:5432 postgres:16-alpine

    GATEWAY_ENV=development GATEWAY_TEST_DISPOSABLE_DB=1 \\
        pytest tests/integration/test_reporting_ingest.py -v -m integration

    docker stop opencode-gateway-test-db

Connection parameters come from the standard Gateway environment variables
(defaulting to ``localhost:5433``).  ``GATEWAY_TEST_DISPOSABLE_DB=1`` is the
operator's explicit attestation that the database is disposable; missing
attestation, an unreachable server, a non-test database name, a Kubernetes
target, or a non-PostgreSQL-16 server fails closed instead of skipping.  CI
runs this suite against a dedicated ``postgres:16`` service container.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi import Request

from app.core.identity import hash_token
from tests.integration._disposable_postgres import (
    DatabaseTarget,
    verify_disposable_target,
)

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

# Resolved once at import (never connects); the safety preflight in
# ``_verified_disposable_database`` verifies it before any destructive step.
_TARGET = DatabaseTarget.from_env()

_DROP_ALL_SQL = (
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
    "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

# Matches the GATEWAY_API_KEY pinned by tests/conftest.py; the seeded
# collector credential hash and the bearer token are both derived from it so
# the API-key middleware and require_collector_token both pass.
_API_KEY = "test-api-key"


def _dsn() -> str:
    return _TARGET.dsn()


async def _drop_all_tables() -> None:
    """Drop every public table — the retry path for a corrupt schema state."""
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        await conn.execute(_DROP_ALL_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def _verified_disposable_database() -> DatabaseTarget:
    """Fail closed unless the target is an explicitly disposable PostgreSQL 16 DB.

    Runs before ``_migrated_schema`` (and therefore before any destructive
    migration reset).  A missing attestation, unreachable server, non-test
    database name, Kubernetes target, or wrong server version raises an error
    — never a skip, so CI cannot go green on an unverified or skipped target.
    """
    return asyncio.run(verify_disposable_target(_TARGET))


@pytest.fixture(scope="module")
def _migrated_schema(_verified_disposable_database: DatabaseTarget) -> None:
    """Migrate the test database to ``head`` once per module.

    Deliberately synchronous: alembic's ``env.py`` calls ``asyncio.run``,
    which cannot run inside the pytest-asyncio event loop.  A failed first
    attempt (e.g. leftover partial tables) drops every public table and
    retries from scratch.  The drop only runs after
    ``_verified_disposable_database`` has proven the target disposable.
    """
    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    try:
        alembic.command.upgrade(alembic_cfg, "head")
    except Exception:
        asyncio.run(_drop_all_tables())
        alembic.command.upgrade(alembic_cfg, "head")


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    """Function-scoped pool on the migrated test database.

    pytest-asyncio runs every async test in a fresh event loop, and an
    asyncpg pool is bound to the loop that created it.  Teardown removes the
    rows this suite writes so each test starts from a clean slate; it never
    drops tables, so it cannot destroy state another suite may still need.
    """
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE reporting_deliveries, delivery_state_trails CASCADE"
        )
        await conn.execute("DELETE FROM collector_credentials")
        await conn.execute("DELETE FROM opencode_clients")
    await pool.close()


# ── Builders ─────────────────────────────────────────────────────────────────


def _make_payload(
    *,
    provider: str = "github",
    delivery_id: str,
    event_type: str = "normalized",
    occurred_at: datetime | None = None,
    payload: dict | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "deliveries": [
            {
                "provider": provider,
                "delivery_id": delivery_id,
                "event_type": event_type,
                "occurred_at": (occurred_at or _utcnow()).isoformat(),
                "payload": payload if payload is not None else {"repository": "acme/backend"},
            }
        ],
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _seed_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed an active client + a collector credential for the test API key."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"cli-{uuid.uuid4()}",
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )
    return client_id, credential_id


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

    Overrides ``get_session`` to acquire/release a real connection and sends
    the bearer token matching both the API-key middleware and the seeded
    collector credential (``hash_token("test-api-key")``).

    ``Request`` is imported at module level (not inside this function): the
    module uses ``from __future__ import annotations``, so the override's
    ``request: Request`` annotation is a string that FastAPI resolves
    against this module's globals — without a module-level import it is
    misread as a query parameter and the request fails with a 422.
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


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_delivery_twice_persists_one_row(db_pool: asyncpg.Pool) -> None:
    """Posting the same delivery twice yields exactly one row in each table."""
    delivery_id = f"delivery-{uuid.uuid4()}"
    async with db_pool.acquire() as conn:
        client_id, _credential_id = await _seed_client(conn)

    client = _build_app(db_pool)
    async with client as c:
        first = await c.post(
            "/api/v1/reporting/ingest/deliveries", json=_make_payload(delivery_id=delivery_id)
        )
        second = await c.post(
            "/api/v1/reporting/ingest/deliveries", json=_make_payload(delivery_id=delivery_id)
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["results"][0]["status"] == "accepted"
    assert second.json()["data"]["results"][0]["status"] == "duplicate"

    async with db_pool.acquire() as conn:
        delivery_count = await conn.fetchval(
            "SELECT COUNT(*) FROM reporting_deliveries WHERE delivery_id = $1",
            delivery_id,
        )
        assert delivery_count == 1, f"expected 1 delivery row, got {delivery_count}"

        trail_count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_state_trails WHERE delivery_id = $1",
            delivery_id,
        )
        assert trail_count == 1, f"expected 1 trail row, got {trail_count}"

        row = await conn.fetchrow(
            "SELECT provider, delivery_id, event_type, client_id, received_at, payload "
            "FROM reporting_deliveries WHERE delivery_id = $1",
            delivery_id,
        )
        assert row["provider"] == "github"
        assert row["event_type"] == "normalized"
        assert row["client_id"] == client_id
        # asyncpg returns ``jsonb`` as text unless the pool installs a codec.
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["repository"] == "acme/backend"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_enforced_at_sql_level(db_pool: asyncpg.Pool) -> None:
    """A second direct INSERT of the same (provider, delivery_id) is rejected."""
    delivery_id = f"delivery-{uuid.uuid4()}"
    occurred_at = _utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reporting_deliveries"
            " (provider, delivery_id, event_type, occurred_at, payload)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            "github",
            delivery_id,
            "normalized",
            occurred_at,
            '{"repository": "acme/backend"}',
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO reporting_deliveries"
                " (provider, delivery_id, event_type, occurred_at, payload)"
                " VALUES ($1, $2, $3, $4, $5::jsonb)",
                "github",
                delivery_id,
                "normalized",
                occurred_at,
                '{"repository": "acme/backend"}',
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trail_unique_constraint_enforced_at_sql_level(
    db_pool: asyncpg.Pool,
) -> None:
    """A second direct INSERT of the same trail key is rejected."""
    delivery_id = f"delivery-{uuid.uuid4()}"
    occurred_at = _utcnow()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO delivery_state_trails"
            " (provider, delivery_id, state, occurred_at)"
            " VALUES ($1, $2, $3, $4)",
            "github",
            delivery_id,
            "persisted",
            occurred_at,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO delivery_state_trails"
                " (provider, delivery_id, state, occurred_at)"
                " VALUES ($1, $2, $3, $4)",
                "github",
                delivery_id,
                "persisted",
                occurred_at,
            )
