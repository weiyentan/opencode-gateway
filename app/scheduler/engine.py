"""Scheduler engine — background loop for periodic cleanup tasks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default interval for the internal tick loop (seconds).
DEFAULT_SLEEP_SECONDS: float = 60.0


class Scheduler:
    """Background scheduler that periodically runs cleanup tasks.

    The scheduler is intended to be co-located with the Gateway
    process and is wired into the application lifespan so that it
    starts on boot and performs a soft-stop on graceful shutdown.
    """

    def __init__(self, interval_seconds: float | None = None) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopped: asyncio.Event = asyncio.Event()
        self._interval = interval_seconds or DEFAULT_SLEEP_SECONDS

    async def start(self, ctx: dict[str, Any]) -> None:
        """Begin the scheduler's background loop.

        ``ctx`` is a context dictionary that may hold references to
        the database pool, executor, or other shared resources.
        """
        logger.info("Cleanup scheduler started (interval=%.1fs)", self._interval)
        self._task = asyncio.create_task(self._run(ctx))

    async def stop(self, ctx: dict[str, Any]) -> None:
        """Perform a soft-stop of the scheduler.

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
    # Internal loop
    # ------------------------------------------------------------------

    async def _run(self, ctx: dict[str, Any]) -> None:
        """Main scheduler loop — ticks at a fixed interval.

        Each tick is wrapped in a try/except so that errors are
        logged but never propagate to crash the Gateway process.
        """
        while not self._stopped.is_set():
            try:
                await self._tick(ctx)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler loop encountered an error")

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _tick(self, ctx: dict[str, Any]) -> None:
        """Single iteration of the scheduler — stub for future work.

        In later issues this method will inspect the database for
        expired workspaces and invoke the executor to clean them up.
        """
        # Placeholder: no-op skeleton
        await asyncio.sleep(0)
