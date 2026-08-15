"""Integration tests for the reporting-ingestion write path (issue #479).

Runs against the docker-compose Postgres (port 5433) and verifies the actual
database-enforced guarantees of ``POST /api/v1/reporting/ingest/deliveries``:

* the ``reporting_deliveries`` UNIQUE (provider, delivery_id) constraint plus
  ``ON CONFLICT DO NOTHING`` absorb a redelivery — exactly one row;
* the ``delivery_state_trails`` UNIQUE
  (provider, delivery_id, state, occurred_at) constraint plus
  ``ON CONFLICT DO NOTHING`` keep the trail to one entry per first delivery;
* the UNIQUE constraints are enforced at the raw-SQL level (a second direct
  INSERT of the same key is rejected).

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(defaulting to the ``docker-compose.test.yml`` service).  The test skips
gracefully when the database is not reachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

# Matches the GATEWAY_API_KEY pinned by tests/conftest.py; the seeded
# collector credential hash and the bearer token are both derived from it so
# the API-key middleware and require_collector_token both pass.
_API_KEY = "test-api-key"


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
    """
    from fastapi import Request
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
        assert row["payload"]["repository"] == "acme/backend"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_enforced_at_sql_level(db_pool: asyncpg.Pool) -> None:
    """A second direct INSERT of the same (provider, delivery_id) is rejected."""
    delivery_id = f"delivery-{uuid.uuid4()}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reporting_deliveries"
            " (provider, delivery_id, event_type, payload)"
            " VALUES ($1, $2, $3, $4::jsonb)",
            "github",
            delivery_id,
            "normalized",
            '{"repository": "acme/backend"}',
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO reporting_deliveries"
                " (provider, delivery_id, event_type, payload)"
                " VALUES ($1, $2, $3, $4::jsonb)",
                "github",
                delivery_id,
                "normalized",
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
