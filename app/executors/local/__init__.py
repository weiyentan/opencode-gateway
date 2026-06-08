"""Local executor plugin — creates workspaces on local disk.

For dev/testing. Creates real directories under a configurable base path,
generates dynamic UUIDs, and provides in-process no-op lifecycle
management (no actual OpenCode Serve processes are started).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from uuid import UUID, uuid4

from app.executors import ExecutorPlugin
from app.executors.models import (
    CleanupWorkspaceRequest,
    CleanupWorkspaceResponse,
    CollectStateRequest,
    CollectStateResponse,
    CreateWorkspaceRequest,
    CreateWorkspaceResponse,
    RestartOpencodeRequest,
    RestartOpencodeResponse,
    StartOpencodeRequest,
    StartOpencodeResponse,
    StopOpencodeRequest,
    StopOpencodeResponse,
    WorkspaceState,
)


class LocalExecutor(ExecutorPlugin):
    """Executor that provisions workspaces as local temp directories.

    No actual OpenCode Serve process is started — lifecycle operations
    are no-ops that return plausible response values.  Suitable for
    development, testing, and CI where real Runner VMs are not needed.
    """

    name = "local"

    def __init__(self, workspace_base: str = "") -> None:
        """Initialise the local executor.

        Args:
            workspace_base: Parent directory for workspaces.  When empty
                (the default), ``tempfile.gettempdir()`` is used.
        """
        self.workspace_base = workspace_base or tempfile.gettempdir()
        self._workspaces: dict[UUID, str] = {}

    # ------------------------------------------------------------------
    # create_workspace
    # ------------------------------------------------------------------

    async def create_workspace(
        self, request: CreateWorkspaceRequest
    ) -> CreateWorkspaceResponse:
        """Create a real temp directory and register it."""
        ws_id = uuid4()
        path = tempfile.mkdtemp(prefix="opencode-ws-", dir=self.workspace_base)
        self._workspaces[ws_id] = path
        return CreateWorkspaceResponse(
            workspace_id=ws_id,
            workspace_path=path,
            status="ready",
        )

    # ------------------------------------------------------------------
    # start_opencode
    # ------------------------------------------------------------------

    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        """Return a plausible session-id — no real process is started."""
        return StartOpencodeResponse(
            session_id=uuid4(),
            status="running",
            port=8080,
        )

    # ------------------------------------------------------------------
    # stop_opencode
    # ------------------------------------------------------------------

    async def stop_opencode(
        self, request: StopOpencodeRequest
    ) -> StopOpencodeResponse:
        """No-op stop."""
        return StopOpencodeResponse(status="stopped")

    # ------------------------------------------------------------------
    # restart_opencode
    # ------------------------------------------------------------------

    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        """No-op restart — simply reports running."""
        return RestartOpencodeResponse(status="running")

    # ------------------------------------------------------------------
    # collect_state
    # ------------------------------------------------------------------

    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        """Report the current tracked state of the workspace."""
        path = self._workspaces.get(request.workspace_id)
        if path and os.path.isdir(path):
            status = WorkspaceState.READY
        else:
            status = WorkspaceState.ERROR
        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=status,
            process_status=None,
            port=None,
        )

    # ------------------------------------------------------------------
    # cleanup_workspace
    # ------------------------------------------------------------------

    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        """Remove the workspace directory if it exists."""
        path = self._workspaces.pop(request.workspace_id, None)
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        return CleanupWorkspaceResponse(status="cleaned")
