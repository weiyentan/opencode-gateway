"""Integration tests for the execution-binding API end-to-end (issue #551).

Runs against the docker-compose Postgres (port 5433) and verifies the actual
database-enforced guarantees of ``/api/v1/afk/executions``:

* Authenticated POST through persistence and retrieve by AWX job ID
* Multiple executions for the same GitHub PR and GitLab MR
* Failed-then-successful retry ordering
* Identical replay is a no-op, conflicting replay is rejected
* DB UNIQUE constraints exercised at SQL level
* No raw tokens, stdout, prompts, or arbitrary AWX payloads in stored/returned data
* ADR 0028: binding writes never project ``afk_runs.status`` from child
  AWX execution outcomes — the run stays ``pending`` after every binding
  write, a completed run accepts new bindings (no completed-lifecycle
  409), and finalization happens only via the provider-event seam
  (``AsyncpgOutcomeRepository.save``)
* Issue #626: every POST payload references a pre-provisioned ``afk_run_id``
  (the legacy auto-provision path is closed) — each test seeds its run via
  ``_seed_afk_run`` and passes it to ``_make_binding_payload``

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_execution_bindings.py -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timezone
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

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

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

    The pre-existing 0024/0025 migrations evaluate ``str | None`` module-level
    annotations, which cannot import on Python 3.9 (the repo's own migration
    tests guard this with a skip).  Copy ``env.py`` and the version modules to
    a temp dir with ``from __future__ import annotations`` injected so the
    revision map builds on 3.9 without touching the shipped migrations — the
    migration bodies are byte-for-byte identical.
    """
    import shutil
    import tempfile

    src = _PROJ_ROOT / "alembic"
    dst = Path(tempfile.mkdtemp(prefix="gateway-alembic-"))
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

    # alembic/env.py's online path drives asyncpg via ``asyncio.run``, which
    # cannot execute inside this fixture's running event loop — run the
    # upgrade in a worker thread where no loop is running.
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
    """Seed the dedicated AWX execution-binding client + collector credential.

    Returns (client_id, credential_id).  The credential hash matches the
    ``_API_KEY`` bearer token so both layers of auth pass.  Idempotent —
    ``opencode_clients.name`` carries a UNIQUE constraint, so re-seeding
    the same module-scoped database across tests reuses the existing row.
    """
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


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

    Sends the bearer token matching both the API-key middleware and the
    seeded collector credential.

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


def _make_binding_payload(
    *,
    awx_job_id: int,
    afk_run_id: str,
    external_session_id: str = "ses_integration_test",
    provider: str = "github",
    repository: str = "https://github.com/acme/proj",
    resource_type: str = "pull_request",
    resource_number: str = "42",
    outcome: str = "completed",
    source_event_id: str | None = "evt_001",
    branch: str | None = "feat/test",
    title: str | None = "Test execution",
    failure_reason: str | None = None,
    started_at: str | None = "2026-08-01T12:00:00Z",
    finished_at: str | None = "2026-08-02T12:00:00Z",
) -> dict:
    """Build a POST /api/v1/afk/executions payload.

    ``afk_run_id`` is required for every new binding (issue #626): the
    payload must reference a pre-provisioned AFK Run seeded via
    ``_seed_afk_run``.
    """
    payload: dict = {
        "awx_job": {"job_id": str(awx_job_id), "job_template_id": 7},
        "external_session_id": external_session_id,
        "afk_run_id": afk_run_id,
        "resource": {
            "provider": provider,
            "repository": repository,
            "resource_type": resource_type,
            "resource_number": resource_number,
        },
        "outcome": outcome,
        "trigger_type": "manual",
    }
    if source_event_id is not None:
        payload["source_event_id"] = source_event_id
    if branch is not None:
        payload["branch"] = branch
    if title is not None:
        payload["title"] = title
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    if started_at is not None:
        payload["started_at"] = started_at
    if finished_at is not None:
        payload["finished_at"] = finished_at
    return payload


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_post_and_get_by_awx_job_id(db_pool: asyncpg.Pool) -> None:
    """Authenticated POST persists a binding; GET by awx_job_id returns it."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)  # unique per run

    payload = _make_binding_payload(awx_job_id=awx_job_id, afk_run_id=run_id)

    async with client as c:
        # POST
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        binding = body["data"]
        assert binding["awx_job"]["job_id"] == str(awx_job_id)
        assert binding["outcome"] == "completed"
        assert binding["resource"]["resource_type"] == "change_request"

        # GET by awx_job_id
        resp2 = await c.get(f"/api/v1/afk/executions/{awx_job_id}")
        assert resp2.status_code == 200, resp2.text
        data2 = resp2.json()["data"]
        assert data2["awx_job"]["job_id"] == str(awx_job_id)
        assert data2["outcome"] == "completed"

    # Verify row exists in DB
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT awx_job_id, outcome, provider, entity_type"
            " FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "completed"
        assert row["provider"] == "github"
        assert row["entity_type"] == "change_request"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_multiple_executions_for_same_github_pr(db_pool: asyncpg.Pool) -> None:
    """Multiple AWX jobs targeting the same GitHub PR are both persisted."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    resource_number = str(int(uuid.uuid4().int >> 96))
    job_1 = int(uuid.uuid4().int >> 96)
    job_2 = int(uuid.uuid4().int >> 96)

    payload_1 = _make_binding_payload(
        awx_job_id=job_1,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="failed",
        failure_reason="Timeout",
    )
    payload_2 = _make_binding_payload(
        awx_job_id=job_2,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="completed",
    )

    async with client as c:
        resp1 = await c.post("/api/v1/afk/executions", json=payload_1)
        assert resp1.status_code == 201, resp1.text
        resp2 = await c.post("/api/v1/afk/executions", json=payload_2)
        assert resp2.status_code == 201, resp2.text

        # List history for this resource
        resp_hist = await c.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": resource_number,
            },
        )
        assert resp_hist.status_code == 200, resp_hist.text
        history = resp_hist.json()["data"]
        assert len(history["bindings"]) == 2
        job_ids = {b["awx_job"]["job_id"] for b in history["bindings"]}
        assert str(job_1) in job_ids
        assert str(job_2) in job_ids


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_multiple_executions_for_same_gitlab_mr(db_pool: asyncpg.Pool) -> None:
    """Multiple AWX jobs targeting the same GitLab MR are all persisted.

    ADR 0028: a completed run does not close the lifecycle to new bindings —
    the second completed execution for the same canonical MR is accepted
    (201) and stored alongside the first.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id(), provider="gitlab")

    client = _build_app(db_pool)
    resource_number = str(int(uuid.uuid4().int >> 96))
    job_1 = int(uuid.uuid4().int >> 96)
    job_2 = int(uuid.uuid4().int >> 96)

    payload_1 = _make_binding_payload(
        awx_job_id=job_1,
        afk_run_id=run_id,
        provider="gitlab",
        repository="https://gitlab.com/cloudnative-pg/cloudnative-pg",
        resource_type="merge_request",
        resource_number=resource_number,
        outcome="completed",
    )
    payload_2 = _make_binding_payload(
        awx_job_id=job_2,
        afk_run_id=run_id,
        provider="gitlab",
        repository="https://gitlab.com/cloudnative-pg/cloudnative-pg",
        resource_type="merge_request",
        resource_number=resource_number,
        outcome="completed",
    )

    async with client as c:
        resp1 = await c.post("/api/v1/afk/executions", json=payload_1)
        assert resp1.status_code == 201, resp1.text
        # ADR 0028: the lifecycle status is untouched by binding writes, so
        # the second execution is accepted, not rejected.
        resp2 = await c.post("/api/v1/afk/executions", json=payload_2)
        assert resp2.status_code == 201, resp2.text

        resp_hist = await c.get(
            "/api/v1/afk/executions",
            params={
                "provider": "gitlab",
                "repository_url": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
                "entity_type": "change_request",
                "entity_number": resource_number,
            },
        )
        assert resp_hist.status_code == 200, resp_hist.text
        history = resp_hist.json()["data"]
        # Both bindings are stored — no completed-lifecycle rejection.
        assert len(history["bindings"]) == 2
        job_ids = {b["awx_job"]["job_id"] for b in history["bindings"]}
        assert str(job_1) in job_ids
        assert str(job_2) in job_ids
        for binding in history["bindings"]:
            assert binding["resource"]["resource_type"] == "change_request"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_failed_then_successful_retry_ordering(db_pool: asyncpg.Pool) -> None:
    """Failed execution followed by successful retry is ordered correctly."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    resource_number = str(int(uuid.uuid4().int >> 96))
    job_failed = int(uuid.uuid4().int >> 96)
    job_success = int(uuid.uuid4().int >> 96)

    # First: failed
    payload_fail = _make_binding_payload(
        awx_job_id=job_failed,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="failed",
        failure_reason="Timeout after 300s",
        started_at="2026-08-01T10:00:00Z",
        finished_at="2026-08-01T10:05:00Z",
    )
    # Second: successful retry
    payload_ok = _make_binding_payload(
        awx_job_id=job_success,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="completed",
        started_at="2026-08-01T11:00:00Z",
        finished_at="2026-08-01T11:10:00Z",
    )

    async with client as c:
        resp1 = await c.post("/api/v1/afk/executions", json=payload_fail)
        assert resp1.status_code == 201, resp1.text
        resp2 = await c.post("/api/v1/afk/executions", json=payload_ok)
        assert resp2.status_code == 201, resp2.text

        resp_hist = await c.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": resource_number,
            },
        )
        assert resp_hist.status_code == 200, resp_hist.text
        bindings = resp_hist.json()["data"]["bindings"]
        assert len(bindings) == 2
        # First binding is the failed one
        assert bindings[0]["outcome"] == "failed"
        assert bindings[0]["failure_reason"] == "Timeout after 300s"
        # Second binding is the successful retry
        assert bindings[1]["outcome"] == "completed"
        assert bindings[1]["failure_reason"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_identical_replay_is_noop(db_pool: asyncpg.Pool) -> None:
    """Identical callback replay returns existing binding without duplication."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    payload = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=f"cr-{uuid.uuid4().hex[:12]}",
    )

    async with client as c:
        resp1 = await c.post("/api/v1/afk/executions", json=payload)
        assert resp1.status_code == 201, resp1.text
        resp2 = await c.post("/api/v1/afk/executions", json=payload)
        # Idempotent replay — same data, same awx_job_id → 200, no new row
        assert resp2.status_code == 200, resp2.text
        data2 = resp2.json()["data"]
        assert data2["awx_job"]["job_id"] == str(awx_job_id)

    # Verify only one row in DB
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1, f"expected 1 row for idempotent replay, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_conflicting_replay_rejected(db_pool: asyncpg.Pool) -> None:
    """Conflicting data for same AWX job ID returns 409 without mutation."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    payload_original = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=f"cr-{uuid.uuid4().hex[:12]}",
        outcome="completed",
    )
    payload_conflict = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=payload_original["resource"]["resource_number"],
        outcome="failed",
        failure_reason="Conflict test",
    )

    async with client as c:
        resp1 = await c.post("/api/v1/afk/executions", json=payload_original)
        assert resp1.status_code == 201, resp1.text

        # A conflicting replay (changed outcome) must be rejected with 409
        # without mutating the stored binding.
        resp2 = await c.post("/api/v1/afk/executions", json=payload_conflict)
        assert resp2.status_code == 409, resp2.text
        data2 = resp2.json()
        assert data2["status"] == "error"
        assert data2["error"]["code"] == "CONFLICT"

    # Verify DB still has original outcome
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "completed"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_unique_constraint_enforced_at_sql_level(db_pool: asyncpg.Pool) -> None:
    """Direct duplicate INSERT of same awx_job_id is rejected by DB constraint."""
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO execution_bindings"
            " (awx_job_id, job_template_id, external_session_id, provider,"
            "  repository_url, entity_type, entity_number, outcome)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            awx_job_id,
            7,
            "ses_test",
            "github",
            "acme/proj",
            "change_request",
            "1",
            "completed",
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO execution_bindings"
                " (awx_job_id, job_template_id, external_session_id, provider,"
                "  repository_url, entity_type, entity_number, outcome)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                awx_job_id,
                7,
                "ses_test_2",
                "github",
                "acme/proj",
                "change_request",
                "1",
                "failed",
            )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_no_sensitive_data_in_stored_or_returned_data(
    db_pool: asyncpg.Pool,
) -> None:
    """No raw tokens, stdout, prompts, or arbitrary AWX payloads."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    payload = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=f"cr-{uuid.uuid4().hex[:12]}",
    )

    async with client as c:
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
        binding = resp.json()["data"]

        # Response must not contain sensitive keys
        for key in (
            "collector_token",
            "token",
            "stdout",
            "prompt",
            "extra_vars",
            "raw_payload",
            "credentials",
            "password",
            "secret",
        ):
            assert key not in binding, f"Sensitive key '{key}' found in response"

    # Verify DB columns do not contain sensitive data
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        # Check that columns that should be NULL or bounded are correct
        assert row["failure_reason"] is None  # No failure in this test
        # Ensure no sensitive data leaked into any text column
        for col in ("failure_reason", "source_event_id", "branch", "title"):
            val = row[col]
            if val is not None:
                assert "token" not in str(val).lower(), (
                    f"Sensitive data found in column '{col}'"
                )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_nonexistent_returns_404(db_pool: asyncpg.Pool) -> None:
    """GET for non-existent AWX job ID returns 404."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)

    async with client as c:
        resp = await c.get("/api/v1/afk/executions/999999999")
        assert resp.status_code == 404, resp.text
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"].lower()


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_read_requires_api_key(db_pool: asyncpg.Pool) -> None:
    """GET without API key returns 401/403."""
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
    # No Authorization header
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/afk/executions/42")
        assert resp.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_get_nonexistent_binding_returns_404(db_pool: asyncpg.Pool) -> None:
    """GET for non-existent AWX job ID returns 404."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)

    async with client as c:
        resp = await c.get("/api/v1/afk/executions/999999999")
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "error"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_list_bindings_empty_resource(db_pool: asyncpg.Pool) -> None:
    """List bindings for resource with no history returns empty list."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)

    async with client as c:
        resp = await c.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/empty-repo",
                "entity_type": "change_request",
                "entity_number": "0",
            },
        )
        assert resp.status_code == 200, resp.text
        history = resp.json()["data"]
        assert history["bindings"] == []


# ── Concurrency tests (issue #568) ──────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_identical_callbacks_one_201_one_200(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent identical callbacks produce one 201 and one replay/conflict.

    Two concurrent POST requests with the same ``awx_job_id`` and identical
    payload race on the INSERT.  Exactly one wins the unique constraint
    (201 Created); the other has a valid outcome of the conflict path —
    either an idempotent replay (200 OK) or a conflict (409).  The database
    must contain exactly one row for that job id.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    payload = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=f"cr-{uuid.uuid4().hex[:12]}",
    )

    async with client as c:
        # Fire both requests concurrently.
        results = await asyncio.gather(
            c.post("/api/v1/afk/executions", json=payload),
            c.post("/api/v1/afk/executions", json=payload),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted); the loser is either an idempotent
        # replay (200) or a conflict (409) — both are valid outcomes.
        assert status_codes in ([200, 201], [201, 409]), (
            f"Expected [200, 201] or [201, 409], got {status_codes}"
        )

        # A 201/200 carries valid binding data; a 409 carries a conflict
        # error body (no binding data).
        for r in results:
            if r.status_code == 409:
                body = r.json()
                assert body["status"] == "error"
                assert body["error"]["code"] == "CONFLICT"
            else:
                assert r.status_code in (200, 201)
                body = r.json()
                assert body["data"]["awx_job"]["job_id"] == str(awx_job_id)

    # Verify only one row in DB.
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1, f"Expected 1 row, got {count}"

        # The stored row must be unmutated (identical payload).
        row = await conn.fetchrow(
            "SELECT outcome, provider, entity_type FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "completed"
        assert row["provider"] == "github"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_conflicting_callbacks_one_201_one_409(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent conflicting callbacks: one 201, one 409, no mutation.

    Two concurrent POST requests with the same ``awx_job_id`` but different
    payloads race on the INSERT.  Exactly one wins (201 Created); the other
    detects the conflict (409 Conflict).  The stored row must remain
    unmutated — the original payload is preserved.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    resource_number = f"cr-{uuid.uuid4().hex[:12]}"
    payload_original = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="completed",
        title="Original",
    )
    payload_conflict = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        resource_number=resource_number,
        outcome="failed",
        title="Conflicting",
        failure_reason="intentional conflict",
    )

    async with client as c:
        # Fire both requests concurrently — one will win, one must conflict.
        results = await asyncio.gather(
            c.post("/api/v1/afk/executions", json=payload_original),
            c.post("/api/v1/afk/executions", json=payload_conflict),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted) and one 409 (conflict rejection).
        assert status_codes == [201, 409], (
            f"Expected [201, 409], got {status_codes}"
        )

        # The 409 response must carry a conflict error body.
        for r in results:
            if r.status_code == 409:
                body = r.json()
                assert body["status"] == "error"
                assert body["error"]["code"] == "CONFLICT"

    # Verify only one row and it is the ORIGINAL (not the conflicting one).
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1, f"Expected 1 row, got {count}"

        row = await conn.fetchrow(
            "SELECT outcome, title, failure_reason FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        # The winner's data must match one of the two payloads.
        # We cannot deterministically predict which one won the race,
        # but the row must be unmutated — it should match one input exactly.
        assert row["outcome"] in ("completed", "failed")
        if row["outcome"] == "completed":
            assert row["title"] == "Original"
            assert row["failure_reason"] is None
        else:
            assert row["title"] == "Conflicting"
            assert row["failure_reason"] == "intentional conflict"


# ── Two-phase lifecycle tests (issue #590) ───────────────────────────────────


def _new_afk_run_id() -> str:
    """Return a fresh 26-char AFK run ULID for seeding."""
    return "01J" + uuid.uuid4().hex[:23]


async def _seed_afk_run(
    conn: asyncpg.Connection, run_id: str, *, provider: str = "github"
) -> None:
    """Insert a provisional afk_runs row the execution can attach to."""
    await conn.execute(
        "INSERT INTO afk_runs (afk_run_id, provider, status, first_seen_at, last_seen_at)"
        " VALUES ($1, $2, 'pending', now(), now())",
        run_id,
        provider,
    )


def _make_two_phase_payload(
    *,
    awx_job_id: int,
    afk_run_id: str,
    outcome: str,
    resource: dict | None = None,
    external_session_id: str | None = None,
    failure_reason: str | None = None,
) -> dict:
    """Build a two-phase POST payload (resource/session optional)."""
    payload: dict = {
        "awx_job": {"job_id": str(awx_job_id), "job_template_id": 7},
        "outcome": outcome,
        "afk_run_id": afk_run_id,
        "trigger_type": "manual",
    }
    if resource is not None:
        payload["resource"] = resource
    if external_session_id is not None:
        payload["external_session_id"] = external_session_id
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    return payload


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_two_phase_running_then_terminal_update(db_pool: asyncpg.Pool) -> None:
    """Provision a running binding under an afk_run, then complete it in place."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        # Phase 1 — provisioning at AWX start (no change request/session yet).
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        resp = await c.post("/api/v1/afk/executions", json=provision)
        assert resp.status_code == 201, resp.text
        binding = resp.json()["data"]
        assert binding["outcome"] == "running"
        assert binding["afk_run_id"] == run_id
        assert binding["resource"] is None
        assert binding["external_session_id"] is None

        # Phase 2 — terminal update on the same row.
        resp2 = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["data"]["outcome"] == "completed"

    # Same row — transitioned, never duplicated.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, provider, repository_url, entity_type, entity_number,"
            " external_session_id, afk_run_id, finished_at"
            " FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "completed"
        assert row["provider"] == "github"
        assert row["repository_url"] == "github.com/acme/proj"
        assert row["entity_type"] == "change_request"
        assert row["entity_number"] == "99"
        assert row["external_session_id"] == "ses_terminal"
        assert row["afk_run_id"] == run_id
        assert row["finished_at"] is not None
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_two_phase_terminal_resource_conflicts_with_lifecycle(
    db_pool: asyncpg.Pool,
) -> None:
    """Issue #600 review: a terminal update filling a resource that contradicts
    the owning lifecycle's change request is a 409 that never mutates the
    execution row."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())
        # The lifecycle is authoritative for PR #5 (as the correlator bound it).
        await conn.execute(
            """
            UPDATE afk_runs
            SET change_request_provider = 'github',
                change_request_repository = 'github.com/acme/proj',
                change_request_external_id = '5'
            WHERE afk_run_id = $1
            """,
            run_id,
        )

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        resp = await c.post("/api/v1/afk/executions", json=provision)
        assert resp.status_code == 201, resp.text

        # Terminal update fills PR #99 — contradicts the lifecycle's PR #5.
        resp2 = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp2.status_code == 409, resp2.text
        assert resp2.json()["error"]["code"] == "CONFLICT"

    # The execution row is untouched — still running, never diverged to PR #99.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, provider, entity_number FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "running"
        assert row["provider"] is None
        assert row["entity_number"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_auto_created_run_persists_change_request_columns(
    db_pool: asyncpg.Pool,
) -> None:
    """A POST attaching a binding to a pre-provisioned AFK Run persists the
    change-request identity on the run in the same transaction — the
    lifecycle is authoritative for the PR immediately (issue #600 review,
    adapted for issue #626: the run is seeded and referenced explicitly)."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    resource_number = f"cr-{uuid.uuid4().hex[:12]}"
    payload = _make_binding_payload(
        awx_job_id=awx_job_id, afk_run_id=run_id, resource_number=resource_number
    )

    async with client as c:
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["afk_run_id"] == run_id

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT change_request_provider, change_request_repository,"
            " change_request_external_id FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        assert row["change_request_provider"] == "github"
        assert row["change_request_repository"] == "github.com/acme/proj"
        assert row["change_request_external_id"] == resource_number


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_auto_provision_reuses_existing_lifecycle_for_same_pr(
    db_pool: asyncpg.Pool,
) -> None:
    """Two AWX jobs referencing the same pre-provisioned lifecycle with the
    same canonical PR both attach to it (201, same afk_run_id) — no second
    afk_runs row.

    ADR 0028: a *failed* execution accepts a retry — binding writes never
    close the lifecycle, and status is never projected from binding
    outcomes.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    resource_number = f"cr-{uuid.uuid4().hex[:12]}"
    job_1 = int(uuid.uuid4().int >> 96)
    job_2 = int(uuid.uuid4().int >> 96)

    async with client as c:
        # First execution FAILS — the lifecycle stays open (ADR 0028).
        resp1 = await c.post(
            "/api/v1/afk/executions",
            json=_make_binding_payload(
                awx_job_id=job_1,
                afk_run_id=run_id,
                resource_number=resource_number,
                outcome="failed",
                failure_reason="AWX job crashed",
            ),
        )
        assert resp1.status_code == 201, resp1.text
        assert resp1.json()["data"]["afk_run_id"] == run_id

        # Second POST, different AWX job, same canonical PR, same lifecycle —
        # the failed lifecycle accepts the retry.
        resp2 = await c.post(
            "/api/v1/afk/executions",
            json=_make_binding_payload(
                awx_job_id=job_2,
                afk_run_id=run_id,
                resource_number=resource_number,
                outcome="completed",
            ),
        )
        assert resp2.status_code == 201, resp2.text
        assert resp2.json()["data"]["afk_run_id"] == run_id

    # Exactly one lifecycle owns the canonical PR; both bindings attach to
    # it, and the parent status is untouched by binding writes (ADR 0028).
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT afk_run_id FROM afk_runs"
            " WHERE change_request_provider = 'github'"
            "   AND change_request_repository = 'github.com/acme/proj'"
            "   AND change_request_external_id = $1",
            resource_number,
        )
        assert len(rows) == 1, f"expected one lifecycle, got {len(rows)}"
        assert rows[0]["afk_run_id"] == run_id
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2
        status = await conn.fetchval(
            "SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        # ADR 0028: binding writes never project afk_runs.status.
        assert status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_auto_provision_reuses_existing_lifecycle_for_same_mr(
    db_pool: asyncpg.Pool,
) -> None:
    """Two AWX jobs referencing the same pre-provisioned lifecycle for the
    same canonical MR both attach to it (201, same afk_run_id) — ADR 0028:
    a completed run does not reject new bindings, and no second afk_runs
    row is created."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id(), provider="gitlab")

    client = _build_app(db_pool)
    resource_number = f"mr-{uuid.uuid4().hex[:12]}"
    job_1 = int(uuid.uuid4().int >> 96)
    job_2 = int(uuid.uuid4().int >> 96)
    repository = "https://gitlab.com/cloudnative-pg/cloudnative-pg"

    async with client as c:
        resp1 = await c.post(
            "/api/v1/afk/executions",
            json=_make_binding_payload(
                awx_job_id=job_1,
                afk_run_id=run_id,
                provider="gitlab",
                repository=repository,
                resource_type="merge_request",
                resource_number=resource_number,
            ),
        )
        assert resp1.status_code == 201, resp1.text
        assert resp1.json()["data"]["afk_run_id"] == run_id

        # ADR 0028: a completed run does not reject new bindings — the
        # second execution is accepted and attached to the same lifecycle.
        resp2 = await c.post(
            "/api/v1/afk/executions",
            json=_make_binding_payload(
                awx_job_id=job_2,
                afk_run_id=run_id,
                provider="gitlab",
                repository=repository,
                resource_type="merge_request",
                resource_number=resource_number,
            ),
        )
        assert resp2.status_code == 201, resp2.text
        assert resp2.json()["data"]["afk_run_id"] == run_id

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT afk_run_id FROM afk_runs"
            " WHERE change_request_provider = 'gitlab'"
            "   AND change_request_repository = 'gitlab.com/cloudnative-pg/cloudnative-pg'"
            "   AND change_request_external_id = $1",
            resource_number,
        )
        assert len(rows) == 1
        assert rows[0]["afk_run_id"] == run_id
        # Both executions are stored against the single lifecycle.
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_auto_provision_same_pr_single_lifecycle(
    db_pool: asyncpg.Pool,
) -> None:
    """Two concurrent POSTs referencing the same pre-provisioned lifecycle
    with the same PR both attach to it (201, shared afk_run_id) — ADR 0028:
    a completed run does not reject new bindings.  Exactly one afk_runs row
    (the seeded one) and two stored bindings."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    resource_number = f"cr-{uuid.uuid4().hex[:12]}"
    job_a = int(uuid.uuid4().int >> 96)
    job_b = int(uuid.uuid4().int >> 96)

    async with client as c:
        results = await asyncio.gather(
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=job_a,
                    afk_run_id=run_id,
                    resource_number=resource_number,
                ),
            ),
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=job_b,
                    afk_run_id=run_id,
                    resource_number=resource_number,
                ),
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        # ADR 0028: no completed-lifecycle rejection — both executions are
        # accepted (201) and attach to the seeded lifecycle.
        assert status_codes == [201, 201], (
            f"Expected [201, 201], got {status_codes}: "
            + "; ".join(r.text for r in results)
        )
        for r in results:
            assert r.json()["data"]["afk_run_id"] == run_id

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT afk_run_id FROM afk_runs"
            " WHERE change_request_provider = 'github'"
            "   AND change_request_repository = 'github.com/acme/proj'"
            "   AND change_request_external_id = $1",
            resource_number,
        )
        assert len(rows) == 1, f"expected one lifecycle, got {len(rows)}"
        assert rows[0]["afk_run_id"] == run_id
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2, f"expected two bindings, got {count}"
        status = await conn.fetchval(
            "SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        # ADR 0028: binding writes never project afk_runs.status.
        assert status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_post_accepts_resource_provider_differing_from_run_provider(
    db_pool: asyncpg.Pool,
) -> None:
    """Issue #600 review (finding #7, Option A): afk_runs.provider is
    trigger/source provenance — a POST with a supplied afk_run_id whose
    stored provider differs from the resource's provider is accepted, and
    the change request is bound with its own canonical provider."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id(), provider="gitlab")

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    resource_number = f"cr-{uuid.uuid4().hex[:12]}"
    payload = _make_binding_payload(
        awx_job_id=awx_job_id,
        afk_run_id=run_id,
        provider="github",
        resource_number=resource_number,
    )

    async with client as c:
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT provider, change_request_provider,"
            " change_request_repository, change_request_external_id"
            " FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        assert row["provider"] == "gitlab"  # source provenance unchanged
        assert row["change_request_provider"] == "github"  # canonical CR provider
        assert row["change_request_repository"] == "github.com/acme/proj"
        assert row["change_request_external_id"] == resource_number


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_same_lifecycle_same_change_request_both_succeed(
    db_pool: asyncpg.Pool,
) -> None:
    """Issue #600 review (finding #6) + ADR 0028: two concurrent terminal
    POSTs attaching different AWX jobs to the same lifecycle with the same
    change request serialize on the parent lock — both succeed (201) since
    a completed run does not reject new bindings.  Both bindings are stored
    and the parent status is untouched by binding writes."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id_a = int(uuid.uuid4().int >> 96)
    awx_job_id_b = int(uuid.uuid4().int >> 96)
    resource_number = f"cr-{uuid.uuid4().hex[:12]}"

    async with client as c:
        results = await asyncio.gather(
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=awx_job_id_a,
                    afk_run_id=run_id,
                    resource_number=resource_number,
                ),
            ),
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=awx_job_id_b,
                    afk_run_id=run_id,
                    resource_number=resource_number,
                ),
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        # ADR 0028: a completed run does not reject new bindings — both
        # executions attach to the lifecycle.
        assert status_codes == [201, 201], (
            f"Expected [201, 201], got {status_codes}"
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT change_request_provider, change_request_repository,"
            " change_request_external_id FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        assert row["change_request_provider"] == "github"
        assert row["change_request_repository"] == "github.com/acme/proj"
        assert row["change_request_external_id"] == resource_number
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2
        status = await conn.fetchval(
            "SELECT status FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        # ADR 0028: binding writes never project afk_runs.status.
        assert status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_same_lifecycle_different_change_request_one_conflicts(
    db_pool: asyncpg.Pool,
) -> None:
    """Issue #600 review (finding #6): two concurrent terminal POSTs attaching
    different AWX jobs to the same lifecycle with different change requests —
    exactly one 201 and one 409; the lifecycle owns exactly one of them."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id_a = int(uuid.uuid4().int >> 96)
    awx_job_id_b = int(uuid.uuid4().int >> 96)
    resource_number_a = f"cr-{uuid.uuid4().hex[:12]}"
    resource_number_b = f"cr-{uuid.uuid4().hex[:12]}"

    async with client as c:
        results = await asyncio.gather(
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=awx_job_id_a,
                    afk_run_id=run_id,
                    resource_number=resource_number_a,
                ),
            ),
            c.post(
                "/api/v1/afk/executions",
                json=_make_binding_payload(
                    awx_job_id=awx_job_id_b,
                    afk_run_id=run_id,
                    resource_number=resource_number_b,
                ),
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        assert status_codes == [201, 409], (
            f"Expected [201, 409], got {status_codes}"
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT change_request_external_id FROM afk_runs"
            " WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        assert row["change_request_external_id"] in (
            resource_number_a,
            resource_number_b,
        )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_failed_terminal_update_without_resource_or_session(
    db_pool: asyncpg.Pool,
) -> None:
    """A failed execution persists without a change request or session."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        assert (await c.post("/api/v1/afk/executions", json=provision)).status_code == 201

        resp = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}",
            json={"outcome": "failed", "failure_reason": "AWX job crashed"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["outcome"] == "failed"
        assert resp.json()["data"]["resource"] is None

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, failure_reason, provider, external_session_id"
            " FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "failed"
        assert row["failure_reason"] == "AWX job crashed"
        assert row["provider"] is None
        assert row["external_session_id"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_callback_without_resource_or_session(
    db_pool: asyncpg.Pool,
) -> None:
    """A direct terminal callback (no running phase) may omit resource/session."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        payload = _make_two_phase_payload(
            awx_job_id=awx_job_id,
            afk_run_id=run_id,
            outcome="failed",
            failure_reason="crashed before launch",
        )
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
        binding = resp.json()["data"]
        assert binding["outcome"] == "failed"
        assert binding["resource"] is None
        assert binding["external_session_id"] is None

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, provider, external_session_id FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "failed"
        assert row["provider"] is None
        assert row["external_session_id"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_update_identical_replay_is_noop(db_pool: asyncpg.Pool) -> None:
    """Repeating an identical terminal update is idempotent (200, no mutation)."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    update_payload = {
        "outcome": "completed",
        "finished_at": "2026-08-02T12:00:00Z",
        "external_session_id": "ses_terminal",
        "resource": {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": f"cr-{uuid.uuid4().hex[:12]}",
        },
    }

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        assert (await c.post("/api/v1/afk/executions", json=provision)).status_code == 201

        first = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}", json=update_payload
        )
        assert first.status_code == 200
        second = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}", json=update_payload
        )
        assert second.status_code == 200

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1
        outcome = await conn.fetchval(
            "SELECT outcome FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert outcome == "completed"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_update_conflicting_replay_rejected(db_pool: asyncpg.Pool) -> None:
    """A conflicting terminal update returns 409 and never overwrites history."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        assert (await c.post("/api/v1/afk/executions", json=provision)).status_code == 201

        completed = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}",
            json={
                "outcome": "completed",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": f"cr-{uuid.uuid4().hex[:12]}",
                },
            },
        )
        assert completed.status_code == 200

        conflicting = await c.patch(
            f"/api/v1/afk/executions/{awx_job_id}",
            json={"outcome": "failed", "failure_reason": "late failure"},
        )
        assert conflicting.status_code == 409, conflicting.text
        body = conflicting.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "CONFLICT"

    # History is never overwritten — the completed outcome stands.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, failure_reason FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] == "completed"
        assert row["failure_reason"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_failure_summary_redacted_before_persist(db_pool: asyncpg.Pool) -> None:
    """Secret-bearing failure summaries are redacted before persistence."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        payload = _make_two_phase_payload(
            awx_job_id=awx_job_id,
            afk_run_id=run_id,
            outcome="failed",
            failure_reason="auth failed: Bearer abc123secret and GITHUB_TOKEN=ghp_leak",
        )
        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        failure_reason = await conn.fetchval(
            "SELECT failure_reason FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert failure_reason == "auth failed: Bearer *** and GITHUB_TOKEN=***"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_identical_terminal_updates(db_pool: asyncpg.Pool) -> None:
    """Concurrent identical terminal updates serialize: both 200, one row."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    update_payload = {
        "outcome": "completed",
        "finished_at": "2026-08-02T12:00:00Z",
        "external_session_id": "ses_terminal",
        "resource": {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": f"cr-{uuid.uuid4().hex[:12]}",
        },
    }

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        assert (await c.post("/api/v1/afk/executions", json=provision)).status_code == 201

        results = await asyncio.gather(
            c.patch(f"/api/v1/afk/executions/{awx_job_id}", json=update_payload),
            c.patch(f"/api/v1/afk/executions/{awx_job_id}", json=update_payload),
        )
        assert sorted(r.status_code for r in results) == [200, 200]

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1
        outcome = await conn.fetchval(
            "SELECT outcome FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert outcome == "completed"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_conflicting_terminal_updates(db_pool: asyncpg.Pool) -> None:
    """Concurrent conflicting terminal updates: one 200, one 409, no overwrite."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        provision = _make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
        )
        assert (await c.post("/api/v1/afk/executions", json=provision)).status_code == 201

        results = await asyncio.gather(
            c.patch(
                f"/api/v1/afk/executions/{awx_job_id}",
                json={
                    "outcome": "completed",
                    "external_session_id": "ses_terminal",
                    "resource": {
                        "provider": "github",
                        "repository": "https://github.com/acme/proj",
                        "resource_type": "pull_request",
                        "resource_number": f"cr-{uuid.uuid4().hex[:12]}",
                    },
                },
            ),
            c.patch(
                f"/api/v1/afk/executions/{awx_job_id}",
                json={"outcome": "failed", "failure_reason": "competing failure"},
            ),
        )
        assert sorted(r.status_code for r in results) == [200, 409]
        for r in results:
            if r.status_code == 409:
                assert r.json()["error"]["code"] == "CONFLICT"

    # The stored outcome must equal the winner's terminal outcome — the
    # loser's conflicting outcome is never applied.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome, failure_reason FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert row is not None
        assert row["outcome"] in ("completed", "failed")
        if row["outcome"] == "completed":
            assert row["failure_reason"] is None
        else:
            assert row["failure_reason"] == "competing failure"


# ── AFK run status non-projection tests (ADR 0028, issue #639) ───────────────


async def _run_row(conn: asyncpg.Connection, run_id: str) -> asyncpg.Record:
    """Read the projection-relevant afk_runs columns for one lifecycle."""
    row = await conn.fetchrow(
        "SELECT status, finished_at, outcome_status, outcome,"
        " change_request_provider, change_request_repository,"
        " change_request_external_id"
        " FROM afk_runs WHERE afk_run_id = $1",
        run_id,
    )
    assert row is not None, f"run {run_id} not found"
    return row


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_running_creation_does_not_project_status_from_binding_outcome(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: creating a running binding records the binding without
    projecting status from the binding outcome — the run stays pending."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        resp = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
            ),
        )
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        row = await _run_row(conn, run_id)
        # ADR 0028: binding writes never project afk_runs.status.
        assert row["status"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_direct_terminal_creation_records_binding_without_status_projection(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: a direct terminal binding is recorded without projecting
    status onto the parent — the run stays pending, and enrichment columns
    remain untouched."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        resp = await c.post(
            "/api/v1/afk/executions",
            json={
                **_make_two_phase_payload(
                    awx_job_id=awx_job_id, afk_run_id=run_id, outcome="completed"
                ),
                "external_session_id": "ses_direct",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": str(int(uuid.uuid4().int >> 96)),
                },
            },
        )
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        row = await _run_row(conn, run_id)
        # ADR 0028: binding writes never project afk_runs.status.
        assert row["status"] == "pending"
        # The enrichment columns are untouched.
        assert row["finished_at"] is None
        assert row["outcome_status"] is None
        assert row["outcome"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_direct_failed_and_cancelled_creation_leave_parent_pending(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: direct failed / cancelled bindings are recorded without
    projecting status — both runs stay pending."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_failed := _new_afk_run_id())
        await _seed_afk_run(conn, run_cancelled := _new_afk_run_id())

    client = _build_app(db_pool)
    job_failed = int(uuid.uuid4().int >> 96)
    job_cancelled = int(uuid.uuid4().int >> 96)

    async with client as c:
        resp = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=job_failed,
                afk_run_id=run_failed,
                outcome="failed",
                failure_reason="AWX job crashed",
            ),
        )
        assert resp.status_code == 201, resp.text
        resp2 = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=job_cancelled,
                afk_run_id=run_cancelled,
                outcome="cancelled",
            ),
        )
        assert resp2.status_code == 201, resp2.text

    async with db_pool.acquire() as conn:
        # ADR 0028: binding writes never project afk_runs.status.
        assert (await _run_row(conn, run_failed))["status"] == "pending"
        assert (await _run_row(conn, run_cancelled))["status"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_patch_records_binding_without_status_projection(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: transitioning the running binding to terminal records the
    new outcome without projecting status onto the run — the run stays
    pending regardless of the binding outcome mix."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    job_running = int(uuid.uuid4().int >> 96)
    job_failed = int(uuid.uuid4().int >> 96)

    async with client as c:
        # Phase one: a running binding.
        assert (
            await c.post(
                "/api/v1/afk/executions",
                json=_make_two_phase_payload(
                    awx_job_id=job_running, afk_run_id=run_id, outcome="running"
                ),
            )
        ).status_code == 201
        # A second, direct terminal (failed) attempt is recorded alongside.
        assert (
            await c.post(
                "/api/v1/afk/executions",
                json=_make_two_phase_payload(
                    awx_job_id=job_failed,
                    afk_run_id=run_id,
                    outcome="failed",
                    failure_reason="first attempt failed",
                ),
            )
        ).status_code == 201
        async with db_pool.acquire() as conn:
            # ADR 0028: binding writes never project afk_runs.status.
            assert (await _run_row(conn, run_id))["status"] == "pending"

        # Phase two: the running binding completes.
        resp = await c.patch(
            f"/api/v1/afk/executions/{job_running}",
            json={
                "outcome": "completed",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": str(int(uuid.uuid4().int >> 96)),
                },
            },
        )
        assert resp.status_code == 200, resp.text

    async with db_pool.acquire() as conn:
        # ADR 0028: binding writes never project afk_runs.status.
        assert (await _run_row(conn, run_id))["status"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_identical_replay_stays_idempotent_without_status_projection(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: an identical replay stays idempotent and never duplicates
    the binding; the replay is a binding write, so it never projects status
    — the run keeps whatever status it had (pending here)."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)
    payload = {
        **_make_two_phase_payload(
            awx_job_id=awx_job_id, afk_run_id=run_id, outcome="completed"
        ),
        "external_session_id": "ses_replay",
        "resource": {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": str(int(uuid.uuid4().int >> 96)),
        },
    }

    async with client as c:
        assert (await c.post("/api/v1/afk/executions", json=payload)).status_code == 201

        # Simulate any pre-existing status before the replay — ADR 0028
        # forbids binding writes from projecting status, so the replay must
        # leave it exactly as stored.
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE afk_runs SET status = 'pending' WHERE afk_run_id = $1",
                run_id,
            )

        resp = await c.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 200, resp.text

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        assert count == 1
        # ADR 0028: binding writes never project afk_runs.status — the
        # replayed binding does not touch the stored status.
        assert (await _run_row(conn, run_id))["status"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_failed_run_accepts_new_running_binding_without_status_change(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: a failed lifecycle accepts a new running binding with a new
    AWX job identity (retry) without projecting status — the run stays
    pending."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    job_failed = int(uuid.uuid4().int >> 96)
    job_retry = int(uuid.uuid4().int >> 96)

    async with client as c:
        assert (
            await c.post(
                "/api/v1/afk/executions",
                json=_make_two_phase_payload(
                    awx_job_id=job_failed,
                    afk_run_id=run_id,
                    outcome="failed",
                    failure_reason="AWX job crashed",
                ),
            )
        ).status_code == 201
        async with db_pool.acquire() as conn:
            # ADR 0028: binding writes never project afk_runs.status.
            assert (await _run_row(conn, run_id))["status"] == "pending"

        resp = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=job_retry, afk_run_id=run_id, outcome="running"
            ),
        )
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        # Both bindings survive — the failed history is preserved.
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count == 2
        # ADR 0028: binding writes never project afk_runs.status.
        assert (await _run_row(conn, run_id))["status"] == "pending"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_completed_run_accepts_new_binding(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: a completed run does not reject new bindings — a new
    running execution is accepted (201) and stored without projecting
    status or modifying the stored history."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    job_completed = int(uuid.uuid4().int >> 96)
    job_new = int(uuid.uuid4().int >> 96)

    async with client as c:
        assert (
            await c.post(
                "/api/v1/afk/executions",
                json={
                    **_make_two_phase_payload(
                        awx_job_id=job_completed,
                        afk_run_id=run_id,
                        outcome="completed",
                    ),
                    "external_session_id": "ses_done",
                    "resource": {
                        "provider": "github",
                        "repository": "https://github.com/acme/proj",
                        "resource_type": "pull_request",
                        "resource_number": str(int(uuid.uuid4().int >> 96)),
                    },
                },
            )
        ).status_code == 201

        # A new running execution on the completed lifecycle is accepted.
        resp = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=job_new, afk_run_id=run_id, outcome="running"
            ),
        )
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        before_new = await _run_row(conn, run_id)
        # The new binding was stored.
        count_new = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            job_new,
        )
        assert count_new == 1
        # History preserved: both bindings exist, status untouched by
        # binding writes (ADR 0028), enrichment columns untouched.
        assert (await _run_row(conn, run_id))["status"] == "pending"
        assert (await _run_row(conn, run_id))["status"] == before_new["status"]
        count_total = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE afk_run_id = $1",
            run_id,
        )
        assert count_total == 2
        outcome = await conn.fetchval(
            "SELECT outcome FROM execution_bindings WHERE awx_job_id = $1",
            job_completed,
        )
        assert outcome == "completed"
        new_outcome = await conn.fetchval(
            "SELECT outcome FROM execution_bindings WHERE awx_job_id = $1",
            job_new,
        )
        assert new_outcome == "running"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_binding_write_touches_no_run_columns(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: a binding write touches no afk_runs columns at all —
    status, finished_at, outcome_status, outcome, and the change-request
    columns are all preserved as stored."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())
        # Simulate backfill enrichment that the binding write must preserve.
        await conn.execute(
            """
            UPDATE afk_runs
            SET finished_at = now(),
                outcome_status = 'merged',
                outcome = '{"status":"merged"}'::jsonb,
                change_request_provider = 'github',
                change_request_repository = 'github.com/acme/proj',
                change_request_external_id = '999'
            WHERE afk_run_id = $1
            """,
            run_id,
        )
        before = await _run_row(conn, run_id)

    client = _build_app(db_pool)
    awx_job_id = int(uuid.uuid4().int >> 96)

    async with client as c:
        resp = await c.post(
            "/api/v1/afk/executions",
            json=_make_two_phase_payload(
                awx_job_id=awx_job_id, afk_run_id=run_id, outcome="running"
            ),
        )
        assert resp.status_code == 201, resp.text

    async with db_pool.acquire() as conn:
        after = await _run_row(conn, run_id)
        # ADR 0028: binding writes never project afk_runs.status — the run
        # row is untouched in every column.
        assert after["status"] == "pending"
        assert after["finished_at"] == before["finished_at"]
        assert after["outcome_status"] == before["outcome_status"]
        assert after["outcome"] == before["outcome"]
        assert after["change_request_provider"] == before["change_request_provider"]
        assert after["change_request_repository"] == before["change_request_repository"]
        assert after["change_request_external_id"] == before["change_request_external_id"]


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_binding_writes_accept_both_without_status_projection(
    db_pool: asyncpg.Pool,
) -> None:
    """ADR 0028: concurrent binding writes both succeed — a completed run
    never rejects a new binding — and the run status is never projected
    from binding outcomes, so it stays pending whatever the interleaving."""
    from afk_outcomes.run_status import resolve_afk_run_status

    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_afk_run(conn, run_id := _new_afk_run_id())

    client = _build_app(db_pool)
    job_terminal = int(uuid.uuid4().int >> 96)
    job_running = int(uuid.uuid4().int >> 96)

    async with client as c:
        results = await asyncio.gather(
            c.post(
                "/api/v1/afk/executions",
                json=_make_two_phase_payload(
                    awx_job_id=job_terminal,
                    afk_run_id=run_id,
                    outcome="failed",
                    failure_reason="terminal attempt",
                ),
            ),
            c.post(
                "/api/v1/afk/executions",
                json=_make_two_phase_payload(
                    awx_job_id=job_running, afk_run_id=run_id, outcome="running"
                ),
            ),
        )
        codes = sorted(r.status_code for r in results)
        # ADR 0028: no completed-lifecycle rejection — both writes succeed.
        assert codes == [201, 201], (
            f"unexpected status codes: {codes}"
        )

    async with db_pool.acquire() as conn:
        outcomes = [
            row["outcome"]
            for row in await conn.fetch(
                "SELECT outcome FROM execution_bindings"
                " WHERE afk_run_id = $1 AND outcome IS NOT NULL",
                run_id,
            )
        ]
        # The pure-domain projection of the recorded outcomes is computed
        # for reference only — under ADR 0028 it is never written to the
        # run row.
        _projected = resolve_afk_run_status(outcomes)
        assert (await _run_row(conn, run_id))["status"] == "pending"
