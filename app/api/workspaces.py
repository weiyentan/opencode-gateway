"""Workspace API endpoints — list and retrieve workspaces."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.models.workspace import WorkspacePydantic
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


_FETCH_COLS = (
    "id, runner_id, workspace_name, path, repo_url, branch, port, "
    "service_name, pinned, cleanup_after, cleanup_status, created_at, updated_at"
)


def _row_to_workspace(row: asyncpg.Record) -> WorkspacePydantic:
    """Map an asyncpg Record row to a WorkspacePydantic."""
    return WorkspacePydantic(
        id=row["id"],
        runner_id=row["runner_id"],
        workspace_name=row["workspace_name"],
        path=row["path"],
        repo_url=row["repo_url"],
        branch=row["branch"],
        port=row["port"],
        service_name=row["service_name"],
        pinned=row["pinned"],
        cleanup_after=row["cleanup_after"],
        cleanup_status=row["cleanup_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("", response_model=list[WorkspacePydantic])
async def list_workspaces(
    runner_id: Optional[uuid.UUID] = Query(default=None, description="Filter by runner VM ID"),
    status: Optional[str] = Query(
        default=None, description="Filter by cleanup_status (active, cleaning, pinned)"
    ),
    conn: asyncpg.Connection = Depends(get_session),
) -> list[WorkspacePydantic]:
    """List all workspaces, optionally filtered by runner_id and/or cleanup_status."""
    clauses: list[str] = []
    params: list[object] = []
    idx = 1

    if runner_id is not None:
        clauses.append(f"runner_id = ${idx}")
        params.append(runner_id)
        idx += 1

    if status is not None:
        clauses.append(f"cleanup_status = ${idx}")
        params.append(status)
        idx += 1

    where = ""
    if clauses:
        where = " WHERE " + " AND ".join(clauses)

    rows = await conn.fetch(
        f"SELECT {_FETCH_COLS} FROM workspaces{where} ORDER BY created_at DESC",
        *params,
    )

    return [_row_to_workspace(r) for r in rows]


@router.get("/{workspace_id}", response_model=WorkspacePydantic)
async def get_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> WorkspacePydantic:
    """Retrieve a single workspace by its ID."""
    row = await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM workspaces WHERE id = $1",
        workspace_id,
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    return _row_to_workspace(row)
