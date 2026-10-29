"""Shared fixtures for the disposable-PostgreSQL integration suites.

The change-request summary, correlation-JSONB, reporting-aggregate,
reporting-ingest, execution-binding, execution-cost,
execution-reconciliation, recovery-checkpoint, and AFK-lifecycle
concurrency suites all migrate a real PostgreSQL database to ``head``
and drop every public table on retry/teardown.  They share the same fail-closed
preflight (:func:`verify_disposable_target`) and the same unmodified Alembic
revision chain (``alembic.ini`` with ``script_location = <proj>/alembic``).

The fixtures are module-scoped: every test module that requests them still gets
its own verified target, fresh schema, and teardown.  Centralising them here
removes the copy-pasted scaffolding without weakening that per-module
isolation.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from tests.integration._disposable_postgres import (
    DROP_ALL_TABLES_SQL,
    DatabaseTarget,
    target_dsn,
    verify_disposable_target,
)

# ``verify_disposable_target`` treats an unset ``GATEWAY_ENV`` as production
# and refuses destructive operations.  Pin the development environment at
# conftest import time — before any module-scoped fixture runs its preflight —
# without overriding an explicit operator-provided value.
os.environ.setdefault("GATEWAY_ENV", "development")

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

#: Resolved once at import (never connects); the preflight below verifies it.
_TARGET = DatabaseTarget.from_env()


@pytest.fixture(scope="module")
def _verified_disposable_database() -> DatabaseTarget:
    """Fail closed unless the target is an explicitly disposable PostgreSQL 16 DB.

    Runs before ``_migrated_schema`` (and therefore before any destructive
    migration reset).  A missing attestation, unreachable server, non-test
    database name, Kubernetes target, or wrong server version raises an error
    — never a skip, so CI cannot go green on an unverified or skipped target.
    """
    return asyncio.run(verify_disposable_target(_TARGET))


@pytest.fixture(scope="module")
def _migrated_schema(_verified_disposable_database: DatabaseTarget):
    """Migrate the test database to ``head`` once per module.

    Uses the unmodified checked-in revision chain via ``alembic.ini`` and
    ``script_location = <proj>/alembic``.  Alembic's ``env.py`` drives asyncpg
    via ``asyncio.run``, so the upgrade runs in a worker thread where no event
    loop is running.  A failed first attempt drops every public table and
    retries — the drop only runs after the disposable preflight has passed.

    Teardown drops every public table again, so the next module starts from a
    clean schema regardless of what this module left behind.
    """
    import alembic.command
    import alembic.config

    sync_url = target_dsn(_TARGET).replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        alembic.command.upgrade(alembic_cfg, "head")

    async def _drop_all() -> None:
        conn = await asyncpg.connect(dsn=target_dsn(_TARGET), timeout=5)
        try:
            await conn.execute(DROP_ALL_TABLES_SQL)
        finally:
            await conn.close()

    try:
        asyncio.run(asyncio.to_thread(_upgrade))
    except Exception:
        asyncio.run(_drop_all())
        asyncio.run(asyncio.to_thread(_upgrade))

    yield

    asyncio.run(_drop_all())
