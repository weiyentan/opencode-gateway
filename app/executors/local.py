"""Local executor — runs executor lifecycle methods in-process.

Used as the default executor when no external orchestrator (AWX, etc.)
is configured.
"""

from __future__ import annotations

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
    """Executor that performs lifecycle actions locally in-process."""

    name = "local"

    async def create_workspace(
        self, request: CreateWorkspaceRequest
    ) -> CreateWorkspaceResponse:
        return CreateWorkspaceResponse(
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_path="/tmp/opencode/ws",
            status="ready",
        )

    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        return StartOpencodeResponse(
            session_id="00000000-0000-0000-0000-000000000002",
            status="running",
            port=8080,
        )

    async def stop_opencode(
        self, request: StopOpencodeRequest
    ) -> StopOpencodeResponse:
        return StopOpencodeResponse(status="stopped")

    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        return RestartOpencodeResponse(status="running")

    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=WorkspaceState.READY,
            process_status="running",
            port=8080,
        )

    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        return CleanupWorkspaceResponse(status="cleaned")
