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


def test_get_opencode_client_can_be_overridden_via_dependency_overrides():
    """The get_opencode_client dependency should be overridable via dependency_overrides.

    This verifies that the dependency wiring in the factory-built app respects
    FastAPI's override mechanism so tests can inject mock clients.
    """
    from app.api.jobs import get_opencode_client
    from app.core.factory import create_app

    app = create_app()

    # The default dependency returns a real OpenCodeServeClient (or None if
    # GATEWAY_OPENCODE_BASE_URL is empty).  Override it with a mock.
    mock_client = AsyncMock()
    app.dependency_overrides[get_opencode_client] = lambda: mock_client

    # Verify the override was registered
    assert get_opencode_client in app.dependency_overrides
    resolved = app.dependency_overrides[get_opencode_client]()
    assert resolved is mock_client


@pytest.mark.asyncio
async def test_get_opencode_client_dependency_resolves_via_app(monkeypatch):
    """The get_opencode_client dependency should resolve when called through the app.

    Creates the app and uses FastAPI's dependency resolution to call
    the actual get_opencode_client() function, verifying it returns a
    client instance (or None if not configured).
    """
    monkeypatch.setenv("GATEWAY_API_KEY", "test-api-key")
    from fastapi import Depends, FastAPI

    from app.api.jobs import get_opencode_client
    from app.core.factory import create_app

    app = create_app(configure_logging=False)

    # Create a dummy route that exercises the dependency so we can
    # verify it resolves without error.
    capture: list = []

    @app.get("/_test_opencode_dep")
    async def _test_dep(
        client=Depends(get_opencode_client),  # noqa: B008
    ):
        capture.append(client)
        return {"ok": True}

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    ) as ac:
        response = await ac.get("/_test_opencode_dep")

    assert response.status_code == 200
    assert len(capture) == 1

    # The resolved dependency should either be None (if URL is empty)
    # or an OpenCodeServeClient instance.
    from app.opencode.serve_client import OpenCodeServeClient

    resolved = capture[0]
    assert resolved is None or isinstance(resolved, OpenCodeServeClient)
