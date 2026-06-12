"""Approval domain model — represents an approval gate for a Job."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class Approval(BaseModel):
    """An approval request tied to a specific Job.

    Represents a gate that must be approved before the associated
    Job can proceed to the next stage.
    """

    id: UUID
    job_id: UUID
    requested_by: str
    requested_action: str
    approval_type: str
    approved_by: Optional[str] = None
    status: str
    created_at: datetime
    decided_at: Optional[datetime] = None


# ApprovalResponse kept as a backward-compatible alias; use Approval directly.
ApprovalResponse = Approval
