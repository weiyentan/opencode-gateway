"""Application factory — builds and returns a configured FastAPI instance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.core.config import get_settings
from app.db.schema import ensure_schema
from app.db.session import DatabasePool
from app.scheduler import Scheduler

logger = logging.getLogger(__name__)


async def _invoke_hook(hook: Callable[[], Any]) -> None:
    """Run a hook, awaiting it if it is a coroutine function, calling it if sync."""
    if asyncio.iscoroutinefunction(hook):
        await hook()
    else:
        hook()


def create_app(
    on_startup: list[Callable[[], Any]] | None = None,
    on_shutdown: list[Callable[[], Any]] | None = None,
) -> FastAPI:
    """Build the FastAPI Gateway application.

    Accepts optional startup/shutdown callbacks that are invoked inside
    an ``@asynccontextmanager`` lifespan handler.  The callbacks may be
    either synchronous or asynchronous.

    The application also initialises a Postgres connection pool on
    startup and closes it on shutdown.  If Postgres is unreachable the
    app logs a warning and continues without a pool.

    A background cleanup scheduler is started during the boot sequence
    and stopped gracefully on shutdown.
    """
    startup_hooks = on_startup or []
    shutdown_hooks = on_shutdown or []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # --- user-provided startup hooks ---
        for hook in startup_hooks:
            await _invoke_hook(hook)

        # --- Postgres pool ---
        settings = get_settings()
        pool = DatabasePool(settings)
        try:
            await pool.connect()
            app.state.pool = pool  # type: ignore[attr-defined]

            # Ensure the database schema is current (idempotent)
            if pool.pool is not None:
                await ensure_schema(pool.pool)
        except Exception:
            logger.warning(
                "Postgres unavailable — starting without database pool",
                exc_info=True,
            )
            app.state.pool = None  # type: ignore[attr-defined]

        # --- Cleanup scheduler ---
        scheduler = Scheduler()
        app.state.scheduler = scheduler  # type: ignore[attr-defined]
        await scheduler.start(ctx={"pool": app.state.pool})  # type: ignore[attr-defined]

        yield

        # --- Cleanup scheduler shutdown ---
        await scheduler.stop(ctx={"pool": app.state.pool})  # type: ignore[attr-defined]

        # --- Postgres pool shutdown ---
        db_pool: DatabasePool | None = app.state.pool  # type: ignore[attr-defined]
        if db_pool is not None:
            await db_pool.close()

        # --- user-provided shutdown hooks ---
        for hook in shutdown_hooks:
            await _invoke_hook(hook)

    app = FastAPI(
        title="OpenCode Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    from app.api.health import router as health_router
    from app.api.jobs import router as jobs_router
    from app.api.workspaces import router as workspaces_router

    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(workspaces_router)

    return app
