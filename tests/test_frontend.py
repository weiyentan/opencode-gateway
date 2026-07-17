"""Tests for the Aurora Glass frontend.

Includes integration tests for the static file mount and unit tests
for JavaScript pure-function equivalents.
"""

from pathlib import Path

import pytest
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app


@pytest.fixture
def app():
    """Create the application for frontend testing with static file mount."""
    application = create_app(configure_logging=False)
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=str(frontend_dir), html=True),
            name="frontend",
        )
    return application


@pytest.fixture
def client(app):
    """Return an httpx AsyncClient against the app.
    
    Uses the same API key that conftest.py configures via
    os.environ.setdefault at import time.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


class TestStaticMount:
    """Integration tests for the static file mount at /."""

    @pytest.mark.asyncio
    async def test_index_html_returns_200(self, client):
        """GET / should return 200 with HTML content."""
        async with client as c:
            response = await c.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    @pytest.mark.asyncio
    async def test_index_html_contains_dashboard_title(self, client):
        """The index.html page should contain the dashboard title."""
        async with client as c:
            response = await c.get("/")
        assert response.status_code == 200
        html = response.text
        assert "Aurora Glass" in html

    @pytest.mark.asyncio
    async def test_static_css_is_served(self, client):
        """GET /style.css should return CSS content."""
        async with client as c:
            response = await c.get("/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_static_js_is_served(self, client):
        """GET /app.js should return JavaScript content."""
        async with client as c:
            response = await c.get("/app.js")
        assert response.status_code == 200
        assert "application/javascript" in response.headers.get("content-type", "") or "text/javascript" in response.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_frontend_directory_exists(self):
        """The frontend/ directory must exist for the static mount."""
        frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
        assert frontend_dir.is_dir(), "frontend/ directory missing"
        assert (frontend_dir / "index.html").exists(), "frontend/index.html missing"
        assert (frontend_dir / "style.css").exists(), "frontend/style.css missing"
        assert (frontend_dir / "app.js").exists(), "frontend/app.js missing"

    @pytest.mark.asyncio
    async def test_unknown_static_file_returns_404(self, client):
        """GET /nonexistent.file should return 404 when the file doesn't exist."""
        async with client as c:
            response = await c.get("/nonexistent-file-xyz.js")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_api_route_takes_priority_over_static(self, client, monkeypatch):
        """API routes should take priority — GET /health should return health JSON, not index.html."""
        async with client as c:
            response = await c.get("/health")
        # Even though StaticFiles is mounted at /, API routes registered first
        # should take priority.  Health endpoint returns JSON.
        assert response.status_code == 200
        ct = response.headers.get("content-type", "")
        assert "application/json" in ct or "text/plain" in ct
