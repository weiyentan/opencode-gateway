"""Workspace API endpoints — list and retrieve workspaces."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.core.models.workspace import WorkspacePydantic, WorkspaceStatus
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workspaces"])

_WORKSPACE_COLS = (
    "id, runner_id, workspace_name, path, repo_url, branch, "
    "port, service_name, pinned, cleanup_after, cleanup_status, "
    "created_at, updated_at"
)


def _row_to_workspace(row: asyncpg.Record) -> WorkspacePydantic:
    """Convert an asyncpg Record to a WorkspacePydantic instance."""
    return WorkspacePydantic(
        id=row["id"],
        runner_id=row.get("runner_id"),
        workspace_name=row["workspace_name"],
        path=row["path"],
        repo_url=row["repo_url"],
        branch=row.get("branch"),
        port=row.get("port"),
        service_name=row.get("service_name"),
        pinned=row["pinned"],
        cleanup_after=row.get("cleanup_after"),
        cleanup_status=WorkspaceStatus(row["cleanup_status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/workspaces", response_model=list[WorkspacePydantic])
async def list_workspaces(
    runner_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    conn: asyncpg.Connection = Depends(get_session),
) -> list[WorkspacePydantic]:
    """List all workspaces, optionally filtered by runner_id and/or status.

    - ``runner_id``: filter by the Runner VM that hosts the workspace.
    - ``status``: filter by cleanup_status (e.g. ``active``, ``cleaning``, ``pinned``).
    """
    query = f"SELECT {_WORKSPACE_COLS} FROM workspaces"
    conditions: list[str] = []
    params: list[str | uuid.UUID] = []
    param_index = 1

    if runner_id is not None:
        conditions.append(f"runner_id = ${param_index}")
        params.append(runner_id)
        param_index += 1

    if status is not None:
        conditions.append(f"cleanup_status = ${param_index}")
        params.append(status)
        param_index += 1

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY created_at DESC"

    rows = await conn.fetch(query, *params)
    return [_row_to_workspace(row) for row in rows]


@router.get("/workspaces/{workspace_id}", response_model=WorkspacePydantic)
async def get_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> WorkspacePydantic:
    """Retrieve a single workspace by its ID."""
    row = await conn.fetchrow(
        f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE id = $1",
        workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _row_to_workspace(row)
