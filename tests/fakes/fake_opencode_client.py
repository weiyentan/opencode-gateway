"""Fake OpenCode Serve client for integration tests.

Provides a deterministic, configurable implementation of the
OpenCodeClientProtocol.  Returns pre-configured responses instead of
making real HTTP calls.  Supports success, error, and timeout modes
with caller-tracking for test assertions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.opencode.protocol import (
    OpenCodeClientProtocol,
    SessionAbortResponse,
    SessionDiffResponse,
    SessionInfo,
    SessionListResponse,
    SessionLogResponse,
)
from app.opencode.serve_client import (
    OpenCodeHTTPError,
    OpenCodeTimeoutError,
)

# ── Constants ──────────────────────────────────────────────────────────────

MODE_SUCCESS = "success"
MODE_ERROR = "error"
MODE_TIMEOUT = "timeout"

# ── Default response data ──────────────────────────────────────────────────


def _default_session_info(
    session_id: str = "fake-session-1",
    status: str = "running",
) -> SessionInfo:
    """Return a default :class:`SessionInfo` for fake responses."""
    return SessionInfo(
        id=session_id,
        status=status,
        workspace_path="/fake/workspace/path",
        task_description="Fake task description",
        created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=None,
    )


def _default_diff_response(session_id: str = "fake-session-1") -> SessionDiffResponse:
    """Return a default :class:`SessionDiffResponse`."""
    return SessionDiffResponse(
        session_id=session_id,
        diff="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
        files_changed=["file.py"],
    )


def _default_log_response(session_id: str = "fake-session-1") -> SessionLogResponse:
    """Return a default :class:`SessionLogResponse`."""
    return SessionLogResponse(
        session_id=session_id,
        log="[INFO] Session started\n[INFO] Task completed successfully\n",
    )


def _default_abort_response(session_id: str = "fake-session-1") -> SessionAbortResponse:
    """Return a default :class:`SessionAbortResponse`."""
    return SessionAbortResponse(
        session_id=session_id,
        aborted=True,
        message="Session aborted successfully",
    )


class FakeOpenCodeServeClient(OpenCodeClientProtocol):
    """Fake OpenCode Serve client with configurable deterministic responses.

    Implements :class:`OpenCodeClientProtocol` and returns pre-configured
    data instead of making real HTTP calls.  All protocol methods are
    implemented (active surface + intentional future surface).

    Args:
        mode: Response mode — one of ``"success"``, ``"error"``, or
            ``"timeout"``.  Defaults to ``"success"``.
        session_info: :class:`SessionInfo` returned by :meth:`health`,
            :meth:`get_session`, and :meth:`create_session`.  Uses a
            default if not provided.
        diff_response: :class:`SessionDiffResponse` returned by
            :meth:`get_session_diff`.  Uses a default if not provided.
        log_response: :class:`SessionLogResponse` returned by
            :meth:`get_session_log`.  Uses a default if not provided.
        abort_response: :class:`SessionAbortResponse` returned by
            :meth:`abort_session` and :meth:`delete_session`.  Uses a
            default if not provided.
        session_list: :class:`SessionListResponse` returned by
            :meth:`list_sessions`.  Uses a default single-session list
            if not provided.
        error_status_code: HTTP status code for error-mode responses
            (default 500).
        error_message: Custom message for error-mode exceptions.

    Caller-tracking attributes (populated during calls):
        * ``diff_calls``: List of session IDs passed to
          :meth:`get_session_diff`.
        * ``log_calls``: List of session IDs passed to
          :meth:`get_session_log`.
        * ``abort_calls``: List of session IDs passed to
          :meth:`abort_session`.
        * ``create_calls``: List of ``(workspace_path, task_description,
          model)`` tuples.
        * ``delete_calls``: List of session IDs passed to
          :meth:`delete_session`.
        * ``health_calls``: Count of :meth:`health` invocations.
        * ``get_session_calls``: List of session IDs passed to
          :meth:`get_session`.
        * ``list_sessions_calls``: Count of :meth:`list_sessions`
          invocations.
    """

    def __init__(
        self,
        mode: str = MODE_SUCCESS,
        *,
        session_info: SessionInfo | None = None,
        diff_response: SessionDiffResponse | None = None,
        log_response: SessionLogResponse | None = None,
        abort_response: SessionAbortResponse | None = None,
        session_list: SessionListResponse | None = None,
        error_status_code: int = 500,
        error_message: str = "Simulated OpenCode Serve error",
    ) -> None:
        _validate_mode(mode)
        self._mode = mode
        self._session_info = session_info or _default_session_info()
        self._diff_response = diff_response or _default_diff_response()
        self._log_response = log_response or _default_log_response()
        self._abort_response = abort_response or _default_abort_response()
        self._session_list = session_list or SessionListResponse(
            sessions=[self._session_info],
            total=1,
        )
        self._error_status_code = error_status_code
        self._error_message = error_message

        # Caller-tracking (active surface)
        self.diff_calls: list[str] = []
        self.log_calls: list[str] = []
        self.abort_calls: list[str] = []
        # Caller-tracking (future surface)
        self.create_calls: list[tuple[str, str, str | None]] = []
        self.delete_calls: list[str] = []
        self.health_calls: int = 0
        self.get_session_calls: list[str] = []
        self.list_sessions_calls: int = 0

        # For close tracking
        self.close_called: bool = False

    @property
    def mode(self) -> str:
        """The current response mode."""
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        """Change the response mode at runtime.

        Useful for tests that need to switch between modes mid-scenario
        (e.g. ``fake_opencode.mode = "timeout"``).
        """
        _validate_mode(value)
        self._mode = value

    async def close(self) -> None:
        """Close the fake client (no-op, records the call)."""
        self.close_called = True

    async def __aenter__(self) -> FakeOpenCodeServeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Mode dispatch helpers ──────────────────────────────────────────

    def _raise_for_mode(self) -> None:
        """Raise the appropriate exception based on the current mode."""
        if self._mode == MODE_TIMEOUT:
            raise OpenCodeTimeoutError(self._error_message)
        if self._mode == MODE_ERROR:
            raise OpenCodeHTTPError(
                self._error_message,
                status_code=self._error_status_code,
            )

    # ── Future surface ─────────────────────────────────────────────────

    async def health(self) -> SessionInfo:
        """Check the health of the OpenCode Serve instance (fake)."""
        self.health_calls += 1
        self._raise_for_mode()
        return self._session_info

    async def list_sessions(self) -> SessionListResponse:
        """List all sessions (fake)."""
        self.list_sessions_calls += 1
        self._raise_for_mode()
        return self._session_list

    async def get_session(self, session_id: str) -> SessionInfo:
        """Get detailed information for a specific session (fake)."""
        self.get_session_calls.append(session_id)
        self._raise_for_mode()
        return self._session_info

    async def create_session(
        self,
        workspace_path: str,
        task_description: str,
        model: str | None = None,
    ) -> SessionInfo:
        """Create a new coding session (fake)."""
        self.create_calls.append((workspace_path, task_description, model))
        self._raise_for_mode()
        return self._session_info

    async def delete_session(self, session_id: str) -> SessionAbortResponse:
        """Delete a session (fake)."""
        self.delete_calls.append(session_id)
        self._raise_for_mode()
        return self._abort_response

    # ── Active surface ─────────────────────────────────────────────────

    async def get_session_diff(self, session_id: str) -> SessionDiffResponse:
        """Retrieve the diff produced by a session (fake).

        Records the session ID in ``diff_calls`` and returns the
        configured ``diff_response``.
        """
        self.diff_calls.append(session_id)
        self._raise_for_mode()
        return self._diff_response

    async def get_session_log(self, session_id: str) -> SessionLogResponse:
        """Retrieve the full log output of a session (fake).

        Records the session ID in ``log_calls`` and returns the
        configured ``log_response``.
        """
        self.log_calls.append(session_id)
        self._raise_for_mode()
        return self._log_response

    async def abort_session(self, session_id: str) -> SessionAbortResponse:
        """Abort a running session (fake).

        Records the session ID in ``abort_calls`` and returns the
        configured ``abort_response``.
        """
        self.abort_calls.append(session_id)
        self._raise_for_mode()
        return self._abort_response


def _validate_mode(mode: str) -> None:
    """Validate the mode is one of the expected constants."""
    if mode not in (MODE_SUCCESS, MODE_ERROR, MODE_TIMEOUT):
        raise ValueError(
            f"Invalid mode: {mode!r}. "
            f"Expected one of: {MODE_SUCCESS!r}, {MODE_ERROR!r}, {MODE_TIMEOUT!r}"
        )
