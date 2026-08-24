"""Integration concurrency tests for the provisional AFK run lifecycle (issue #589).

Runs against the docker-compose Postgres (port 5433) and verifies the actual
database-enforced guarantees of ``/api/v1/afk/executions/runs``:

* Concurrent identical provisioning of the same
  ``(provider, host, source_event_id)`` key — exactly one 201 Created and
  one 200 OK (idempotent replay), exactly one ``afk_runs`` row for the key.
* Concurrent conflicting provisioning (same key, different repository) —
  exactly one 201 Created and one 409 Conflict; the stored row remains
  exactly the winner's payload (never mutated, never blended).
* Concurrent binding of the same change request to two separate lifecycles
  — exactly one 200 OK and one 409 Conflict, enforcing the 1:1
  lifecycle<->change-request invariant backed by the partial unique index
  ``uq_afk_runs_change_request_identity`` (migration 0039).

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_lifecycle_concurrency.py -v -m integration
    docker compose -f docker-compose.test.yml down -v

Fixture layout notes
--------------------
* **Migrations run in a subprocess** (module-scoped): alembic's ``env.py``
  wraps its async engine in ``asyncio.run()``, which cannot run inside the
  pytest-asyncio event loop.  The subprocess tries the in-process
  interpreter first and falls back to a uv-managed Python 3.12 interpreter
  when the in-process interpreter is older than the repo's ``>=3.12`` floor
  (the migration chain uses PEP 604 annotations that Python 3.9 cannot
  import).
* **``db_pool`` is function-scoped**: pytest-asyncio 0.26 runs every async
  test in a fresh event loop, and an asyncpg pool is bound to the loop that
  created it — a module-scoped pool would be used across loops.  Each test
  gets its own pool, and teardown truncates ``afk_runs`` and removes the
  seeded auth rows so every test starts from a clean slate.
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

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

_API_KEY = "test-api-key"

# The dedicated AWX execution-binding client name — matches the constant in
# app.api.afk_executions (issue #550).
_AWX_CLIENT_NAME = "awx-execution-bindings"

_LIFECYCLE_PROVISION_PATH = "/api/v1/afk/executions/runs"

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


def _dsn() -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


def _migration_env() -> dict[str, str]:
    """Environment for the migration subprocess — point app settings at the test DB."""
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
    return env


def _migration_commands() -> list[list[str]]:
    """Candidate ``alembic upgrade head`` commands, most preferred first.

    The in-process interpreter is tried first.  When it is older than the
    repo's Python 3.12 floor, a uv-managed 3.12 interpreter carries the
    migration chain instead.
    """
    commands: list[list[str]] = [[sys.executable, "-m", "alembic", "upgrade", "head"]]
    uv = shutil.which("uv")
    if uv is not None:
        cmd = [uv, "run", "--no-project", "--python", "3.12"]
        for package in _UV_MIGRATION_PACKAGES:
            cmd += ["--with", package]
        cmd += ["python", "-m", "alembic", "upgrade", "head"]
        commands.append(cmd)
    return commands


def _run_migrations() -> None:
    """Apply all alembic migrations against the test database.

    Raises :class:`RuntimeError` when every candidate command fails.
    """
    env = _migration_env()
    failures: list[str] = []
    for cmd in _migration_commands():
        try:
            result = subprocess.run(
                cmd,
                cwd=_PROJ_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            failures.append(f"{cmd[:2]}: {result.stderr.strip()[-500:]}")
        except OSError as exc:
            failures.append(f"{cmd[:2]}: {exc}")
    raise RuntimeError(
        "Failed to apply database migrations with any interpreter:\n" + "\n".join(failures)
    )


async def _drop_all_tables() -> None:
    """Drop every public table — the retry path for a corrupt schema state."""
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        await conn.execute(_DROP_ALL_SQL)
    finally:
        await conn.close()


async def _can_connect() -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn=_dsn(), timeout=5), timeout=10.0)
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
def _migrated_schema(_integration_db_available: bool) -> None:
    """Migrate the test database to ``head`` once per module.

    Deliberately synchronous: the migration subprocess must not run inside
    the pytest-asyncio event loop (alembic's env.py calls ``asyncio.run``).
    A failed first attempt (e.g. leftover partial tables) drops every
    public table and retries from scratch.
    """
    try:
        _run_migrations()
    except RuntimeError:
        asyncio.run(_drop_all_tables())
        _run_migrations()


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    """Function-scoped pool on the migrated test database.

    Teardown truncates ``afk_runs`` and removes the seeded auth rows so
    every test starts from a clean slate.
    """
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


async def _seed_afk_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the dedicated AWX execution-binding client + collector credential.

    Returns (client_id, credential_id).  The credential hash matches the
    ``_API_KEY`` bearer token so both layers of auth pass.
    """
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        _AWX_CLIENT_NAME,
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

    Sends the bearer token matching both the API-key middleware and the
    seeded collector credential.

    ``Request`` is imported at module level (not inside this function): the
    module uses ``from __future__ import annotations``, so the override's
    ``request: Request`` annotation is a string that FastAPI resolves
    against this module's globals — without a module-level import it would
    be misread as a query parameter.
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
        "source_event_id": "eda-concurrent-provision",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Concurrent provisioning",
    }
    payload.update(overrides)
    return payload


def _binding_payload(**overrides) -> dict:
    """Build a ``POST /runs/{afk_run_id}/change-request`` binding payload."""
    payload = {
        "provider": "gitlab",
        "repository": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
        "external_id": "6",
    }
    payload.update(overrides)
    return payload


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_provision_same_key_same_payload(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent identical provisioning: one 201, one 200, exactly one row.

    Two concurrent POSTs with the same ``(provider, host, source_event_id)``
    and identical payload race on the INSERT behind the partial unique index
    ``uq_afk_runs_provisioning_key``.  Exactly one inserts (201 Created);
    the loser re-reads the winner's row and resolves to an idempotent replay
    (200 OK).  Both responses carry the same ``afk_run_id``, and the
    database holds exactly one row for the key.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(source_event_id=source_event_id)

    async with client as c:
        results = await asyncio.gather(
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload),
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted) and one 200 (idempotent replay).
        assert status_codes == [200, 201], f"Expected [200, 201], got {status_codes}"

        run_ids: set[str] = set()
        for r in results:
            body = r.json()
            assert body["status"] == "ok"
            assert body["data"]["source_event_id"] == source_event_id
            run_ids.add(body["data"]["afk_run_id"])
        assert len(run_ids) == 1, "Both responses must reference the same lifecycle"

    # Verify exactly one row exists in afk_runs for the provisioning key.
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert count == 1, f"Expected 1 row for the key, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_provision_same_key_different_payload(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent conflicting provisioning: one 201, one 409, no mutation.

    Two concurrent POSTs share the provisioning key but disagree on
    ``repository``.  Exactly one inserts (201 Created); the loser detects
    the payload mismatch against the winner's row (409 Conflict).  The
    stored row must remain exactly the winner's payload — never a blend.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload_a = _provision_payload(
        source_event_id=source_event_id,
        repository="https://github.com/acme/proj",
    )
    payload_b = _provision_payload(
        source_event_id=source_event_id,
        repository="https://github.com/acme/other",
    )

    async with client as c:
        results = await asyncio.gather(
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload_a),
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload_b),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted) and one 409 (conflict rejection).
        assert status_codes == [201, 409], f"Expected [201, 409], got {status_codes}"

        winner = next(r for r in results if r.status_code == 201)
        winner_repository = winner.json()["data"]["repository"]
        assert winner_repository in (
            "github.com/acme/proj",
            "github.com/acme/other",
        )

        loser = next(r for r in results if r.status_code == 409)
        loser_body = loser.json()
        assert loser_body["status"] == "error"
        assert loser_body["error"]["code"] == "CONFLICT"

    # Verify exactly one row, unmutated — the stored repository must match
    # the 201 winner's payload exactly (never the conflicting one).
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert count == 1, f"Expected 1 row for the key, got {count}"

        row = await conn.fetchrow(
            "SELECT repository, trigger_type, title FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert row is not None
        assert row["repository"] == winner_repository
        assert row["trigger_type"] == "eda"
        assert row["title"] == "Concurrent provisioning"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_bind_same_change_request(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent binding of one change request to two lifecycles: one 200, one 409.

    Two lifecycles are provisioned first (sequential, different
    ``source_event_id``s).  The same change-request identity is then bound
    to both concurrently.  The 1:1 lifecycle<->change-request invariant
    (partial unique index ``uq_afk_runs_change_request_identity``) admits
    exactly one winner (200 OK); the loser resolves to 409 Conflict.
    Exactly one lifecycle carries the bound change request in the database.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    run_ids: list[str] = []

    async with client as c:
        for _ in range(2):
            payload = _provision_payload(source_event_id=f"eda-{uuid.uuid4().hex[:16]}")
            resp = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
            assert resp.status_code == 201, resp.text
            run_ids.append(resp.json()["data"]["afk_run_id"])

        bind_payload = _binding_payload()
        results = await asyncio.gather(
            c.post(
                f"{_LIFECYCLE_PROVISION_PATH}/{run_ids[0]}/change-request",
                json=bind_payload,
            ),
            c.post(
                f"{_LIFECYCLE_PROVISION_PATH}/{run_ids[1]}/change-request",
                json=bind_payload,
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 200 (bound) and one 409 (the 1:1 invariant held).
        assert status_codes == [200, 409], f"Expected [200, 409], got {status_codes}"

        for r in results:
            body = r.json()
            if r.status_code == 409:
                assert body["status"] == "error"
                assert body["error"]["code"] == "CONFLICT"
            else:
                assert body["status"] == "ok"
                bound = body["data"]["change_request"]
                assert bound is not None
                assert bound["resource_type"] == "change_request"
                assert bound["resource_number"] == "6"

    # Verify database consistency: exactly one lifecycle owns the change
    # request; the other remains unbound.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT afk_run_id, change_request_provider,"
            " change_request_repository, change_request_external_id"
            " FROM afk_runs WHERE afk_run_id = ANY($1::text[])",
            run_ids,
        )
        assert len(rows) == 2
        bound_rows = [r for r in rows if r["change_request_provider"] is not None]
        unbound_rows = [r for r in rows if r["change_request_provider"] is None]
        assert len(bound_rows) == 1, "Exactly one lifecycle must own the change request"
        assert len(unbound_rows) == 1
        assert (
            bound_rows[0]["change_request_provider"],
            bound_rows[0]["change_request_repository"],
            bound_rows[0]["change_request_external_id"],
        ) == ("gitlab", "gitlab.com/cloudnative-pg/cloudnative-pg", "6")
