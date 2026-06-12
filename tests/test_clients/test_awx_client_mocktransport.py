"""MockTransport tests for AWXApiClient.

Uses ``httpx.MockTransport`` to simulate the AWX REST API directly,
providing realistic HTTP simulation without real network calls.

Follows the same pattern as ``test_serve_client_comprehensive.py``.
"""

from __future__ import annotations

import json
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

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_client(
    handler,
    base_url: str = "https://awx.example.com",
    token: str = "test-token-abc123",
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 5,
) -> AWXApiClient:
    """Build an ``AWXApiClient`` wired to an ``httpx.MockTransport``.

    Args:
        handler: A callable ``(httpx.Request) -> httpx.Response`` that
            simulates the AWX server.
        base_url: Base URL for the AWX instance.
        token: API Bearer token.
        timeout_seconds: Timeout for HTTP requests.
        poll_interval_seconds: Interval between job poll retries.

    Returns:
        A configured ``AWXApiClient`` that uses the mock transport.
    """
    client = AWXApiClient(
        base_url=base_url,
        token=token,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        headers={"Authorization": f"Bearer {token}"},
    )
    return client


# ══════════════════════════════════════════════════════════════════════════
# 1.  AUTH HEADER
# ══════════════════════════════════════════════════════════════════════════


class TestAuthHeader:
    """Client sends ``Authorization: Bearer <token>`` on every request."""

    @pytest.mark.asyncio
    async def test_bearer_token_on_launch(self) -> None:
        """Bearer token is sent in the launch request."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-token-abc123"
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        client = _make_client(handler)
        await client.launch_job_template(1)

    @pytest.mark.asyncio
    async def test_bearer_token_on_get_job(self) -> None:
        """Bearer token is sent in the get_job request."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-token-abc123"
            return httpx.Response(200, json={"id": 1, "status": "running"})

        client = _make_client(handler)
        await client.get_job(1)

    @pytest.mark.asyncio
    async def test_bearer_token_on_cancel(self) -> None:
        """Bearer token is sent in the cancel request."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer test-token-abc123"
            return httpx.Response(
                200,
                json={"id": 1, "status": "canceled"},
            )

        client = _make_client(handler)
        await client.cancel_job(1)

    @pytest.mark.asyncio
    async def test_custom_token_appears_in_header(self) -> None:
        """A custom token is reflected in the Authorization header."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer my-custom-secret"
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        client = _make_client(handler, token="my-custom-secret")
        await client.launch_job_template(1)


# ══════════════════════════════════════════════════════════════════════════
# 2.  launch_job_template
# ══════════════════════════════════════════════════════════════════════════


class TestLaunchJobTemplate:
    """``POST /api/v2/job_templates/{id}/launch/``."""

    @pytest.mark.asyncio
    async def test_launches_with_extra_vars(self) -> None:
        """Should POST extra_vars and return AWXJobSummary."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url).endswith("/api/v2/job_templates/7/launch/")
            body = json.loads(request.content)
            assert body == {
                "extra_vars": {
                    "repo_url": "https://git.example.com/repo.git",
                    "branch": "main",
                },
            }
            return httpx.Response(200, json={"id": 42, "status": "pending"})

        client = _make_client(handler)
        result = await client.launch_job_template(
            template_id=7,
            extra_vars={
                "repo_url": "https://git.example.com/repo.git",
                "branch": "main",
            },
        )

        assert isinstance(result, AWXJobSummary)
        assert result.job_id == 42
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_launches_without_extra_vars(self) -> None:
        """Should POST empty body when extra_vars is None."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url).endswith("/api/v2/job_templates/3/launch/")
            body = json.loads(request.content)
            assert body == {}
            return httpx.Response(200, json={"id": 99, "status": "running"})

        client = _make_client(handler)
        result = await client.launch_job_template(template_id=3)

        assert result.job_id == 99
        assert result.status == "running"

    @pytest.mark.asyncio
    async def test_defaults_status_to_unknown(self) -> None:
        """Should default status to 'unknown' when AWX omits it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 123})

        client = _make_client(handler)
        result = await client.launch_job_template(template_id=1)

        assert result.status == "unknown"

    @pytest.mark.asyncio
    async def test_raises_job_error_when_no_id(self) -> None:
        """Should raise AWXJobError when response is missing the job ID."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error"})

        client = _make_client(handler)
        with pytest.raises(AWXJobError) as exc_info:
            await client.launch_job_template(template_id=1)

        assert "missing job" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_launch_uses_correct_url(self) -> None:
        """Should construct the correct launch URL."""

        def handler(request: httpx.Request) -> httpx.Response:
            expected = "https://awx.example.com/api/v2/job_templates/42/launch/"
            assert str(request.url) == expected
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        client = _make_client(handler)
        await client.launch_job_template(42)


# ══════════════════════════════════════════════════════════════════════════
# 3.  get_job
# ══════════════════════════════════════════════════════════════════════════


class TestGetJob:
    """``GET /api/v2/jobs/{id}/``."""

    @pytest.mark.asyncio
    async def test_returns_job_summary(self) -> None:
        """Should return AWXJobSummary with job details."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert str(request.url) == "https://awx.example.com/api/v2/jobs/42/"
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "status": "running",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": None,
                },
            )

        client = _make_client(handler)
        result = await client.get_job(42)

        assert isinstance(result, AWXJobSummary)
        assert result.job_id == 42
        assert result.status == "running"
        assert result.started == "2024-01-01T00:00:00Z"
        assert result.finished is None

    @pytest.mark.asyncio
    async def test_defaults_status_to_unknown(self) -> None:
        """Should default status to 'unknown' when AWX omits it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 99})

        client = _make_client(handler)
        result = await client.get_job(99)

        assert result.status == "unknown"
        assert result.started is None
        assert result.finished is None

    @pytest.mark.asyncio
    async def test_populates_timestamps(self) -> None:
        """Should populate started and finished when AWX provides them."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "status": "successful",
                    "started": "2024-06-01T10:00:00Z",
                    "finished": "2024-06-01T10:05:30Z",
                },
            )

        client = _make_client(handler)
        result = await client.get_job(7)

        assert result.started == "2024-06-01T10:00:00Z"
        assert result.finished == "2024-06-01T10:05:30Z"

    @pytest.mark.asyncio
    async def test_handles_integer_job_id(self) -> None:
        """Should handle integer job IDs correctly."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://awx.example.com/api/v2/jobs/999/"
            return httpx.Response(200, json={"id": 999, "status": "failed"})

        client = _make_client(handler)
        result = await client.get_job(999)

        assert result.job_id == 999
        assert result.status == "failed"


# ══════════════════════════════════════════════════════════════════════════
# 4.  wait_for_job
# ══════════════════════════════════════════════════════════════════════════


class TestWaitForJob:
    """Poll loop ``wait_for_job()``."""

    @pytest.mark.asyncio
    async def test_returns_immediately_when_already_successful(self) -> None:
        """Should return AWXJobResult on first poll if already successful."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                    "elapsed": 300.0,
                    "artifacts": {"key": "value"},
                },
            )

        client = _make_client(handler)
        result = await client.wait_for_job(1)

        assert isinstance(result, AWXJobResult)
        assert result.job_id == 1
        assert result.status == "successful"
        assert result.elapsed_seconds == 300.0
        assert result.artifacts == {"key": "value"}
        # Two calls: one for status check, one for artifacts
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_job_error_when_failed(self) -> None:
        """Should raise AWXJobError when job status is 'failed'."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "status": "failed",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                },
            )

        client = _make_client(handler)
        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(2)

        assert exc_info.value.job_id == 2
        assert "failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_job_error_when_canceled(self) -> None:
        """Should raise AWXJobError when job status is 'canceled'."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 3, "status": "canceled"})

        client = _make_client(handler)
        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(3)

        assert exc_info.value.job_id == 3
        assert "canceled" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_job_error_when_error(self) -> None:
        """Should raise AWXJobError when job status is 'error'."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 4, "status": "error"})

        client = _make_client(handler)
        with pytest.raises(AWXJobError) as exc_info:
            await client.wait_for_job(4)

        assert exc_info.value.job_id == 4

    @pytest.mark.asyncio
    async def test_polls_until_successful(self) -> None:
        """Should poll multiple times until status becomes 'successful'."""
        responses = [
            httpx.Response(200, json={"id": 10, "status": "pending"}),
            httpx.Response(200, json={"id": 10, "status": "running"}),
            httpx.Response(
                200,
                json={
                    "id": 10,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ),
            # Final request for artifacts
            httpx.Response(
                200,
                json={
                    "id": 10,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                    "artifacts": {"output": "done"},
                },
            ),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            return resp

        client = _make_client(handler)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.wait_for_job(10)

        assert result.job_id == 10
        assert result.status == "successful"
        assert result.artifacts == {"output": "done"}
        assert call_index == 4

    @pytest.mark.asyncio
    async def test_polls_until_failed(self) -> None:
        """Should poll multiple times and raise AWXJobError on failure."""
        responses = [
            httpx.Response(200, json={"id": 11, "status": "pending"}),
            httpx.Response(200, json={"id": 11, "status": "running"}),
            httpx.Response(
                200,
                json={
                    "id": 11,
                    "status": "failed",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            return resp

        client = _make_client(handler)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXJobError) as exc_info:
                await client.wait_for_job(11)

        assert exc_info.value.job_id == 11

    @pytest.mark.asyncio
    async def test_times_out_without_auto_cancel(self) -> None:
        """Should raise AWXTimeoutError without auto-cancelling the job."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 12, "status": "running"})

        client = _make_client(handler)

        with patch(
            "app.executors.awx.client.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with patch(
                "app.executors.awx.client.time.monotonic",
                side_effect=[0, 0, 99999],
            ):
                with pytest.raises(AWXTimeoutError) as exc_info:
                    await client.wait_for_job(12, max_seconds=30)

        assert "12" in str(exc_info.value)
        assert "30" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_uses_configured_timeout_when_max_seconds_is_none(self) -> None:
        """Should default to self._timeout when max_seconds is None."""
        timeout_seconds = 30

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 13, "status": "running"})

        client = _make_client(handler, timeout_seconds=timeout_seconds)

        with patch(
            "app.executors.awx.client.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with patch(
                "app.executors.awx.client.time.monotonic",
                side_effect=[0, 0, 99999],
            ):
                with pytest.raises(AWXTimeoutError) as exc_info:
                    await client.wait_for_job(13)

        assert "30" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connection_error_mid_poll_propagates(self) -> None:
        """Connection errors during polling should propagate to caller."""
        responses = [
            httpx.Response(200, json={"id": 14, "status": "pending"}),
            httpx.ConnectError("Connection lost"),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            if isinstance(resp, httpx.Response):
                return resp
            raise resp  # Re-raise the ConnectError

        client = _make_client(handler)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXConnectionError):
                await client.wait_for_job(14)

    @pytest.mark.asyncio
    async def test_empty_artifacts_handled(self) -> None:
        """Empty or missing artifacts should default to empty dict."""
        responses = [
            httpx.Response(
                200,
                json={
                    "id": 15,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ),
            httpx.Response(
                200,
                json={
                    "id": 15,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                    # No artifacts field
                },
            ),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            return resp

        client = _make_client(handler)
        result = await client.wait_for_job(15)

        assert result.artifacts == {}

    @pytest.mark.asyncio
    async def test_elapsed_seconds_none_when_timestamps_missing(self) -> None:
        """elapsed_seconds should be None if started/finished are missing."""
        responses = [
            httpx.Response(
                200,
                json={
                    "id": 16,
                    "status": "successful",
                    # No started/finished fields
                },
            ),
            httpx.Response(
                200,
                json={
                    "id": 16,
                    "status": "successful",
                    "artifacts": {},
                },
            ),
        ]
        call_index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            return resp

        client = _make_client(handler)
        result = await client.wait_for_job(16)

        assert result.elapsed_seconds is None


# ══════════════════════════════════════════════════════════════════════════
# 5.  cancel_job
# ══════════════════════════════════════════════════════════════════════════


class TestCancelJob:
    """``POST /api/v2/jobs/{id}/cancel/``."""

    @pytest.mark.asyncio
    async def test_cancels_job_and_returns_result(self) -> None:
        """Should POST to cancel endpoint and return AWXJobResult."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert (
                str(request.url)
                == "https://awx.example.com/api/v2/jobs/42/cancel/"
            )
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "status": "canceled",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:03:00Z",
                    "elapsed": 180.0,
                    "artifacts": {},
                },
            )

        client = _make_client(handler)
        result = await client.cancel_job(42)

        assert isinstance(result, AWXJobResult)
        assert result.job_id == 42
        assert result.status == "canceled"
        assert result.elapsed_seconds == 180.0
        assert result.artifacts == {}

    @pytest.mark.asyncio
    async def test_defaults_missing_fields(self) -> None:
        """Should default missing fields gracefully."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 7, "status": "canceled"})

        client = _make_client(handler)
        result = await client.cancel_job(7)

        assert result.job_id == 7
        assert result.status == "canceled"
        assert result.elapsed_seconds is None
        assert result.artifacts == {}

    @pytest.mark.asyncio
    async def test_elapsed_seconds_none_when_no_timestamps(self) -> None:
        """elapsed_seconds should be None if started/finished are missing."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"id": 5, "status": "canceled", "artifacts": {}},
            )

        client = _make_client(handler)
        result = await client.cancel_job(5)

        assert result.elapsed_seconds is None


# ══════════════════════════════════════════════════════════════════════════
# 6.  ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Client should translate httpx exceptions into custom AWX ones."""

    @pytest.mark.asyncio
    async def test_raises_awx_timeout_on_httpx_timeout(self) -> None:
        """httpx.TimeoutException should become AWXTimeoutError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(AWXTimeoutError):
            await client.launch_job_template(1)

    @pytest.mark.asyncio
    async def test_raises_awx_connection_error_on_connect_error(self) -> None:
        """httpx.ConnectError should become AWXConnectionError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(AWXConnectionError):
            await client.launch_job_template(1)

    @pytest.mark.asyncio
    async def test_raises_awx_http_error_on_404(self) -> None:
        """A 404 response should raise AWXHTTPError with status_code."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc_info:
            await client.launch_job_template(1)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_awx_http_error_on_500(self) -> None:
        """A 500 response should raise AWXHTTPError with status_code."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Server error"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc_info:
            await client.launch_job_template(1)

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_awx_http_error_on_403(self) -> None:
        """A 403 response should raise AWXHTTPError with status_code."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "Forbidden"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc_info:
            await client.launch_job_template(1)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_raises_awx_client_error_on_generic_http_error(self) -> None:
        """Generic httpx.HTTPError should become AWXClientError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.HTTPError("Generic error")

        client = _make_client(handler)
        with pytest.raises(AWXClientError):
            await client.launch_job_template(1)

    @pytest.mark.asyncio
    async def test_timeout_on_get_job(self) -> None:
        """Timeout exception on get_job should become AWXTimeoutError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out on get_job")

        client = _make_client(handler)
        with pytest.raises(AWXTimeoutError):
            await client.get_job(1)

    @pytest.mark.asyncio
    async def test_connection_error_on_cancel_job(self) -> None:
        """Connection error on cancel_job should become AWXConnectionError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused on cancel")

        client = _make_client(handler)
        with pytest.raises(AWXConnectionError):
            await client.cancel_job(1)

    @pytest.mark.asyncio
    async def test_http_error_on_get_job(self) -> None:
        """HTTP error on get_job should become AWXHTTPError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "Service unavailable"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc_info:
            await client.get_job(1)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_http_error_on_cancel_job(self) -> None:
        """HTTP error on cancel_job should become AWXHTTPError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": "Cannot cancel"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc_info:
            await client.cancel_job(99)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_awx_http_error_stores_status_code(self) -> None:
        """AWXHTTPError should store the HTTP status_code."""
        err = AWXHTTPError("Forbidden", status_code=403)
        assert err.status_code == 403
        assert "Forbidden" in str(err)

    @pytest.mark.asyncio
    async def test_awx_job_error_stores_job_id(self) -> None:
        """AWXJobError should store the job_id passed to its constructor."""
        err = AWXJobError("Job failed", job_id=57)
        assert err.job_id == 57
        assert "Job failed" in str(err)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [400, 401, 403, 409, 422, 502, 503],
    )
    async def test_multiple_error_codes_on_launch(self, status_code: int) -> None:
        """Various HTTP error codes on launch_job_template."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc:
            await client.launch_job_template(1)
        assert exc.value.status_code == status_code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [404, 409, 422, 502, 503],
    )
    async def test_multiple_error_codes_on_get_job(self, status_code: int) -> None:
        """Various HTTP error codes on get_job."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(AWXHTTPError) as exc:
            await client.get_job(1)
        assert exc.value.status_code == status_code


# ══════════════════════════════════════════════════════════════════════════
# 7.  EDGE CASES
# ══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases for the AWX API client."""

    @pytest.mark.asyncio
    async def test_different_base_url(self) -> None:
        """Should work with a custom base URL."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).startswith("http://my-awx.internal:8043")
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        client = _make_client(handler, base_url="http://my-awx.internal:8043")
        await client.launch_job_template(1)

    @pytest.mark.asyncio
    async def test_very_large_job_id(self) -> None:
        """Should handle large integer job IDs."""

        large_id = 2147483647

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith(f"/api/v2/jobs/{large_id}/")
            return httpx.Response(200, json={"id": large_id, "status": "running"})

        client = _make_client(handler)
        result = await client.get_job(large_id)

        assert result.job_id == large_id

    @pytest.mark.asyncio
    async def test_elapsed_with_trailing_z_timestamps(self) -> None:
        """Should handle ISO 8601 timestamps with trailing Z."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 20,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T02:30:00Z",
                    "artifacts": {},
                },
            )

        responses = [handler(None), handler(None)]
        call_index = 0

        def stateful_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_index
            resp = responses[call_index]
            call_index += 1
            return resp

        client = _make_client(stateful_handler)
        result = await client.wait_for_job(20)

        assert result.elapsed_seconds == 9000.0  # 2.5 hours
