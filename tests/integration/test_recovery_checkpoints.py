"""Integration tests for the AFK recovery-checkpoint API (issue #754).

Runs against the docker-compose Postgres (port 5433) and verifies the
execution-scoped recovery-checkpoint resource end-to-end:

* zero / one / many checkpoints for an execution,
* duplicate POST (same execution + ref + commit_sha) is idempotent,
* distinct refs pointing at the same SHA are preserved as distinct rows,
* a deterministic, stable list order,
* unknown ``awx_job_id`` fails closed with 404 on both POST and GET,
* a failed execution keeps its outcome/failure metadata/session/resource
  binding untouched when checkpoints are persisted, and checkpoints remain
  readable after the execution is terminal,
* existing Gateway auth rules apply (write needs the collector credential,
  reads need only the Admin API Key).

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_recovery_checkpoints.py -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Request

from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

_API_KEY = "test-api-key"

# The dedicated AWX execution-binding client name — matches the constant in
# app.api.afk_executions (issue #550).
_AWX_CLIENT_NAME = "awx-execution-bindings"


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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _integration_db_available() -> bool:
    if not await _can_connect():
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


def _migration_script_dir() -> str:
    """Return a Python-3.9-import-safe copy of the alembic script directory.

    Mirrors the execution-bindings integration suite: copies ``env.py`` and
    the version modules to a temp dir with ``from __future__ import
    annotations`` injected so the revision map builds on 3.9 without
    touching the shipped migrations.
    """
    import shutil
    import tempfile

    src = _PROJ_ROOT / "alembic"
    dst = Path(tempfile.mkdtemp(prefix="gateway-alembic-754-"))
    shutil.copy(src / "env.py", dst / "env.py")
    (dst / "versions").mkdir()
    for version in sorted((src / "versions").glob("*.py")):
        text = version.read_text()
        if not text.startswith("from __future__ import annotations"):
            text = "from __future__ import annotations\n" + text
        (dst / "versions" / version.name).write_text(text)
    return str(dst)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config
    import shutil

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    migration_dir = _migration_script_dir()
    alembic_cfg.set_main_option("script_location", migration_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        alembic.command.upgrade(alembic_cfg, "head")

    try:
        await asyncio.to_thread(_upgrade)
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        await asyncio.to_thread(_upgrade)

    yield pool

    shutil.rmtree(migration_dir, ignore_errors=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        )
    await pool.close()


# ── Builders ─────────────────────────────────────────────────────────────────


async def _seed_awx_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the dedicated AWX execution-binding client + collector credential."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1)"
        " ON CONFLICT (name) DO NOTHING RETURNING id",
        _AWX_CLIENT_NAME,
    )
    if client_id is None:
        client_id = await conn.fetchval(
            "SELECT id FROM opencode_clients WHERE name = $1", _AWX_CLIENT_NAME
        )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )
    return client_id, credential_id


def _build_app(db_pool: asyncpg.Pool, *, api_key: str | None = _API_KEY) -> object:
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

    headers = {"Authorization": f"Bearer {api_key}"} if api_key is not None else {}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


def _new_afk_run_id() -> str:
    """Return a fresh 26-char AFK run ULID for seeding."""
    return "01J" + uuid.uuid4().hex[:23]


def _new_awx_job_id() -> int:
    """Return a unique AWX job id (fits in a signed 64-bit int)."""
    return int(uuid.uuid4().int >> 96)


async def _seed_afk_run(
    conn: asyncpg.Connection, run_id: str, *, provider: str = "github"
) -> None:
    """Insert a provisional afk_runs row the execution can attach to."""
    await conn.execute(
        "INSERT INTO afk_runs (afk_run_id, provider, first_seen_at, last_seen_at)"
        " VALUES ($1, $2, now(), now())",
        run_id,
        provider,
    )


def _make_binding_payload(
    *,
    awx_job_id: int,
    afk_run_id: str,
    outcome: str = "completed",
    resource: bool = True,
    external_session_id: str | None = "ses_recovery_test",
    failure_reason: str | None = None,
    resource_number: str = "42",
) -> dict:
    """Build a POST /api/v1/afk/executions payload."""
    payload: dict = {
        "awx_job": {"job_id": str(awx_job_id), "job_template_id": 7},
        "outcome": outcome,
        "afk_run_id": afk_run_id,
        "trigger_type": "manual",
    }
    if resource:
        payload["resource"] = {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": resource_number,
        }
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    return payload


async def _create_execution(client, payload: dict) -> dict:
    resp = await client.post("/api/v1/afk/executions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _checkpoint_url(awx_job_id: int) -> str:
    return f"/api/v1/afk/executions/{awx_job_id}/recovery-checkpoints"


# ── Fixtures ─────────────────────────────────────────────────────────────────


async def _seed_failed_execution(db_pool: asyncpg.Pool, *, failure_reason: str = "timeout"):
    """Seed an AWX client + AFK run + failed execution; return (job, run)."""
    job = _new_awx_job_id()
    run = _new_afk_run_id()
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run)
    client = _build_app(db_pool)
    async with client as c:
        await _create_execution(
            c,
            _make_binding_payload(
                awx_job_id=job,
                afk_run_id=run,
                outcome="failed",
                resource=False,
                external_session_id=None,
                failure_reason=failure_reason,
            ),
        )
    return job, run


# ═══════════════════════════════════════════════════════════════════════════
#  Schema
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_recovery_checkpoints_table_schema(db_pool: asyncpg.Pool) -> None:
    """The additive migration creates the checkpoint table with its guarantees."""
    async with db_pool.acquire() as conn:
        table = await conn.fetchval(
            "SELECT to_regclass('public.recovery_checkpoints')"
        )
        assert table is not None
        columns = {
            row["column_name"]: row
            for row in await conn.fetch(
                "SELECT column_name, is_nullable, data_type"
                " FROM information_schema.columns"
                " WHERE table_name = 'recovery_checkpoints'"
            )
        }
        assert {"id", "awx_job_id", "afk_run_id", "ref", "commit_sha", "created_at"} <= set(columns)
        assert columns["ref"]["is_nullable"] == "NO"
        assert columns["commit_sha"]["is_nullable"] == "NO"
        # Idempotency key: (awx_job_id, ref, commit_sha) UNIQUE.
        unique_cols = await conn.fetch(
            "SELECT array_agg(a.attname ORDER BY a.attname) AS cols"
            " FROM pg_constraint c"
            " JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN unnest(c.conkey) AS k(attnum) ON TRUE"
            " JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum"
            " WHERE t.relname = 'recovery_checkpoints' AND c.contype = 'u'"
            " GROUP BY c.conname"
        )
        unique_sets = [list(r["cols"]) for r in unique_cols]
        assert ["awx_job_id", "commit_sha", "ref"] in [
            sorted(cols) for cols in unique_sets
        ]


# ═══════════════════════════════════════════════════════════════════════════
#  Zero / one / many
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_zero_checkpoints_for_execution(db_pool: asyncpg.Pool) -> None:
    """An execution with no checkpoints lists an empty collection (200)."""
    job, run = await _seed_failed_execution(db_pool)
    client = _build_app(db_pool)
    async with client as c:
        resp = await c.get(_checkpoint_url(job))
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["awx_job_id"] == str(job)
        assert data["checkpoints"] == []


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_one_checkpoint_roundtrip(db_pool: asyncpg.Pool) -> None:
    """A single checkpoint persists and reads back with owning identifiers."""
    job, run = await _seed_failed_execution(db_pool)
    ref = "ai/recovery/emergency-10219/tmp/issue-57-store-optional-profile-avatars"
    sha = "bce8392508357d5316bb3a0263da3132727b78c3"
    client = _build_app(db_pool)
    async with client as c:
        resp = await c.post(_checkpoint_url(job), json={"ref": ref, "commit_sha": sha})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        created = body["data"]
        assert created["awx_job_id"] == str(job)
        assert created["afk_run_id"] == run
        assert created["ref"] == ref
        assert created["commit_sha"] == sha
        assert created["id"]
        assert created["created_at"]

        listed = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
        assert len(listed) == 1
        assert listed[0]["ref"] == ref
        assert listed[0]["commit_sha"] == sha
        assert listed[0]["afk_run_id"] == run


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_many_checkpoints_deterministic_order(db_pool: asyncpg.Pool) -> None:
    """Many checkpoints list in a stable deterministic order."""
    job, _ = await _seed_failed_execution(db_pool)
    refs = [
        "ai/recovery/emergency-10219/tmp/issue-55-store-optional-profile-avatars",
        "ai/recovery/emergency-10219/tmp/issue-56-store-optional-profile-avatars",
        "ai/recovery/emergency-10219/tmp/issue-57-store-optional-profile-avatars",
    ]
    client = _build_app(db_pool)
    async with client as c:
        for i, ref in enumerate(refs):
            resp = await c.post(
                _checkpoint_url(job), json={"ref": ref, "commit_sha": f"{i:040x}"}
            )
            assert resp.status_code == 201, resp.text
        first = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
        second = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
    assert [c["ref"] for c in first] == refs
    assert first == second


# ═══════════════════════════════════════════════════════════════════════════
#  Idempotency & distinct refs
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_duplicate_post_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """Posting the same execution + ref + SHA twice creates one row."""
    job, _ = await _seed_failed_execution(db_pool)
    payload = {
        "ref": "ai/recovery/emergency-10219/tmp/issue-57-store-optional-profile-avatars",
        "commit_sha": "bce8392508357d5316bb3a0263da3132727b78c3",
    }
    client = _build_app(db_pool)
    async with client as c:
        first = await c.post(_checkpoint_url(job), json=payload)
        second = await c.post(_checkpoint_url(job), json=payload)
        assert first.status_code == 201, first.text
        assert second.status_code == 200, second.text
        assert first.json()["data"]["id"] == second.json()["data"]["id"]
        listed = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
    assert len(listed) == 1

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM recovery_checkpoints WHERE awx_job_id = $1", job
        )
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_distinct_refs_same_sha_preserved(db_pool: asyncpg.Pool) -> None:
    """Two refs pointing at one SHA are two distinct checkpoint rows."""
    job, _ = await _seed_failed_execution(db_pool)
    sha = "bce8392508357d5316bb3a0263da3132727b78c3"
    ref_57 = "ai/recovery/emergency-10219/tmp/issue-57-store-optional-profile-avatars"
    ref_58 = "ai/recovery/emergency-10219/tmp/issue-58-store-optional-profile-avatars"
    client = _build_app(db_pool)
    async with client as c:
        r1 = await c.post(_checkpoint_url(job), json={"ref": ref_57, "commit_sha": sha})
        r2 = await c.post(_checkpoint_url(job), json={"ref": ref_58, "commit_sha": sha})
        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.json()["data"]["id"] != r2.json()["data"]["id"]
        listed = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
    assert [c["ref"] for c in listed] == [ref_57, ref_58]
    assert all(c["commit_sha"] == sha for c in listed)


# ═══════════════════════════════════════════════════════════════════════════
#  Unknown execution / auth
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_unknown_execution_fails_closed(db_pool: asyncpg.Pool) -> None:
    """Unknown awx_job_id returns 404 on both GET and POST."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
    unknown = _new_awx_job_id()
    client = _build_app(db_pool)
    async with client as c:
        get_resp = await c.get(_checkpoint_url(unknown))
        assert get_resp.status_code == 404, get_resp.text
        post_resp = await c.post(
            _checkpoint_url(unknown),
            json={"ref": "ai/recovery/x", "commit_sha": "a" * 40},
        )
        assert post_resp.status_code == 404, post_resp.text


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_read_requires_api_key(db_pool: asyncpg.Pool) -> None:
    """GET needs the Admin API Key; POST needs a bearer credential too."""
    job, _ = await _seed_failed_execution(db_pool)
    # No Authorization header at all → rejected by the global middleware.
    unauth = _build_app(db_pool, api_key=None)
    async with unauth as c:
        assert (await c.get(_checkpoint_url(job))).status_code == 401
        assert (
            await c.post(
                _checkpoint_url(job),
                json={"ref": "ai/recovery/x", "commit_sha": "a" * 40},
            )
        ).status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
#  Failed execution is never mutated
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_checkpoints_do_not_mutate_failed_execution(db_pool: asyncpg.Pool) -> None:
    """Persisting checkpoints leaves the failed execution fully unchanged."""
    job, run = await _seed_failed_execution(db_pool, failure_reason="OpenCode timeout 14400s")

    async with db_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT outcome, failure_reason, failure_summary, afk_run_id,"
            " provider, repository_url, entity_type, entity_number,"
            " external_session_id, updated_at"
            " FROM execution_bindings WHERE awx_job_id = $1",
            job,
        )
        assert before["outcome"] == "failed"

    client = _build_app(db_pool)
    ref = "ai/recovery/emergency-10219/tmp/issue-57-store-optional-profile-avatars"
    sha = "bce8392508357d5316bb3a0263da3132727b78c3"
    async with client as c:
        resp = await c.post(_checkpoint_url(job), json={"ref": ref, "commit_sha": sha})
        assert resp.status_code == 201, resp.text

        # The execution is still failed with its original failure metadata.
        exec_resp = await c.get(f"/api/v1/afk/executions/{job}")
        assert exec_resp.status_code == 200, exec_resp.text
        execution = exec_resp.json()["data"]
        assert execution["outcome"] == "failed"
        assert execution["failure_reason"] == "OpenCode timeout 14400s"
        assert execution["awx_job"]["job_id"] == str(job)

        # Checkpoints remain readable after the terminal outcome.
        listed = (await c.get(_checkpoint_url(job))).json()["data"]["checkpoints"]
        assert len(listed) == 1

    async with db_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT outcome, failure_reason, failure_summary, afk_run_id,"
            " provider, repository_url, entity_type, entity_number,"
            " external_session_id, updated_at"
            " FROM execution_bindings WHERE awx_job_id = $1",
            job,
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM recovery_checkpoints WHERE awx_job_id = $1", job
        )
    assert dict(after) == dict(before)
    assert count == 1
    assert after["afk_run_id"] == run
