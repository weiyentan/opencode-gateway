"""Tests for API-key authentication and response envelope.

Covers:
- Auth missing → 401 with envelope error format
- Auth invalid → 401 with envelope error format
- Auth valid → 2xx with envelope success format
- Envelope shape for health and jobs endpoints
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from tests.conftest import create_client, make_job_row, mock_row

# Re-use the test API key from conftest
_TEST_API_KEY = "test-api-key"


# ── Auth fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def client_no_auth():
    """Client with no Authorization header — should get 401."""
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client_bad_auth():
    """Client with an invalid API key — should get 401."""
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer wrong-key"},
    )


@pytest.fixture
def client_valid_auth():
    """Client with the valid test API key — should pass auth."""
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
    )


# ── Auth failure tests ───────────────────────────────────────────────────


class TestAuthMissing:
    """Requests without an Authorization header must return 401."""

    @pytest.mark.asyncio
    async def test_get_health_without_auth_returns_401(self, client_no_auth):
        async with client_no_auth as c:
            response = await c.get("/health")
        assert response.status_code == 401
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Missing" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_jobs_without_auth_returns_401(self, client_no_auth):
        async with client_no_auth as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Test task",
                },
            )
        assert response.status_code == 401
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_get_runners_without_auth_returns_401(self, client_no_auth):
        async with client_no_auth as c:
            response = await c.get("/runners")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_auth_header_returns_401(self, client_no_auth):
        """Authorization header without Bearer prefix is treated as missing."""
        async with client_no_auth as c:
            response = await c.get(
                "/health",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        assert response.status_code == 401
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"


class TestAuthInvalid:
    """Requests with an invalid API key must return 401."""

    @pytest.mark.asyncio
    async def test_get_health_with_invalid_key_returns_401(self, client_bad_auth):
        async with client_bad_auth as c:
            response = await c.get("/health")
        assert response.status_code == 401
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Invalid" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_jobs_with_invalid_key_returns_401(self, client_bad_auth):
        async with client_bad_auth as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Test task",
                },
            )
        assert response.status_code == 401


# ── Auth success tests ───────────────────────────────────────────────────


class TestAuthValid:
    """Requests with a valid API key must pass through to the endpoint."""

    @pytest.mark.asyncio
    async def test_get_health_with_valid_key_returns_envelope(self, client_valid_auth):
        """GET /health with valid key returns 200 with envelope."""
        async with client_valid_auth as c:
            response = await c.get("/health")
        assert response.status_code == 200
        data = response.json()
        # Envelope shape
        assert data["status"] == "ok"
        assert "data" in data
        # Inner data
        inner = data["data"]
        assert inner["status"] == "ok"
        assert "version" in inner
        assert "database" in inner

    @pytest.mark.asyncio
    async def test_get_runners_with_valid_key_returns_envelope(self, mock_conn):
        """GET /runners with valid key returns 200 with envelope."""
        mock_conn.fetch = AsyncMock(return_value=[])

        client = create_client(mock_conn, api_key=_TEST_API_KEY)

        async with client as c:
            response = await c.get("/runners")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"] == []

    @pytest.mark.asyncio
    async def test_get_job_not_found_returns_envelope_error(self, mock_conn):
        """GET /jobs/{id} returns enveloped 404 when job not found."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        client = create_client(mock_conn, api_key=_TEST_API_KEY)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}")
        assert response.status_code == 404
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "NOT_FOUND"
        assert "not found" in data["error"]["message"].lower()


# ── Envelope shape tests ─────────────────────────────────────────────────


class TestEnvelopeShape:
    """Verify that the envelope has the correct structure."""

    @pytest.mark.asyncio
    async def test_health_envelope_has_status_data(self, client_valid_auth):
        """Health endpoint envelope contains status=ok and data dict."""
        async with client_valid_auth as c:
            response = await c.get("/health")
        assert response.status_code == 200
        data = response.json()

        # Top-level envelope keys
        assert set(data.keys()) == {"status", "data"}
        assert data["status"] == "ok"
        assert isinstance(data["data"], dict)

    @pytest.mark.asyncio
    async def test_error_envelope_has_status_error_code_message(self, client_no_auth):
        """Error envelope contains status=error and error with code+message."""
        async with client_no_auth as c:
            response = await c.get("/health")
        assert response.status_code == 401
        data = response.json()

        # Top-level envelope keys
        assert set(data.keys()) == {"status", "error"}
        assert data["status"] == "error"
        assert isinstance(data["error"], dict)
        assert set(data["error"].keys()) == {"code", "message"}
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert isinstance(data["error"]["message"], str)
        assert len(data["error"]["message"]) > 0

    @pytest.mark.asyncio
    async def test_validation_error_uses_envelope(self, client_valid_auth):
        """Pydantic validation error (422) uses the envelope format."""
        async with client_valid_auth as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "",
                },
            )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "VALIDATION_ERROR"
        assert "validation" in data["error"]["message"].lower()
