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


# ── Static file serving (Aurora Glass dashboard) ───────────────────────


def test_app_registers_root_static_route():
    """The factory-built app should have the root `/` route registered."""
    from app.core.factory import create_app

    app = create_app(configure_logging=False)
    routes = [r.path for r in app.routes]
    assert "/" in routes


def test_app_registers_static_mount():
    """When frontend/ directory exists, the `/static` mount should be present."""
    from app.core.factory import create_app

    app = create_app(configure_logging=False)

    # Check for a Starlette StaticFiles mount at /static
    static_routes = [r for r in app.routes if getattr(r, "path", None) == "/static"]
    assert len(static_routes) > 0, "Expected a /static route mount"


@pytest.mark.asyncio
async def test_static_root_returns_index_html(monkeypatch):
    """The `/` route should serve index.html when frontend/ exists."""
    import os

    monkeypatch.setenv("GATEWAY_API_KEY", "test-api-key")
    from httpx import ASGITransport, AsyncClient

    from app.core.factory import create_app

    # Verify the frontend directory exists (it's part of the repo)
    assert os.path.isdir("frontend"), "frontend/ directory must exist for this test"

    app = create_app(configure_logging=False)
    app.state.pool = None

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    ) as ac:
        response = await ac.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Aurora Glass" in response.text


@pytest.mark.asyncio
async def test_static_serves_css(monkeypatch):
    """The /static mount should serve CSS files from frontend/."""
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
        response = await ac.get("/static/style.css")

    assert response.status_code == 200
    assert "text/css" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_static_root_graceful_degradation(monkeypatch):
    """When static_dir does not exist, the app still works and health endpoint responds."""
    monkeypatch.setenv("GATEWAY_STATIC_DIR", "/tmp/nonexistent-opencode-test-dir")
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
        # Health endpoint should still work even without frontend dir
        health_resp = await ac.get("/health")
        assert health_resp.status_code == 200

        # Root route returns 404 when index.html is missing
        root_resp = await ac.get("/")
        assert root_resp.status_code == 404


@pytest.mark.asyncio
async def test_static_root_missing_index_html(monkeypatch):
    """When static_dir exists but has no index.html, the root route returns 404."""
    monkeypatch.setenv("GATEWAY_STATIC_DIR", "tests")
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
        response = await ac.get("/")
    assert response.status_code == 404
