"""Workspace domain model — represents a workspace on a Runner VM."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class WorkspaceStatus(str, Enum):
    """Allowed status values for a Workspace's cleanup lifecycle."""

    ACTIVE = "active"
    CLEANING = "cleaning"
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
    created_at: datetime
    updated_at: datetime


class WorkspacePydantic(BaseModel):
    """Response-oriented model for returning workspace data via the API.

    Mirrors Workspace but is intended for API response serialisation
    where the caller may receive partial or summary views.
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
    created_at: datetime
    updated_at: datetime
