"""AWX executor plugin — maps the 6 lifecycle methods to AWX job templates.

Uses the AWXApiClient for API calls and translates between the typed
Pydantic request/response models and AWX extra_vars / artifacts.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.core.secrets import redact_dict
from app.executors import ExecutorPlugin
from app.executors.awx.client import AWXApiClient, AWXJobResult
from app.executors.awx.exceptions import AWXClientError
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

logger = logging.getLogger(__name__)

# Default base path on the Runner VM where workspace directories live.
_DEFAULT_WORKSPACE_BASE_PATH = "/home/runner/workspaces"


def _workspace_path(
    workspace_base_path: str,
    workspace_id: UUID,
    explicit_path: str | None = None,
) -> str:
    """Resolve a workspace path, preferring an explicit path over a derived one."""
    if explicit_path:
        return explicit_path
    return f"{workspace_base_path}/{workspace_id}"


def _artifacts_to_dict(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    """Return a safe dictionary from AWX artifacts (handle None)."""
    if artifacts is None:
        return {}
    return artifacts


class AWXExecutorPlugin(ExecutorPlugin):
    """Executor that delegates lifecycle actions to AWX job templates.

    Per ADR 0002, each of the six lifecycle methods maps to one of three
    AWX job templates:

    ===================================== ===============================
    Lifecycle method                      AWX job template
    ===================================== ===============================
    ``create_workspace``                  ``gateway-create-workspace``
    ``start_opencode``                    ``gateway-opencode-lifecycle``
    ``stop_opencode``                     ``gateway-opencode-lifecycle``
    ``restart_opencode``                  ``gateway-opencode-lifecycle``
    ``collect_state``                     ``gateway-workspace-teardown``
    ``cleanup_workspace``                 ``gateway-workspace-teardown``
    ===================================== ===============================

    When ``gateway-opencode-lifecycle`` or ``gateway-workspace-teardown``
    is launched, an ``action`` extra_var distinguishes the operation.

    Args:
        client: An :class:`AWXApiClient` instance for communicating with AWX.
        create_workspace_template_id: AWX template ID for workspace creation.
        opencode_lifecycle_template_id: AWX template ID for start/stop/restart.
        workspace_teardown_template_id: AWX template ID for collect/cleanup.
        workspace_base_path: Base directory for workspace paths on the
            Runner VM (default ``/home/runner/workspaces``).

    Raises:
        ValueError: If any template ID is ``None``.
    """

    name = "awx"

    def __init__(
        self,
        client: AWXApiClient,
        create_workspace_template_id: int,
        opencode_lifecycle_template_id: int,
        workspace_teardown_template_id: int,
        workspace_base_path: str = _DEFAULT_WORKSPACE_BASE_PATH,
    ) -> None:
        if create_workspace_template_id is None:
            raise ValueError("create_workspace_template_id must not be None")
        if opencode_lifecycle_template_id is None:
            raise ValueError("opencode_lifecycle_template_id must not be None")
        if workspace_teardown_template_id is None:
            raise ValueError("workspace_teardown_template_id must not be None")

        self._client = client
        self._create_workspace_template_id = create_workspace_template_id
        self._opencode_lifecycle_template_id = opencode_lifecycle_template_id
        self._workspace_teardown_template_id = workspace_teardown_template_id
        self._workspace_base_path = workspace_base_path.rstrip("/")

    # ── Internal helpers ────────────────────────────────────────────────

    async def _launch_and_wait(
        self,
        template_id: int,
        extra_vars: dict[str, Any],
    ) -> AWXJobResult:
        """Launch a job template and wait for it to complete.

        Wraps errors in AWX-specific exceptions with logging for
        observability.

        Raises:
            AWXConnectionError: If the AWX instance is unreachable.
            AWXTimeoutError: If the job does not complete in time.
            AWXJobError: If the job fails or completes with an error.
        """
        logger.debug(
            "Launching AWX template %d with extra_vars=%s",
            template_id,
            redact_dict(extra_vars),
        )
        summary = await self._client.launch_job_template(
            template_id,
            extra_vars=extra_vars,
        )
        logger.info(
            "Launched AWX job %d (template %d, status=%s)",
            summary.job_id,
            template_id,
            summary.status,
        )

        result = await self._client.wait_for_job(summary.job_id)
        logger.info(
            "AWX job %d completed with status=%s",
            result.job_id,
            result.status,
        )
        return result

    # ── Lifecycle methods ───────────────────────────────────────────────

    async def create_workspace(
        self, request: CreateWorkspaceRequest
    ) -> CreateWorkspaceResponse:
        """Provision a workspace directory via the gateway-create-workspace template.

        Extra vars: ``repo_url``, ``branch`` (optional), ``job_id`` (optional).
        """
        extra_vars: dict[str, Any] = {"repo_url": request.repo_url}
        if request.branch is not None:
            extra_vars["branch"] = request.branch
        if request.job_id is not None:
            extra_vars["job_id"] = str(request.job_id)

        try:
            result = await self._launch_and_wait(
                self._create_workspace_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception("create_workspace failed for repo=%s", request.repo_url)
            raise

        artifacts = _artifacts_to_dict(result.artifacts)
        workspace_id_str = artifacts.get("workspace_id", "")
        workspace_path = artifacts.get("workspace_path", "")

        try:
            workspace_id = UUID(workspace_id_str) if workspace_id_str else UUID(int=0)
        except (ValueError, AttributeError):
            workspace_id = UUID(int=0)

        return CreateWorkspaceResponse(
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            status=result.status,
        )

    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        """Start OpenCode Serve via the gateway-opencode-lifecycle template.

        Extra vars: ``action: start``, ``workspace_path``.
        """
        workspace_path = _workspace_path(
            self._workspace_base_path,
            request.workspace_id,
            request.workspace_path,
        )
        extra_vars: dict[str, Any] = {
            "action": "start",
            "workspace_path": workspace_path,
        }

        try:
            result = await self._launch_and_wait(
                self._opencode_lifecycle_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception(
                "start_opencode failed for workspace=%s",
                request.workspace_id,
            )
            raise

        artifacts = _artifacts_to_dict(result.artifacts)
        session_id_str = artifacts.get("session_id", "")

        try:
            session_id = UUID(session_id_str) if session_id_str else UUID(int=0)
        except (ValueError, AttributeError):
            session_id = UUID(int=0)

        port = artifacts.get("port", 0)

        return StartOpencodeResponse(
            session_id=session_id,
            status=result.status,
            port=port,
        )

    async def stop_opencode(
        self, request: StopOpencodeRequest
    ) -> StopOpencodeResponse:
        """Stop OpenCode Serve via the gateway-opencode-lifecycle template.

        Extra vars: ``action: stop``, ``workspace_path``.
        """
        workspace_path = _workspace_path(
            self._workspace_base_path,
            request.workspace_id,
        )
        extra_vars: dict[str, Any] = {
            "action": "stop",
            "workspace_path": workspace_path,
        }

        try:
            result = await self._launch_and_wait(
                self._opencode_lifecycle_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception(
                "stop_opencode failed for workspace=%s",
                request.workspace_id,
            )
            raise

        return StopOpencodeResponse(status=result.status)

    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        """Restart OpenCode Serve via the gateway-opencode-lifecycle template.

        Extra vars: ``action: restart``, ``workspace_path``.
        """
        workspace_path = _workspace_path(
            self._workspace_base_path,
            request.workspace_id,
        )
        extra_vars: dict[str, Any] = {
            "action": "restart",
            "workspace_path": workspace_path,
        }

        try:
            result = await self._launch_and_wait(
                self._opencode_lifecycle_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception(
                "restart_opencode failed for workspace=%s",
                request.workspace_id,
            )
            raise

        return RestartOpencodeResponse(status=result.status)

    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        """Collect workspace state via the gateway-workspace-teardown template.

        Extra vars: ``action: collect``, ``workspace_path``.
        """
        workspace_path = _workspace_path(
            self._workspace_base_path,
            request.workspace_id,
        )
        extra_vars: dict[str, Any] = {
            "action": "collect",
            "workspace_path": workspace_path,
        }

        try:
            result = await self._launch_and_wait(
                self._workspace_teardown_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception(
                "collect_state failed for workspace=%s",
                request.workspace_id,
            )
            raise

        artifacts = _artifacts_to_dict(result.artifacts)

        # Map the AWX status to a WorkspaceState if possible.
        raw_status = artifacts.get("status", result.status)
        try:
            ws_state = WorkspaceState(raw_status)
        except ValueError:
            # Default to ERROR for unrecognised states.
            ws_state = WorkspaceState.ERROR

        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=ws_state,
            process_status=artifacts.get("process_status"),
            port=artifacts.get("port"),
        )

    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        """Tear down a workspace via the gateway-workspace-teardown template.

        Extra vars: ``action: cleanup``, ``workspace_path``.
        """
        workspace_path = _workspace_path(
            self._workspace_base_path,
            request.workspace_id,
        )
        extra_vars: dict[str, Any] = {
            "action": "cleanup",
            "workspace_path": workspace_path,
        }

        try:
            result = await self._launch_and_wait(
                self._workspace_teardown_template_id,
                extra_vars,
            )
        except AWXClientError:
            logger.exception(
                "cleanup_workspace failed for workspace=%s",
                request.workspace_id,
            )
            raise

        return CleanupWorkspaceResponse(status=result.status)