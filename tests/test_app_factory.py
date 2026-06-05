"""Tests for the app factory — the core FastAPI application builder."""

import pytest
from fastapi import FastAPI


def test_create_app_returns_fastapi_instance():
    """The factory should return a FastAPI application instance."""
    from app.core.factory import create_app

    app = create_app()
    assert isinstance(app, FastAPI)


@pytest.mark.asyncio
async def test_lifespan_calls_startup_shutdown_in_order():
    """The custom lifespan yields between startup and shutdown phases."""
    from app.core.factory import create_app

    events: list[str] = []

    app = create_app(
        on_startup=[lambda: events.append("startup")],
        on_shutdown=[lambda: events.append("shutdown")],
    )
    async with app.router.lifespan_context(app):
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]
