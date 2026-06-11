"""Job API endpoints — create, retrieve, and monitor coding jobs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from pydantic.config import ConfigDict

from app.core.config import get_settings
from app.db.session import get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import (
    CleanupWorkspaceRequest,
    CreateWorkspaceRequest,
    StartOpencodeRequest,
    StopOpencodeRequest,
)
from app.opencode.protocol import OpenCodeClientProtocol
from app.policy import ObservationBasedPolicy

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


_FETCH_COLS = (
    "id, repo_url, task_summary, status, created_at, updated_at, completed_at, "
    "opencode_session_id, diff, workspace_name"
)


async def _fetch_job(conn: asyncpg.Connection, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Fetch a single job row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM gateway_jobs WHERE id = $1",
        job_id,
    )


async def get_opencode_client() -> Optional[OpenCodeClientProtocol]:
    """Dependency that returns the OpenCode client for diff fetching.

    Returns None by default; tests override this via
    ``app.dependency_overrides`` to inject a mock.
    """
    return None


async def _set_workspace_cleanup_after(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
    retention_hours: int,
) -> None:
    """Set cleanup_after on a workspace based on its created_at + retention.

    The workspace row must already exist in the database (created by the
    executor or workspace lifecycle).  When the row does not exist the
    UPDATE is a silent no-op.
    """
    await conn.execute(
        "UPDATE workspaces SET cleanup_after = created_at + $2::interval, "
        "updated_at = $3 WHERE id = $1",
        workspace_id,
        timedelta(hours=retention_hours),
        datetime.now(timezone.utc),
    )


def _resolve_workspace_id(workspace_name: Optional[str]) -> Optional[uuid.UUID]:
    """Parse the *workspace_name* column (stored as a string UUID) back to a UUID."""
    if not workspace_name:
        return None
    try:
        return uuid.UUID(workspace_name)
    except (ValueError, TypeError):
        logger.warning("Invalid workspace_name: %r", workspace_name)
        return None


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: Optional[OpenCodeClientProtocol] = Depends(get_opencode_client),
) -> JobResponse:
    """Create a new job, dispatch via executor, and return the final state."""
    job_id = uuid.uuid4()
    settings = get_settings()

    # 1. Insert the job record in pending state
    await conn.execute(
        "INSERT INTO gateway_jobs (id, repo_url, task_summary, status, executor_type) "
        "VALUES ($1, $2, $3, 'pending', $4)",
        job_id,
        str(body.repo_url),
        body.task_summary,
        executor.name,
    )

    # 2. Transition to running and dispatch to executor
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        datetime.now(timezone.utc),
    )

    try:
        # 3. Create workspace and store its ID (before potentially-failing start)
        ws_response = await executor.create_workspace(
            CreateWorkspaceRequest(
                repo_url=str(body.repo_url),
                job_id=job_id,
            )
        )
        new_workspace_id = ws_response.workspace_id

        # Store the workspace ID immediately so it is available even if
        # start_opencode fails and the job is marked "failed".
        await conn.execute(
            "UPDATE gateway_jobs SET workspace_name = $2 WHERE id = $1",
            job_id,
            str(new_workspace_id),
        )

        # 3b. Run pre-flight policy check (skeleton — always returns None)
        policy = ObservationBasedPolicy(settings)
        await policy.check(str(new_workspace_id))

        # 4. Start OpenCode Serve
        start_response = await executor.start_opencode(
            StartOpencodeRequest(
                workspace_id=new_workspace_id,
                workspace_path=ws_response.workspace_path,
            )
        )
        session_id = str(start_response.session_id)

        # Store the session ID so it is available for diff retrieval
        await conn.execute(
            "UPDATE gateway_jobs SET opencode_session_id = $2 WHERE id = $1",
            job_id,
            session_id,
        )

        # 5. Mark completed
        now = datetime.now(timezone.utc)
        diff_summary = f"Job completed: {body.task_summary}"
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'completed', updated_at = $2, "
            "completed_at = $3, diff = $4 WHERE id = $1",
            job_id,
            now,
            now,
            diff_summary,
        )

        # 6. Set workspace cleanup_after for successful completion
        if new_workspace_id is not None:
            await _set_workspace_cleanup_after(
                conn, new_workspace_id, settings.cleanup_success_retention_hours
            )

        # 7. Fetch and persist the diff (non-blocking — failure does not fail the job)
        if opencode_client is not None:
            try:
                diff_response = await opencode_client.get_session_diff(session_id)
                if diff_response and diff_response.diff:
                    await conn.execute(
                        "UPDATE gateway_jobs SET diff = $2 WHERE id = $1",
                        job_id,
                        diff_response.diff,
                    )
                    logger.info(
                        "Diff fetched and persisted for job %s (session %s)",
                        job_id,
                        session_id,
                    )
            except Exception:
                logger.warning(
                    "Failed to fetch diff for job %s (session %s)",
                    job_id,
                    session_id,
                    exc_info=True,
                )

    except Exception:
        logger.exception("Executor dispatch failed for job %s", job_id)
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'failed', updated_at = $2 WHERE id = $1",
            job_id,
            datetime.now(timezone.utc),
        )

        # Set workspace cleanup_after for failure
        row = await _fetch_job(conn, job_id)
        if row is not None:
            ws_id = _resolve_workspace_id(row.get("workspace_name"))
            if ws_id is not None:
                await _set_workspace_cleanup_after(
                    conn, ws_id, settings.cleanup_failure_retention_hours
                )

    # 7. Return final state
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
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] not in ("pending", "running", "aborting"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in '{row['status']}' state, "
                f"expected 'pending', 'running', or 'aborting'"
            ),
        )

    now = datetime.now(timezone.utc)
    session_id = row.get("opencode_session_id")
    previous_status = row["status"]  # capture before transition

    # Transition to aborting unless already in that state (retry)
    if row["status"] != "aborting":
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'aborting', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )

    aborted = False

    # If the job has an active session, cancel it via the OpenCode client
    if session_id and opencode_client is not None:
        try:
            await opencode_client.abort_session(session_id)
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 WHERE id = $1",
                job_id,
                datetime.now(timezone.utc),
            )
            aborted = True
        except Exception:
            logger.exception(
                "Failed to abort OpenCode session %s for job %s",
                session_id,
                job_id,
            )
            raise HTTPException(
                status_code=503,
                detail="OpenCode Serve unreachable; job remains in aborting state",
            )
    else:
        # No session to cancel — mark aborted immediately
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )
        aborted = True

    # When the job reached aborted state, set workspace cleanup_after
    if aborted:
        settings = get_settings()
        ws_id = _resolve_workspace_id(row.get("workspace_name"))
        if ws_id is not None:
            await _set_workspace_cleanup_after(
                conn, ws_id, settings.cleanup_failure_retention_hours
            )

    # Executor cleanup --- best effort, do not block the abort response
    workspace_name = row.get("workspace_name")
    if workspace_name:
        try:
            parsed_id = uuid.UUID(workspace_name)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid workspace_name for job %s: %r", job_id, workspace_name
            )
        else:
            try:
                await executor.stop_opencode(
                    StopOpencodeRequest(workspace_id=parsed_id)
                )
            except Exception:
                logger.exception(
                    "Failed to stop OpenCode Serve for job %s (workspace %s)",
                    job_id,
                    parsed_id,
                )
            try:
                await executor.cleanup_workspace(
                    CleanupWorkspaceRequest(workspace_id=parsed_id)
                )
            except Exception:
                logger.exception(
                    "Failed to clean up workspace for job %s (workspace %s)",
                    job_id,
                    parsed_id,
                )

    # Record abort event
    event_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO job_events "
        "(id, job_id, event_type, actor, details, previous_status, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        event_id,
        job_id,
        "aborted",
        "api",
        "Job aborted",
        previous_status,
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
