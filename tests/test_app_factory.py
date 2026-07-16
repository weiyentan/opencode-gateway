"""Tests for the app factory — the core FastAPI application builder."""

from unittest.mock import AsyncMock

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


def test_app_registers_health_router():
    """The factory-built app should have the /health endpoint."""
    from app.core.factory import create_app

    app = create_app(configure_logging=False)

    # Check that the health route is registered
    routes = [r.path for r in app.routes]
    assert "/health" in routes


@pytest.mark.asyncio
async def test_health_endpoint_works(monkeypatch):
    """The health endpoint should return 200 on the factory-built app."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-api-key")
    from httpx import ASGITransport, AsyncClient

    from app.core.factory import create_app

    app = create_app(configure_logging=False)
    app.state.pool = None

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    ) as ac:
        response = await ac.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
