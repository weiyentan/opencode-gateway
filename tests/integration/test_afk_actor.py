"""Integration tests for the original AFK trigger actor (issue #859).

Drives the real public API seam against an isolated, disposable PostgreSQL 16
database and verifies the immutable original-actor contract added on top of
the existing provisional AFK run lifecycle:

* ``POST /api/v1/afk/executions/runs`` accepts an optional provider-qualified
  ``actor`` (``github:login`` / ``gitlab:username``) and persists it on the
  canonical ``afk_run_id``.
* ``GET /api/v1/afk/executions/runs/{afk_run_id}/lifecycle`` reads the actor
  back from durable storage — a freshly built app instance (the restart
  analogue) sees the same run and actor.
* An identical retry returns the same ``afk_run_id`` and actor (200).
* A retry that changes the stored actor returns 409 and never replaces it;
  omitting a bound actor is likewise a conflict (non-erasure).
* Concurrent replays with different actors race safely: exactly one
  inserts/replays, the other is a conflict; the stored actor is never a blend.
* Runs provisioned without an actor (old clients) and pre-existing rows
  surface ``actor = None`` — never a fabricated value.
* The canonical ``GET /api/v1/afk/runs/{afk_run_id}`` readback surfaces the
  same stored actor without a separate store.

Prerequisites
-------------
This suite migrates the schema and can drop every table in the ``public``
schema, so it refuses to run unless the target is positively verified as a
disposable PostgreSQL 16 database::

    podman run --rm -d --name opencode-gateway-test-db \\
        -e POSTGRES_DB=opencode_gateway_disposable_test \\
        -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \\
        -p 5433:5432 postgres:16-alpine

    GATEWAY_ENV=development GATEWAY_TEST_DISPOSABLE_DB=1 \\
        pytest tests/integration/test_afk_actor.py -v -m integration

Fixture layout mirrors ``test_afk_logical_target.py``: migrations run in a
subprocess, and the ``db_pool`` fixture is function-scoped because asyncpg
pools are bound to the event loop that created them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
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

_TARGET = DatabaseTarget.from_env()

_API_KEY = "test-api-key"

# The dedicated AWX execution-binding client name — matches the constant in
# app.api.afk_executions (issue #550).
_AWX_CLIENT_NAME = "awx-execution-bindings"

_LIFECYCLE_PATH = "/api/v1/afk/executions/runs"

# Packages the uv-managed migration fallback must provide (the alembic env
# chain imports app.core.config + app.db.models, which need pydantic).
_UV_MIGRATION_PACKAGES = (
    "alembic",
    "sqlalchemy[asyncio]",
    "asyncpg",
    "pydantic",
    "pydantic-settings",
)

_DROP_ALL_SQL = (
    "DO $$ DECLARE r RECORD; BEGIN "
    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
    "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
    "END LOOP; END $$;"
)

_TEST_ACTOR = "github:octocat"


def _dsn() -> str:
    return _TARGET.dsn()


def _migration_env() -> dict[str, str]:
    """Environment for the migration subprocess — point app settings at the test DB."""
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


def _migration_commands(*args: str) -> list[list[str]]:
    """Candidate alembic commands (e.g. ``upgrade head``), most preferred first."""
    commands: list[list[str]] = [
        [sys.executable, "-m", "alembic", *args]
    ]
    uv = shutil.which("uv")
    if uv is not None:
        cmd = [uv, "run", "--no-project", "--python", "3.12"]
        for package in _UV_MIGRATION_PACKAGES:
            cmd += ["--with", package]
        cmd += ["python", "-m", "alembic", *args]
        commands.append(cmd)
    return commands


def _run_alembic(*args: str) -> None:
    """Run one alembic command against the test database."""
    env = _migration_env()
    failures: list[str] = []
    for cmd in _migration_commands(*args):
        try:
            result = subprocess.run(
                cmd, cwd=_PROJ_ROOT, env=env, capture_output=True, text=True
            )
            if result.returncode == 0:
                return
            failures.append(f"{cmd[:2]}: {result.stderr.strip()[-500:]}")
        except OSError as exc:
            failures.append(f"{cmd[:2]}: {exc}")
    raise RuntimeError(
        f"Failed to run alembic {' '.join(args)} with any interpreter:\n"
        + "\n".join(failures)
    )


def _run_migrations() -> None:
    """Apply all alembic migrations against the test database."""
    _run_alembic("upgrade", "head")


async def _drop_all_tables() -> None:
    """Drop every public table — the retry path for a corrupt schema state."""
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        await conn.execute(_DROP_ALL_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def _verified_disposable_database() -> DatabaseTarget:
    """Fail closed unless the target is an explicitly disposable PostgreSQL 16 DB."""
    return asyncio.run(verify_disposable_target(_TARGET))


@pytest.fixture(scope="module")
def _migrated_schema(_verified_disposable_database: DatabaseTarget) -> None:
    """Migrate the test database to ``head`` once per module."""
    try:
        _run_migrations()
    except RuntimeError:
        asyncio.run(_drop_all_tables())
        _run_migrations()


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    """Function-scoped pool on the migrated test database."""
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE afk_runs CASCADE")
        await conn.execute(
            "DELETE FROM collector_credentials WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name = $1)",
            _AWX_CLIENT_NAME,
        )
        await conn.execute(
            "DELETE FROM opencode_clients WHERE name = $1",
            _AWX_CLIENT_NAME,
        )
    await pool.close()


# ── Builders ─────────────────────────────────────────────────────────────────


async def _seed_afk_client(conn: asyncpg.Connection) -> None:
    """Seed the dedicated AWX execution-binding client + collector credential."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        _AWX_CLIENT_NAME,
    )
    await conn.execute(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3)",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

    A fresh app + client is the restart analogue used by the persistence
    test: it shares no in-process state with a previous app instance.
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


def _provision_payload(**overrides) -> dict:
    """Build a ``POST /api/v1/afk/executions/runs`` provisioning payload."""
    payload = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": "eda-actor",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Actor provisioning",
    }
    payload.update(overrides)
    return payload


def _lifecycle_path(afk_run_id: str) -> str:
    """Path of the durable lifecycle read-back."""
    return f"{_LIFECYCLE_PATH}/{afk_run_id}/lifecycle"


async def _fetch_stored_actor(db_pool: asyncpg.Pool, afk_run_id: str) -> str | None:
    """Read the persisted actor straight from ``afk_runs``."""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT actor FROM afk_runs WHERE afk_run_id = $1",
            afk_run_id,
        )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_persists_actor_and_reads_back(
    db_pool: asyncpg.Pool,
) -> None:
    """A provisioned actor is persisted and survives a fresh app instance."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id,
        actor=_TEST_ACTOR,
    )

    async with _build_app(db_pool) as c:
        created = await c.post(_LIFECYCLE_PATH, json=payload)

    assert created.status_code == 201, created.text
    data = created.json()["data"]
    afk_run_id = data["afk_run_id"]
    assert data["actor"] == _TEST_ACTOR
    assert await _fetch_stored_actor(db_pool, afk_run_id) == _TEST_ACTOR

    # Restart analogue: a fresh app/client reads the same run and actor.
    async with _build_app(db_pool) as c:
        readback = await c.get(_lifecycle_path(afk_run_id))

    assert readback.status_code == 200, readback.text
    assert readback.json()["data"]["actor"] == _TEST_ACTOR


@pytest.mark.integration
@pytest.mark.asyncio
async def test_identical_retry_preserves_original_actor(
    db_pool: asyncpg.Pool,
) -> None:
    """An identical replay is idempotent: same run and original actor."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id,
        actor=_TEST_ACTOR,
    )

    async with _build_app(db_pool) as c:
        first = await c.post(_LIFECYCLE_PATH, json=payload)
        replay = await c.post(_LIFECYCLE_PATH, json=payload)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    first_data = first.json()["data"]
    replay_data = replay.json()["data"]
    assert replay_data["afk_run_id"] == first_data["afk_run_id"]
    assert replay_data["actor"] == _TEST_ACTOR
    assert first_data["actor"] == _TEST_ACTOR

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_changed_actor_on_replay_is_conflict_without_replacement(
    db_pool: asyncpg.Pool,
) -> None:
    """A later identity cannot replace the original actor (409, no mutation).

    Also covers the non-erasure rule: a replay that omits a bound actor is a
    conflict, never a silent clear.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id,
        actor=_TEST_ACTOR,
    )

    async with _build_app(db_pool) as c:
        created = await c.post(_LIFECYCLE_PATH, json=payload)
        afk_run_id = created.json()["data"]["afk_run_id"]
        replaced = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(
                source_event_id=source_event_id,
                actor="gitlab:reviewer",
            ),
        )
        erased = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(source_event_id=source_event_id),
        )
        readback = await c.get(_lifecycle_path(afk_run_id))

    assert created.status_code == 201, created.text
    assert replaced.status_code == 409, replaced.text
    assert replaced.json()["error"]["code"] == "CONFLICT"
    assert erased.status_code == 409, erased.text
    assert erased.json()["error"]["code"] == "CONFLICT"

    assert await _fetch_stored_actor(db_pool, afk_run_id) == _TEST_ACTOR
    assert readback.json()["data"]["actor"] == _TEST_ACTOR


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_conflicting_actors_one_wins(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent replays with different actors race safely (201 + 409)."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    actor_a = "github:alice"
    actor_b = "github:bob"

    async with _build_app(db_pool) as c:
        results = await asyncio.gather(
            c.post(
                _LIFECYCLE_PATH,
                json=_provision_payload(
                    source_event_id=source_event_id, actor=actor_a
                ),
            ),
            c.post(
                _LIFECYCLE_PATH,
                json=_provision_payload(
                    source_event_id=source_event_id, actor=actor_b
                ),
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        assert status_codes == [201, 409], (
            f"Expected [201, 409], got {status_codes}"
        )

        winner = next(r for r in results if r.status_code == 201)
        loser = next(r for r in results if r.status_code == 409)
        winner_data = winner.json()["data"]
        assert winner_data["actor"] in {actor_a, actor_b}
        assert loser.json()["error"]["code"] == "CONFLICT"

        readback = await c.get(_lifecycle_path(winner_data["afk_run_id"]))

    stored = await _fetch_stored_actor(db_pool, winner_data["afk_run_id"])
    assert stored == winner_data["actor"]
    assert readback.json()["data"]["actor"] == stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provisioning_without_actor_preserves_legacy_shape(
    db_pool: asyncpg.Pool,
) -> None:
    """Old clients omit the field: no actor is fabricated; invalid values fail."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"

    async with _build_app(db_pool) as c:
        created = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(source_event_id=source_event_id),
        )
        afk_run_id = created.json()["data"]["afk_run_id"]
        readback = await c.get(_lifecycle_path(afk_run_id))

        invalid_actor = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(
                source_event_id=f"eda-{uuid.uuid4().hex[:16]}",
                actor="not-provider-qualified",
            ),
        )
        oversized_actor = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(
                source_event_id=f"eda-{uuid.uuid4().hex[:16]}",
                actor="github:" + "a" * 249,
            ),
        )

    assert created.status_code == 201, created.text
    assert created.json()["data"]["actor"] is None
    assert readback.status_code == 200, readback.text
    assert readback.json()["data"]["actor"] is None
    assert await _fetch_stored_actor(db_pool, afk_run_id) is None

    assert invalid_actor.status_code == 422, invalid_actor.text
    assert oversized_actor.status_code == 422, oversized_actor.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_row_without_actor_reads_null(
    db_pool: asyncpg.Pool,
) -> None:
    """A pre-existing historic row reads back with a null actor."""
    historic_id = "01J8ABCDEFGHJKMNPQRSTVWXYZ"
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)
        await conn.execute(
            "INSERT INTO afk_runs (afk_run_id, provider, title)"
            " VALUES ($1, 'github', 'Historic run')",
            historic_id,
        )

    async with _build_app(db_pool) as c:
        lifecycle_readback = await c.get(_lifecycle_path(historic_id))
        canonical_readback = await c.get(f"/api/v1/afk/runs/{historic_id}")

    assert lifecycle_readback.status_code == 200, lifecycle_readback.text
    assert lifecycle_readback.json()["data"]["actor"] is None
    assert canonical_readback.status_code == 200, canonical_readback.text
    assert canonical_readback.json()["data"]["run"]["actor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_run_readback_surfaces_actor(
    db_pool: asyncpg.Pool,
) -> None:
    """The canonical list/detail surfaces the same stored actor."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"

    async with _build_app(db_pool) as c:
        created = await c.post(
            _LIFECYCLE_PATH,
            json=_provision_payload(
                source_event_id=source_event_id,
                actor=_TEST_ACTOR,
            ),
        )
        afk_run_id = created.json()["data"]["afk_run_id"]
        detail = await c.get(f"/api/v1/afk/runs/{afk_run_id}")
        listing = await c.get(
            "/api/v1/afk/runs",
            params={"provider": "github", "repository": "github.com/acme/proj"},
        )

    assert created.status_code == 201, created.text
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["run"]["actor"] == _TEST_ACTOR

    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]
    matching = [i for i in items if i["afk_run_id"] == afk_run_id]
    assert matching, listing.text
    assert matching[0]["actor"] == _TEST_ACTOR


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_downgrade_upgrade_preserves_historic_runs(
    db_pool: asyncpg.Pool,
) -> None:
    """The migration is additive: nullable column, historic rows untouched."""
    historic_id = "01J8ABCDEFGHJKMNPQRSTVWXZ"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO afk_runs (afk_run_id, provider, title)"
            " VALUES ($1, 'github', 'Historic run')",
            historic_id,
        )
        column = await conn.fetchrow(
            "SELECT is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_name = 'afk_runs' AND column_name = 'actor'"
        )
    assert column is not None
    assert column["is_nullable"] == "YES"
    assert column["column_default"] is None

    _run_alembic("downgrade", "0051")

    async with db_pool.acquire() as conn:
        downgraded_column = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'afk_runs' AND column_name = 'actor'"
        )
        historic_after_downgrade = await conn.fetchval(
            "SELECT afk_run_id FROM afk_runs WHERE afk_run_id = $1",
            historic_id,
        )
    assert downgraded_column is None
    assert historic_after_downgrade == historic_id

    _run_alembic("upgrade", "head")

    async with db_pool.acquire() as conn:
        restored = await conn.fetchrow(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'afk_runs' AND column_name = 'actor'"
        )
        historic_actor = await conn.fetchval(
            "SELECT actor FROM afk_runs WHERE afk_run_id = $1",
            historic_id,
        )
    assert restored is not None
    assert restored["is_nullable"] == "YES"
    assert historic_actor is None
