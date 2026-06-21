"""Pydantic request/response models for the ExecutorPlugin interface.

Defined per ADR 0002 — typed request and response models for all seven
executor lifecycle methods: create_workspace, start_opencode,
stop_opencode, restart_opencode, collect_state, cleanup_workspace,
cancel_job.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared enum
# ---------------------------------------------------------------------------


class WorkspaceState(str, Enum):
    """Reported state of a workspace after a collect_state call."""

    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


class CreateWorkspaceRequest(BaseModel):
    """Request to provision a workspace directory for a new job.

    *runner_id* is an optional hint that allows the Gateway to pre-select
    which runner (VM) should host the workspace.  When provided, the
    executor should create the workspace on the specified runner.

    *env_vars* are environment variables to pass to the OpenCode session.
    """

    repo_url: str
    branch: Optional[str] = None
    job_id: Optional[UUID] = None
    runner_id: Optional[str] = None
    env_vars: dict[str, str] = {}


class CreateWorkspaceResponse(BaseModel):
    """Result of creating a workspace."""

    workspace_id: UUID
    workspace_path: str
    status: str


# ---------------------------------------------------------------------------
# start_opencode
# ---------------------------------------------------------------------------


class StartOpencodeRequest(BaseModel):
    """Request to start the OpenCode Serve process for a workspace.

    *port* is the port number allocated by the Gateway's port-allocation
    service (ADN 0003).  It is passed to the AWX playbook so it can bind
    the OpenCode Serve process to the correct port.

    *gateway_job_id* is the UUID of the Gateway job row so that the
    executor can persist the AWX job ID immediately after launch,
    enabling cross-process cancellation to target the currently active
    AWX job rather than a completed lifecycle step.

    *env_vars* are environment variables to pass to the OpenCode session.
    """

    workspace_id: UUID
    workspace_path: Optional[str] = None
    port: Optional[int] = None
    gateway_job_id: Optional[UUID] = None
    env_vars: dict[str, str] = {}


class StartOpencodeResponse(BaseModel):
    """Result of starting OpenCode Serve."""

    session_id: UUID
    status: str
    port: int


# ---------------------------------------------------------------------------
# stop_opencode
# ---------------------------------------------------------------------------


class StopOpencodeRequest(BaseModel):
    """Request to stop the OpenCode Serve process for a workspace.

    *gateway_job_id* is the UUID of the Gateway job row so that the
    executor can persist the AWX job ID immediately after launch,
    enabling cross-process cancellation to target the currently active
    AWX job rather than a completed lifecycle step.
    """

    workspace_id: UUID
    gateway_job_id: Optional[UUID] = None


class StopOpencodeResponse(BaseModel):
    """Result of stopping OpenCode Serve."""

    status: str


# ---------------------------------------------------------------------------
# restart_opencode
# ---------------------------------------------------------------------------


class RestartOpencodeRequest(BaseModel):
    """Request to restart the OpenCode Serve process for a workspace."""

    workspace_id: UUID


class RestartOpencodeResponse(BaseModel):
    """Result of restarting OpenCode Serve."""

    status: str


# ---------------------------------------------------------------------------
# collect_state
# ---------------------------------------------------------------------------


class CollectStateRequest(BaseModel):
    """Request to collect the current state of a workspace and its service."""

    workspace_id: UUID


class CollectStateResponse(BaseModel):
    """Current state of a workspace and its OpenCode Serve process."""

    workspace_id: UUID
    status: WorkspaceState
    process_status: Optional[str] = None
    port: Optional[int] = None


# ---------------------------------------------------------------------------
# cleanup_workspace
# ---------------------------------------------------------------------------


class CleanupWorkspaceRequest(BaseModel):
    """Request to tear down a workspace, removing its directory.

    *gateway_job_id* is the UUID of the Gateway job row so that the
    executor can persist the AWX job ID immediately after launch,
    enabling cross-process cancellation to target the currently active
    AWX job rather than a completed lifecycle step.
    """

    workspace_id: UUID
    gateway_job_id: Optional[UUID] = None


class CleanupWorkspaceResponse(BaseModel):
    """Result of cleaning up a workspace."""

    status: str


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


class CancelJobRequest(BaseModel):
    """Request to cancel a running job for a workspace.

    *workspace_id* identifies the workspace whose AWX job should be
    cancelled.  The cancellation path prefers the in-memory tracking
    dict (``_active_awx_jobs``) on the executor plugin instance.  May
    be ``None`` when cancelling by ``executor_job_id`` alone (e.g. during
    ``create_workspace`` before the workspace UUID is known).

    *executor_job_id* provides a fallback for cross-process cancellation:
    when no in-memory entry exists (e.g. the abort request landed in a
    different process), the caller passes the AWX job ID that was
    persisted on the ``gateway_jobs`` row at launch time.  Also used
    directly when *workspace_id* is ``None`` (pre-workspace cancellation).
    """

    workspace_id: UUID | None = Field(
        default=None,
        description="Workspace whose AWX job should be cancelled. "
        "May be None when cancelling by executor_job_id alone "
        "(e.g. during create_workspace before the workspace UUID is known).",
    )
    executor_job_id: int | None = Field(
        default=None,
        description="AWX job ID to cancel when the in-memory tracking dict has no "
        "entry for this workspace, or when workspace_id is None "
        "(cross-process / pre-workspace cancellation).",
    )


class CancelJobResponse(BaseModel):
    """Result of cancelling a job."""

    status: str
