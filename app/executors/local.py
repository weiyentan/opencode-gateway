"""Local executor — runs executor lifecycle methods in-process.

Used as the default executor when no external orchestrator (AWX, etc.)
is configured.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid as _uuid
from typing import Optional
from uuid import UUID

from app.core.secrets import redact_dict
from app.executors import ExecutorPlugin
from app.executors.models import (
    CancelJobRequest,
    CancelJobResponse,
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

logger = logging.getLogger(__name__)


class LocalExecutor(ExecutorPlugin):
    """Executor that performs lifecycle actions locally in-process."""

    name = "local"

    def __init__(self) -> None:
        self._workspaces: dict[UUID, str] = {}

    async def create_workspace(
        self, request: CreateWorkspaceRequest
    ) -> CreateWorkspaceResponse:
        workspace_id = _uuid.uuid4()
        path = tempfile.mkdtemp(prefix="opencode-ws-")
        self._workspaces[workspace_id] = path
        logger.info(
            "create_workspace: repo=%s -> workspace=%s path=%s",
            request.repo_url,
            workspace_id,
            path,
        )
        return CreateWorkspaceResponse(
            workspace_id=workspace_id,
            workspace_path=path,
            status="ready",
        )

    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        session_id = _uuid.uuid4()
        if request.env_vars:
            logger.info(
                "start_opencode: workspace=%s -> session=%s env_vars=%s",
                request.workspace_id,
                session_id,
                redact_dict(request.env_vars),
            )
        else:
            logger.info(
                "start_opencode: workspace=%s -> session=%s",
                request.workspace_id,
                session_id,
            )
        # Store env_vars for test verification
        self._last_env_vars: dict[str, str] = dict(request.env_vars)
        self._last_port: Optional[int] = request.port
        resolved_port = request.port if request.port is not None else 8080
        return StartOpencodeResponse(
            session_id=session_id,
            status="running",
            port=resolved_port,
        )

    async def stop_opencode(
        self, request: StopOpencodeRequest
    ) -> StopOpencodeResponse:
        logger.info("stop_opencode: workspace=%s", request.workspace_id)
        return StopOpencodeResponse(status="stopped")

    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        logger.info("restart_opencode: workspace=%s", request.workspace_id)
        return RestartOpencodeResponse(status="running")

    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        logger.info("collect_state: workspace=%s", request.workspace_id)
        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=WorkspaceState.READY,
            process_status="running",
            port=8080,
        )

    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        path = self._workspaces.pop(request.workspace_id, None)
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)
        logger.info(
            "cleanup_workspace: workspace=%s path=%s", request.workspace_id, path
        )
        return CleanupWorkspaceResponse(status="cleaned")

    async def cancel_job(
        self, request: CancelJobRequest
    ) -> CancelJobResponse:
        """Cancel a running job."""
        logger.info("cancel_job: workspace=%s", request.workspace_id)
        return CancelJobResponse(status="cancelled")
