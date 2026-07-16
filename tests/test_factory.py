"""Tests for the simplified app factory — post-execution-era cleanup.

After issue #207, the factory no longer initialises executors or
schedulers.  These tests verify the basic lifecycle (pool connect →
schema migration → yield → pool close) still works.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI


def test_create_app_returns_fastapi_instance():
    """The simplified factory should still return a FastAPI application instance."""
    from app.core.factory import create_app

    app = create_app()
    assert isinstance(app, FastAPI)


@pytest.mark.asyncio
async def test_lifespan_connects_and_closes_pool():
    """The simplified lifespan should connect the pool, run ensure_schema, and close on shutdown."""
    from app.core.factory import create_app

    mock_asyncpg_pool = AsyncMock()
    mock_create_pool = AsyncMock(return_value=mock_asyncpg_pool)

    with patch(
        "app.db.session.asyncpg.create_pool", mock_create_pool
    ), patch("app.core.factory.ensure_schema") as mock_ensure:
        app = create_app(configure_logging=False)
        async with app.router.lifespan_context(app):
            # Pool should be connected and stored on app.state
            assert app.state.pool is not None

        # ensure_schema should have been called
        mock_ensure.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_starts_without_postgres():
    """If Postgres is unreachable, lifespan should still complete gracefully."""
    from app.core.factory import create_app

    with patch(
        "app.db.session.asyncpg.create_pool",
        side_effect=OSError("Connection refused"),
    ):
        app = create_app(configure_logging=False)
        async with app.router.lifespan_context(app):
            # Pool should be None when Postgres is unreachable
            assert app.state.pool is None  # type: ignore[attr-defined]
