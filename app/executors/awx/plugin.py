"""AWX executor plugin — maps the 7 lifecycle methods to AWX job templates.

Uses the AWXApiClient for API calls and translates between the typed
Pydantic request/response models and AWX extra_vars / artifacts.

Per issue #113, required artifact schemas are validated before
constructing response models.  Missing or malformed artifacts raise
:class:`AWXArtifactError` — the executor must **not** fall back to
placeholder values such as zero UUID.
"""

from __future__ import annotations

import logging
from typing import Any, Set
from uuid import UUID

from app.core.secrets import redact_dict
from app.executors import ExecutorPlugin
from app.executors.awx.artifacts import (
    CollectStateArtifacts,
    CreateWorkspaceArtifacts,
    StartOpencodeArtifacts,
    validate_artifacts,
)
from app.executors.awx.client import AWXApiClient, AWXJobResult
from app.executors.awx.exceptions import AWXArtifactError, AWXClientError
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


class AWXExecutorPlugin(ExecutorPlugin):
    """Executor that delegates lifecycle actions to AWX job templates.

    Per ADR 0002, each of the seven lifecycle methods maps to one of three
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
    ``cancel_job``                        TBD future surface (no AWX template yet)
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

        # Mapping of workspace UUID → AWX job ID for in-flight jobs.
        # Used by cancel_job to find the active AWX job for a workspace.
        # Only accessed within async event-loop context — no locks needed.
        self._active_awx_jobs: dict[UUID, int] = {}

        # Set of workspace IDs that have ever been tracked in _active_awx_jobs.
        # Used by cancel_job to distinguish "never tracked" (no job was ever
        # launched for this workspace) from "late cancel" (the job already
        # completed and its tracking entry was cleaned up).
        self._ever_tracked_workspaces: Set[UUID] = set()

        # Mapping of Gateway job UUID → AWX job ID for persisting the
        # executor_job_id on the gateway_jobs row after a successful launch.
        self._executor_job_ids: dict[UUID, int] = {}

    # ── Internal helpers ────────────────────────────────────────────────

    async def _launch_and_wait(
        self,
        template_id: int,
        extra_vars: dict[str, Any],
        workspace_id: UUID | None = None,
        gateway_job_id: UUID | None = None,
    ) -> AWXJobResult:
        """Launch a job template and wait for it to complete.

        When *workspace_id* is provided, the AWX job is tracked in
        :attr:`_active_awx_jobs` so that :meth:`cancel_job` can later
        look up the in-flight job for a given workspace.

        If the workspace already has a tracked job a warning is logged
        and the mapping is replaced with the new AWX job ID.

        The tracking entry is removed (popped) when the job reaches a
        terminal state — whether via successful completion, job failure,
        or timeout — **only** if the stored job ID still matches
        ``summary.job_id``.  This prevents a stale waiter from deleting
        the tracking entry of a concurrently launched job for the same
        workspace (e.g. cleanup after cancel).

        When *gateway_job_id* is provided, the AWX job ID is recorded in
        :attr:`_executor_job_ids` so that the API layer can persist it
        as ``executor_job_id`` on the ``gateway_jobs`` row.

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

        # Record the AWX job ID for the Gateway job so it can be persisted
        # as executor_job_id on the gateway_jobs row.
        if gateway_job_id is not None:
            self._executor_job_ids[gateway_job_id] = summary.job_id

        # Track the in-flight job for cancellation lookups.
        if workspace_id is not None:
            if workspace_id in self._active_awx_jobs:
                old_job_id = self._active_awx_jobs[workspace_id]
                logger.warning(
                    "Workspace %s already has active AWX job %d; "
                    "replacing with %d",
                    workspace_id,
                    old_job_id,
                    summary.job_id,
                )
            self._active_awx_jobs[workspace_id] = summary.job_id
            self._ever_tracked_workspaces.add(workspace_id)

        try:
            result = await self._client.wait_for_job(summary.job_id)
        except Exception:
            # Clean up tracking on any failure (timeout, job error, etc.).
            # Only pop if the stored job ID still matches — otherwise a
            # concurrently launched job (e.g. cleanup) may have replaced
            # the tracking entry for this workspace.
            if (
                workspace_id is not None
                and self._active_awx_jobs.get(workspace_id) == summary.job_id
            ):
                self._active_awx_jobs.pop(workspace_id)
            raise

        # Clean up tracking on successful completion.
        # Only pop if the stored job ID still matches — otherwise a
        # concurrently launched job (e.g. cleanup) may have replaced
        # the tracking entry for this workspace.
        if (
            workspace_id is not None
            and self._active_awx_jobs.get(workspace_id) == summary.job_id
        ):
            self._active_awx_jobs.pop(workspace_id)

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
                # workspace_id is unknown until the AWX job returns artifacts.
                gateway_job_id=request.job_id,
            )
        except AWXClientError:
            logger.exception("create_workspace failed for repo=%s", request.repo_url)
            raise

        # Validate artifacts — missing or malformed workspace_id / workspace_path
        # is a hard failure.  No more zero-UUID fallback.
        try:
            validated = validate_artifacts(
                CreateWorkspaceArtifacts,
                result.artifacts,
                template_name="gateway-create-workspace",
            )
        except AWXArtifactError:
            logger.exception(
                "create_workspace: invalid artifacts from AWX job %d "
                "(repo=%s)",
                result.job_id,
                request.repo_url,
            )
            raise

        return CreateWorkspaceResponse(
            workspace_id=validated.workspace_id,
            workspace_path=validated.workspace_path,
            status=result.status,
        )

    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        """Start OpenCode Serve via the gateway-opencode-lifecycle template.

        Extra vars: ``action: start``, ``workspace_path``, ``port`` (when set).
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
        if request.port is not None:
            extra_vars["port"] = request.port

        try:
            result = await self._launch_and_wait(
                self._opencode_lifecycle_template_id,
                extra_vars,
                workspace_id=request.workspace_id,
            )
        except AWXClientError:
            logger.exception(
                "start_opencode failed for workspace=%s",
                request.workspace_id,
            )
            raise

        # Validate artifacts — missing or malformed session_id / port
        # is a hard failure.  No more zero-UUID / zero-port fallback.
        try:
            validated = validate_artifacts(
                StartOpencodeArtifacts,
                result.artifacts,
                template_name="gateway-opencode-lifecycle (action=start)",
            )
        except AWXArtifactError:
            logger.exception(
                "start_opencode: invalid artifacts from AWX job %d "
                "(workspace=%s)",
                result.job_id,
                request.workspace_id,
            )
            raise

        return StartOpencodeResponse(
            session_id=validated.session_id,
            status=result.status,
            port=validated.port,
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
                workspace_id=request.workspace_id,
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
                workspace_id=request.workspace_id,
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
                workspace_id=request.workspace_id,
            )
        except AWXClientError:
            logger.exception(
                "collect_state failed for workspace=%s",
                request.workspace_id,
            )
            raise

        # Validate artifacts — status is required.  No more falling back
        # to the AWX job status or silently defaulting to ERROR.
        try:
            validated = validate_artifacts(
                CollectStateArtifacts,
                result.artifacts,
                template_name="gateway-workspace-teardown (action=collect)",
            )
        except AWXArtifactError:
            logger.exception(
                "collect_state: invalid artifacts from AWX job %d "
                "(workspace=%s)",
                result.job_id,
                request.workspace_id,
            )
            raise

        # Map the validated status to a WorkspaceState.
        try:
            ws_state = WorkspaceState(validated.status)
        except ValueError:
            logger.warning(
                "collect_state: unrecognised workspace state %r "
                "(workspace=%s, job=%d), defaulting to ERROR",
                validated.status,
                request.workspace_id,
                result.job_id,
            )
            ws_state = WorkspaceState.ERROR

        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=ws_state,
            process_status=validated.process_status,
            port=validated.port,
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
                workspace_id=request.workspace_id,
            )
        except AWXClientError:
            logger.exception(
                "cleanup_workspace failed for workspace=%s",
                request.workspace_id,
            )
            raise

        return CleanupWorkspaceResponse(status=result.status)

    async def cancel_job(
        self, request: CancelJobRequest
    ) -> CancelJobResponse:
        """Cancel an in-flight AWX job for a workspace.

        Looks up the tracked AWX job ID for the workspace in
        :attr:`_active_awx_jobs`. If a live AWX job is tracked,
        calls :meth:`AWXApiClient.cancel_job` to cancel it.

        If the workspace was **never** tracked (no job was ever launched
        for it via a lifecycle method that supplies a workspace_id),
        logs a warning and returns ``CancelJobResponse(status="cancelled")``
        — there is nothing to cancel so the operation is trivially done.

        If the workspace **was** tracked but the tracking entry has
        already been cleaned up (the job reached terminal status before
        cancel_job was called), logs a warning and returns
        ``CancelJobResponse(status="no_active_job")``.

        Raises:
            AWXConnectionError: If the AWX instance is unreachable.
            AWXHTTPError: If the server returns a non-2xx status.
            AWXTimeoutError: If the cancel request times out.
        """
        logger.info("cancel_job: workspace=%s", request.workspace_id)

        awx_job_id = self._active_awx_jobs.get(request.workspace_id)
        if awx_job_id is not None:
            # Active in-flight job found — cancel it via the AWX API.
            # Pop the mapping only after a successful cancel so that
            # transient API failures don't lose the tracked AWX job ID.
            await self._client.cancel_job(awx_job_id)
            self._active_awx_jobs.pop(request.workspace_id, None)
            logger.info(
                "cancel_job: AWX job %d cancelled for workspace %s",
                awx_job_id,
                request.workspace_id,
            )
            return CancelJobResponse(status="cancelled")

        # No active job tracked for this workspace.
        if request.workspace_id in self._ever_tracked_workspaces:
            # Was tracked before but the job already completed.
            logger.warning(
                "cancel_job: workspace %s had an active AWX job that already "
                "completed (late cancel)",
                request.workspace_id,
            )
            return CancelJobResponse(status="no_active_job")

        # Workspace was never tracked at all.
        logger.warning(
            "cancel_job: no AWX job was ever tracked for workspace %s",
            request.workspace_id,
        )
        return CancelJobResponse(status="cancelled")