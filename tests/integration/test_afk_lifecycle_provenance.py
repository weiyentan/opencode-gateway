"""Integration tests for batch provenance + execution-binding multiplicity (issue #595).

Runs against the docker-compose Postgres (port 5433) and verifies the actual
database-enforced guarantees:

* **Batch provenance** — provisioning with ``deliveries`` stores the first
  triggering delivery on ``afk_runs.first_delivery_id`` and every
  contributing delivery identity as an ``afk_run_delivery_batches`` row in
  accepted-batch order.  An identical replay is idempotent (no duplicate
  batch rows); a conflicting batch replay returns 409; provisioning without
  a batch preserves the legacy shape (NULL first delivery, no batch rows).

* **Execution-binding multiplicity** — an execution callback carrying a
  pre-provisioned ``afk_run_id`` attaches to that lifecycle, so a retry with
  a new ``awx_job_id`` preserves the same run.  An unknown ``afk_run_id``
  returns 404.  A callback without ``afk_run_id`` preserves the legacy
  auto-provision behavior, and its replay stays idempotent.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_lifecycle_provenance.py -v -m integration
    docker compose -f docker-compose.test.yml down -v

Fixture layout notes
--------------------
* **Migrations run in a subprocess** (module-scoped): alembic's ``env.py``
  wraps its async engine in ``asyncio.run()``, which cannot run inside the
  pytest-asyncio event loop.  The subprocess falls back to a uv-managed
  Python 3.12 interpreter when the in-process interpreter is older than the
  repo's ``>=3.12`` floor.
* **``db_pool`` is function-scoped**: pytest-asyncio runs every async test
  in a fresh event loop, and an asyncpg pool is bound to the loop that
  created it.  Teardown truncates ``afk_runs`` (CASCADEs the batch table
  and execution bindings) and removes the seeded auth rows.
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
_EXECUTIONS_PATH = "/api/v1/afk/executions"

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


def _migration_env() -> dict:
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


def _migration_commands() -> list:
    """Candidate ``alembic upgrade head`` commands, most preferred first."""
    commands = [[sys.executable, "-m", "alembic", "upgrade", "head"]]
    uv = shutil.which("uv")
    if uv is not None:
        cmd = [uv, "run", "--no-project", "--python", "3.12"]
        for package in _UV_MIGRATION_PACKAGES:
            cmd += ["--with", package]
        cmd += ["python", "-m", "alembic", "upgrade", "head"]
        commands.append(cmd)
    return commands


def _run_migrations() -> None:
    """Apply all alembic migrations against the test database."""
    env = _migration_env()
    failures = []
    for cmd in _migration_commands():
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
        "Failed to apply database migrations with any interpreter:\n"
        + "\n".join(failures)
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
    """Migrate the test database to ``head`` once per module."""
    try:
        _run_migrations()
    except RuntimeError:
        asyncio.run(_drop_all_tables())
        _run_migrations()


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    """Function-scoped pool on the migrated test database.

    Teardown truncates ``afk_runs`` (CASCADE clears the batch table and
    execution bindings) and removes the seeded auth rows.
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


async def _seed_afk_client(conn: asyncpg.Connection) -> tuple:
    """Seed the dedicated AWX execution-binding client + collector credential."""
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
    """Build a FastAPI app connected to the real integration DB pool."""
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
        "source_event_id": "eda-provenance",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Batch provenance",
    }
    payload.update(overrides)
    return payload


def _execution_payload(**overrides) -> dict:
    """Build a ``POST /api/v1/afk/executions`` callback payload."""
    payload = {
        "awx_job": {"job_id": "1", "job_template_id": 7},
        "external_session_id": "ses_provenance",
        "resource": {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": "42",
        },
        "outcome": "completed",
        "trigger_type": "eda",
        "source_event_id": "evt-provenance",
    }
    payload.update(overrides)
    return payload


# ── Batch provenance ─────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_with_batch_persists_provenance(db_pool: asyncpg.Pool) -> None:
    """Provisioning with deliveries stores the run's first delivery + batch rows."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id,
        deliveries=["d1", "d2", "d3"],
    )

    async with client as c:
        resp = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()["data"]
        assert body["status"] == "pending"
        assert len(body["afk_run_id"]) == 26
        assert body["first_delivery_id"] == "d1"
        assert body["delivery_ids"] == ["d1", "d2", "d3"]
        run_id = body["afk_run_id"]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_delivery_id, status FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        assert row["first_delivery_id"] == "d1"
        assert row["status"] == "pending"

        batch_rows = await conn.fetch(
            "SELECT delivery_id, position FROM afk_run_delivery_batches"
            " WHERE afk_run_id = $1 ORDER BY position ASC",
            run_id,
        )
        assert [(r["delivery_id"], r["position"]) for r in batch_rows] == [
            ("d1", 0),
            ("d2", 1),
            ("d3", 2),
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_replay_same_batch_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """An identical batch replay returns 200 and never duplicates batch rows."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id, deliveries=["d1", "d2"]
    )

    async with client as c:
        first = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
        assert first.status_code == 201, first.text
        run_id = first.json()["data"]["afk_run_id"]

        replay = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["data"]["afk_run_id"] == run_id
        assert replay.json()["data"]["delivery_ids"] == ["d1", "d2"]

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_run_delivery_batches WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2, f"Expected 2 batch rows, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_conflicting_batch_replay_returns_409(
    db_pool: asyncpg.Pool,
) -> None:
    """A replay with a different batch returns 409 without mutation."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(
        source_event_id=source_event_id, deliveries=["d1", "d2"]
    )

    async with client as c:
        first = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
        assert first.status_code == 201, first.text
        run_id = first.json()["data"]["afk_run_id"]

        conflict = await c.post(
            _LIFECYCLE_PROVISION_PATH,
            json=_provision_payload(
                source_event_id=source_event_id, deliveries=["d1", "d9"]
            ),
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "CONFLICT"

    async with db_pool.acquire() as conn:
        batch_rows = await conn.fetch(
            "SELECT delivery_id FROM afk_run_delivery_batches"
            " WHERE afk_run_id = $1 ORDER BY position ASC",
            run_id,
        )
        assert [r["delivery_id"] for r in batch_rows] == ["d1", "d2"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_without_batch_preserves_legacy_shape(
    db_pool: asyncpg.Pool,
) -> None:
    """Legacy provisioning (no deliveries) carries no batch provenance."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"

    async with client as c:
        resp = await c.post(
            _LIFECYCLE_PROVISION_PATH,
            json=_provision_payload(source_event_id=source_event_id),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()["data"]
        assert body["first_delivery_id"] is None
        assert body["delivery_ids"] == []
        run_id = body["afk_run_id"]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_delivery_id FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        assert row is not None and row["first_delivery_id"] is None
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_run_delivery_batches WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 0


# ── Execution-binding multiplicity ───────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_callbacks_attach_to_provisioned_run(
    db_pool: asyncpg.Pool,
) -> None:
    """Two callbacks (a retry with a new awx_job_id) share one afk_run_id."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"

    async with client as c:
        provision = await c.post(
            _LIFECYCLE_PROVISION_PATH,
            json=_provision_payload(source_event_id=source_event_id),
        )
        assert provision.status_code == 201, provision.text
        run_id = provision.json()["data"]["afk_run_id"]

        job_1 = str(int(uuid.uuid4().int >> 96))
        job_2 = str(int(uuid.uuid4().int >> 96))

        binding_1 = await c.post(
            _EXECUTIONS_PATH,
            json=_execution_payload(
                awx_job={"job_id": job_1, "job_template_id": 7},
                outcome="failed",
                afk_run_id=run_id,
            ),
        )
        assert binding_1.status_code == 201, binding_1.text
        assert binding_1.json()["data"]["afk_run_id"] == run_id

        binding_2 = await c.post(
            _EXECUTIONS_PATH,
            json=_execution_payload(
                awx_job={"job_id": job_2, "job_template_id": 7},
                outcome="completed",
                afk_run_id=run_id,
            ),
        )
        assert binding_2.status_code == 201, binding_2.text
        assert binding_2.json()["data"]["afk_run_id"] == run_id

    async with db_pool.acquire() as conn:
        run_count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        assert run_count == 1, "Both callbacks must reference the one provisioned run"
        bindings = await conn.fetch(
            "SELECT awx_job_id, afk_run_id FROM execution_bindings"
            " WHERE afk_run_id = $1",
            run_id,
        )
        assert len(bindings) == 2
        assert {str(b["awx_job_id"]) for b in bindings} == {job_1, job_2}
        assert all(b["afk_run_id"] == run_id for b in bindings)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_callback_unknown_afk_run_id_returns_404(
    db_pool: asyncpg.Pool,
) -> None:
    """A callback referencing no provisioned lifecycle returns 404."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)

    async with client as c:
        resp = await c.post(
            _EXECUTIONS_PATH,
            json=_execution_payload(afk_run_id="01HMISSING0000000000000001"),
        )
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["error"]["message"].lower()

    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM execution_bindings")
        assert count == 0, "A rejected callback must not persist a binding"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_callback_without_run_auto_provisions(db_pool: asyncpg.Pool) -> None:
    """A callback without afk_run_id preserves the legacy auto-provision path."""
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    job_id = str(int(uuid.uuid4().int >> 96))

    async with client as c:
        resp = await c.post(
            _EXECUTIONS_PATH,
            json=_execution_payload(awx_job={"job_id": job_id, "job_template_id": 7}),
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["data"]["afk_run_id"]
        assert run_id is not None and len(run_id) == 26

        # Legacy identical replay stays idempotent.
        replay = await c.post(
            _EXECUTIONS_PATH,
            json=_execution_payload(awx_job={"job_id": job_id, "job_template_id": 7}),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["data"]["afk_run_id"] == run_id

    async with db_pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT status, first_delivery_id FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert run is not None
        assert run["status"] == "pending"
        assert run["first_delivery_id"] is None
        binding_count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1", run_id
        )
        assert binding_count == 1
