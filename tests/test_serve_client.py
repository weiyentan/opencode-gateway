"""Tests for the OpenCode Serve HTTP client.

Covers construction, each protocol method, error handling, and logging.
Uses mocked httpx.AsyncClient to avoid real network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.opencode.protocol import (
    SessionAbortResponse,
    SessionDiffResponse,
    SessionInfo,
    SessionListResponse,
)
from app.opencode.serve_client import (
    OpenCodeClientError,
    OpenCodeConnectionError,
    OpenCodeHTTPError,
    OpenCodeServeClient,
    OpenCodeTimeoutError,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _mock_response(data: dict, status_code: int = 200) -> AsyncMock:
    """Build a minimal mock ``httpx.Response``."""
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


def _sample_session_info(
    session_id: str = "sess-1",
    status: str = "running",
) -> dict:
    return {
        "id": session_id,
        "status": status,
        "workspace_path": "/workspaces/repo",
        "task_description": "Fix bug",
        "created_at": "2025-06-01T12:00:00Z",
        "updated_at": None,
    }


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """Return an OpenCodeServeClient with a mocked HTTP transport."""
    c = OpenCodeServeClient(base_url="http://opencode-serve:8080", timeout=10)
    c._client = AsyncMock(spec=httpx.AsyncClient)
    return c


# ── Construction ────────────────────────────────────────────────────────


class TestConstruction:
    """Client construction and configuration."""

    def test_accepts_base_url_and_timeout(self):
        """Constructor should store base_url and timeout."""
        c = OpenCodeServeClient(base_url="http://localhost:8080", timeout=30)
        assert c._base_url == "http://localhost:8080"
        assert c._timeout == 30
        assert c._client is not None

    def test_strips_trailing_slash(self):
        """Trailing slash in base_url should be stripped."""
        c = OpenCodeServeClient(base_url="http://localhost:8080/", timeout=10)
        assert c._base_url == "http://localhost:8080"

    def test_default_timeout_is_30(self):
        """Default timeout should be 30 seconds."""
        c = OpenCodeServeClient(base_url="http://localhost:8080")
        assert c._timeout == 30

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Client should work as an async context manager."""
        async with OpenCodeServeClient(
            base_url="http://localhost:8080", timeout=5
        ) as c:
            assert isinstance(c, OpenCodeServeClient)

    @pytest.mark.asyncio
    async def test_close_method(self):
        """close() should not raise."""
        c = OpenCodeServeClient(base_url="http://localhost:8080")
        await c.close()  # should be a no-op / succeed silently


# ── health() ────────────────────────────────────────────────────────────


class TestHealth:
    """``GET /global/health``."""

    @pytest.mark.asyncio
    async def test_returns_session_info(self, client):
        """health() should return a SessionInfo instance."""
        client._client.request.return_value = _mock_response(
            _sample_session_info("server-1", "ok"),
        )

        result = await client.health()

        assert isinstance(result, SessionInfo)
        assert result.id == "server-1"
        assert result.status == "ok"
        client._client.request.assert_awaited_once_with(
            "GET", "http://opencode-serve:8080/global/health"
        )


# ── list_sessions() ─────────────────────────────────────────────────────


class TestListSessions:
    """``GET /session``."""

    @pytest.mark.asyncio
    async def test_returns_session_list_response(self, client):
        """list_sessions() should return a SessionListResponse."""
        client._client.request.return_value = _mock_response(
            {"sessions": [_sample_session_info("sess-1")], "total": 1},
        )

        result = await client.list_sessions()

        assert isinstance(result, SessionListResponse)
        assert len(result.sessions) == 1
        assert result.total == 1
        assert result.sessions[0].id == "sess-1"
        client._client.request.assert_awaited_once_with(
            "GET", "http://opencode-serve:8080/session"
        )

    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        """list_sessions() should handle an empty list."""
        client._client.request.return_value = _mock_response(
            {"sessions": [], "total": 0},
        )

        result = await client.list_sessions()

        assert result.sessions == []
        assert result.total == 0


# ── get_session() ───────────────────────────────────────────────────────


class TestGetSession:
    """``GET /session/{session_id}``."""

    @pytest.mark.asyncio
    async def test_returns_session_info(self, client):
        """get_session() should return a SessionInfo."""
        client._client.request.return_value = _mock_response(
            _sample_session_info("sess-42"),
        )

        result = await client.get_session("sess-42")

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-42"
        assert result.workspace_path == "/workspaces/repo"
        client._client.request.assert_awaited_once_with(
            "GET", "http://opencode-serve:8080/session/sess-42"
        )


# ── create_session() ────────────────────────────────────────────────────


class TestCreateSession:
    """``POST /session``."""

    @pytest.mark.asyncio
    async def test_creates_with_required_fields(self, client):
        """create_session() should POST workspace_path and task_description."""
        client._client.request.return_value = _mock_response(
            _sample_session_info("sess-new"),
        )

        result = await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Fix bug",
        )

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-new"
        client._client.request.assert_awaited_once_with(
            "POST",
            "http://opencode-serve:8080/session",
            json={
                "workspace_path": "/workspaces/repo",
                "task_description": "Fix bug",
            },
        )

    @pytest.mark.asyncio
    async def test_creates_with_model(self, client):
        """create_session() should include model in the payload when given."""
        client._client.request.return_value = _mock_response(
            _sample_session_info("sess-model"),
        )

        result = await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Refactor",
            model="claude-4",
        )

        assert result.id == "sess-model"
        client._client.request.assert_awaited_once_with(
            "POST",
            "http://opencode-serve:8080/session",
            json={
                "workspace_path": "/workspaces/repo",
                "task_description": "Refactor",
                "model": "claude-4",
            },
        )

    @pytest.mark.asyncio
    async def test_model_omitted_when_none(self, client):
        """create_session() should omit model from payload when not provided."""
        client._client.request.return_value = _mock_response(
            _sample_session_info("sess-none"),
        )

        await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Fix bug",
        )

        _call_kwargs = client._client.request.await_args[1]  # keyword args
        payload = _call_kwargs["json"]
        assert "model" not in payload


# ── delete_session() ────────────────────────────────────────────────────


class TestDeleteSession:
    """``DELETE /session/{session_id}``."""

    @pytest.mark.asyncio
    async def test_returns_abort_response(self, client):
        """delete_session() should return SessionAbortResponse."""
        client._client.request.return_value = _mock_response(
            {"session_id": "sess-1", "aborted": True, "message": "Deleted"},
        )

        result = await client.delete_session("sess-1")

        assert isinstance(result, SessionAbortResponse)
        assert result.session_id == "sess-1"
        assert result.aborted is True
        client._client.request.assert_awaited_once_with(
            "DELETE", "http://opencode-serve:8080/session/sess-1"
        )


# ── get_session_diff() ──────────────────────────────────────────────────


class TestGetSessionDiff:
    """``GET /session/{session_id}/diff``."""

    @pytest.mark.asyncio
    async def test_returns_diff_response(self, client):
        """get_session_diff() should return SessionDiffResponse."""
        client._client.request.return_value = _mock_response(
            {
                "session_id": "sess-1",
                "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
                "files_changed": ["file.py"],
            },
        )

        result = await client.get_session_diff("sess-1")

        assert isinstance(result, SessionDiffResponse)
        assert result.session_id == "sess-1"
        assert "file.py" in result.files_changed
        assert "old" in result.diff
        client._client.request.assert_awaited_once_with(
            "GET", "http://opencode-serve:8080/session/sess-1/diff"
        )

    @pytest.mark.asyncio
    async def test_empty_diff(self, client):
        """get_session_diff() should handle empty diff gracefully."""
        client._client.request.return_value = _mock_response(
            {"session_id": "sess-1", "diff": "", "files_changed": []},
        )

        result = await client.get_session_diff("sess-1")

        assert result.diff == ""
        assert result.files_changed == []


# ── abort_session() ─────────────────────────────────────────────────────


class TestAbortSession:
    """``POST /session/{session_id}/abort``."""

    @pytest.mark.asyncio
    async def test_returns_abort_response(self, client):
        """abort_session() should return SessionAbortResponse."""
        client._client.request.return_value = _mock_response(
            {"session_id": "sess-1", "aborted": True, "message": "Abort initiated"},
        )

        result = await client.abort_session("sess-1")

        assert isinstance(result, SessionAbortResponse)
        assert result.session_id == "sess-1"
        assert result.aborted is True
        client._client.request.assert_awaited_once_with(
            "POST", "http://opencode-serve:8080/session/sess-1/abort"
        )


# ── Error handling ──────────────────────────────────────────────────────


class TestErrorHandling:
    """Client should translate httpx exceptions into custom ones."""

    @pytest.mark.asyncio
    async def test_raises_opencode_timeout_on_httpx_timeout(self, client):
        """httpx.TimeoutException should become OpenCodeTimeoutError."""
        client._client.request.side_effect = httpx.TimeoutException("Timed out")

        with pytest.raises(OpenCodeTimeoutError):
            await client.health()

    @pytest.mark.asyncio
    async def test_raises_opencode_connection_error_on_connect_error(self, client):
        """httpx.ConnectError should become OpenCodeConnectionError."""
        client._client.request.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(OpenCodeConnectionError):
            await client.health()

    @pytest.mark.asyncio
    async def test_raises_opencode_http_error_on_404(self, client):
        """A 404 response should raise OpenCodeHTTPError with status_code."""
        client._client.request.return_value = _mock_response(
            {"detail": "Not found"}, status_code=404,
        )

        with pytest.raises(OpenCodeHTTPError) as exc_info:
            await client.health()

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_opencode_http_error_on_500(self, client):
        """A 500 response should raise OpenCodeHTTPError with status_code."""
        client._client.request.return_value = _mock_response(
            {"detail": "Server error"}, status_code=500,
        )

        with pytest.raises(OpenCodeHTTPError) as exc_info:
            await client.health()

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_opencode_client_error_on_generic_http_error(self, client):
        """Generic httpx.HTTPError should become OpenCodeClientError."""
        client._client.request.side_effect = httpx.HTTPError("Generic error")

        with pytest.raises(OpenCodeClientError):
            await client.health()

    @pytest.mark.asyncio
    async def test_error_on_all_methods(self, client):
        """Verify that error wrapping works across every protocol method."""
        client._client.request.side_effect = httpx.TimeoutException("Timed out")

        methods: list[tuple[str, list, dict]] = [
            ("health", [], {}),
            ("list_sessions", [], {}),
            ("get_session", ["sess-1"], {}),
            ("create_session", [], {"workspace_path": "/x", "task_description": "y"}),
            ("delete_session", ["sess-1"], {}),
            ("get_session_diff", ["sess-1"], {}),
            ("abort_session", ["sess-1"], {}),
        ]

        for method_name, args, kwargs in methods:
            meth = getattr(client, method_name)
            # Reset side_effect for each call in case it got consumed
            client._client.request.side_effect = httpx.TimeoutException("Timed out")
            with pytest.raises(OpenCodeTimeoutError):
                await meth(*args, **kwargs)


# ── Logging ─────────────────────────────────────────────────────────────


class TestLogging:
    """Client should emit DEBUG-level log messages."""

    @pytest.mark.asyncio
    async def test_logs_request_and_response(self, client, caplog):
        """A successful request should produce request and response log lines."""
        import logging

        from app.opencode import serve_client as sc

        sc.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=sc.logger.name)

        client._client.request.return_value = _mock_response(
            _sample_session_info("sess-1", "ok"),
        )

        await client.health()

        assert any("Sending GET request" in msg for msg in caplog.messages)
        assert any("Received response 200" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_logs_timeout(self, client, caplog):
        """A timeout should produce a debug log message."""
        import logging

        from app.opencode import serve_client as sc

        sc.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=sc.logger.name)

        client._client.request.side_effect = httpx.TimeoutException("Timed out")

        with pytest.raises(OpenCodeTimeoutError):
            await client.health()

        assert any("timed out" in msg.lower() for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_logs_connection_error(self, client, caplog):
        """A connection error should produce a debug log message."""
        import logging

        from app.opencode import serve_client as sc

        sc.logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=sc.logger.name)

        client._client.request.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(OpenCodeConnectionError):
            await client.health()

        assert any("failed" in msg.lower() for msg in caplog.messages)
