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
    ABORTING = "aborting"
    ABORTED = "aborted"

    @staticmethod
    def validate_transition(current: JobStatus, target: JobStatus) -> bool:
        """Validate whether a transition from *current* to *target* is allowed.

        Allowed transitions:
            pending → aborting
            running → aborting
            aborting → aborted

        All other transitions return ``False``.
        """
        if current == JobStatus.PENDING and target == JobStatus.ABORTING:
            return True
        if current == JobStatus.RUNNING and target == JobStatus.ABORTING:
            return True
        if current == JobStatus.ABORTING and target == JobStatus.ABORTED:
            return True
        return False


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
