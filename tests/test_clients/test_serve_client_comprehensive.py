"""Comprehensive unit tests for OpenCodeServeClient.

Covers all 7 protocol methods with success, error, connection-failure,
timeout, and edge-case scenarios using ``httpx.MockTransport`` for
realistic HTTP simulation without real network calls.

This suite complements the basic tests in ``tests/test_serve_client.py``
by providing broader coverage of error codes, per-method error handling,
and edge cases.
"""

from __future__ import annotations

import json

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

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_client(
    handler,
    base_url: str = "http://opencode-serve:8080",
    timeout: int = 10,
) -> OpenCodeServeClient:
    """Build an ``OpenCodeServeClient`` wired to an ``httpx.MockTransport``.

    Args:
        handler: A callable ``(httpx.Request) -> httpx.Response`` that
            simulates the server.
        base_url: Base URL for the client.
        timeout: Timeout in seconds for HTTP requests.

    Returns:
        A configured ``OpenCodeServeClient`` that uses the mock transport.
    """
    client = OpenCodeServeClient(base_url=base_url, timeout=timeout)
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout),
    )
    return client


def _assert_url(request: httpx.Request, expected_path: str) -> None:
    """Assert that a request targets the expected URL path on the mock host."""
    assert str(request.url) == f"http://opencode-serve:8080{expected_path}"


# ── Sample payloads ──────────────────────────────────────────────────────

SESSION_INFO_PAYLOAD = {
    "id": "sess-1",
    "status": "running",
    "workspace_path": "/workspaces/repo",
    "task_description": "Fix the issue",
    "created_at": "2025-06-01T12:00:00Z",
    "updated_at": None,
}

SESSION_LIST_PAYLOAD = {
    "sessions": [SESSION_INFO_PAYLOAD],
    "total": 1,
}

SESSION_DIFF_PAYLOAD = {
    "session_id": "sess-1",
    "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
    "files_changed": ["file.py"],
}

SESSION_ABORT_PAYLOAD = {
    "session_id": "sess-1",
    "aborted": True,
    "message": "Abort initiated",
}

# ══════════════════════════════════════════════════════════════════════════
# 1.  HEALTH  (GET /global/health)
# ══════════════════════════════════════════════════════════════════════════


class TestHealth:
    """``GET /global/health`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionInfo`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/global/health")
            assert request.method == "GET"
            return httpx.Response(200, json=SESSION_INFO_PAYLOAD)

        client = _make_client(handler)
        result = await client.health()

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-1"
        assert result.status == "running"
        assert result.workspace_path == "/workspaces/repo"
        assert result.task_description == "Fix the issue"

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` on not-found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.health()
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_500(self) -> None:
        """Raises ``OpenCodeHTTPError(500)`` on server error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Server error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.health()
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when the connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.health()

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` when the request times out."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.health()

    @pytest.mark.asyncio
    async def test_generic_http_error(self) -> None:
        """Raises ``OpenCodeClientError`` on a generic httpx error."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.HTTPError("Unexpected transport error")

        client = _make_client(handler)
        with pytest.raises(OpenCodeClientError):
            await client.health()


# ══════════════════════════════════════════════════════════════════════════
# 2.  LIST SESSIONS  (GET /session)
# ══════════════════════════════════════════════════════════════════════════


class TestListSessions:
    """``GET /session`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionListResponse`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session")
            assert request.method == "GET"
            return httpx.Response(200, json=SESSION_LIST_PAYLOAD)

        client = _make_client(handler)
        result = await client.list_sessions()

        assert isinstance(result, SessionListResponse)
        assert len(result.sessions) == 1
        assert result.total == 1
        assert result.sessions[0].id == "sess-1"

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        """Returns a ``SessionListResponse`` with zero sessions."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sessions": [], "total": 0})

        client = _make_client(handler)
        result = await client.list_sessions()

        assert result.sessions == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` on not-found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.list_sessions()
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_500(self) -> None:
        """Raises ``OpenCodeHTTPError(500)`` on server error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Server error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.list_sessions()
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.list_sessions()

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.list_sessions()


# ══════════════════════════════════════════════════════════════════════════
# 3.  GET SESSION  (GET /session/{session_id})
# ══════════════════════════════════════════════════════════════════════════


class TestGetSession:
    """``GET /session/{session_id}`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionInfo`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session/sess-42")
            assert request.method == "GET"
            return httpx.Response(200, json=SESSION_INFO_PAYLOAD)

        client = _make_client(handler)
        result = await client.get_session("sess-42")

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-1"

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` when session is not found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Session not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.get_session("nonexistent")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.get_session("sess-1")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.get_session("sess-1")

    @pytest.mark.asyncio
    async def test_empty_session_id(self) -> None:
        """Handles an empty session ID string (edge case)."""

        def handler(request: httpx.Request) -> httpx.Response:
            # Empty session ID results in a trailing slash path
            assert str(request.url).endswith("/session/")
            return httpx.Response(404, json={"detail": "Not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.get_session("")
        assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# 4.  CREATE SESSION  (POST /session)
# ══════════════════════════════════════════════════════════════════════════


class TestCreateSession:
    """``POST /session`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success_minimal(self) -> None:
        """Creates a session with required fields."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session")
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body == {
                "workspace_path": "/workspaces/repo",
                "task_description": "Fix bug",
            }
            return httpx.Response(200, json=SESSION_INFO_PAYLOAD)

        client = _make_client(handler)
        result = await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Fix bug",
        )

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-1"

    @pytest.mark.asyncio
    async def test_success_with_model(self) -> None:
        """Creates a session with an optional model parameter."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session")
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body == {
                "workspace_path": "/workspaces/repo",
                "task_description": "Refactor",
                "model": "claude-4",
            }
            return httpx.Response(200, json=SESSION_INFO_PAYLOAD)

        client = _make_client(handler)
        result = await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Refactor",
            model="claude-4",
        )

        assert isinstance(result, SessionInfo)
        assert result.id == "sess-1"

    @pytest.mark.asyncio
    async def test_model_omitted_when_none(self) -> None:
        """Omits ``model`` from the JSON body when not provided."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "model" not in body
            return httpx.Response(200, json=SESSION_INFO_PAYLOAD)

        client = _make_client(handler)
        await client.create_session(
            workspace_path="/workspaces/repo",
            task_description="Fix bug",
        )

    @pytest.mark.asyncio
    async def test_500(self) -> None:
        """Raises ``OpenCodeHTTPError(500)`` on server error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Creation failed"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.create_session(
                workspace_path="/workspaces/repo",
                task_description="Fix bug",
            )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.create_session(
                workspace_path="/workspaces/repo",
                task_description="Fix bug",
            )

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.create_session(
                workspace_path="/workspaces/repo",
                task_description="Fix bug",
            )


# ══════════════════════════════════════════════════════════════════════════
# 5.  DELETE SESSION  (DELETE /session/{session_id})
# ══════════════════════════════════════════════════════════════════════════


class TestDeleteSession:
    """``DELETE /session/{session_id}`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionAbortResponse`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session/sess-1")
            assert request.method == "DELETE"
            return httpx.Response(200, json=SESSION_ABORT_PAYLOAD)

        client = _make_client(handler)
        result = await client.delete_session("sess-1")

        assert isinstance(result, SessionAbortResponse)
        assert result.session_id == "sess-1"
        assert result.aborted is True
        assert result.message == "Abort initiated"

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` when session is not found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Session not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.delete_session("nonexistent")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.delete_session("sess-1")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.delete_session("sess-1")


# ══════════════════════════════════════════════════════════════════════════
# 6.  GET SESSION DIFF  (GET /session/{session_id}/diff)
# ══════════════════════════════════════════════════════════════════════════


class TestGetSessionDiff:
    """``GET /session/{session_id}/diff`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionDiffResponse`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session/sess-1/diff")
            assert request.method == "GET"
            return httpx.Response(200, json=SESSION_DIFF_PAYLOAD)

        client = _make_client(handler)
        result = await client.get_session_diff("sess-1")

        assert isinstance(result, SessionDiffResponse)
        assert result.session_id == "sess-1"
        assert "old" in result.diff
        assert "file.py" in result.files_changed

    @pytest.mark.asyncio
    async def test_empty_diff(self) -> None:
        """Handles a diff response with no changes."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "session_id": "sess-1",
                    "diff": "",
                    "files_changed": [],
                },
            )

        client = _make_client(handler)
        result = await client.get_session_diff("sess-1")

        assert result.diff == ""
        assert result.files_changed == []

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` when session is not found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Session not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.get_session_diff("nonexistent")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.get_session_diff("sess-1")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.get_session_diff("sess-1")


# ══════════════════════════════════════════════════════════════════════════
# 7.  ABORT SESSION  (POST /session/{session_id}/abort)
# ══════════════════════════════════════════════════════════════════════════


class TestAbortSession:
    """``POST /session/{session_id}/abort`` — success and error scenarios."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Returns a ``SessionAbortResponse`` on 200."""

        def handler(request: httpx.Request) -> httpx.Response:
            _assert_url(request, "/session/sess-1/abort")
            assert request.method == "POST"
            return httpx.Response(200, json=SESSION_ABORT_PAYLOAD)

        client = _make_client(handler)
        result = await client.abort_session("sess-1")

        assert isinstance(result, SessionAbortResponse)
        assert result.session_id == "sess-1"
        assert result.aborted is True

    @pytest.mark.asyncio
    async def test_404(self) -> None:
        """Raises ``OpenCodeHTTPError(404)`` when session is not found."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Session not found"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.abort_session("nonexistent")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Raises ``OpenCodeConnectionError`` when connection is refused."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(OpenCodeConnectionError):
            await client.abort_session("sess-1")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Raises ``OpenCodeTimeoutError`` on timeout."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        with pytest.raises(OpenCodeTimeoutError):
            await client.abort_session("sess-1")


# ══════════════════════════════════════════════════════════════════════════
# 8.  NON-200 / NON-404 HTTP ERROR CODES
# ══════════════════════════════════════════════════════════════════════════


class TestHTTPErrorCodes:
    """Client raises ``OpenCodeHTTPError`` with correct status codes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [400, 401, 403, 405, 409, 422, 429, 502, 503],
    )
    async def test_various_error_codes_on_health(self, status_code: int) -> None:
        """Returns correct ``OpenCodeHTTPError`` status codes on health()."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.health()
        assert exc.value.status_code == status_code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [400, 403, 409, 422, 429, 502, 503],
    )
    async def test_various_error_codes_on_list_sessions(
        self, status_code: int
    ) -> None:
        """Returns correct ``OpenCodeHTTPError`` on list_sessions()."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.list_sessions()
        assert exc.value.status_code == status_code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [400, 403, 409, 422, 502, 503],
    )
    async def test_various_error_codes_on_create_session(
        self, status_code: int
    ) -> None:
        """Returns correct ``OpenCodeHTTPError`` on create_session()."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.create_session(
                workspace_path="/workspaces/repo",
                task_description="Fix bug",
            )
        assert exc.value.status_code == status_code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code",
        [400, 403, 409, 422, 502, 503],
    )
    async def test_various_error_codes_on_get_session_diff(
        self, status_code: int
    ) -> None:
        """Returns correct ``OpenCodeHTTPError`` on get_session_diff()."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "Error"})

        client = _make_client(handler)
        with pytest.raises(OpenCodeHTTPError) as exc:
            await client.get_session_diff("sess-1")
        assert exc.value.status_code == status_code


# ══════════════════════════════════════════════════════════════════════════
# 9.  CONNECTION ERRORS ACROSS ALL METHODS
# ══════════════════════════════════════════════════════════════════════════


class TestConnectionErrors:
    """``OpenCodeConnectionError`` is raised for every protocol method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,args,kwargs",
        [
            ("health", [], {}),
            ("list_sessions", [], {}),
            ("get_session", ["sess-1"], {}),
            ("create_session", [], {"workspace_path": "/x", "task_description": "y"}),
            ("delete_session", ["sess-1"], {}),
            ("get_session_diff", ["sess-1"], {}),
            ("abort_session", ["sess-1"], {}),
        ],
    )
    async def test_connection_error_on_all_methods(
        self, method_name: str, args: list, kwargs: dict
    ) -> None:
        """Connection refused raises ``OpenCodeConnectionError`` everywhere."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        method = getattr(client, method_name)
        with pytest.raises(OpenCodeConnectionError):
            await method(*args, **kwargs)


# ══════════════════════════════════════════════════════════════════════════
# 10.  TIMEOUT ERRORS ACROSS ALL METHODS
# ══════════════════════════════════════════════════════════════════════════


class TestTimeoutErrors:
    """``OpenCodeTimeoutError`` is raised for every protocol method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,args,kwargs",
        [
            ("health", [], {}),
            ("list_sessions", [], {}),
            ("get_session", ["sess-1"], {}),
            ("create_session", [], {"workspace_path": "/x", "task_description": "y"}),
            ("delete_session", ["sess-1"], {}),
            ("get_session_diff", ["sess-1"], {}),
            ("abort_session", ["sess-1"], {}),
        ],
    )
    async def test_timeout_on_all_methods(
        self, method_name: str, args: list, kwargs: dict
    ) -> None:
        """Timeout raises ``OpenCodeTimeoutError`` everywhere."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timed out")

        client = _make_client(handler)
        method = getattr(client, method_name)
        with pytest.raises(OpenCodeTimeoutError):
            await method(*args, **kwargs)


# ══════════════════════════════════════════════════════════════════════════
# 11.  GENERIC HTTP ERRORS ACROSS ALL METHODS
# ══════════════════════════════════════════════════════════════════════════


class TestGenericHTTPErrors:
    """``OpenCodeClientError`` is raised for generic httpx errors."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,args,kwargs",
        [
            ("health", [], {}),
            ("list_sessions", [], {}),
            ("get_session", ["sess-1"], {}),
            ("create_session", [], {"workspace_path": "/x", "task_description": "y"}),
            ("delete_session", ["sess-1"], {}),
            ("get_session_diff", ["sess-1"], {}),
            ("abort_session", ["sess-1"], {}),
        ],
    )
    async def test_generic_http_error_on_all_methods(
        self, method_name: str, args: list, kwargs: dict
    ) -> None:
        """Generic httpx errors raise ``OpenCodeClientError`` everywhere."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.HTTPError("Unexpected transport error")

        client = _make_client(handler)
        method = getattr(client, method_name)
        with pytest.raises(OpenCodeClientError):
            await method(*args, **kwargs)


# ══════════════════════════════════════════════════════════════════════════
# 12.  EDGE CASES
# ══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases for response parsing and input handling."""

    @pytest.mark.asyncio
    async def test_session_info_with_null_task_description(self) -> None:
        """Parses a ``SessionInfo`` where ``task_description`` is null."""

        payload = {
            "id": "sess-null",
            "status": "running",
            "workspace_path": "/workspaces/repo",
            "task_description": None,
            "created_at": "2025-06-01T12:00:00Z",
            "updated_at": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _make_client(handler)
        result = await client.health()

        assert result.id == "sess-null"
        assert result.task_description is None

    @pytest.mark.asyncio
    async def test_session_info_with_null_updated_at(self) -> None:
        """Parses a ``SessionInfo`` where ``updated_at`` is null."""

        payload = {
            "id": "sess-null-upd",
            "status": "running",
            "workspace_path": "/workspaces/repo",
            "task_description": "Task",
            "created_at": "2025-06-01T12:00:00Z",
            "updated_at": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _make_client(handler)
        result = await client.health()

        assert result.id == "sess-null-upd"
        assert result.updated_at is None

    @pytest.mark.asyncio
    async def test_session_info_missing_optional_fields(self) -> None:
        """Parses a ``SessionInfo`` with only required fields."""

        payload = {
            "id": "sess-min",
            "status": "running",
            "workspace_path": "/workspaces/repo",
            "created_at": "2025-06-01T12:00:00Z",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _make_client(handler)
        result = await client.health()

        assert result.id == "sess-min"
        assert result.task_description is None
        assert result.updated_at is None

    @pytest.mark.asyncio
    async def test_list_sessions_no_sessions(self) -> None:
        """Handles a session list response with no sessions."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sessions": [], "total": 0})

        client = _make_client(handler)
        result = await client.list_sessions()

        assert result.sessions == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_abort_response_without_message(self) -> None:
        """Parses a ``SessionAbortResponse`` with a null message."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"session_id": "sess-1", "aborted": True, "message": None},
            )

        client = _make_client(handler)
        result = await client.abort_session("sess-1")

        assert result.session_id == "sess-1"
        assert result.aborted is True
        assert result.message is None

    @pytest.mark.asyncio
    async def test_diff_response_without_files_changed(self) -> None:
        """Handles a diff response where ``files_changed`` is omitted."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"session_id": "sess-1", "diff": "some diff"},
            )

        client = _make_client(handler)
        result = await client.get_session_diff("sess-1")

        assert result.session_id == "sess-1"
        assert result.diff == "some diff"
        assert result.files_changed == []  # default factory

    @pytest.mark.asyncio
    async def test_long_workspace_path(self) -> None:
        """Handles a long workspace path in create_session."""

        long_path = "/very/long/" + "subdir/" * 50 + "workspace"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["workspace_path"] == long_path
            return httpx.Response(
                200,
                json={
                    "id": "sess-long",
                    "status": "running",
                    "workspace_path": long_path,
                    "created_at": "2025-06-01T12:00:00Z",
                },
            )

        client = _make_client(handler)
        result = await client.create_session(
            workspace_path=long_path,
            task_description="Fix bug",
        )

        assert result.id == "sess-long"
        assert result.workspace_path == long_path

    @pytest.mark.asyncio
    async def test_special_characters_in_task_description(self) -> None:
        """Handles special characters in task_description."""

        special_desc = "Fix: αβγ 🎉 $PATH 'quote' \"double\" & <html>"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["task_description"] == special_desc
            return httpx.Response(
                200,
                json={
                    "id": "sess-special",
                    "status": "running",
                    "workspace_path": "/workspaces/repo",
                    "task_description": special_desc,
                    "created_at": "2025-06-01T12:00:00Z",
                },
            )

        client = _make_client(handler)
        result = await client.create_session(
            workspace_path="/workspaces/repo",
            task_description=special_desc,
        )

        assert result.task_description == special_desc
