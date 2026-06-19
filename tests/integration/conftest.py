"""Integration test fixtures and configuration.

Starts a test Postgres container via docker-compose.test.yml, runs schema
migrations, and provides connection fixtures for real-database tests.

Documentation — How to run integration tests
---------------------------------------------
1. Prerequisites: Docker and docker compose must be installed.
2. Start the test database::

       docker compose -f docker-compose.test.yml up -d

3. Run the integration tests::

       pytest tests/integration/ -v -m integration

4. (Optional) Run all tests including integration::

       pytest tests/ -v

5. Tear down the test database::

       docker compose -f docker-compose.test.yml down -v

The test fixture will automatically handle the compose lifecycle if
``docker compose`` is available, but you can also manage it manually
as shown above.
"""

from __future__ import annotations

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9
import asyncio
import logging
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

# Project root (parent of tests/)
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_FILE = _PROJ_ROOT / "docker-compose.test.yml"

logger = logging.getLogger(__name__)

# ── Database connection settings for the test container ───────────────────
TEST_DB_HOST = os.environ.get("GATEWAY_TEST_DATABASE_HOST", "localhost")
TEST_DB_PORT = int(os.environ.get("GATEWAY_TEST_DATABASE_PORT", "5433"))
TEST_DB_NAME = os.environ.get("GATEWAY_TEST_DATABASE_NAME", "opencode_gateway_test")
TEST_DB_USER = os.environ.get("GATEWAY_TEST_DATABASE_USER", "opencode_test")
TEST_DB_PASSWORD = os.environ.get("GATEWAY_TEST_DATABASE_PASSWORD", "opencode_test")

# Tables that get truncated between tests to keep them isolated
_TRUNCATE_ORDER = [
    "opencode_instance_observations",
    "workspace_observations",
    "runner_observations",
    "runner_events",
    "job_events",
    "approvals",
    "gateway_jobs",
    "workspaces",
    "runners",
]


# ═══════════════════════════════════════════════════════════════════════════
#  Pytest marker registration
# ═══════════════════════════════════════════════════════════════════════════


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``integration`` mark."""
    config.addinivalue_line(
        "markers",
        "integration: mark a test as an integration test that requires a real Postgres database",
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Compose lifecycle helpers
# ═══════════════════════════════════════════════════════════════════════════


def _compose_is_available() -> bool:
    """Check whether docker compose (or docker-compose) is on PATH."""
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Fall back to the old docker-compose command
    try:
        subprocess.run(
            ["docker-compose", "version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _compose_cmd() -> list[str]:
    """Return the available compose command (docker compose or docker-compose)."""
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
        )
        return ["docker", "compose"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ["docker-compose"]


def _compose_up() -> None:
    """Start the test Postgres container."""
    cmd = _compose_cmd() + ["-f", str(_COMPOSE_FILE), "up", "-d", "--wait"]
    logger.info("Starting test database: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=120)


def _compose_down() -> None:
    """Stop and remove the test Postgres container (with volumes)."""
    cmd = _compose_cmd() + ["-f", str(_COMPOSE_FILE), "down", "-v"]
    logger.info("Tearing down test database: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=60)


# ═══════════════════════════════════════════════════════════════════════════
#  Schema setup
# ═══════════════════════════════════════════════════════════════════════════


async def _run_schema_sql(pool: asyncpg.Pool) -> None:
    """Execute app/db/schema.sql to create the non-ORM tables."""
    schema_path = _PROJ_ROOT / "app" / "db" / "schema.sql"
    sql = schema_path.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("schema.sql applied.")


async def _run_alembic_migrations() -> None:
    """Run Alembic migrations to create ORM-managed tables (runners, observations).

    Sets GATEWAY_* env vars so alembic/env.py picks up the test database
    connection parameters.
    """
    import alembic.command
    import alembic.config

    # Alembic's env.py uses get_settings() which reads GATEWAY_* env vars.
    os.environ["GATEWAY_DATABASE_HOST"] = TEST_DB_HOST
    os.environ["GATEWAY_DATABASE_PORT"] = str(TEST_DB_PORT)
    os.environ["GATEWAY_DATABASE_NAME"] = TEST_DB_NAME
    os.environ["GATEWAY_DATABASE_USER"] = TEST_DB_USER
    os.environ["GATEWAY_DATABASE_PASSWORD"] = TEST_DB_PASSWORD

    alembic_cfg = alembic.config.Config(str(_PROJ_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic.command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied.")


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    """Run both schema.sql and Alembic migrations."""
    await _run_schema_sql(pool)
    await _run_alembic_migrations()


async def _truncate_all_tables(pool: asyncpg.Pool) -> None:
    """Truncate all tables between tests (respects FK order)."""
    async with pool.acquire() as conn:
        for table in _TRUNCATE_ORDER:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def _compose_session() -> None:
    """Start the test Postgres container once per session.

    Skips if docker compose is not available (tests will still attempt to
    connect — the caller is responsible for ensuring the database is up).
    """
    if _compose_is_available() and _COMPOSE_FILE.exists():
        _compose_up()
        yield
        _compose_down()
    else:
        logger.info(
            "docker compose not available or %s not found — "
            "assuming the test database is already running.",
            _COMPOSE_FILE,
        )
        yield


@pytest.fixture(scope="session")
async def _db_pool(_compose_session) -> asyncpg.Pool:  # type: ignore[no-untyped-def]
    """Session-scoped asyncpg pool connected to the test Postgres.

    Manages the full lifecycle: pool creation, schema migration, and cleanup.
    """
    # Retry loop — Postgres may take a moment to be ready
    last_exc: Exception | None = None
    for attempt in range(1, 31):
        try:
            pool = await asyncpg.create_pool(
                host=TEST_DB_HOST,
                port=TEST_DB_PORT,
                database=TEST_DB_NAME,
                user=TEST_DB_USER,
                password=TEST_DB_PASSWORD,
                min_size=1,
                max_size=5,
            )
            # Quick health check
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            break
        except (asyncpg.exceptions.PostgresError, OSError, ConnectionRefusedError) as exc:
            last_exc = exc
            await asyncio.sleep(1)
    else:
        raise RuntimeError(
            f"Could not connect to test Postgres after 30 attempts: {last_exc}"
        )

    # Run schema migrations once per session.
    await _ensure_schema(pool)

    yield pool

    await pool.close()


@pytest.fixture
async def db_conn(_db_pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Per-test database connection with automatic truncation between tests.

    Truncates all tables at the start of each test to provide a clean slate.
    """
    await _truncate_all_tables(_db_pool)
    async with _db_pool.acquire() as conn:
        yield conn


# ═══════════════════════════════════════════════════════════════════════════
#  Helper builders for integration tests
# ═══════════════════════════════════════════════════════════════════════════


async def create_runner(
    conn: asyncpg.Connection,
    *,
    hostname: str = "test-runner.example.com",
    status: str = "HEALTHY",
    executor_type: str = "local",
    labels: dict | None = None,
) -> uuid.UUID:
    """Insert a runner and return its UUID."""
    rid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO runners (id, runner_id, hostname, executor_type, labels, "
        "status, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $7)",
        rid,
        str(rid),
        hostname,
        executor_type,
        '{"env": "test"}' if labels is None else str(labels).replace("'", '"'),
        status,
        datetime.now(timezone.utc),
    )
    return rid


async def create_workspace(
    conn: asyncpg.Connection,
    *,
    runner_id: uuid.UUID | None = None,
    workspace_name: str = "test-ws-001",
    repo_url: str = "https://github.com/example/test.git",
) -> uuid.UUID:
    """Insert a workspace and return its UUID."""
    ws_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO workspaces (id, runner_id, workspace_name, path, repo_url, "
        "branch, pinned, cleanup_after, cleanup_status, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)",
        ws_id,
        runner_id,
        workspace_name,
        f"/data/workspaces/{workspace_name}",
        repo_url,
        "main",
        False,
        None,
        "active",
        datetime.now(timezone.utc),
    )
    return ws_id


async def create_job(
    conn: asyncpg.Connection,
    *,
    job_id: uuid.UUID | None = None,
    repo_url: str = "https://github.com/example/test.git",
    task_summary: str = "Integration test job",
    status: str = "pending",
    executor_type: str = "local",
) -> uuid.UUID:
    """Insert a job and return its UUID."""
    jid = job_id or uuid.uuid4()
    await conn.execute(
        "INSERT INTO gateway_jobs (id, repo_url, task_summary, status, executor_type, "
        "created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $6)",
        jid,
        repo_url,
        task_summary,
        status,
        executor_type,
        datetime.now(timezone.utc),
    )
    return jid
