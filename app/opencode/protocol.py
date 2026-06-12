"""Protocol definitions for the OpenCode Serve client.

Defines the OpenCodeClientProtocol abstract class and Pydantic response
models used for communicating with an OpenCode Serve REST API instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field


class SessionInfo(BaseModel):
    """Information about an OpenCode coding session.

    Represents a session managed by the OpenCode Serve instance,
    including its current status, workspace, and timestamps.
    """

    id: str
    status: str
    workspace_path: str
    task_description: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class SessionListResponse(BaseModel):
    """Response containing a list of OpenCode sessions."""

    sessions: list[SessionInfo]
    total: int


class SessionDiffResponse(BaseModel):
    """Response containing diff data produced by a session."""

    session_id: str
    diff: str
    files_changed: list[str] = Field(default_factory=list)


class SessionAbortResponse(BaseModel):
    """Confirmation that a session abort was processed."""

    session_id: str
    aborted: bool
    message: str | None = None


class OpenCodeClientProtocol(ABC):
    """Abstract protocol for communicating with an OpenCode Serve instance.

    Defines the interface that any OpenCode Serve client implementation
    must satisfy.  Concrete implementations handle the actual HTTP
    transport (httpx, requests, etc.) and error handling.

    .. rubric:: Current active surface

    The Gateway currently calls two protocol methods at runtime:

    * :meth:`get_session_diff` — retrieve the diff produced by a session
    * :meth:`abort_session`     — abort a running session

    .. rubric:: Intentional future surface

    The following methods exist to allow a future Gateway or monitoring
    layer to manage OpenCode Serve sessions directly.  They are
    implemented by :class:`OpenCodeServeClient` but are not yet invoked
    at runtime by the Gateway:

    * :meth:`health`          — check server health
    * :meth:`list_sessions`   — list all sessions
    * :meth:`get_session`     — get a specific session by ID
    * :meth:`create_session`  — create a new coding session
    * :meth:`delete_session`  — delete a session
    """

    # -- Intentional future surface ---------------------------------------

    @abstractmethod
    async def health(self) -> SessionInfo:
        """Check the health of the OpenCode Serve instance.

        **Future surface.**  Calls ``GET /global/health``.  Not yet
        invoked by the Gateway at runtime; available for monitoring
        or health-check callers.

        Returns:
            SessionInfo with server health and status details.
        """
        ...

    @abstractmethod
    async def list_sessions(self) -> SessionListResponse:
        """List all sessions managed by the OpenCode Serve instance.

        **Future surface.**  Calls ``GET /session``.  Not yet invoked
        by the Gateway at runtime.

        Returns:
            SessionListResponse containing all sessions and a total count.
        """
        ...

    @abstractmethod
    async def get_session(self, session_id: str) -> SessionInfo:
        """Get detailed information for a specific session.

        **Future surface.**  Calls ``GET /session/{session_id}``.  Not
        yet invoked by the Gateway at runtime.

        Args:
            session_id: The unique identifier of the session to retrieve.

        Returns:
            SessionInfo for the requested session.
        """
        ...

    @abstractmethod
    async def create_session(
        self,
        workspace_path: str,
        task_description: str,
        model: str | None = None,
    ) -> SessionInfo:
        """Create a new coding session on the OpenCode Serve instance.

        **Future surface.**  Calls ``POST /session``.  Not yet invoked
        by the Gateway at runtime; the Gateway currently manages
        sessions through the executor plugin instead.

        Args:
            workspace_path: Path to the workspace directory on the Runner VM.
            task_description: Natural-language description of the coding task.
            model: Optional model identifier to use for the session.

        Returns:
            SessionInfo for the newly created session.
        """
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> SessionAbortResponse:
        """Delete a session from the OpenCode Serve instance.

        **Future surface.**  Calls ``DELETE /session/{session_id}``.
        Not yet invoked by the Gateway at runtime.

        Args:
            session_id: The unique identifier of the session to delete.

        Returns:
            SessionAbortResponse confirming deletion.
        """
        ...

    # -- Active surface ---------------------------------------------------

    @abstractmethod
    async def get_session_diff(self, session_id: str) -> SessionDiffResponse:
        """Retrieve the diff produced by a session.

        Calls ``GET /session/{session_id}/diff``.

        Args:
            session_id: The unique identifier of the session.

        Returns:
            SessionDiffResponse containing the diff and list of changed files.
        """
        ...

    @abstractmethod
    async def abort_session(self, session_id: str) -> SessionAbortResponse:
        """Abort a running session on the OpenCode Serve instance.

        Calls ``POST /session/{session_id}/abort``.

        Args:
            session_id: The unique identifier of the session to abort.

        Returns:
            SessionAbortResponse confirming the abort was processed.
        """
        ...
