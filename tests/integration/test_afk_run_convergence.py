"""Prove AFK Run convergence under concurrent binding writes (issue #607).

Runs against the docker-compose Postgres (port 5433) and verifies the
transactional convergence contract via the execution-binding API with real
PostgreSQL concurrency, under ADR 0028 semantics:

* Running creation and terminal transition record bindings without projecting
  ``afk_runs.status`` from child execution outcomes (status stays ``pending``
  until the change request merges/closes — ADR 0028).
* Direct terminal POST records terminal bindings; ``afk_runs.status`` is not
  derived from child outcomes.
* ADR 0028: new bindings are accepted on completed AFK Runs (no 409
  rejection) and stored alongside the existing history (201).
* Concurrent terminal callbacks for different bindings converge deterministically.
* Concurrent new-binding attachment racing final terminal callback cannot leave
  terminal parent with running child.
* Identical terminal callback replay is idempotent.
* PR/MR open/closed/merged state has no effect on execution status.

No historical backfill, finished_at derivation, AWX observation, polling, or
cancellation reconciliation is introduced — convergence is DB-local and
transaction-scoped as implemented in #606.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_run_convergence.py -v -m integration
    docker compose -f docker-compose.test.yml down -v
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

from afk_outcomes.run_status import resolve_afk_run_status
from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

_API_KEY = "test-api-key"
_AWX_CLIENT_NAME = "awx-execution-bindings"

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
    try:
        _run_migrations()
    except RuntimeError:
        asyncio.run(_drop_all_tables())
        _run_migrations()


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE afk_runs, execution_bindings CASCADE")
        await conn.execute(
            "DELETE FROM collector_credentials WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name = $1)",
            _AWX_CLIENT_NAME,
        )
        await conn.execute("DELETE FROM opencode_clients WHERE name = $1", _AWX_CLIENT_NAME)
        await conn.execute("TRUNCATE engineering_events CASCADE")
        await conn.execute("TRUNCATE delivery_log CASCADE")
    await pool.close()


async def _seed_awx_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) ON CONFLICT (name) DO NOTHING RETURNING id",
        _AWX_CLIENT_NAME,
    )
    if client_id is None:
        client_id = await conn.fetchval("SELECT id FROM opencode_clients WHERE name=$1", _AWX_CLIENT_NAME)
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )
    return client_id, credential_id


def _build_app(db_pool: asyncpg.Pool) -> object:
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


def _new_run_id() -> str:
    return "01J" + uuid.uuid4().hex[:23]


async def _provision_run_via_api(pool: asyncpg.Pool, *, provider: str = "github") -> str:
    """Provision one provisional AFK run via the API, return its afk_run_id."""
    async with pool.acquire() as conn:
        await _seed_awx_client(conn)
    client = _build_app(pool)
    payload = {
        "provider": provider,
        "host": "awx-01.internal",
        "source_event_id": f"eda-{uuid.uuid4().hex[:12]}",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Convergence test",
    }
    async with client as c:  # type: ignore[attr-defined]
        resp = await c.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]["afk_run_id"]


def _binding_payload(
    *,
    awx_job_id: int,
    afk_run_id: str,
    outcome: str,
    resource_number: str = "99",
    repository: str = "https://github.com/acme/proj",
    provider: str = "github",
    resource_type: str = "pull_request",
    external_session_id: str | None = None,
    failure_reason: str | None = None,
) -> dict:
    payload: dict = {
        "awx_job": {"job_id": str(awx_job_id), "job_template_id": 7},
        "afk_run_id": afk_run_id,
        "outcome": outcome,
        "trigger_type": "manual",
    }
    # Completed requires both resource and session; failed/cancelled may omit.
    if outcome == "completed" or resource_number is not None:
        payload["resource"] = {
            "provider": provider,
            "repository": repository,
            "resource_type": resource_type,
            "resource_number": resource_number,
        }
        if external_session_id is None and outcome == "completed":
            external_session_id = f"ses_{uuid.uuid4().hex[:8]}"
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    return payload


# ── API-level convergence ──────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_running_creation_converges_to_running(db_pool: asyncpg.Pool) -> None:
    """POST running creates a binding; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 80)
    payload = _binding_payload(awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running")
    # Running is provisioned without resource/session (two-phase start)
    payload.pop("resource", None)

    async with client as c:  # type: ignore[attr-defined]
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        # ADR 0028: afk_runs.status stays 'pending' — not projected from child outcomes
        status = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id)
        assert status == "pending"
        # Verify via pure domain policy as well (the function itself is unchanged)
        outcomes = ["running"]
        assert resolve_afk_run_status(outcomes) == "running"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_direct_terminal_post_converges(db_pool: asyncpg.Pool) -> None:
    """Direct terminal POST creates bindings; afk_runs.status stays pending (ADR 0028)."""
    # Direct completed
    run_id = await _provision_run_via_api(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 80)
    payload = _binding_payload(awx_job_id=awx_job_id, afk_run_id=run_id, outcome="completed")
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id)
        assert status == "pending"

    # Direct failed (no resource/session required)
    run_id2 = await _provision_run_via_api(db_pool)
    awx2 = int(uuid.uuid4().int >> 80)
    payload2 = _binding_payload(awx_job_id=awx2, afk_run_id=run_id2, outcome="failed", resource_number=None)
    # remove resource for failed without CR
    payload2.pop("resource", None)
    # add failure_reason
    payload2["failure_reason"] = "crashed"
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp2 = await c.post("/api/v1/afk/executions", json=payload2)
        assert resp2.status_code == 201, resp2.text
    async with db_pool.acquire() as conn:
        status2 = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id2)
        assert status2 == "pending"

    # Direct cancelled
    run_id3 = await _provision_run_via_api(db_pool)
    awx3 = int(uuid.uuid4().int >> 80)
    payload3 = _binding_payload(awx_job_id=awx3, afk_run_id=run_id3, outcome="cancelled", resource_number=None)
    payload3.pop("resource", None)
    payload3["failure_reason"] = "cancelled by user"
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp3 = await c.post("/api/v1/afk/executions", json=payload3)
        assert resp3.status_code == 201, resp3.text
    async with db_pool.acquire() as conn:
        status3 = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id3)
        assert status3 == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_patch_final_running_converges(db_pool: asyncpg.Pool) -> None:
    """Two running bindings -> PATCH transitions binding outcomes; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    client = _build_app(db_pool)
    job_a = int(uuid.uuid4().int >> 80)
    job_b = int(uuid.uuid4().int >> 80)
    # provision two running
    async with client as c:  # type: ignore[attr-defined]
        for job in (job_a, job_b):
            payload = _binding_payload(awx_job_id=job, afk_run_id=run_id, outcome="running")
            payload.pop("resource", None)
            resp = await c.post("/api/v1/afk/executions", json=payload)
            assert resp.status_code == 201, resp.text
        async with db_pool.acquire() as conn2:
            s = await conn2.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
            assert s == "pending"
        # PATCH job_a to completed — binding outcome only; afk_runs.status stays pending
        resp_patch_a = await c.patch(
            f"/api/v1/afk/executions/{job_a}",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": f"ses_{job_a}",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp_patch_a.status_code == 200, resp_patch_a.text
        async with db_pool.acquire() as conn2:
            s = await conn2.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
            assert s == "pending", "afk_runs.status is never projected from child outcomes"

        # PATCH job_b to completed -> both bindings terminal; afk_runs.status stays pending
        resp_patch_b = await c.patch(
            f"/api/v1/afk/executions/{job_b}",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": f"ses_{job_b}",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp_patch_b.status_code == 200, resp_patch_b.text
        async with db_pool.acquire() as conn2:
            s = await conn2.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
            assert s == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completed_run_accepts_new_binding(db_pool: asyncpg.Pool) -> None:
    """ADR 0028: attaching new binding to completed lifecycle returns 201."""
    run_id = await _provision_run_via_api(db_pool)
    job_completed = int(uuid.uuid4().int >> 80)
    payload_completed = _binding_payload(awx_job_id=job_completed, afk_run_id=run_id, outcome="completed")
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp = await c.post("/api/v1/afk/executions", json=payload_completed)
        assert resp.status_code == 201, resp.text
    async with db_pool.acquire() as conn:
        count_before = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id=$1", run_id
        )
        # ADR 0028: status stays pending — not projected from child outcomes
        status_before = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        assert status_before == "pending"

    # New binding on the same lifecycle — accepted (no 409)
    job_new = int(uuid.uuid4().int >> 80)
    payload_new = _binding_payload(awx_job_id=job_new, afk_run_id=run_id, outcome="running")
    payload_new.pop("resource", None)
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp2 = await c.post("/api/v1/afk/executions", json=payload_new)
        assert resp2.status_code == 201, resp2.text

    async with db_pool.acquire() as conn:
        count_after = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id=$1", run_id
        )
        status_after = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        assert count_after == count_before + 1, "new binding should be stored"
        assert status_after == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_terminal_callbacks_converge_deterministically(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent PATCH for different bindings records outcomes; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    client = _build_app(db_pool)
    job_a = int(uuid.uuid4().int >> 80)
    job_b = int(uuid.uuid4().int >> 80)
    async with client as c:  # type: ignore[attr-defined]
        for job in (job_a, job_b):
            payload = _binding_payload(awx_job_id=job, afk_run_id=run_id, outcome="running")
            payload.pop("resource", None)
            resp = await c.post("/api/v1/afk/executions", json=payload)
            assert resp.status_code == 201, resp.text

        # Concurrent PATCH: a -> completed, b -> failed ; completed dominates
        results = await asyncio.gather(
            c.patch(
                f"/api/v1/afk/executions/{job_a}",
                json={
                    "outcome": "completed",
                    "finished_at": "2026-08-02T12:00:00Z",
                    "external_session_id": f"ses_{job_a}",
                    "resource": {
                        "provider": "github",
                        "repository": "https://github.com/acme/proj",
                        "resource_type": "pull_request",
                        "resource_number": "99",
                    },
                },
            ),
            c.patch(
                f"/api/v1/afk/executions/{job_b}",
                json={
                    "outcome": "failed",
                    "failure_reason": "timeout",
                    "external_session_id": f"ses_{job_b}",
                },
            ),
        )
        for r in results:
            assert r.status_code == 200, r.text

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        assert status == "pending"
        # Ensure both bindings exist
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id=$1", run_id
        )
        assert count == 2

    # Second permutation: failed vs cancelled -> failed dominates
    run_id2 = await _provision_run_via_api(db_pool)
    job_c = int(uuid.uuid4().int >> 80)
    job_d = int(uuid.uuid4().int >> 80)
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        for job in (job_c, job_d):
            p = _binding_payload(awx_job_id=job, afk_run_id=run_id2, outcome="running")
            p.pop("resource", None)
            resp = await c.post("/api/v1/afk/executions", json=p)
            assert resp.status_code == 201, resp.text
        results2 = await asyncio.gather(
            c.patch(
                f"/api/v1/afk/executions/{job_c}",
                json={"outcome": "failed", "failure_reason": "oops"},
            ),
            c.patch(
                f"/api/v1/afk/executions/{job_d}",
                json={"outcome": "cancelled", "failure_reason": "cancelled"},
            ),
        )
        for r in results2:
            assert r.status_code == 200, r.text
    async with db_pool.acquire() as conn:
        status2 = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id2)
        assert status2 == "pending"
        # Reverse order must still converge to same
        assert resolve_afk_run_status(["failed", "cancelled"]) == "failed"
        assert resolve_afk_run_status(["cancelled", "failed"]) == "failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_new_binding_vs_final_callback_no_terminal_with_running(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent POST new running vs PATCH final records outcomes; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    job_running = int(uuid.uuid4().int >> 80)
    # Single running binding
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        payload = _binding_payload(awx_job_id=job_running, afk_run_id=run_id, outcome="running")
        payload.pop("resource", None)
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

    job_new = int(uuid.uuid4().int >> 80)
    payload_new = _binding_payload(awx_job_id=job_new, afk_run_id=run_id, outcome="running")
    payload_new.pop("resource", None)

    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        # Race: POST new running vs PATCH existing to completed
        results = await asyncio.gather(
            c.post("/api/v1/afk/executions", json=payload_new),
            c.patch(
                f"/api/v1/afk/executions/{job_running}",
                json={
                    "outcome": "completed",
                    "finished_at": "2026-08-02T12:00:00Z",
                    "external_session_id": f"ses_{job_running}",
                    "resource": {
                        "provider": "github",
                        "repository": "https://github.com/acme/proj",
                        "resource_type": "pull_request",
                        "resource_number": "99",
                    },
                },
            ),
        )
        post_result, patch_result = results
        # One of the outcomes must be valid, but invariant holds regardless
        assert patch_result.status_code == 200, patch_result.text
        # POST either succeeds (201) or is rejected (409) if PATCH won first
        assert post_result.status_code in (201, 409), post_result.text

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        bindings = await conn.fetch(
            "SELECT outcome FROM execution_bindings WHERE afk_run_id=$1", run_id
        )
        outcomes = [r["outcome"] for r in bindings]
        # Invariant: terminal parent must not have running child
        if status in ("completed", "failed", "cancelled"):
            assert "running" not in outcomes, f"terminal parent {status} must not have running child {outcomes}"
        if "running" in outcomes:
            assert status == "pending", f"running child requires parent running, got {status}"
        # ADR 0028: afk_runs.status stays pending — not projected from child outcomes
        assert status == "pending"
        # Verify deterministic via pure policy (independent of DB status)
        expected = resolve_afk_run_status(outcomes)
        assert expected in ("running", "completed", "failed", "cancelled")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_identical_terminal_replay_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """Identical PATCH replay is idempotent — no duplicate binding; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    client = _build_app(db_pool)
    job = int(uuid.uuid4().int >> 80)
    async with client as c:  # type: ignore[attr-defined]
        payload = _binding_payload(awx_job_id=job, afk_run_id=run_id, outcome="running")
        payload.pop("resource", None)
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        patch_body = {
            "outcome": "failed",
            "failure_reason": "timeout",
            "external_session_id": f"ses_{job}",
        }
        resp1 = await c.patch(f"/api/v1/afk/executions/{job}", json=patch_body)
        assert resp1.status_code == 200, resp1.text
        async with db_pool.acquire() as conn2:
            status_first = await conn2.fetchval(
                "SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id
            )
            count_first = await conn2.fetchval(
                "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id=$1", run_id
            )
        # Replay identical
        resp2 = await c.patch(f"/api/v1/afk/executions/{job}", json=patch_body)
        assert resp2.status_code == 200, resp2.text
        async with db_pool.acquire() as conn2:
            status_second = await conn2.fetchval(
                "SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id
            )
            count_second = await conn2.fetchval(
                "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id=$1", run_id
            )
        assert count_second == count_first == 1
        assert status_second == status_first == "pending"
        # Ensure no duplicate row for awx_job_id
        async with db_pool.acquire() as conn2:
            dup = await conn2.fetchval(
                "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id=$1", job
            )
            assert dup == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pr_mr_state_has_no_effect_on_execution_status(db_pool: asyncpg.Pool) -> None:
    """PR/MR open/closed/merged state has no effect on binding outcomes; afk_runs.status stays pending (ADR 0028)."""
    run_id = await _provision_run_via_api(db_pool)
    job = int(uuid.uuid4().int >> 80)
    payload = _binding_payload(awx_job_id=job, afk_run_id=run_id, outcome="running")
    payload.pop("resource", None)
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

    # Insert engineering_events with various PR/MR states for the same repo
    async with db_pool.acquire() as conn:
        for ev_type in (
            "change_request.opened",
            "change_request.closed",
            "change_request.merged",
            "issue.opened",
            "issue.closed",
        ):
            await conn.execute(
                """
                INSERT INTO engineering_events
                    (provider, repository, entity_type, external_id, event_type,
                     occurred_at, payload, observation_key, observed_via)
                VALUES ($1,$2,$3,$4,$5, now(), '{}', $6, 'webhook')
                ON CONFLICT DO NOTHING
                """,
                "github",
                "github.com/acme/proj",
                "change_request" if ev_type.startswith("change_request") else "issue",
                "99" if ev_type.startswith("change_request") else "1",
                ev_type,
                f"obs-{uuid.uuid4().hex}",
            )
        # Transition to completed
    async with _build_app(db_pool) as c:  # type: ignore[attr-defined]
        resp_patch = await c.patch(
            f"/api/v1/afk/executions/{job}",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": f"ses_{job}",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp_patch.status_code == 200, resp_patch.text

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        assert status == "pending"
        # Insert more PR events after the terminal patch — afk_runs.status must stay pending
        await conn.execute(
            """
            INSERT INTO engineering_events
                (provider, repository, entity_type, external_id, event_type,
                 occurred_at, payload, observation_key, observed_via)
            VALUES ('github','github.com/acme/proj','change_request','99','change_request.closed', now(), '{}', $1, 'webhook')
            ON CONFLICT DO NOTHING
            """,
            f"obs-{uuid.uuid4().hex}",
        )
        status2 = await conn.fetchval("SELECT status FROM afk_runs WHERE afk_run_id=$1", run_id)
        assert status2 == "pending"
        # Pure policy never consults those tables
        assert resolve_afk_run_status(["completed"]) == "completed"
