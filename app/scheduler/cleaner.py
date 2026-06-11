"""Cleanup scheduler — queries expired workspaces and invokes the executor.

Implements the background workspace cleanup loop with PostgreSQL advisory
locks, configurable batching, and executor integration.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from typing import Any

import asyncpg

from app.executors import CleanupWorkspaceRequest, ExecutorPlugin
from app.scheduler.engine import Scheduler

logger = logging.getLogger(__name__)

# Defaults when settings are not explicitly provided.
DEFAULT_INTERVAL_SECONDS: float = 900.0  # 15 minutes
DEFAULT_BATCH_SIZE: int = 10


def _uuid_to_lock_key(workspace_id: Any) -> int:
    """Convert a workspace UUID value to a positive bigint for advisory locks.

    PostgreSQL advisory locks accept ``bigint`` keys (signed 64-bit on the
    wire).  We derive the key by taking the low 63 bits of the UUID's
    128-bit integer representation so the result is always a non-negative
    bigint.
    """
    if isinstance(workspace_id, _uuid.UUID):
        uid = workspace_id
    else:
        uid = _uuid.UUID(str(workspace_id))
    # Mask to 63 bits to guarantee a non-negative bigint.
    return uid.int & 0x7FFFFFFFFFFFFFFF


class CleanupScheduler(Scheduler):
    """Background scheduler that periodically cleans up expired workspaces.

    Each tick:
      1. Queries the database for eligible workspaces (past their
         ``cleanup_after`` timestamp, not pinned, not already cleaned).
      2. Attempts to acquire a PostgreSQL advisory lock per workspace.
      3. Calls ``executor.cleanup_workspace()`` and on success marks
         ``cleanup_status = 'cleaned'`` and releases the lock.
      4. Processes at most ``batch_size`` workspaces per tick.
    """

    def __init__(
        self,
        interval_seconds: float | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        super().__init__(
            interval_seconds=interval_seconds or DEFAULT_INTERVAL_SECONDS,
        )
        self._batch_size = batch_size

    # ------------------------------------------------------------------
    # Tick implementation — the real work happens here
    # ------------------------------------------------------------------

    async def _tick(self, ctx: dict[str, Any]) -> None:
        """Single cleanup iteration.

        Requires ``pool`` (asyncpg.Pool) and ``executor``
        (ExecutorPlugin) in the context dictionary.  Silently skips
        when either is missing so the scheduler does not crash when
        Postgres is unavailable during boot.
        """
        pool: asyncpg.Pool | None = ctx.get("pool")
        executor: ExecutorPlugin | None = ctx.get("executor")

        if pool is None:
            logger.debug("CleanupScheduler._tick: no database pool available — skipping")
            return
        if executor is None:
            logger.debug("CleanupScheduler._tick: no executor available — skipping")
            return

        rows = await self._query_expired(pool)
        if not rows:
            return

        logger.info("CleanupScheduler._tick: processing %d expired workspace(s)", len(rows))

        for row in rows:
            try:
                await self._process_one(pool, executor, row)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:
                logger.exception(
                    "Unhandled error during cleanup of workspace %s", row.get("id")
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _query_expired(
        self, pool: asyncpg.Pool
    ) -> list[asyncpg.Record]:
        """Return expired workspaces eligible for cleanup, up to batch_size."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id
                FROM workspaces
                WHERE cleanup_after < NOW()
                  AND pinned = FALSE
                  AND cleanup_status != 'cleaned'
                ORDER BY cleanup_after ASC
                LIMIT $1
                """,
                self._batch_size,
            )

    async def _process_one(
        self,
        pool: asyncpg.Pool,
        executor: ExecutorPlugin,
        row: asyncpg.Record,
    ) -> None:
        """Acquire advisory lock, clean up one workspace, and update status."""
        ws_id = row["id"]
        lock_key = _uuid_to_lock_key(ws_id)

        # Advisory locks are connection-scoped — we hold a single
        # connection for the entire lock→cleanup→unlock sequence.
        async with pool.acquire() as conn:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", lock_key
            )
            if not acquired:
                logger.info(
                    "Advisory lock unavailable for workspace %s — skipping", ws_id
                )
                return

            logger.debug("Acquired advisory lock for workspace %s (key=%d)", ws_id, lock_key)
            try:
                request = CleanupWorkspaceRequest(workspace_id=ws_id)
                response = await executor.cleanup_workspace(request)

                if response.status == "cleaned":
                    await conn.execute(
                        """
                        UPDATE workspaces
                        SET cleanup_status = 'cleaned'
                        WHERE id = $1
                        """,
                        ws_id,
                    )
                    logger.info("Workspace %s cleaned successfully", ws_id)
                else:
                    logger.warning(
                        "Workspace %s cleanup returned unexpected status: %s",
                        ws_id,
                        response.status,
                    )
            except Exception:
                logger.exception("Cleanup failed for workspace %s", ws_id)
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1::bigint)", lock_key
                    )
                except Exception:
                    logger.exception(
                        "Failed to release advisory lock for workspace %s", ws_id
                    )
