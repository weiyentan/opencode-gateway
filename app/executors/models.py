"""Pydantic request/response models for the ExecutorPlugin interface.

Defined per ADR 0002 — typed request and response models for all six
executor lifecycle methods: create_workspace, start_opencode,
stop_opencode, restart_opencode, collect_state, cleanup_workspace.
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

    *env_vars* are environment variables to pass to the OpenCode session.
    """

    workspace_id: UUID
    workspace_path: Optional[str] = None
    port: Optional[int] = None
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
    """Request to stop the OpenCode Serve process for a workspace."""

    workspace_id: UUID


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
    """Request to tear down a workspace, removing its directory."""

    workspace_id: UUID


class CleanupWorkspaceResponse(BaseModel):
    """Result of cleaning up a workspace."""

    status: str


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


class CancelJobRequest(BaseModel):
    """Request to cancel a running job for a workspace."""

    workspace_id: UUID


class CancelJobResponse(BaseModel):
    """Result of cancelling a job."""

    status: str
