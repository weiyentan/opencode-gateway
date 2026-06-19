"""Workspace API endpoints — list, retrieve, pin, and cleanup workspaces."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.models.workspace import Workspace, WorkspaceStatus
from app.core.ports import release_port
from app.db.lock import release_cleanup_lock, try_acquire_cleanup_lock
from app.db.session import get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import CleanupWorkspaceRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workspaces"])


# ---------------------------------------------------------------------------
# Column helpers
# ---------------------------------------------------------------------------

_WORKSPACE_COLS = (
    "id, runner_id, workspace_name, path, repo_url, branch, "
    "port, service_name, pinned, cleanup_after, cleanup_status, "
    "created_at, updated_at"
)


def _row_to_workspace(row: asyncpg.Record) -> Workspace:
    """Convert an asyncpg Record to a Workspace (GET endpoints)."""
    return Workspace(
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_response(row: asyncpg.Record) -> Workspace:
    """Convert an asyncpg workspace row to a Workspace (POST endpoints)."""
    return Workspace(
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


# ---------------------------------------------------------------------------
# GET endpoints  (from issue #42)
# ---------------------------------------------------------------------------


@router.get("/workspaces", response_model=list[Workspace])
async def list_workspaces(
    runner_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    conn: asyncpg.Connection = Depends(get_session),
) -> list[Workspace]:
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


@router.get("/workspaces/{workspace_id}", response_model=Workspace)
async def get_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> Workspace:
    """Retrieve a single workspace by its ID."""
    row = await conn.fetchrow(
        f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE id = $1",
        workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _row_to_workspace(row)


# ---------------------------------------------------------------------------
# Internal helpers  (from issue #43)
# ---------------------------------------------------------------------------


async def _fetch_workspace(
    conn: asyncpg.Connection, workspace_id: uuid.UUID
) -> Optional[asyncpg.Record]:
    """Fetch a workspace row by ID, or None."""
    return await conn.fetchrow(
        f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE id = $1",
        workspace_id,
    )


# ---------------------------------------------------------------------------
# POST endpoints  (from issue #43)
# ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/pin", response_model=Workspace)
async def pin_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> Workspace:
    """Toggle the pinned flag on a workspace.

    Pinned workspaces are excluded from automatic cleanup policies
    (``cleanup_after`` is set to NULL).  Unpinning resets the
    ``cleanup_after`` timestamp using the configured retention period.
    Calling this endpoint flips the current pin state.
    """
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    new_pinned = not row["pinned"]
    now = datetime.now(timezone.utc)
    settings = get_settings()

    if new_pinned:
        # Pinned → exclude from automatic cleanup
        cleanup_after = None
    else:
        # Unpinned → reset cleanup_after using default success retention
        # from the workspace creation time
        cleanup_after = row["created_at"] + timedelta(
            hours=settings.cleanup_success_retention_hours
        )

    await conn.execute(
        "UPDATE workspaces SET pinned = $1, cleanup_after = $2, updated_at = $3 "
        "WHERE id = $4",
        new_pinned,
        cleanup_after,
        now,
        workspace_id,
    )

    row = await _fetch_workspace(conn, workspace_id)
    return _build_response(row)


@router.post("/workspaces/{workspace_id}/cleanup", response_model=Workspace)
async def cleanup_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
) -> Workspace:
    """Trigger cleanup of a workspace via the executor plugin.

    Transitions the workspace to ``'cleaning'`` status, acquires a
    per-workspace PG advisory lock to serialise concurrent cleanup
    requests, and delegates the actual teardown to the executor plugin.
    """
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if row["cleanup_status"] == WorkspaceStatus.CLEANING.value:
        raise HTTPException(
            status_code=409,
            detail="Workspace is already being cleaned",
        )

    # Serialise concurrent cleanup attempts
    if not await try_acquire_cleanup_lock(conn, workspace_id):
        raise HTTPException(
            status_code=409,
            detail="Workspace cleanup is already in progress (lock held by another process)",
        )

    try:
        now = datetime.now(timezone.utc)
        await conn.execute(
            "UPDATE workspaces SET cleanup_status = $1, updated_at = $2 WHERE id = $3",
            WorkspaceStatus.CLEANING.value,
            now,
            workspace_id,
        )

        await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=workspace_id)
        )

        # Release the port so it becomes available for reuse (ADR 0003).
        await release_port(conn, workspace_id)

        row = await _fetch_workspace(conn, workspace_id)
        return _build_response(row)
    except Exception:
        logger.exception("Cleanup failed for workspace %s", workspace_id)
        await conn.execute(
            "UPDATE workspaces SET cleanup_status = $1, updated_at = $2 WHERE id = $3",
            WorkspaceStatus.ACTIVE.value,
            datetime.now(timezone.utc),
            workspace_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Cleanup failed",
        )
    finally:
        await release_cleanup_lock(conn, workspace_id)
