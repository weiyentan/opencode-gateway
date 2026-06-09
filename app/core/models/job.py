"""Job domain model — represents a unit of work submitted to the Gateway."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class JobStatus(str, Enum):
    """Allowed status values for a Job's lifecycle state machine."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    REJECTED = "rejected"


class Job(BaseModel):
    """A unit of work submitted to the Gateway.

    Maps to one coding task executed against one workspace via one
    OpenCode session.
    """

    id: UUID
    status: JobStatus
    repo_url: str
    task_summary: str
    runner_id: Optional[UUID] = None
    workspace_name: Optional[str] = None
    opencode_url: Optional[str] = None
    opencode_session_id: Optional[str] = None
    executor_type: str
    executor_job_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    diff: Optional[str] = None
