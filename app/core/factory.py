"""Application factory — builds and returns a configured FastAPI instance."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from starlette.staticfiles import StaticFiles

from app.core.config import get_settings
from app.db.schema import ensure_schema
from app.db.session import DatabasePool

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
    *,
    configure_logging: bool = True,
) -> FastAPI:
    """Build the FastAPI Gateway observability application.

    Accepts optional startup/shutdown callbacks that are invoked inside
    an ``@asynccontextmanager`` lifespan handler.  The callbacks may be
    either synchronous or asynchronous.

    The application initialises a Postgres connection pool on startup
    and closes it on shutdown.  If Postgres is unreachable the app logs
    a warning and continues without a pool.

    Args:
        on_startup: Optional list of callbacks to run on startup.
        on_shutdown: Optional list of callbacks to run on shutdown.
        configure_logging: If ``True`` (the default), install the
            :class:`~app.core.logging.RedactingFormatter` on the root
            logger so that secret-like values are redacted from all
            log output.  Set to ``False`` in tests that need to
            inspect raw log messages.
    """
    if configure_logging:
        from app.core.logging import configure_root_logger
        configure_root_logger()

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
        except Exception:
            logger.warning(
                "Postgres unavailable — starting without database pool",
                exc_info=True,
            )
            app.state.pool = None  # type: ignore[attr-defined]

        # --- Schema migration (Alembic) ---
        # Only run if the pool connected successfully.  Schema migration
        # failures are NOT treated as graceful degradation — they are a
        # hard startup error because missing tables would cause runtime
        # failures in API endpoints.
        if app.state.pool is not None and app.state.pool.pool is not None:  # type: ignore[attr-defined]
            await ensure_schema(app.state.pool.pool)  # type: ignore[attr-defined]

        yield

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

    # ── Middleware (applied in registration order — last added runs first) ──
    from app.core.auth import ApiKeyMiddleware
    from app.core.envelope import ResponseEnvelopeMiddleware

    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(ResponseEnvelopeMiddleware)

    # ── Exception handlers ──────────────────────────────────────────────
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException

    from app.core.envelope import (
        http_exception_handler,
        validation_exception_handler,
    )

    # RequestValidationError must be registered BEFORE HTTPException
    # because the former is a subclass of the latter.  Starlette
    # resolves handlers in insertion order via isinstance(), so the
    # more-specific handler must come first.
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    from app.api.admin_clients import router as admin_clients_router
    from app.api.health import router as health_router
    from app.api.ingest import router as ingest_router
    from app.api.usage import router as usage_router

    app.include_router(health_router)
    app.include_router(admin_clients_router)
    app.include_router(ingest_router)
    app.include_router(usage_router, prefix="/api/v1/usage")

    # ── Frontend static files (Aurora Glass dashboard) ──────────────────
    settings = get_settings()
    static_dir = os.path.abspath(settings.static_dir)

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Root route — serves the dashboard ``index.html``.
    @app.get("/", include_in_schema=False)
    async def spa_root():
        index_path = os.path.join(static_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return HTMLResponse(
            "<html><body><h1>404 Not Found</h1></body></html>",
            status_code=404,
        )

    # Note: No SPA catch-all route is registered because:
    #   (a) the current dashboard has no client-side routing, and
    #   (b) a bare catch-all breaks the 404 response-envelope contract
    #       for unknown API paths.
    # If future SPA routes are needed, register them explicitly here or
    # add a catch-all that excludes known API prefixes.

    return app
