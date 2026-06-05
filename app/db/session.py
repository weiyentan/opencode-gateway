"""Database session management — asyncpg connection pool."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import asyncpg
from fastapi import Request

from app.core.config import Settings

logger = logging.getLogger(__name__)


class DatabasePool:
    """Manages an asyncpg connection pool for the Gateway application."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool | None:
        """Return the underlying asyncpg pool, or None if not connected."""
        return self._pool

    async def connect(self) -> None:
        """Initialize the connection pool from settings."""
        self._pool = await asyncpg.create_pool(
            host=self._settings.database_host,
            port=self._settings.database_port,
            database=self._settings.database_name,
            user=self._settings.database_user,
            password=self._settings.database_password,
            min_size=self._settings.database_min_connections,
            max_size=self._settings.database_max_connections,
            timeout=self._settings.database_connection_timeout,
        )

    async def close(self) -> None:
        """Close the connection pool gracefully."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def acquire(self) -> asyncpg.Connection:
        """Acquire a connection from the pool."""
        if self._pool is None:
            raise RuntimeError("Connection pool is not initialized")
        return await self._pool.acquire()

    async def release(self, conn: asyncpg.Connection) -> None:
        """Release a connection back to the pool."""
        if self._pool is not None:
            await self._pool.release(conn)


async def get_session(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency that yields a database connection from the pool."""
    db_pool: DatabasePool = request.app.state.pool  # type: ignore[attr-defined]
    conn = await db_pool.acquire()
    try:
        yield conn
    finally:
        await db_pool.release(conn)
