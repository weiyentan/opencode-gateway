"""Job API endpoints — create and retrieve coding jobs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

from app.db.session import get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import CreateWorkspaceRequest, StartOpencodeRequest

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


_FETCH_COLS = (
    "id, repo_url, task_summary, status, created_at, updated_at, completed_at"
)


async def _fetch_job(conn: asyncpg.Connection, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Fetch a single job row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM jobs WHERE id = $1",
        job_id,
    )


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
) -> JobResponse:
    """Create a new job, dispatch via executor, and return the final state."""
    job_id = uuid.uuid4()

    # 1. Insert the job record in pending state
    await conn.execute(
        "INSERT INTO jobs (id, repo_url, task_summary, status, executor_type) "
        "VALUES ($1, $2, $3, 'pending', $4)",
        job_id,
        str(body.repo_url),
        body.task_summary,
        executor.name,
    )

    # 2. Transition to running and dispatch to executor
    await conn.execute(
        "UPDATE jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        datetime.now(timezone.utc),
    )

    try:
        # 3. Create workspace and start OpenCode Serve
        ws_response = await executor.create_workspace(
            CreateWorkspaceRequest(
                repo_url=str(body.repo_url),
                job_id=job_id,
            )
        )
        await executor.start_opencode(
            StartOpencodeRequest(
                workspace_id=ws_response.workspace_id,
                workspace_path=ws_response.workspace_path,
            )
        )

        # 4. Mark completed
        now = datetime.now(timezone.utc)
        await conn.execute(
            "UPDATE jobs SET status = 'completed', updated_at = $2, completed_at = $3 "
            "WHERE id = $1",
            job_id,
            now,
            now,
        )
    except Exception:
        logger.exception("Executor dispatch failed for job %s", job_id)
        await conn.execute(
            "UPDATE jobs SET status = 'failed', updated_at = $2 WHERE id = $1",
            job_id,
            datetime.now(timezone.utc),
        )

    # 5. Return final state
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
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
    )
