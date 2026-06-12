"""Job API endpoints — create, retrieve, and monitor coding jobs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from pydantic.config import ConfigDict

from app.core.job_orchestrator import (
    JobNotFoundError,
    InvalidJobTransitionError,
    SessionAbortError,
    _fetch_job,
    execute_abort_job,
    execute_create_job,
)
from app.core.lifecycle import can_transition
from app.core.models.job import JobStatus
from app.db.session import get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.opencode.protocol import OpenCodeClientProtocol

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


class JobCreateRequest(BaseModel):
    """Request body for POST /jobs."""

    repo_url: HttpUrl = Field(description="URL of the repository to work on")
    task_summary: str = Field(min_length=1, description="Summary of the task to perform")


class JobResponse(BaseModel):
    """Response body for job endpoints."""

    id: uuid.UUID
    repo_url: str
    task_summary: str
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    opencode_session_id: Optional[str] = None
    diff: Optional[str] = None


class JobEvent(BaseModel):
    """A single approval/rejection event for a job."""

    event_type: str
    timestamp: datetime
    actor: str
    details: str
    previous_status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


async def get_opencode_client() -> Optional[OpenCodeClientProtocol]:
    """Dependency that returns the OpenCode client for diff fetching.

    Returns None by default; tests override this via
    ``app.dependency_overrides`` to inject a mock.
    """
    return None


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: Optional[OpenCodeClientProtocol] = Depends(get_opencode_client),
) -> JobResponse:
    """Create a new job, dispatch via executor, and return the final state."""
    job_data = await execute_create_job(
        conn=conn,
        executor=executor,
        opencode_client=opencode_client,
        repo_url=str(body.repo_url),
        task_summary=body.task_summary,
    )
    return JobResponse(**job_data)


@router.post("/jobs/{job_id}/approve", response_model=JobResponse)
async def approve_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Approve a job that is waiting for approval, transitioning it to running."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "needs_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Job is in '{row['status']}' state, expected 'needs_approval'",
        )

    # Validate the transition against the centralised rule set (defence-in-depth)
    if not can_transition(JobStatus.NEEDS_APPROVAL, JobStatus.RUNNING):
        logger.error(
            "Lifecycle rejected transition needs_approval→running for job %s", job_id
        )
        raise HTTPException(status_code=500, detail="Internal state machine error")

    now = datetime.now(timezone.utc)

    # Record approval
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals "
        "(id, job_id, requested_by, requested_action, approval_type, approved_by, "
        "status, created_at, decided_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        approval_id,
        job_id,
        "system",
        "run_job",
        "manual",
        "api",
        "approved",
        now,
        now,
    )

    # Transition job to running
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        now,
    )

    # Return updated job
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
    )


@router.post("/jobs/{job_id}/reject", response_model=JobResponse)
async def reject_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Reject a job that is waiting for approval, transitioning it to rejected."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "needs_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Job is in '{row['status']}' state, expected 'needs_approval'",
        )

    # Validate the transition against the centralised rule set (defence-in-depth)
    if not can_transition(JobStatus.NEEDS_APPROVAL, JobStatus.REJECTED):
        logger.error(
            "Lifecycle rejected transition needs_approval→rejected for job %s", job_id
        )
        raise HTTPException(status_code=500, detail="Internal state machine error")

    now = datetime.now(timezone.utc)

    # Record rejection
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals "
        "(id, job_id, requested_by, requested_action, approval_type, approved_by, "
        "status, created_at, decided_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        approval_id,
        job_id,
        "system",
        "run_job",
        "manual",
        "api",
        "rejected",
        now,
        now,
    )

    # Transition job to rejected
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'rejected', updated_at = $2 WHERE id = $1",
        job_id,
        now,
    )

    # Return updated job
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
    )



@router.post("/jobs/{job_id}/abort", response_model=JobResponse)
async def abort_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: Optional[OpenCodeClientProtocol] = Depends(get_opencode_client),
) -> JobResponse:
    """Abort a job by cancelling its OpenCode session and marking it aborted.

    If the job has an active OpenCode session the endpoint transitions the job
    to ``aborting``, calls :meth:`OpenCodeClientProtocol.abort_session`, and on
    success marks the job ``aborted``.  When the session is unreachable the job
    stays in ``aborting`` and the caller receives a 503.

    Jobs without a session (e.g. still *pending*) are immediately marked
    ``aborted``.
    """
    try:
        job_data = await execute_abort_job(
            conn=conn,
            executor=executor,
            opencode_client=opencode_client,
            job_id=job_id,
        )
        return JobResponse(**job_data)
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidJobTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SessionAbortError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Retrieve a job by its ID."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
    )


@router.get("/jobs/{job_id}/events", response_model=list[JobEvent])
async def get_job_events(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> list[JobEvent]:
    """Return approval/rejection events for a job.

    Queries the approvals table and maps each record to an event dict
    with event_type, timestamp, actor, and details.  Returns an empty
    list when the job has no events, or 404 if the job does not exist.
    """
    # Verify the job exists first
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Query the approvals table for this job
    approval_records = await conn.fetch(
        "SELECT status, approved_by, requested_by, requested_action, created_at "
        "FROM approvals WHERE job_id = $1 ORDER BY created_at ASC",
        job_id,
    )

    # Query the job_events table for this job
    event_records = await conn.fetch(
        "SELECT event_type, actor, details, created_at, previous_status "
        "FROM job_events WHERE job_id = $1 ORDER BY created_at ASC",
        job_id,
    )

    events: list[JobEvent] = []
    for rec in approval_records:
        events.append(
            JobEvent(
                event_type=rec["status"],
                timestamp=rec["created_at"],
                actor=rec["approved_by"] if rec["approved_by"] else rec["requested_by"],
                details=rec["requested_action"],
                previous_status=None,
            )
        )
    for rec in event_records:
        events.append(
            JobEvent(
                event_type=rec["event_type"],
                timestamp=rec["created_at"],
                actor=rec["actor"],
                details=rec["details"],
                previous_status=rec["previous_status"],
            )
        )

    # Sort combined events by timestamp ascending
    events.sort(key=lambda e: e.timestamp)

    return events


@router.get("/jobs/{job_id}/diff")
async def get_job_diff(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JSONResponse:
    """Return the stored diff for a completed job.

    Returns the diff payload on success (200), a conflict response when the
    job is still running (409), or 404 when the job does not exist or has no
    diff available.
    """
    row = await conn.fetchrow(
        "SELECT id, status, diff FROM gateway_jobs WHERE id = $1",
        job_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if row["status"] == "running":
        return JSONResponse(
            status_code=409,
            content={
                "job_id": str(job_id),
                "diff": None,
                "status": "running",
            },
        )

    if row["diff"] is None:
        raise HTTPException(status_code=404, detail="Diff not available")

    return JSONResponse(
        status_code=200,
        content={
            "job_id": str(row["id"]),
            "diff": row["diff"],
        },
    )
