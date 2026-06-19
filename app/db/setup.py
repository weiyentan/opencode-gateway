"""Database setup — runs Alembic migrations and validates required tables.

This module replaces the old ``app/db/schema.py`` ``ensure_schema()``
approach that executed a static ``schema.sql`` file at startup.  Alembic
is now the single source of truth for the production database schema.

On startup the Gateway:

1. Runs ``alembic upgrade head`` to bring the database to the latest
   migration revision.
2. Checks that all required tables (core domain tables + observation
   tables from ADR 0001) exist in the database.
3. Fails fast with a clear error message if any required table is
   missing, so operators can diagnose the problem immediately.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# Tables that MUST exist after migrations have been applied.
# This list covers the four core domain tables (created by migration
# 0000), the runner/observation tables (created by 0001), and webhooks
# (created by 0002).
_REQUIRED_TABLES = [
    "gateway_jobs",
    "workspaces",
    "job_events",
    "approvals",
    "runners",
    "runner_observations",
    "workspace_observations",
    "opencode_instance_observations",
    "webhooks",
]

# Resolved once when the module is loaded.
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_migrations_sync() -> None:
    """Synchronous core — called via ``asyncio.to_thread``."""
    import alembic.command
    import alembic.config

    alembic_cfg = alembic.config.Config(str(_PROJ_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic.command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied successfully.")


async def run_migrations() -> None:
    """Run Alembic migrations to upgrade the database to the latest revision.

    This should be called during the Gateway startup lifespan, after the
    Postgres connection pool has been established.  The function is a
    no-op if Alembic determines the database is already at ``head``.

    Alembic reads database connection parameters from the Gateway
    settings (``GATEWAY_*`` env vars) via ``alembic/env.py``, so no
    additional configuration is needed.
    """
    logger.info("Running Alembic migrations...")
    try:
        await asyncio.to_thread(_run_migrations_sync)
    except Exception:
        logger.exception("Alembic migrations failed.")
        raise


async def check_required_tables(pool: asyncpg.Pool) -> None:
    """Verify all required tables exist in the database.

    Queries ``information_schema.tables`` for the public schema and
    compares the result against the canonical list of required tables.

    Raises:
        RuntimeError: If one or more required tables are missing,
            with a clear message naming the missing tables and
            suggesting ``alembic upgrade head`` as a fix.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        existing = {row["table_name"] for row in rows}

    required = set(_REQUIRED_TABLES)
    missing = required - existing

    if missing:
        msg = (
            f"Required database tables are missing: {', '.join(sorted(missing))}. "
            f"Run 'alembic upgrade head' to create them."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    logger.info("All %d required database tables are present.", len(required))
    return None
