"""Job API endpoints — create and retrieve coding jobs."""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

from app.db.session import get_session

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


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Create a new job and return its record."""
    job_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO jobs (id, repo_url, task_summary, status) VALUES ($1, $2, $3, 'pending')",
        job_id,
        str(body.repo_url),
        body.task_summary,
    )
    row = await conn.fetchrow(
        "SELECT id, repo_url, task_summary, status, created_at, updated_at "
        "FROM jobs WHERE id = $1",
        job_id,
    )
    # row is guaranteed non-None after successful insert
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Retrieve a job by its ID."""
    row = await conn.fetchrow(
        "SELECT id, repo_url, task_summary, status, created_at, updated_at "
        "FROM jobs WHERE id = $1",
        job_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
