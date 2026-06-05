"""Application factory — builds and returns a configured FastAPI instance."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI


def create_app(
    on_startup: list[Callable[[], Any]] | None = None,
    on_shutdown: list[Callable[[], Any]] | None = None,
) -> FastAPI:
    """Build the FastAPI Gateway application.

    Accepts optional startup/shutdown callbacks that are invoked inside
    an ``@asynccontextmanager`` lifespan handler.
    """
    startup_hooks = on_startup or []
    shutdown_hooks = on_shutdown or []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for hook in startup_hooks:
            hook()
        yield
        for hook in shutdown_hooks:
            hook()

    app = FastAPI(
        title="OpenCode Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    return app
