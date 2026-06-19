"""Workspace domain model — represents a workspace on a Runner VM."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class WorkspaceStatus(str, Enum):
    """Allowed status values for a Workspace's cleanup lifecycle.

    State machine for workspace cleanup:

        active ──► cleaning ──► cleaned
                       │
                       └──────► cleanup_failed
    """

    ACTIVE = "active"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    CLEANUP_FAILED = "cleanup_failed"
    PINNED = "pinned"


class Workspace(BaseModel):
    """A directory on the Runner VM containing a cloned repository.

    Created per-job, cleaned up according to policy. Maps to one
    OpenCode Serve instance on one Runner VM.
    """

    id: UUID
    runner_id: Optional[UUID] = None
    workspace_name: str
    path: str
    repo_url: str
    branch: Optional[str] = None
    port: Optional[int] = None
    service_name: Optional[str] = None
    pinned: bool = False
    cleanup_after: Optional[datetime] = None
    cleanup_status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    cleanup_started_at: Optional[datetime] = None
    cleanup_completed_at: Optional[datetime] = None
    cleanup_failed_at: Optional[datetime] = None
    cleanup_failure_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# WorkspacePydantic kept as a backward-compatible alias; use Workspace directly.
WorkspacePydantic = Workspace
