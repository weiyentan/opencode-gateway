"""Integration tests for the current-aggregate layer (issue #480).

Runs against the docker-compose Postgres (port 5433) and verifies the
database-enforced guarantees of the forward-only current aggregate:

* a delivery carrying a ``resource`` object produces exactly one
  ``reporting_resource_aggregates`` row keyed by the normalized identity;
* a late event (older ``occurred_at`` than the current last event) fills
  absent keys forward but never regresses state set by a newer event
  (explicit late-event non-regression);
* live-then-replay and replay-then-live ingestion converge on identical
  aggregate rows (payload + last_occurred_at + last_delivery_id);
* the UNIQUE identity constraint is enforced at the raw-SQL level.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v

The test skips gracefully when the database is not reachable.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from app.core.identity import hash_token
from app.core.reporting_aggregates import (
    ResourceIdentity,
    get_aggregate,
    normalize_repository_url,
)

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc

_API_KEY = "test-api-key"

_T1 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
_T1_5 = datetime(2026, 8, 14, 12, 30, 0, tzinfo=UTC)
_T2 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


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


def _resource(*, repository_url: str, resource_type: str, resource_number: str) -> dict:
    return {
        "repository_url": repository_url,
        "resource_type": resource_type,
        "resource_number": resource_number,
    }


def _make_payload(
    *,
    delivery_id: str,
    occurred_at: datetime,
    resource: dict,
    extra: dict | None = None,
    provider: str = "github",
) -> dict:
    return {
        "schema_version": "1.0",
        "deliveries": [
            {
                "provider": provider,
                "delivery_id": delivery_id,
                "event_type": "normalized",
                "occurred_at": occurred_at.isoformat(),
                "payload": {"resource": resource, **(extra or {})},
            }
        ],
    }


async def _seed_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
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
async def test_aggregate_row_created_after_ingest(db_pool: asyncpg.Pool) -> None:
    """Ingesting a delivery with a ``resource`` object creates one aggregate row."""
    async with db_pool.acquire() as conn:
        await _seed_client(conn)

    resource = _resource(
        repository_url="https://github.com/Acme/Backend/",
        resource_type="issue",
        resource_number="42",
    )
    client = _build_app(db_pool)
    async with client as c:
        response = await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=f"delivery-{uuid.uuid4()}",
                occurred_at=_T1,
                resource=resource,
                extra={"status": "open", "title": "in progress"},
            ),
        )
    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["status"] == "accepted"

    identity = ResourceIdentity(
        provider="github",
        repository_url=normalize_repository_url(resource["repository_url"]),
        resource_type="issue",
        resource_number="42",
    )
    async with db_pool.acquire() as conn:
        row = await get_aggregate(conn, identity)
    assert row is not None
    assert row["repository_url"] == "https://github.com/acme/backend"
    assert row["last_delivery_id"].startswith("delivery-")
    assert row["payload"]["status"] == "open"
    assert row["payload"]["title"] == "in progress"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_event_fills_absent_but_never_regresses(db_pool: asyncpg.Pool) -> None:
    """A stale delivery (older occurred_at) never regresses newer state."""
    async with db_pool.acquire() as conn:
        await _seed_client(conn)

    resource = _resource(
        repository_url="https://github.com/acme/backend",
        resource_type="issue",
        resource_number="7",
    )
    client = _build_app(db_pool)

    # Newer event first
    async with client as c:
        newer = await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=f"newer-{uuid.uuid4()}",
                occurred_at=_T2,
                resource=resource,
                extra={"status": "closed", "title": "done"},
            ),
        )
        # Late event (older occurred_at) with a conflicting field + a new field
        stale = await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=f"stale-{uuid.uuid4()}",
                occurred_at=_T1,
                resource=resource,
                extra={"status": "open", "title": "stale", "labels": ["bug"]},
            ),
        )
    assert newer.status_code == 200
    assert stale.status_code == 200

    identity = ResourceIdentity(
        provider="github",
        repository_url="https://github.com/acme/backend",
        resource_type="issue",
        resource_number="7",
    )
    async with db_pool.acquire() as conn:
        row = await get_aggregate(conn, identity)
    assert row is not None
    # conflicting fields stay at the newer event's values (no regression)
    assert row["payload"]["status"] == "closed"
    assert row["payload"]["title"] == "done"
    # absent key from the late event is filled forward
    assert row["payload"]["labels"] == ["bug"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_then_replay_equals_replay_then_live(db_pool: asyncpg.Pool) -> None:
    """Replay convergence: ingestion order does not affect the final aggregate."""
    async with db_pool.acquire() as conn:
        await _seed_client(conn)

    live = {
        "delivery_id": f"live-{uuid.uuid4()}",
        "occurred_at": _T2,
        "payload": {"status": "closed", "title": "done"},
    }
    replay = {
        "delivery_id": f"replay-{uuid.uuid4()}",
        "occurred_at": _T1,
        "payload": {"status": "open", "title": "in progress", "labels": ["bug"]},
    }
    # Order 2 needs its own fresh delivery_ids: reporting_deliveries dedups on
    # (provider, delivery_id), so reusing Order 1's ids would make conv-b's
    # deliveries duplicate no-ops instead of genuinely exercising the ingest path.
    live2 = {**live, "delivery_id": f"live-{uuid.uuid4()}"}
    replay2 = {**replay, "delivery_id": f"replay-{uuid.uuid4()}"}

    def _resource_for(number: str) -> dict:
        return _resource(
            repository_url="https://github.com/acme/backend",
            resource_type="pull_request",
            resource_number=number,
        )

    client = _build_app(db_pool)

    # Order 1: live then replay → resource "conv-a"
    async with client as c:
        await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=live["delivery_id"],
                occurred_at=live["occurred_at"],
                resource=_resource_for("conv-a"),
                extra=live["payload"],
            ),
        )
        await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=replay["delivery_id"],
                occurred_at=replay["occurred_at"],
                resource=_resource_for("conv-a"),
                extra=replay["payload"],
            ),
        )

    # Order 2: replay then live → resource "conv-b"
    async with client as c:
        await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=replay2["delivery_id"],
                occurred_at=replay2["occurred_at"],
                resource=_resource_for("conv-b"),
                extra=replay2["payload"],
            ),
        )
        await c.post(
            "/api/v1/reporting/ingest/deliveries",
            json=_make_payload(
                delivery_id=live2["delivery_id"],
                occurred_at=live2["occurred_at"],
                resource=_resource_for("conv-b"),
                extra=live2["payload"],
            ),
        )

    async with db_pool.acquire() as conn:
        row_a = await conn.fetchrow(
            "SELECT payload, last_occurred_at, last_delivery_id "
            "FROM reporting_resource_aggregates WHERE resource_number = 'conv-a'"
        )
        row_b = await conn.fetchrow(
            "SELECT payload, last_occurred_at, last_delivery_id "
            "FROM reporting_resource_aggregates WHERE resource_number = 'conv-b'"
        )

    assert row_a is not None and row_b is not None
    assert row_a["payload"] == row_b["payload"]
    assert row_a["last_occurred_at"] == row_b["last_occurred_at"]
    assert row_a["last_delivery_id"] == row_b["last_delivery_id"]

    # The converged state is the newer (live) event's values, with the
    # replay-only key filled forward.
    assert row_a["payload"] == {
        "status": "closed",
        "title": "done",
        "labels": ["bug"],
    }
    assert row_a["last_delivery_id"] == live["delivery_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_event_merge_converges_across_arrival_orders(
    db_pool: asyncpg.Pool,
) -> None:
    """3-event counterexample (review finding #1): per-key provenance converges.

    Three events with distinct ``occurred_at`` times write disjoint keys:
    ``e1`` (newest) writes ``x``, ``e2`` (oldest) writes ``y=2``, ``e3``
    (mid-stale) writes ``y=3``.  Without per-key provenance the arrival order
    ``e1,e2,e3`` yielded ``y=2`` while ``e1,e3,e2`` yielded ``y=3``.  With
    per-key provenance every order converges to ``{x: 1, y: 3}``.
    """
    async with db_pool.acquire() as conn:
        await _seed_client(conn)

    e1 = {"occurred_at": _T2, "extra": {"x": 1}}
    e2 = {"occurred_at": _T1, "extra": {"y": 2}}
    e3 = {"occurred_at": _T1_5, "extra": {"y": 3}}

    def _resource_for(number: str) -> dict:
        return _resource(
            repository_url="https://github.com/acme/backend",
            resource_type="pull_request",
            resource_number=number,
        )

    client = _build_app(db_pool)
    async with client as c:
        for i, order in enumerate(itertools.permutations([e1, e2, e3])):
            number = f"conv-3ev-{i}"
            for ev in order:
                await c.post(
                    "/api/v1/reporting/ingest/deliveries",
                    json=_make_payload(
                        # Unique delivery_id per posting: the delivery table is
                        # keyed by (provider, delivery_id), so each order needs
                        # its own ids to actually enrich its own resource.
                        delivery_id=f"d-{i}-{uuid.uuid4()}",
                        occurred_at=ev["occurred_at"],
                        resource=_resource_for(number),
                        extra=ev["extra"],
                    ),
                )

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM reporting_resource_aggregates "
            "WHERE resource_number LIKE 'conv-3ev-%' ORDER BY resource_number"
        )

    assert len(rows) == 6
    first = rows[0]["payload"]
    for row in rows:
        assert row["payload"] == first
    # The converged state takes the newest value per key: y written by e3
    # (newer than e2), x written by e1 — independent of arrival order.
    assert first["x"] == 1
    assert first["y"] == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_none_key_merge_converges_across_arrival_orders(
    db_pool: asyncpg.Pool,
) -> None:
    """F1-residual: a ``None``-valued key never makes the merge arrival-order dependent.

    ``e1`` (oldest) carries ``{a: None}``, ``e2`` (newest) carries
    ``{b: 1}``, ``e3`` (mid) carries ``{a: 7}``.  Before the fix, the order
    ``e1,e2,e3`` persisted ``a`` as ``None`` and then rejected ``e3``'s real
    ``a=7`` (the aggregate's global last event had advanced to ``e2``),
    yielding ``{a: None, b: 1}`` while other orders yielded ``{a: 7, b: 1}``.
    After the fix every order converges to ``{a: 7, b: 1}`` (``a`` written by
    ``e3``, ``b`` by ``e2``).
    """
    async with db_pool.acquire() as conn:
        await _seed_client(conn)

    e1 = {"occurred_at": _T1, "extra": {"a": None}}
    e2 = {"occurred_at": _T2, "extra": {"b": 1}}
    e3 = {"occurred_at": _T1_5, "extra": {"a": 7}}

    def _resource_for(number: str) -> dict:
        return _resource(
            repository_url="https://github.com/acme/backend",
            resource_type="pull_request",
            resource_number=number,
        )

    client = _build_app(db_pool)
    async with client as c:
        for i, order in enumerate(itertools.permutations([e1, e2, e3])):
            number = f"conv-none-{i}"
            for ev in order:
                # ``_make_payload`` merges ``extra`` verbatim, so ``a: None``
                # round-trips as JSON null into ``ReportingDeliveryIn.payload``
                # (a genuinely-present ``None``-valued key, not an omitted key).
                await c.post(
                    "/api/v1/reporting/ingest/deliveries",
                    json=_make_payload(
                        delivery_id=f"d-{i}-{uuid.uuid4()}",
                        occurred_at=ev["occurred_at"],
                        resource=_resource_for(number),
                        extra=ev["extra"],
                    ),
                )

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload, last_occurred_at FROM reporting_resource_aggregates "
            "WHERE resource_number LIKE 'conv-none-%' ORDER BY resource_number"
        )

    assert len(rows) == 6

    # The ``resource`` object is the aggregate identity and therefore carries
    # a different ``resource_number`` per permutation; strip it so the *state*
    # portion of each row can be compared for convergence.
    def _without_resource(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k != "resource"}

    first = _without_resource(rows[0]["payload"])
    first_last_occurred_at = rows[0]["last_occurred_at"]
    assert first == {"a": 7, "b": 1}
    for row in rows:
        assert _without_resource(row["payload"]) == first
        assert row["last_occurred_at"] == first_last_occurred_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_identity_constraint_enforced(db_pool: asyncpg.Pool) -> None:
    """A second direct INSERT of the same identity key is rejected."""
    async with db_pool.acquire() as conn:
        insert_sql = (
            "INSERT INTO reporting_resource_aggregates "
            "(provider, repository_url, resource_type, resource_number, "
            " last_occurred_at, last_delivery_id, payload) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)"
        )
        args = (
            "github",
            "https://github.com/acme/backend",
            "issue",
            "99",
            _T1,
            "delivery-x",
            '{"status": "open"}',
        )
        await conn.execute(insert_sql, *args)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert_sql, *args)
