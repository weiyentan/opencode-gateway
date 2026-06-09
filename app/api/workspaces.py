"""Workspace API endpoints — list, retrieve, pin, and cleanup workspaces."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.models.workspace import WorkspacePydantic, WorkspaceStatus
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


def _row_to_workspace(row: asyncpg.Record) -> WorkspacePydantic:
    """Convert an asyncpg Record to a WorkspacePydantic (GET endpoints)."""
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


# ---------------------------------------------------------------------------
# PG advisory lock key constants
# ---------------------------------------------------------------------------

_PORT_LOCK_KEY = 47001
_CLEANUP_LOCK_CLASS = 47002


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_response(row: asyncpg.Record) -> WorkspacePydantic:
    """Convert an asyncpg workspace row to a WorkspacePydantic (POST endpoints)."""
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


# ---------------------------------------------------------------------------
# GET endpoints  (from issue #42)
# ---------------------------------------------------------------------------


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


async def allocate_port(conn: asyncpg.Connection) -> int:
    """Allocate a free port using a PG advisory lock to avoid race conditions.

    Acquires an exclusive advisory lock (key ``_PORT_LOCK_KEY``), scans the
    ``workspaces`` table for the highest port in the dynamic range
    (10000–10999), and returns the next available number.

    The lock is held for the duration of the call so that no two concurrent
    requests can receive the same port.  It is released automatically when
    the connection is returned to the pool.
    """
    await conn.execute("SELECT pg_advisory_lock($1)", _PORT_LOCK_KEY)
    try:
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(port), 0) AS max_port FROM workspaces "
            "WHERE port IS NOT NULL AND port >= $1 AND port <= $2",
            10000,
            10999,
        )
        max_port: int = row["max_port"] if row else 0  # type: ignore[index]
        return max_port + 1 if max_port > 0 else 10000
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _PORT_LOCK_KEY)


async def _acquire_cleanup_lock(conn: asyncpg.Connection, workspace_id: uuid.UUID) -> bool:
    """Try to acquire a per-workspace cleanup advisory lock.

    Uses ``pg_try_advisory_lock`` so the caller can distinguish between
    "lock already held" and other failures.  Returns ``True`` when the
    lock is acquired, ``False`` when another process holds it.
    """
    key = (_CLEANUP_LOCK_CLASS << 32) | (workspace_id.int & 0xFFFFFFFF)
    locked: bool = await conn.fetchval(  # type: ignore[assignment]
        "SELECT pg_try_advisory_lock($1, $2)",
        _CLEANUP_LOCK_CLASS,
        workspace_id.int & 0xFFFFFFFF,
    )
    return locked


async def _release_cleanup_lock(conn: asyncpg.Connection, workspace_id: uuid.UUID) -> None:
    """Release the per-workspace cleanup advisory lock."""
    await conn.execute(
        "SELECT pg_advisory_unlock($1, $2)",
        _CLEANUP_LOCK_CLASS,
        workspace_id.int & 0xFFFFFFFF,
    )


# ---------------------------------------------------------------------------
# POST endpoints  (from issue #43)
# ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/pin", response_model=WorkspacePydantic)
async def pin_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> WorkspacePydantic:
    """Toggle the pinned flag on a workspace.

    Pinned workspaces are excluded from automatic cleanup policies.
    Calling this endpoint flips the current pin state.
    """
    row = await _fetch_workspace(conn, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    new_pinned = not row["pinned"]
    now = datetime.now(timezone.utc)

    await conn.execute(
        "UPDATE workspaces SET pinned = $1, updated_at = $2 WHERE id = $3",
        new_pinned,
        now,
        workspace_id,
    )

    row = await _fetch_workspace(conn, workspace_id)
    return _build_response(row)


@router.post("/workspaces/{workspace_id}/cleanup", response_model=WorkspacePydantic)
async def cleanup_workspace(
    workspace_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
) -> WorkspacePydantic:
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
    if not await _acquire_cleanup_lock(conn, workspace_id):
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
        await _release_cleanup_lock(conn, workspace_id)
