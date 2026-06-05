"""Health check endpoint — reports application status and database connectivity."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.db.session import DatabasePool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _get_version() -> str:
    """Return the installed package version, or a sensible fallback."""
    try:
        return version("opencode-gateway")
    except PackageNotFoundError:
        return "0.1.0-dev"


class HealthResponse(BaseModel):
    """Response model for the GET /health endpoint."""

    status: str = Field(default="ok", description="Application health status")
    version: str = Field(description="Installed package version")
    database: str = Field(
        default="disconnected", description="Database connectivity status"
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Return application health including database connectivity.

    Checks the connection pool stored on the application state
    (``request.app.state.pool``).  If the pool is ``None`` or an
    attempt to acquire a connection fails the endpoint still returns
    a 200 — the ``database`` field simply reports ``"disconnected"``.
    """
    db_pool: DatabasePool | None = getattr(request.app.state, "pool", None)

    if db_pool is None:
        return HealthResponse(version=_get_version(), database="disconnected")

    try:
        conn = await db_pool.acquire()
        await db_pool.release(conn)
        return HealthResponse(version=_get_version(), database="connected")
    except Exception:
        logger.warning("Health endpoint: database acquire failed", exc_info=True)
        return HealthResponse(version=_get_version(), database="disconnected")
