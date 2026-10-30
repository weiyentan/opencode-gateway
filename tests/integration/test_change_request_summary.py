"""DB-backed integration tests for the change-request summary endpoint (issue #779).

Runs against the docker-compose Postgres (port 5433) and verifies the
page-scoped cost enrichment against representative data:

* AC1 — ``GET /api/v1/afk-outcomes/change-requests`` returns HTTP 200
  within the configured timeout for an unfiltered ``limit=3`` and
  ``limit=30`` on representative data.
* AC4 — integration coverage proves page-scoped cost enrichment: each
  page row carries its cost (``null`` when unavailable, never zero) while
  ``total`` still reflects every match, and the filtered path (provider /
  repository / state / activity window) retains the filtered total.

Both layers are exercised against a real PostgreSQL database: the SQL
built by ``app.api.afk_outcomes._build_change_request_queries`` executed
directly, and the HTTP endpoint through the real database pool.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_change_request_summary.py -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(``GATEWAY_DATABASE_*``), defaulting to the ``docker-compose.test.yml``
service.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Request

from afk_outcomes.models import build_observation_key

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

_API_KEY = "test-api-key"
_ENDPOINT = "/api/v1/afk-outcomes/change-requests"

#: Two repository namespaces so repository filtering is meaningful.
REPO_MAIN = "it779/summary"
REPO_OTHER = "it779/other"


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

    import shutil

    import alembic.command
    import alembic.config

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


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

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


# ── Seed builders ────────────────────────────────────────────────────────────


def _run_id(n: int) -> str:
    """Return a 26-char run identifier in the ``afk_runs.afk_run_id`` shape."""
    return f"01J7790{n:019d}"


async def _insert_event(
    conn: asyncpg.Connection,
    *,
    provider: str,
    repository: str,
    external_id: str,
    event_type: str,
    occurred_at: datetime,
) -> None:
    await conn.execute(
        "INSERT INTO engineering_events"
        " (provider, repository, entity_type, external_id, event_type,"
        "  occurred_at, observation_key, observed_via, snapshot_at)"
        " VALUES ($1, $2, 'change_request', $3, $4, $5, $6, 'webhook', $5)",
        provider,
        repository,
        external_id,
        event_type,
        occurred_at,
        build_observation_key(
            provider=provider,
            repository=repository,
            entity_type="change_request",
            external_id=external_id,
            event_type=event_type,
            occurred_at=occurred_at,
        ),
    )


async def _insert_run(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    provider: str,
    last_seen_at: datetime,
    change_request: tuple[str, str, str] | None = None,
) -> None:
    cr_provider, cr_repository, cr_external_id = change_request or (None, None, None)
    await conn.execute(
        "INSERT INTO afk_runs"
        " (afk_run_id, provider, first_seen_at, last_seen_at,"
        "  change_request_provider, change_request_repository,"
        "  change_request_external_id)"
        " VALUES ($1, $2, $3, $3, $4, $5, $6)",
        run_id,
        provider,
        last_seen_at,
        cr_provider,
        cr_repository,
        cr_external_id,
    )


async def _insert_run_entity(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    provider: str,
    repository: str,
    external_id: str,
) -> None:
    await conn.execute(
        "INSERT INTO afk_run_entities"
        " (afk_run_id, provider, repository, entity_type, external_id,"
        "  role, correlation_confidence)"
        " VALUES ($1, $2, $3, 'change_request', $4, 'resolved', 1.0)",
        run_id,
        provider,
        repository,
        external_id,
    )


async def _insert_session(conn: asyncpg.Connection, *, cost: Decimal) -> uuid.UUID:
    """Insert one client/credential/source-database/session chain; return the
    internal ``sessions.id`` UUID."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"it779-{uuid.uuid4()}",
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        f"it779-hash-{uuid.uuid4()}",
        "it779",
    )
    database_id = await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id)"
        " VALUES ($1, $2) RETURNING id",
        credential_id,
        client_id,
    )
    started = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    return await conn.fetchval(
        "INSERT INTO sessions"
        " (client_id, source_database_id, external_session_id,"
        "  first_message_at, last_message_at, total_estimated_cost_usd)"
        " VALUES ($1, $2, $3, $4, $4, $5) RETURNING id",
        client_id,
        database_id,
        f"ses_779_{uuid.uuid4().hex[:8]}",
        started,
        cost,
    )


async def _link_run_session(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    session_id: uuid.UUID,
    external_session_id: str,
) -> None:
    await conn.execute(
        "INSERT INTO afk_run_sessions (afk_run_id, session_id, external_session_id)"
        " VALUES ($1, $2, $3)",
        run_id,
        session_id,
        external_session_id,
    )


async def _insert_binding(
    conn: asyncpg.Connection,
    *,
    awx_job_id: int,
    provider: str,
    repository: str,
    external_id: str,
    outcome: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO execution_bindings"
        " (awx_job_id, job_template_id, provider, repository_url, entity_type,"
        "  entity_number, outcome, started_at, finished_at)"
        " VALUES ($1, 7, $2, $3, 'change_request', $4, $5, $6, $7)",
        awx_job_id,
        provider,
        repository,
        external_id,
        outcome,
        started_at,
        finished_at,
    )


async def _seed_summary_fixtures(conn: asyncpg.Connection) -> None:
    """Reset the identity sources and seed five deterministic change requests.

    Five identities, known through all three durable sources:

    * ``github/it779/summary/101`` — facts (``merged``) + run + two
      executions.  Two linked runs share one internal session, so its
      0.10 cost must be counted exactly once.  Latest execution 08-05.
    * ``github/it779/summary/102`` — run + one running execution, no facts
      (provider state unavailable).  Cost 0.20.  Latest execution 08-03.
    * ``gitlab/it779/summary/201`` — execution binding only.  No linked
      session, so cost is unavailable (``null``, never zero).  Latest
      execution 08-04 (running).
    * ``gitlab/it779/summary/202`` — facts (``closed``) + run.  Cost 0.25.
    * ``github/it779/other/303`` — facts (``open``) + run.  Cost 0.05.

    The identity-source tables are truncated first so ``total`` assertions
    are deterministic regardless of data left by other integration modules.
    """
    await conn.execute(
        "TRUNCATE TABLE engineering_events, execution_bindings, afk_runs CASCADE"
    )

    # ── identity 101: facts merged + two runs sharing one session ───────
    await _insert_event(
        conn,
        provider="github",
        repository=REPO_MAIN,
        external_id="101",
        event_type="change_request.opened",
        occurred_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC),
    )
    await _insert_event(
        conn,
        provider="github",
        repository=REPO_MAIN,
        external_id="101",
        event_type="change_request.merged",
        occurred_at=datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC),
    )
    session_a = await _insert_session(conn, cost=Decimal("0.10"))
    await _insert_run(
        conn,
        run_id=_run_id(101),
        provider="github",
        last_seen_at=datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC),
        change_request=("github", REPO_MAIN, "101"),
    )
    await _link_run_session(
        conn,
        run_id=_run_id(101),
        session_id=session_a,
        external_session_id="ses_779_dedupe",
    )
    # Second run linked to the same change request through afk_run_entities.
    # Both runs link the same internal session — its cost must be counted once.
    await _insert_run(
        conn,
        run_id=_run_id(102),
        provider="github",
        last_seen_at=datetime(2026, 8, 3, 1, 0, 0, tzinfo=UTC),
    )
    await _insert_run_entity(
        conn,
        run_id=_run_id(102),
        provider="github",
        repository=REPO_MAIN,
        external_id="101",
    )
    await _link_run_session(
        conn,
        run_id=_run_id(102),
        session_id=session_a,
        external_session_id="ses_779_dedupe",
    )
    await _insert_binding(
        conn,
        awx_job_id=779_101_001,
        provider="github",
        repository=REPO_MAIN,
        external_id="101",
        outcome="completed",
        started_at=datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
    )
    await _insert_binding(
        conn,
        awx_job_id=779_101_002,
        provider="github",
        repository=REPO_MAIN,
        external_id="101",
        outcome="completed",
        started_at=datetime(2026, 8, 5, 0, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC),
    )

    # ── identity 102: run + running execution, no facts ─────────────────
    session_b = await _insert_session(conn, cost=Decimal("0.20"))
    await _insert_run(
        conn,
        run_id=_run_id(103),
        provider="github",
        last_seen_at=datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC),
        change_request=("github", REPO_MAIN, "102"),
    )
    await _link_run_session(
        conn,
        run_id=_run_id(103),
        session_id=session_b,
        external_session_id="ses_779_b",
    )
    await _insert_binding(
        conn,
        awx_job_id=779_102_001,
        provider="github",
        repository=REPO_MAIN,
        external_id="102",
        outcome="running",
        started_at=datetime(2026, 8, 3, 8, 0, 0, tzinfo=UTC),
    )

    # ── identity 201: execution binding only (cost unavailable) ─────────
    await _insert_binding(
        conn,
        awx_job_id=779_201_001,
        provider="gitlab",
        repository=REPO_MAIN,
        external_id="201",
        outcome="running",
        started_at=datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC),
    )

    # ── identity 202: facts closed + run ────────────────────────────────
    await _insert_event(
        conn,
        provider="gitlab",
        repository=REPO_MAIN,
        external_id="202",
        event_type="change_request.opened",
        occurred_at=datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC),
    )
    await _insert_event(
        conn,
        provider="gitlab",
        repository=REPO_MAIN,
        external_id="202",
        event_type="change_request.closed",
        occurred_at=datetime(2026, 8, 2, 8, 0, 0, tzinfo=UTC),
    )
    session_c = await _insert_session(conn, cost=Decimal("0.25"))
    await _insert_run(
        conn,
        run_id=_run_id(105),
        provider="gitlab",
        last_seen_at=datetime(2026, 8, 2, 9, 0, 0, tzinfo=UTC),
        change_request=("gitlab", REPO_MAIN, "202"),
    )
    await _link_run_session(
        conn,
        run_id=_run_id(105),
        session_id=session_c,
        external_session_id="ses_779_c",
    )

    # ── identity 303: facts open + run in the other repository ──────────
    await _insert_event(
        conn,
        provider="github",
        repository=REPO_OTHER,
        external_id="303",
        event_type="change_request.opened",
        occurred_at=datetime(2026, 8, 6, 0, 0, 0, tzinfo=UTC),
    )
    session_d = await _insert_session(conn, cost=Decimal("0.05"))
    await _insert_run(
        conn,
        run_id=_run_id(106),
        provider="github",
        last_seen_at=datetime(2026, 8, 6, 1, 0, 0, tzinfo=UTC),
        change_request=("github", REPO_OTHER, "303"),
    )
    await _link_run_session(
        conn,
        run_id=_run_id(106),
        session_id=session_d,
        external_session_id="ses_779_d",
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_builder_sql_page_scoped_against_postgres(db_pool: asyncpg.Pool) -> None:
    """The real builder SQL enriches only the ordered page with cost.

    Runs the count and data SQL produced by ``_build_change_request_queries``
    directly against PostgreSQL: ``total`` counts all matches, the page is
    ordered and limited before cost enrichment, each page row carries its
    cost (``null`` when unavailable), shared-session cost is deduplicated,
    and filtered paths keep the filtered total.
    """
    from app.api.afk_outcomes import _build_change_request_queries

    async with db_pool.acquire() as conn:
        await _seed_summary_fixtures(conn)

        # ── unfiltered: total reflects all five, page limit 3 ───────────
        count_sql, data_sql, params = _build_change_request_queries(
            None, None, None, None, None
        )
        assert params == []
        assert await conn.fetchval(count_sql, *params) == 5

        page = await conn.fetch(data_sql, *params, 3, 0)
        assert [r["external_id"] for r in page] == ["101", "201", "102"]
        assert page[0]["provider_state"] == "merged"
        assert page[0]["execution_total"] == 2
        assert page[0]["total_estimated_cost_usd"] == Decimal("0.10")
        assert page[1]["total_estimated_cost_usd"] is None
        assert page[2]["total_estimated_cost_usd"] == Decimal("0.20")

        # ── unfiltered limit=30: every match, same total ────────────────
        full = await conn.fetch(data_sql, *params, 30, 0)
        assert len(full) == 5
        assert await conn.fetchval(count_sql, *params) == 5

        # ── filtered path: provider + repository + state + activity ─────
        count_sql, data_sql, params = _build_change_request_queries(
            "gitlab",
            REPO_MAIN,
            "closed",
            datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 2, 23, 59, 59, tzinfo=UTC),
        )
        assert await conn.fetchval(count_sql, *params) == 1
        page = await conn.fetch(data_sql, *params, 3, 0)
        assert [r["external_id"] for r in page] == ["202"]
        assert page[0]["total_estimated_cost_usd"] == Decimal("0.25")

        # ── activity window alone ────────────────────────────────────────
        count_sql, data_sql, params = _build_change_request_queries(
            None,
            None,
            None,
            datetime(2026, 8, 4, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 5, 23, 59, 59, tzinfo=UTC),
        )
        assert await conn.fetchval(count_sql, *params) == 2
        page = await conn.fetch(data_sql, *params, 3, 0)
        assert [r["external_id"] for r in page] == ["101", "201"]


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_endpoint_returns_200_with_page_scoped_cost(
    db_pool: asyncpg.Pool,
) -> None:
    """AC1/AC4 through the HTTP endpoint on representative PostgreSQL data.

    Unfiltered ``limit=3`` and ``limit=30`` both return HTTP 200 within the
    configured request timeout; ``total`` reflects all five matches while
    each page row carries its page-scoped cost (``null`` when unavailable);
    the filtered path retains the filtered total.
    """
    async with db_pool.acquire() as conn:
        await _seed_summary_fixtures(conn)

    client = _build_app(db_pool)
    async with client as c:
        # ── AC1: unfiltered limit=3 on representative data ──────────────
        resp = await c.get(_ENDPOINT, params={"limit": 3})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 5
        assert data["limit"] == 3
        assert data["offset"] == 0
        assert [i["external_id"] for i in data["items"]] == ["101", "201", "102"]
        assert data["items"][0]["provider_state"] == "merged"
        assert data["items"][0]["executions"] == {
            "total": 2,
            "running": 0,
            "completed": 2,
            "failed": 0,
            "cancelled": 0,
        }
        assert Decimal(str(data["items"][0]["total_estimated_cost_usd"])) == Decimal(
            "0.10"
        )
        # No linked session — unavailable, never zero.
        assert data["items"][1]["total_estimated_cost_usd"] is None
        assert Decimal(str(data["items"][2]["total_estimated_cost_usd"])) == Decimal(
            "0.20"
        )

        # ── AC4: unfiltered limit=30 returns every match ────────────────
        resp = await c.get(_ENDPOINT, params={"limit": 30})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 5
        assert data["limit"] == 30
        assert len(data["items"]) == 5
        costs = {i["external_id"]: i["total_estimated_cost_usd"] for i in data["items"]}
        assert set(costs) == {"101", "102", "201", "202", "303"}
        assert Decimal(str(costs["101"])) == Decimal("0.10")
        assert Decimal(str(costs["102"])) == Decimal("0.20")
        assert costs["201"] is None
        assert Decimal(str(costs["202"])) == Decimal("0.25")
        assert Decimal(str(costs["303"])) == Decimal("0.05")

        # ── AC4: filtered path retains the filtered total ───────────────
        resp = await c.get(
            _ENDPOINT,
            params={
                "provider": "gitlab",
                "repository": REPO_MAIN,
                "provider_state": "closed",
                "activity_from": "2026-08-01T00:00:00Z",
                "activity_to": "2026-08-02T23:59:59Z",
                "limit": 3,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["external_id"] == "202"
        assert Decimal(str(data["items"][0]["total_estimated_cost_usd"])) == Decimal(
            "0.25"
        )
