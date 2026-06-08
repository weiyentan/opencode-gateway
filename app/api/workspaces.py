"""Workspace API endpoints — list, retrieve, pin, and cleanup workspaces."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.models.workspace import WorkspacePydantic, WorkspaceStatus
from app.db.session import get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import CleanupWorkspaceRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

_FETCH_COLS = (
    "id, runner_id, workspace_name, path, repo_url, branch, port, "
    "service_name, pinned, cleanup_after, cleanup_status, created_at, updated_at"
)


def _workspace_from_row(row: asyncpg.Record) -> WorkspacePydantic:
    """Build a WorkspacePydantic from an asyncpg row."""
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

    return [_workspace_from_row(r) for r in rows]


async def _fetch_workspace(
    conn: asyncpg.Connection, workspace_id: uuid.UUID
) -> asyncpg.Record | None:
    """Fetch a single workspace row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM workspaces WHERE id = $1",
        workspace_id,
    )


@router.get("/{workspace_id}", response_model=WorkspacePydantic)
async def get_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> WorkspacePydantic:
    """Retrieve a single workspace by its ID."""
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    return _workspace_from_row(row)


@router.post("/{workspace_id}/pin", response_model=WorkspacePydantic)
async def pin_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> WorkspacePydantic:
    """Toggle the pinned flag on a workspace.

    Pinned workspaces are excluded from automatic cleanup.  If the
    workspace is currently pinned, calling this endpoint unpins it;
    if it is not pinned, it becomes pinned.
    """
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    now = datetime.now(timezone.utc)
    new_pinned = not row["pinned"]
    new_status = WorkspaceStatus.PINNED if new_pinned else WorkspaceStatus.ACTIVE

    await conn.execute(
        "UPDATE workspaces SET pinned = $2, cleanup_status = $3, updated_at = $4 "
        "WHERE id = $1",
        workspace_id,
        new_pinned,
        new_status.value,
        now,
    )

    row = await _fetch_workspace(conn, workspace_id)
    return _workspace_from_row(row)  # type: ignore[arg-type]


@router.post("/{workspace_id}/cleanup", response_model=WorkspacePydantic)
async def cleanup_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
) -> WorkspacePydantic:
    """Trigger cleanup of a workspace via the executor plugin.

    Marks the workspace as 'cleaning' and delegates the actual tear-down
    to the configured executor.  A PostgreSQL advisory lock is acquired
    on the workspace's port (when set) to serialise port
    deallocation/reallocation across concurrent requests.
    """
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    port = row["port"]

    # --- Serialise port deallocation via PG advisory lock (ADR 0003) ---
    if port is not None:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock($1)", port
        )
        if not locked:
            raise HTTPException(
                status_code=409,
                detail=f"Port {port} is currently being allocated — retry later",
            )

    # --- Mark workspace as cleaning ---
    now = datetime.now(timezone.utc)
    await conn.execute(
        "UPDATE workspaces SET cleanup_status = $2, updated_at = $3 WHERE id = $1",
        workspace_id,
        WorkspaceStatus.CLEANING.value,
        now,
    )

    # --- Delegate to executor ---
    try:
        await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=workspace_id)
        )
    except Exception:
        logger.exception("Executor cleanup failed for workspace %s", workspace_id)
        # Still return the workspace — it is marked 'cleaning' and can be retried.

    row = await _fetch_workspace(conn, workspace_id)
    return _workspace_from_row(row)  # type: ignore[arg-type]
