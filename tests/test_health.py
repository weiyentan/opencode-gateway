"""Tests for the GET /health endpoint."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app


@pytest.fixture
def client_no_db():
    """Return an httpx AsyncClient against an app with no database pool."""
    app = create_app()
    app.state.pool = None
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


@pytest.fixture
def client_healthy_db():
    """Return an httpx AsyncClient against an app with a mocked healthy pool."""
    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    app = create_app()
    app.state.pool = mock_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


@pytest.fixture
def client_broken_db():
    """Return an httpx AsyncClient against an app with a pool whose acquire raises."""
    mock_pool = AsyncMock()
    mock_pool.acquire = AsyncMock(side_effect=OSError("Connection refused"))
    app = create_app()
    app.state.pool = mock_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


class TestHealthEndpointBasic:
    """Basic smoke tests for GET /health."""

    @pytest.mark.asyncio
    async def test_returns_200_with_correct_json_structure(self, client_no_db):
        """GET /health should return 200 with status, version, and database fields."""
        async with client_no_db as client:
            response = await client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
        assert "status" in data
        assert "version" in data
        assert "database" in data
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_version_is_non_empty_string(self, client_no_db):
        """The version field should be a non-empty string."""
        async with client_no_db as client:
            response = await client.get("/health")

        payload = response.json()
        version = payload["data"]["version"]
        assert isinstance(version, str)
        assert len(version) > 0


class TestHealthDatabaseConnected:
    """Tests for the database connectivity check — connected case."""

    @pytest.mark.asyncio
    async def test_database_connected_when_pool_healthy(self, client_healthy_db):
        """If the pool is healthy, database should be 'connected'."""
        async with client_healthy_db as client:
            response = await client.get("/health")

        payload = response.json()
        assert payload["data"]["database"] == "connected"


class TestHealthDatabaseDisconnected:
    """Tests for the database connectivity check — disconnected cases."""

    @pytest.mark.asyncio
    async def test_database_disconnected_when_pool_is_none(self, client_no_db):
        """If app.state.pool is None, database should be 'disconnected'."""
        async with client_no_db as client:
            response = await client.get("/health")

        payload = response.json()
        assert payload["data"]["database"] == "disconnected"

    @pytest.mark.asyncio
    async def test_database_disconnected_when_acquire_raises(self, client_broken_db):
        """If pool.acquire() raises, database should be 'disconnected' (no 500 crash)."""
        async with client_broken_db as client:
            response = await client.get("/health")

        # Must still return 200, never 500
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"]["database"] == "disconnected"
