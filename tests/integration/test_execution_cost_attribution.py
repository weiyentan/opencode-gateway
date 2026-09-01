"""Integration tests for AWX Execution Cost subtotals (issue #628).

Runs against the standalone test Postgres (port 5433) and proves the
per-execution subtotal behavior end-to-end through the change-request detail
API:

* complete attribution — the subtotal aggregates every explicitly
  associated session (nested subagent sessions included);
* legacy fallback — pre-#627 bindings (JSONB NULL) keep the singular
  compatibility path;
* missing attribution — unavailable (NULL), never zero;
* unknown session cost — unavailable (NULL), never a partial amount;
* duplicate/replayed usage — canonical aggregate accounting keeps the
  subtotal stable (no double-count);
* multiple executions under one AFK Run — independent subtotals.

Prerequisites::

    GATEWAY_DATABASE_HOST=localhost GATEWAY_DATABASE_PORT=5433 \
    GATEWAY_DATABASE_NAME=opencode_gateway_test \
    GATEWAY_DATABASE_USER=opencode_test GATEWAY_DATABASE_PASSWORD=opencode_test \
    GATEWAY_ENV=development
    pytest tests/integration/test_execution_cost_attribution.py -v -m integration
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from fastapi import Request

from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = __import__("datetime").timezone.utc  # noqa: UP017

_API_KEY = "test-api-key"
_AWX_CLIENT_NAME = "awx-execution-bindings"


def _dsn() -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


async def _can_connect() -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn=_dsn(), timeout=5), timeout=10.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
async def _integration_db_available() -> bool:
    if not await _can_connect():
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


def _migration_script_dir() -> str:
    """Python-3.9-import-safe copy of the alembic script directory."""
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


@pytest.fixture(scope="module")
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

    await pool.close()


def _build_app(db_pool: asyncpg.Pool):
    from httpx import ASGITransport, AsyncClient

    from app.core.factory import create_app
    from app.db.session import get_session

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


async def _seed_awx_client(conn: asyncpg.Connection) -> None:
    await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
        _AWX_CLIENT_NAME,
    )
    client_id = await conn.fetchval(
        "SELECT id FROM opencode_clients WHERE name = $1", _AWX_CLIENT_NAME
    )
    await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3)",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )


async def _seed_gateway_session(
    conn: asyncpg.Connection,
    *,
    external_session_id: str,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: Decimal | None = Decimal("0.05"),
) -> uuid.UUID:
    """Seed a canonical session aggregate; return its internal UUID."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"cli-{uuid.uuid4()}",
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        "dummy-token-hash",
        "pref",
    )
    database_id = await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id)"
        " VALUES ($1, $2) RETURNING id",
        credential_id,
        client_id,
    )
    return await conn.fetchval(
        "INSERT INTO sessions"
        " (client_id, source_database_id, external_session_id,"
        "  first_message_at, last_message_at, total_input_tokens,"
        "  total_output_tokens, total_cache_read_tokens,"
        "  total_cache_write_tokens, total_estimated_cost_usd)"
        " VALUES ($1, $2, $3, now() - interval '1 hour', now(), $4, $5, $6,"
        "         $7, $8) RETURNING id",
        client_id,
        database_id,
        external_session_id,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        cost,
    )


async def _seed_execution(
    conn: asyncpg.Connection,
    *,
    awx_job_id: int,
    external_session_ids: list[str] | None,
    external_session_id_singular: str | None = None,
    afk_run_id: str | None = None,
    outcome: str = "completed",
) -> None:
    """Insert one execution_bindings row directly (bypasses the write API)."""
    if afk_run_id is None:
        afk_run_id = f"01J628{'0' * 11}{awx_job_id:03d}"[:26]
        await conn.execute(
            "INSERT INTO afk_runs (afk_run_id, provider, status,"
            " first_seen_at, last_seen_at)"
            " VALUES ($1, 'github', 'completed', now(), now())"
            " ON CONFLICT (afk_run_id) DO NOTHING",
            afk_run_id,
        )
    await conn.execute(
        """
        INSERT INTO execution_bindings
            (awx_job_id, job_template_id, external_session_id, provider,
             repository_url, entity_type, entity_number, outcome,
             afk_run_id, trigger_type, external_session_ids,
             created_at, updated_at)
        VALUES ($1, 7, $2, 'github', 'github.com/acme/proj',
                'change_request', $3, $4, $5, 'manual', $6, now(), now())
        """,
        awx_job_id,
        external_session_id_singular,
        str(awx_job_id),
        outcome,
        afk_run_id,
        json.dumps(external_session_ids)
        if external_session_ids is not None
        else (json.dumps([external_session_id_singular]) if external_session_id_singular else None),
    )


def _detail_endpoint(number: int) -> str:
    # The detail path identity is matched verbatim against the stored
    # ``repository_url`` / ``change_request_repository`` — the seeded
    # normalized identity (host-qualified) is what Aurora Glass navigates
    # with (``crIdentity`` → ``buildChangeRequestDetailPath``).
    return f"/api/v1/afk-outcomes/change-requests/github/github.com%2Facme%2Fproj/{number}"


async def _fetch_executions(client, number: int) -> list[dict]:
    resp = await client.get(_detail_endpoint(number))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["executions"]


def _execution_by_job(executions: list[dict], job_id: str) -> dict:
    matches = [e for e in executions if e["awx_job"]["job_id"] == job_id]
    assert matches, f"execution {job_id} not in {executions}"
    return matches[0]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_complete_attribution_sums_all_sessions(db_pool) -> None:
    """The subtotal aggregates every explicitly associated session."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_a = f"ses_628_a_{uuid.uuid4().hex[:8]}"
        ext_b = f"ses_628_b_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(conn, external_session_id=ext_a, cost=Decimal("0.10"))
        await _seed_gateway_session(
            conn,
            external_session_id=ext_b,
            input_tokens=2000,
            output_tokens=1500,
            cost=Decimal("0.20"),
        )
        await _seed_execution(
            conn,
            awx_job_id=628001,
            external_session_ids=[ext_a, ext_b],
            external_session_id_singular=ext_a,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628001)
        item = _execution_by_job(executions, "628001")
        assert Decimal(str(item["estimated_cost_usd"])) == Decimal("0.30")
        assert item["total_input_tokens"] == 3000
        assert item["total_output_tokens"] == 2000


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_nested_explicit_session_included(db_pool) -> None:
    """A nested (subagent) session counts when explicitly associated."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_root = f"ses_628_root_{uuid.uuid4().hex[:8]}"
        ext_child = f"ses_628_child_{uuid.uuid4().hex[:8]}"
        root_id = await _seed_gateway_session(  # noqa: F841 — root parent linkage
            conn, external_session_id=ext_root, cost=Decimal("0.10")
        )
        child_id = await _seed_gateway_session(
            conn,
            external_session_id=ext_child,
            input_tokens=500,
            output_tokens=250,
            cost=Decimal("0.05"),
        )
        # Mark the child as a descendant of the root (parent linkage is the
        # source external session id).
        await conn.execute(
            "UPDATE sessions SET parent_session_id = $1 WHERE id = $2",
            ext_root,
            child_id,
        )
        await _seed_execution(
            conn,
            awx_job_id=628002,
            external_session_ids=[ext_root, ext_child],
            external_session_id_singular=ext_root,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628002)
        item = _execution_by_job(executions, "628002")
        assert Decimal(str(item["estimated_cost_usd"])) == Decimal("0.15")
        assert item["total_input_tokens"] == 1500


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_legacy_singular_fallback(db_pool) -> None:
    """A pre-#627 row (JSONB NULL) keeps the singular compatibility path."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_legacy = f"ses_628_legacy_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(
            conn,
            external_session_id=ext_legacy,
            input_tokens=700,
            output_tokens=300,
            cost=Decimal("0.07"),
        )
        await _seed_execution(
            conn,
            awx_job_id=628003,
            external_session_ids=None,
            external_session_id_singular=ext_legacy,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628003)
        item = _execution_by_job(executions, "628003")
        assert Decimal(str(item["estimated_cost_usd"])) == Decimal("0.07")
        assert item["total_input_tokens"] == 700


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_missing_attribution_is_unavailable(db_pool) -> None:
    """No attribution and no singular session — unavailable, never zero."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        await _seed_execution(
            conn,
            awx_job_id=628004,
            external_session_ids=None,
            external_session_id_singular=None,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628004)
        item = _execution_by_job(executions, "628004")
        assert item["estimated_cost_usd"] is None
        assert item["session_id"] is None
        assert item["total_input_tokens"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_unknown_session_cost_is_unavailable(db_pool) -> None:
    """One attributed session with unknown cost poisons the subtotal."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_known = f"ses_628_known_{uuid.uuid4().hex[:8]}"
        ext_unknown = f"ses_628_unknown_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(conn, external_session_id=ext_known, cost=Decimal("0.10"))
        await _seed_gateway_session(conn, external_session_id=ext_unknown, cost=None)
        await _seed_execution(
            conn,
            awx_job_id=628005,
            external_session_ids=[ext_known, ext_unknown],
            external_session_id_singular=ext_known,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628005)
        item = _execution_by_job(executions, "628005")
        assert item["estimated_cost_usd"] is None
        # Token totals remain available when only cost is unknown.
        assert item["total_input_tokens"] == 2000


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_unresolved_attribution_id_is_unavailable(db_pool) -> None:
    """An attributed id matching no internal session leaves the subtotal
    unknown (fail-safe — never partial)."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_known = f"ses_628_ok_{uuid.uuid4().hex[:8]}"
        ext_ghost = f"ses_628_ghost_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(conn, external_session_id=ext_known, cost=Decimal("0.10"))
        await _seed_execution(
            conn,
            awx_job_id=628006,
            external_session_ids=[ext_known, ext_ghost],
            external_session_id_singular=ext_known,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628006)
        item = _execution_by_job(executions, "628006")
        assert item["estimated_cost_usd"] is None
        assert item["total_input_tokens"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_duplicate_session_entries_dedupe(db_pool) -> None:
    """The same session listed twice contributes once."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_a = f"ses_628_dup_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(
            conn,
            external_session_id=ext_a,
            input_tokens=1000,
            output_tokens=500,
            cost=Decimal("0.10"),
        )
        await _seed_execution(
            conn,
            awx_job_id=628007,
            external_session_ids=[ext_a, ext_a],
            external_session_id_singular=ext_a,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628007)
        item = _execution_by_job(executions, "628007")
        assert Decimal(str(item["estimated_cost_usd"])) == Decimal("0.10")
        assert item["total_input_tokens"] == 1000


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_multiple_executions_independent_subtotals(db_pool) -> None:
    """Sibling executions under one AFK Run keep independent subtotals."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        run_id = "01J6280000000000000000TEST"  # 26-char ULID
        await conn.execute(
            "INSERT INTO afk_runs (afk_run_id, provider, status,"
            " first_seen_at, last_seen_at)"
            " VALUES ($1, 'github', 'completed', now(), now())",
            run_id,
        )
        ext_first = f"ses_628_first_{uuid.uuid4().hex[:8]}"
        ext_second = f"ses_628_second_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(
            conn,
            external_session_id=ext_first,
            input_tokens=1000,
            output_tokens=100,
            cost=Decimal("0.10"),
        )
        await _seed_gateway_session(
            conn,
            external_session_id=ext_second,
            input_tokens=3000,
            output_tokens=300,
            cost=Decimal("0.30"),
        )
        await _seed_execution(
            conn,
            awx_job_id=628008,
            external_session_ids=[ext_first],
            external_session_id_singular=ext_first,
            afk_run_id=run_id,
        )
        await _seed_execution(
            conn,
            awx_job_id=628009,
            external_session_ids=[ext_second],
            external_session_id_singular=ext_second,
            afk_run_id=run_id,
        )

    async with _build_app(db_pool) as client:
        executions = await _fetch_executions(client, 628008)
        first = _execution_by_job(executions, "628008")
        second = _execution_by_job(executions, "628009")
        assert Decimal(str(first["estimated_cost_usd"])) == Decimal("0.10")
        assert Decimal(str(second["estimated_cost_usd"])) == Decimal("0.30")
        assert first["total_input_tokens"] == 1000
        assert second["total_input_tokens"] == 3000


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_replayed_usage_does_not_double_count(db_pool) -> None:
    """Canonical accounting: the sessions aggregate is delta-adjusted on
    replay (ADR 0012), so a re-delivered usage record leaves the subtotal
    stable — the subtotal sums the deduplicated session aggregates, never
    raw event rows."""
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)
        ext_replay = f"ses_628_replay_{uuid.uuid4().hex[:8]}"
        session_id = await _seed_gateway_session(
            conn,
            external_session_id=ext_replay,
            input_tokens=1000,
            output_tokens=500,
            cost=Decimal("0.10"),
        )
        await _seed_execution(
            conn,
            awx_job_id=628010,
            external_session_ids=[ext_replay],
            external_session_id_singular=ext_replay,
        )

        async with _build_app(db_pool) as client:
            executions = await _fetch_executions(client, 628010)
            item = _execution_by_job(executions, "628010")
            assert Decimal(str(item["estimated_cost_usd"])) == Decimal("0.10")

            # Simulate the canonical Replay Merge moving the session
            # aggregate by a delta (1000 → 1200 input) — the subtotal tracks
            # the corrected aggregate, exactly once.
            await conn.execute(
                "UPDATE sessions SET total_input_tokens = 1200 WHERE id = $1",
                session_id,
            )
            executions = await _fetch_executions(client, 628010)
            item = _execution_by_job(executions, "628010")
            assert item["total_input_tokens"] == 1200
