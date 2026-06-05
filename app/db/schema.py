"""Database schema management — loads and executes schema.sql idempotently."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_SCHEMA_SQL: str | None = None


def _load_schema_sql() -> str:
    """Read the schema SQL from the file (cached)."""
    global _SCHEMA_SQL
    if _SCHEMA_SQL is None:
        _SCHEMA_SQL = _SCHEMA_PATH.read_text()
    return _SCHEMA_SQL


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Execute schema.sql against the given pool (idempotent via IF NOT EXISTS)."""
    sql = _load_schema_sql()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("Database schema ensured.")
