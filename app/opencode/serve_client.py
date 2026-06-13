"""HTTP client for the OpenCode Serve REST API.

Implements OpenCodeClientProtocol using httpx.AsyncClient with proper
error handling, logging, and typed Pydantic response models.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.opencode.protocol import (
    OpenCodeClientProtocol,
    SessionAbortResponse,
    SessionDiffResponse,
    SessionInfo,
    SessionListResponse,
    SessionLogResponse,
)

logger = logging.getLogger(__name__)


# ── Custom exceptions ──────────────────────────────────────────────────


class OpenCodeClientError(Exception):
    """Base exception for OpenCode Serve client errors."""


class OpenCodeConnectionError(OpenCodeClientError):
    """Raised when the client cannot connect to the OpenCode Serve instance."""


class OpenCodeTimeoutError(OpenCodeClientError):
    """Raised when a request to the OpenCode Serve instance times out."""


class OpenCodeHTTPError(OpenCodeClientError):
    """Raised when the OpenCode Serve instance returns a non-2xx response.

    Attributes:
        status_code: The HTTP status code returned by the server.
    """

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


# ── Client implementation ──────────────────────────────────────────────


class OpenCodeServeClient(OpenCodeClientProtocol):
    """HTTP client for the OpenCode Serve REST API.

    Implements :class:`OpenCodeClientProtocol` using ``httpx.AsyncClient``.
    Communicates with an OpenCode Serve instance to manage coding sessions.

    Args:
        base_url: Base URL of the OpenCode Serve instance
            (e.g. ``http://localhost:8080``).
        timeout: Timeout in seconds for HTTP requests (default 30).

    Usage::

        client = OpenCodeServeClient("http://opencode-serve:8080")
        health = await client.health()
        await client.close()
    """

    def __init__(self, base_url: str, timeout: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and free resources."""
        await self._client.aclose()

    async def __aenter__(self) -> OpenCodeServeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Internal helpers ───────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Perform an HTTP request and handle errors transparently.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.).
            path: URL path relative to the base URL (e.g. ``/global/health``).
            **kwargs: Additional arguments passed to ``httpx.AsyncClient.request``.

        Returns:
            The HTTP response on success (2xx).

        Raises:
            OpenCodeTimeoutError: If the request times out.
            OpenCodeConnectionError: If the connection fails.
            OpenCodeHTTPError: If the server returns a non-2xx status.
            OpenCodeClientError: For any other httpx error.
        """
        url = f"{self._base_url}{path}"
        logger.debug("Sending %s request to %s", method, url)

        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            logger.debug("Request to %s timed out: %s", url, exc)
            raise OpenCodeTimeoutError(str(exc)) from exc
        except httpx.ConnectError as exc:
            logger.debug("Connection to %s failed: %s", url, exc)
            raise OpenCodeConnectionError(str(exc)) from exc
        except httpx.HTTPError as exc:
            logger.debug("HTTP error during request to %s: %s", url, exc)
            raise OpenCodeClientError(str(exc)) from exc

        logger.debug("Received response %s from %s", response.status_code, url)

        if response.status_code >= 400:
            raise OpenCodeHTTPError(
                f"OpenCode Serve returned status {response.status_code} "
                f"for {method} {url}",
                status_code=response.status_code,
            )

        return response

    # ── Protocol methods ────────────────────────────────────────────────

    async def health(self) -> SessionInfo:
        """Check the health of the OpenCode Serve instance.

        Returns:
            SessionInfo with server health and status details.
        """
        response = await self._request("GET", "/global/health")
        return SessionInfo(**response.json())

    async def list_sessions(self) -> SessionListResponse:
        """List all sessions managed by the OpenCode Serve instance.

        Returns:
            SessionListResponse containing all sessions and a total count.
        """
        response = await self._request("GET", "/session")
        return SessionListResponse(**response.json())

    async def get_session(self, session_id: str) -> SessionInfo:
        """Get detailed information for a specific session.

        Args:
            session_id: The unique identifier of the session to retrieve.

        Returns:
            SessionInfo for the requested session.
        """
        response = await self._request("GET", f"/session/{session_id}")
        return SessionInfo(**response.json())

    async def create_session(
        self,
        workspace_path: str,
        task_description: str,
        model: str | None = None,
    ) -> SessionInfo:
        """Create a new coding session on the OpenCode Serve instance.

        Args:
            workspace_path: Path to the workspace directory on the Runner VM.
            task_description: Natural-language description of the coding task.
            model: Optional model identifier to use for the session.

        Returns:
            SessionInfo for the newly created session.
        """
        payload: dict[str, Any] = {
            "workspace_path": workspace_path,
            "task_description": task_description,
        }
        if model is not None:
            payload["model"] = model

        response = await self._request("POST", "/session", json=payload)
        return SessionInfo(**response.json())

    async def delete_session(self, session_id: str) -> SessionAbortResponse:
        """Delete a session from the OpenCode Serve instance.

        Args:
            session_id: The unique identifier of the session to delete.

        Returns:
            SessionAbortResponse confirming deletion.
        """
        response = await self._request("DELETE", f"/session/{session_id}")
        return SessionAbortResponse(**response.json())

    async def get_session_log(self, session_id: str) -> SessionLogResponse:
        """Retrieve the full log output of a session.

        Args:
            session_id: The unique identifier of the session.

        Returns:
            SessionLogResponse containing the session's full log output.
        """
        response = await self._request("GET", f"/session/{session_id}/log")
        return SessionLogResponse(**response.json())

    async def get_session_diff(self, session_id: str) -> SessionDiffResponse:
        """Retrieve the diff produced by a session.

        Args:
            session_id: The unique identifier of the session.

        Returns:
            SessionDiffResponse containing the diff and list of changed files.
        """
        response = await self._request("GET", f"/session/{session_id}/diff")
        return SessionDiffResponse(**response.json())

    async def abort_session(self, session_id: str) -> SessionAbortResponse:
        """Abort a running session on the OpenCode Serve instance.

        Args:
            session_id: The unique identifier of the session to abort.

        Returns:
            SessionAbortResponse confirming the abort was processed.
        """
        response = await self._request("POST", f"/session/{session_id}/abort")
        return SessionAbortResponse(**response.json())
