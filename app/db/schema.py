"""Database schema management — delegates to Alembic migrations.

.. deprecated:: 0.1.0
    The static ``schema.sql`` file is no longer the production schema
    source of truth.  Alembic migrations (in ``alembic/versions/``) now
    create ALL required tables.  ``schema.sql`` is kept as an
    auto-generated reference for tests and documentation.

The ``ensure_schema()`` function still exists for backward
compatibility, but it now runs Alembic migrations and verifies required
tables instead of executing a raw SQL file.
"""

from __future__ import annotations

import logging

import asyncpg

from app.db.setup import check_required_tables, run_migrations

logger = logging.getLogger(__name__)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Ensure the database schema is current (via Alembic migrations).

    This replaces the old behaviour of executing ``schema.sql``
    idempotently.  Alembic is the single source of truth — migrations
    are applied automatically and the result is validated.

    1. Runs ``alembic upgrade head`` to bring the database to the
       latest revision.
    2. Checks that all required tables exist.
    """
    await run_migrations()
    await check_required_tables(pool)
    logger.info("Database schema ensured via Alembic migrations.")
