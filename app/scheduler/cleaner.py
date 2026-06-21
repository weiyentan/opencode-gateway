"""Cleanup scheduler — background workspace cleanup with PostgreSQL advisory locks.

Contains the complete scheduler loop (previously split across engine.py and
cleaner.py).  The one-adapter ``Scheduler`` base class has been inlined here
because it had exactly one implementation — there was no benefit in carrying a
separate abstraction that was never used for anything else.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import asyncpg

from app.core.ports import release_port
from app.db.lock import release_cleanup_lock, try_acquire_cleanup_lock
from app.executors import CleanupWorkspaceRequest, ExecutorPlugin

logger = logging.getLogger(__name__)

# Defaults when settings are not explicitly provided.
DEFAULT_INTERVAL_SECONDS: float = 900.0  # 15 minutes
DEFAULT_BATCH_SIZE: int = 10


class SupportsAcquireRelease(Protocol):
    """Minimal pool interface required by the cleanup scheduler.

    Matches ``DatabasePool.acquire()`` / ``DatabasePool.release()``
    as well as any other connection-pool wrapper that exposes explicit
    coroutine-based acquire/release (rather than the async context
    manager protocol used by ``asyncpg.Pool.acquire()``).
    """

    async def acquire(self) -> asyncpg.Connection: ...
    async def release(self, conn: asyncpg.Connection) -> None: ...


class CleanupScheduler:
    """Background scheduler that periodically cleans up expired workspaces.

    Accepts explicit typed dependencies (``pool`` and ``executor``) rather
    than a loosely-typed context dictionary.  The scheduler is wired into
    the Gateway application lifespan: it starts on boot, performs a soft-stop
    on graceful shutdown, and errors inside a tick never crash the process.

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
        pool: SupportsAcquireRelease | None = None,
        executor: ExecutorPlugin | None = None,
        interval_seconds: float | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        # --- Dependencies (may also be supplied via start()) ---
        self._pool: SupportsAcquireRelease | None = pool
        self._executor: ExecutorPlugin | None = executor

        # --- Scheduler loop state ---
        self._task: asyncio.Task[None] | None = None
        self._stopped: asyncio.Event = asyncio.Event()
        self._interval: float = interval_seconds or DEFAULT_INTERVAL_SECONDS
        self._batch_size: int = batch_size

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        pool: SupportsAcquireRelease | None = None,
        executor: ExecutorPlugin | None = None,
    ) -> None:
        """Begin the background loop.

        Optionally override *pool* and *executor* — useful when
        dependencies are not available at construction time (e.g. the
        database pool is created inside the lifespan handler).
        """
        if pool is not None:
            self._pool = pool
        if executor is not None:
            self._executor = executor

        logger.info("Cleanup scheduler started (interval=%.1fs)", self._interval)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Soft-stop the scheduler.

        Cancels the background task and waits for any in-flight work
        to complete before returning.  Errors during the cancellation
        window are logged and swallowed so the Gateway can shut down
        cleanly.
        """
        if self._task is None:
            return

        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Scheduler raised an error during shutdown")
        finally:
            self._task = None
            logger.info("Cleanup scheduler stopped")

    # ------------------------------------------------------------------
    # Tick implementation — the real work happens here
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Single cleanup iteration.

        Uses ``self._pool`` and ``self._executor``.  Silently skips when
        either is ``None`` so the scheduler does not crash when Postgres
        is unavailable during boot.
        """
        if self._pool is None:
            logger.debug("CleanupScheduler._tick: no database pool available — skipping")
            return
        if self._executor is None:
            logger.debug("CleanupScheduler._tick: no executor available — skipping")
            return

        rows = await self._query_expired(self._pool)
        if not rows:
            return

        logger.info("CleanupScheduler._tick: processing %d expired workspace(s)", len(rows))

        for row in rows:
            try:
                await self._process_one(self._pool, self._executor, row)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:
                logger.exception(
                    "Unhandled error during cleanup of workspace %s", row.get("id")
                )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Main scheduler loop — ticks at the configured interval.

        Each tick is wrapped in a try/except so that errors are logged
        but never propagate to crash the Gateway process.
        """
        while not self._stopped.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler loop encountered an error")

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _query_expired(
        self, pool: SupportsAcquireRelease
    ) -> list[asyncpg.Record]:
        """Return expired workspaces eligible for cleanup, up to batch_size."""
        conn = await pool.acquire()
        try:
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
        finally:
            await pool.release(conn)

    async def _process_one(
        self,
        pool: SupportsAcquireRelease,
        executor: ExecutorPlugin,
        row: asyncpg.Record,
    ) -> None:
        """Acquire advisory lock, clean up one workspace, and update status."""
        ws_id = row["id"]

        # Advisory locks are connection-scoped — we hold a single
        # connection for the entire lock→cleanup→unlock sequence.
        conn = await pool.acquire()
        try:
            acquired = await try_acquire_cleanup_lock(conn, ws_id)
            if not acquired:
                logger.info(
                    "Advisory lock unavailable for workspace %s — skipping", ws_id
                )
                return

            logger.debug("Acquired advisory lock for workspace %s", ws_id)
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
                    # Release the port so it becomes available for reuse (ADR 0003).
                    await release_port(conn, ws_id)
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
                    await release_cleanup_lock(conn, ws_id)
                except Exception:
                    logger.exception(
                        "Failed to release advisory lock for workspace %s", ws_id
                    )
        finally:
            await pool.release(conn)
