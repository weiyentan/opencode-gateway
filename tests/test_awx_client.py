"""Tests for the AWX API client.

Covers construction, launch_job_template, get_job, wait_for_job,
cancel_job, error handling, and logging.
Uses mocked httpx.AsyncClient to avoid real network calls, following
the same pattern as test_serve_client.py.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.executors.awx.client import AWXApiClient, AWXJobResult, AWXJobSummary
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


# ── get_job() ────────────────────────────────────────────────────────


class TestGetJob:
    """``GET /api/v2/jobs/{id}/``."""

    @pytest.mark.asyncio
    async def test_returns_job_summary(self, client):
        """Should return AWXJobSummary with job details."""
        client._client.request.return_value = _mock_response(
            {
                "id": 42,
                "status": "running",
                "started": "2024-01-01T00:00:00Z",
                "finished": None,
            },
        )

        result = await client.get_job(42)

        assert isinstance(result, AWXJobSummary)
        assert result.job_id == 42
        assert result.status == "running"
        assert result.started == "2024-01-01T00:00:00Z"
        assert result.finished is None
        client._client.request.assert_awaited_once_with(
            "GET",
            "https://awx.example.com/api/v2/jobs/42/",
        )

    @pytest.mark.asyncio
    async def test_defaults_status_to_unknown(self, client):
        """Should default status to 'unknown' when AWX omits it."""
        client._client.request.return_value = _mock_response(
            {"id": 99},
        )

        result = await client.get_job(99)
        assert result.status == "unknown"
        assert result.started is None
        assert result.finished is None

    @pytest.mark.asyncio
    async def test_raises_http_error_on_404(self, client):
        """A 404 from AWX should raise AWXHTTPError."""
        client._client.request.return_value = _mock_response(
            {"detail": "Not found"}, status_code=404,
        )

        with pytest.raises(AWXHTTPError) as exc_info:
            await client.get_job(99999)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_populates_timestamps(self, client):
        """Should populate started and finished when AWX provides them."""
        client._client.request.return_value = _mock_response(
            {
                "id": 7,
                "status": "successful",
                "started": "2024-06-01T10:00:00Z",
                "finished": "2024-06-01T10:05:30Z",
            },
        )

        result = await client.get_job(7)
        assert result.started == "2024-06-01T10:00:00Z"
        assert result.finished == "2024-06-01T10:05:30Z"


# ── wait_for_job() ───────────────────────────────────────────────────


class TestWaitForJob:
    """Poll loop ``wait_for_job()``."""

    @pytest.mark.asyncio
    async def test_returns_immediately_when_already_successful(self, client):
        """Should return AWXJobResult on first poll if already successful."""
        client._client.request.return_value = _mock_response(
            {
                "id": 1,
                "status": "successful",
                "started": "2024-01-01T00:00:00Z",
                "finished": "2024-01-01T00:05:00Z",
                "elapsed": 300.0,
                "artifacts": {"key": "value"},
            },
        )

        result = await client.wait_for_job(1)

        assert isinstance(result, AWXJobResult)
        assert result.job_id == 1
        assert result.status == "successful"
        assert result.elapsed_seconds == 300.0
        assert result.artifacts == {"key": "value"}

    @pytest.mark.asyncio
    async def test_raises_job_error_when_failed(self, client):
        """Should raise AWXJobError when job status is 'failed'."""
        client._client.request.return_value = _mock_response(
            {
                "id": 2,
                "status": "failed",
                "started": "2024-01-01T00:00:00Z",
                "finished": "2024-01-01T00:01:00Z",
            },
        )

        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(2)
        assert exc_info.value.job_id == 2
        assert "failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_job_error_when_canceled(self, client):
        """Should raise AWXJobError when job status is 'canceled'."""
        client._client.request.return_value = _mock_response(
            {
                "id": 3,
                "status": "canceled",
            },
        )

        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(3)
        assert exc_info.value.job_id == 3
        assert "canceled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_job_error_when_error(self, client):
        """Should raise AWXJobError when job status is 'error'."""
        client._client.request.return_value = _mock_response(
            {
                "id": 4,
                "status": "error",
            },
        )

        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(4)
        assert exc_info.value.job_id == 4

    @pytest.mark.asyncio
    async def test_polls_until_successful(self, client):
        """Should poll multiple times until status becomes 'successful'."""
        # get_job calls: pending → running → successful
        # Then wait_for_job makes an extra request for artifacts
        call_responses = [
            _mock_response({"id": 10, "status": "pending"}),
            _mock_response({"id": 10, "status": "running"}),
            _mock_response(
                {
                    "id": 10,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ),
            # Extra request for artifacts (success case)
            _mock_response(
                {
                    "id": 10,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                    "artifacts": {"output": "done"},
                },
            ),
        ]
        client._client.request.side_effect = call_responses

        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.wait_for_job(10)

        assert result.job_id == 10
        assert result.status == "successful"
        assert result.artifacts == {"output": "done"}

    @pytest.mark.asyncio
    async def test_polls_until_failed(self, client):
        """Should poll multiple times and raise AWXJobError on failure."""
        call_responses = [
            _mock_response({"id": 11, "status": "pending"}),
            _mock_response({"id": 11, "status": "running"}),
            _mock_response(
                {
                    "id": 11,
                    "status": "failed",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ),
        ]
        client._client.request.side_effect = call_responses

        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXJobError) as exc_info:
                await client.wait_for_job(11)
        assert exc_info.value.job_id == 11

    @pytest.mark.asyncio
    async def test_times_out_without_auto_cancel(self, client):
        """Should raise AWXTimeoutError without auto-cancelling the job."""
        client._client.request.return_value = _mock_response(
            {"id": 12, "status": "running"},
        )

        with patch(
            "app.executors.awx.client.time.monotonic",
            side_effect=[0, 0, 99999],  # Start, after first poll → expired
        ):
            with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(AWXTimeoutError) as exc_info:
                    await client.wait_for_job(12, max_seconds=30)
        assert "12" in str(exc_info.value)
        assert "30" in str(exc_info.value)
        # Verify cancel endpoint was never called
        for call in client._client.request.call_args_list:
            url = call.args[1]  # Second positional arg is the URL
            assert "/cancel/" not in url

    @pytest.mark.asyncio
    async def test_uses_configured_timeout_when_max_seconds_is_none(self, client):
        """Should default to self._timeout when max_seconds is None."""
        client._client.request.return_value = _mock_response(
            {"id": 13, "status": "running"},
        )

        with patch(
            "app.executors.awx.client.time.monotonic",
            side_effect=[0, 0, 99999],
        ):
            with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(AWXTimeoutError) as exc_info:
                    await client.wait_for_job(13)
        # The fixture has timeout_seconds=30 (see client fixture)
        assert "30" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connection_error_mid_poll_propagates(self, client):
        """Connection errors during polling should propagate to caller."""
        call_responses = [
            _mock_response({"id": 14, "status": "pending"}),
            httpx.ConnectError("Connection lost"),
        ]
        # First: successful mock response, then: side_effect for the error
        client._client.request.side_effect = [
            call_responses[0],
            call_responses[1],
        ]

        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXConnectionError):
                await client.wait_for_job(14)

    @pytest.mark.asyncio
    async def test_empty_artifacts_handled(self, client):
        """Empty or missing artifacts should default to empty dict."""
        client._client.request.side_effect = [
            _mock_response(
                {
                    "id": 15,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ),
            _mock_response(
                {
                    "id": 15,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                    # No artifacts field at all
                },
            ),
        ]

        result = await client.wait_for_job(15)
        assert result.artifacts == {}

    @pytest.mark.asyncio
    async def test_elapsed_seconds_none_when_timestamps_missing(self, client):
        """elapsed_seconds should be None if started/finished are missing."""
        client._client.request.side_effect = [
            _mock_response(
                {
                    "id": 16,
                    "status": "successful",
                    # No started/finished fields
                },
            ),
            _mock_response(
                {
                    "id": 16,
                    "status": "successful",
                    "artifacts": {},
                },
            ),
        ]

        result = await client.wait_for_job(16)
        assert result.elapsed_seconds is None


# ── cancel_job() ──────────────────────────────────────────────────────


class TestCancelJob:
    """``POST /api/v2/jobs/{id}/cancel/``."""

    @pytest.mark.asyncio
    async def test_cancels_job_and_returns_result(self, client):
        """Should POST to cancel endpoint and return AWXJobResult."""
        client._client.request.return_value = _mock_response(
            {
                "id": 42,
                "status": "canceled",
                "started": "2024-01-01T00:00:00Z",
                "finished": "2024-01-01T00:03:00Z",
                "elapsed": 180.0,
                "artifacts": {},
            },
        )

        result = await client.cancel_job(42)

        assert isinstance(result, AWXJobResult)
        assert result.job_id == 42
        assert result.status == "canceled"
        assert result.elapsed_seconds == 180.0
        assert result.artifacts == {}
        client._client.request.assert_awaited_once_with(
            "POST",
            "https://awx.example.com/api/v2/jobs/42/cancel/",
        )

    @pytest.mark.asyncio
    async def test_raises_http_error_on_failure(self, client):
        """Should raise AWXHTTPError if cancel request fails."""
        client._client.request.return_value = _mock_response(
            {"detail": "Cannot cancel finished job"}, status_code=400,
        )

        with pytest.raises(AWXHTTPError) as exc_info:
            await client.cancel_job(99)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_defaults_missing_fields(self, client):
        """Should default missing fields gracefully."""
        client._client.request.return_value = _mock_response(
            {"id": 7, "status": "canceled"},
        )

        result = await client.cancel_job(7)
        assert result.job_id == 7
        assert result.status == "canceled"
        assert result.elapsed_seconds is None
        assert result.artifacts == {}

    @pytest.mark.asyncio
    async def test_elapsed_seconds_none_when_no_timestamps(self, client):
        """elapsed_seconds should be None if started/finished are missing."""
        client._client.request.return_value = _mock_response(
            {
                "id": 5,
                "status": "canceled",
                "artifacts": {},
            },
        )

        result = await client.cancel_job(5)
        assert result.elapsed_seconds is None
