"""Tests for the AWX API client.

Covers construction, launch_job_template, error handling, and logging.
Uses mocked httpx.AsyncClient to avoid real network calls, following
the same pattern as test_serve_client.py.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from app.executors.awx.client import AWXApiClient, AWXJobSummary
from app.executors.awx.exceptions import (
    AWXClientError,
    AWXConnectionError,
    AWXHTTPError,
    AWXJobError,
    AWXTimeoutError,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _mock_response(data: dict, status_code: int = 200) -> AsyncMock:
    """Build a minimal mock ``httpx.Response``."""
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """Return an AWXApiClient with a mocked HTTP transport."""
    c = AWXApiClient(
        base_url="https://awx.example.com",
        token="test-token-abc123",
        timeout_seconds=30,
        poll_interval_seconds=2,
    )
    c._client = AsyncMock(spec=httpx.AsyncClient)
    return c


# ── Construction ────────────────────────────────────────────────────


class TestConstruction:
    """Client construction and configuration."""

    def test_accepts_base_url_token_and_timeout(self):
        """Constructor should store configuration values."""
        c = AWXApiClient(
            base_url="https://awx.example.com",
            token="secret",
            timeout_seconds=120,
            poll_interval_seconds=10,
        )
        assert c._base_url == "https://awx.example.com"
        assert c._token == "secret"
        assert c._timeout == 120
        assert c._poll_interval == 10
        assert c._client is not None

    def test_strips_trailing_slash(self):
        """Trailing slash in base_url should be stripped."""
        c = AWXApiClient(
            base_url="https://awx.example.com/",
            token="secret",
        )
        assert c._base_url == "https://awx.example.com"

    def test_default_timeout_is_300(self):
        """Default timeout should be 300 seconds."""
        c = AWXApiClient(
            base_url="https://awx.example.com",
            token="secret",
        )
        assert c._timeout == 300

    def test_default_poll_interval_is_5(self):
        """Default poll interval should be 5 seconds."""
        c = AWXApiClient(
            base_url="https://awx.example.com",
            token="secret",
        )
        assert c._poll_interval == 5

    def test_sets_bearer_auth_header(self):
        """Client should configure the Authorization header with Bearer token."""
        c = AWXApiClient(
            base_url="https://awx.example.com",
            token="my-token",
        )
        headers = c._client.headers
        assert headers["Authorization"] == "Bearer my-token"

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Client should work as an async context manager."""
        async with AWXApiClient(
            base_url="https://awx.example.com",
            token="secret",
            timeout_seconds=5,
        ) as c:
            assert isinstance(c, AWXApiClient)

    @pytest.mark.asyncio
    async def test_close_method(self):
        """close() should not raise."""
        c = AWXApiClient(
            base_url="https://awx.example.com",
            token="secret",
        )
        await c.close()


# ── launch_job_template() ───────────────────────────────────────────


class TestLaunchJobTemplate:
    """``POST /api/v2/job_templates/{id}/launch/``."""

    @pytest.mark.asyncio
    async def test_launches_with_extra_vars(self, client):
        """Should POST extra_vars and return AWXJobSummary."""
        client._client.request.return_value = _mock_response(
            {"id": 42, "status": "pending", "name": "test-job"},
        )

        result = await client.launch_job_template(
            template_id=7,
            extra_vars={"repo_url": "https://git.example.com/repo.git", "branch": "main"},
        )

        assert isinstance(result, AWXJobSummary)
        assert result.job_id == 42
        assert result.status == "pending"
        client._client.request.assert_awaited_once_with(
            "POST",
            "https://awx.example.com/api/v2/job_templates/7/launch/",
            json={"extra_vars": {"repo_url": "https://git.example.com/repo.git", "branch": "main"}},
        )

    @pytest.mark.asyncio
    async def test_launches_without_extra_vars(self, client):
        """Should POST empty body when extra_vars is None."""
        client._client.request.return_value = _mock_response(
            {"id": 99, "status": "running"},
        )

        result = await client.launch_job_template(template_id=3)

        assert result.job_id == 99
        assert result.status == "running"
        client._client.request.assert_awaited_once_with(
            "POST",
            "https://awx.example.com/api/v2/job_templates/3/launch/",
            json={},
        )

    @pytest.mark.asyncio
    async def test_defaults_status_to_unknown(self, client):
        """Should default status to 'unknown' when AWX omits it."""
        client._client.request.return_value = _mock_response(
            {"id": 123},
        )

        result = await client.launch_job_template(template_id=1)
        assert result.status == "unknown"

    @pytest.mark.asyncio
    async def test_raises_job_error_when_no_id(self, client):
        """Should raise AWXJobError when response is missing the job ID."""
        client._client.request.return_value = _mock_response(
            {"status": "error"},
        )

        with pytest.raises(AWXJobError) as exc_info:
            await client.launch_job_template(template_id=1)

        assert "missing job" in str(exc_info.value).lower()


# ── Error handling ──────────────────────────────────────────────────


class TestErrorHandling:
    """Client should translate httpx exceptions into custom AWX ones."""

    @pytest.mark.asyncio
    async def test_raises_awx_timeout_on_httpx_timeout(self, client):
        """httpx.TimeoutException should become AWXTimeoutError."""
        client._client.request.side_effect = httpx.TimeoutException("Timed out")

        with pytest.raises(AWXTimeoutError):
            await client.launch_job_template(template_id=1)

    @pytest.mark.asyncio
    async def test_raises_awx_connection_error_on_connect_error(self, client):
        """httpx.ConnectError should become AWXConnectionError."""
        client._client.request.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(AWXConnectionError):
            await client.launch_job_template(template_id=1)

    @pytest.mark.asyncio
    async def test_raises_awx_http_error_on_404(self, client):
        """A 404 response should raise AWXHTTPError with status_code."""
        client._client.request.return_value = _mock_response(
            {"detail": "Not found"}, status_code=404,
        )

        with pytest.raises(AWXHTTPError) as exc_info:
            await client.launch_job_template(template_id=1)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_awx_http_error_on_500(self, client):
        """A 500 response should raise AWXHTTPError with status_code."""
        client._client.request.return_value = _mock_response(
            {"detail": "Server error"}, status_code=500,
        )

        with pytest.raises(AWXHTTPError) as exc_info:
            await client.launch_job_template(template_id=1)

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_awx_client_error_on_generic_http_error(self, client):
        """Generic httpx.HTTPError should become AWXClientError."""
        client._client.request.side_effect = httpx.HTTPError("Generic error")

        with pytest.raises(AWXClientError):
            await client.launch_job_template(template_id=1)

    @pytest.mark.asyncio
    async def test_awx_job_error_stores_job_id(self):
        """AWXJobError should store the job_id passed to its constructor."""
        err = AWXJobError("Job failed", job_id=57)
        assert err.job_id == 57
        assert "Job failed" in str(err)

    @pytest.mark.asyncio
    async def test_awx_http_error_stores_status_code(self):
        """AWXHTTPError should store the HTTP status_code."""
        err = AWXHTTPError("Forbidden", status_code=403)
        assert err.status_code == 403
        assert "Forbidden" in str(err)


# ── Logging ─────────────────────────────────────────────────────────


class TestLogging:
    """Client should emit DEBUG-level log messages."""

    @pytest.mark.asyncio
    async def test_logs_request_and_response(self, client, caplog):
        """A successful request should produce request and response log lines."""
        from app.executors.awx import client as awx_client

        awx_client.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=awx_client.logger.name)

        client._client.request.return_value = _mock_response(
            {"id": 1, "status": "pending"},
        )

        await client.launch_job_template(template_id=1)

        assert any("Sending POST request" in msg for msg in caplog.messages)
        assert any("Received response 200" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_logs_timeout(self, client, caplog):
        """A timeout should produce a debug log message."""
        from app.executors.awx import client as awx_client

        awx_client.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=awx_client.logger.name)

        client._client.request.side_effect = httpx.TimeoutException("Timed out")

        with pytest.raises(AWXTimeoutError):
            await client.launch_job_template(template_id=1)

        assert any("timed out" in msg.lower() for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_logs_connection_error(self, client, caplog):
        """A connection error should produce a debug log message."""
        from app.executors.awx import client as awx_client

        awx_client.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=awx_client.logger.name)

        client._client.request.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(AWXConnectionError):
            await client.launch_job_template(template_id=1)

        assert any("failed" in msg.lower() for msg in caplog.messages)
