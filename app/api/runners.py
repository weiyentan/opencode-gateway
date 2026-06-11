"""Runner API endpoints — list runners and retrieve runner details."""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.session import get_session

logger = __import__("logging").getLogger(__name__)

router = APIRouter(tags=["runners"])


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class RunnerObservationSummary(BaseModel):
    """Summary of the latest observation for a Runner VM."""

    disk_used_percent: float | None = None
    memory_used_percent: float | None = None
    load_1m: float | None = None
    observed_at: datetime | None = None


class RunnerResponse(BaseModel):
    """Response model for a single runner returned by the list endpoint."""

    id: uuid.UUID
    runner_id: str
    hostname: str
    status: str
    executor_type: str
    labels: dict | None = None
    created_at: datetime
    updated_at: datetime
    latest_observation: RunnerObservationSummary | None = None


class WorkspaceObservationItem(BaseModel):
    """A single workspace observation entry."""

    workspace_name: str
    status: str | None = None
    opencode_status: str | None = None
    observed_at: datetime


class OpenCodeInstanceObservationItem(BaseModel):
    """A single OpenCode Serve instance observation entry."""

    instance_name: str
    version: str | None = None
    status: str | None = None
    observed_at: datetime


class RunnerDetailResponse(RunnerResponse):
    """Response model for a single runner with observation history."""

    workspace_observations: list[WorkspaceObservationItem] = []
    opencode_instance_observations: list[OpenCodeInstanceObservationItem] = []


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

_RUNNER_COLS = (
    "r.id, r.runner_id, r.hostname, r.status, r.executor_type, "
    "r.labels, r.created_at, r.updated_at"
)

_OBSERVATION_COLS = (
    "lo.disk_used_percent, lo.memory_used_percent, lo.load_1m, lo.observed_at"
)

_RUNNER_LIST_QUERY = (
    f"SELECT {_RUNNER_COLS}, {_OBSERVATION_COLS} "
    "FROM runners r "
    "LEFT JOIN LATERAL ( "
    "  SELECT disk_used_percent, memory_used_percent, load_1m, observed_at "
    "  FROM runner_observations "
    "  WHERE runner_id = r.id "
    "  ORDER BY observed_at DESC "
    "  LIMIT 1 "
    ") lo ON true "
    "ORDER BY r.created_at DESC"
)


def _row_to_runner_response(row: asyncpg.Record) -> RunnerResponse:
    """Convert an asyncpg Record to a RunnerResponse."""
    # Determine whether a latest_observation exists
    has_observation = row.get("observed_at") is not None

    observation: RunnerObservationSummary | None
    if has_observation:
        observation = RunnerObservationSummary(
            disk_used_percent=row.get("disk_used_percent"),
            memory_used_percent=row.get("memory_used_percent"),
            load_1m=row.get("load_1m"),
            observed_at=row["observed_at"],
        )
    else:
        observation = None

    return RunnerResponse(
        id=row["id"],
        runner_id=row["runner_id"],
        hostname=row["hostname"],
        status=row["status"],
        executor_type=row["executor_type"],
        labels=row.get("labels"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        latest_observation=observation,
    )


# ---------------------------------------------------------------------------
# Query helpers — detail endpoint
# ---------------------------------------------------------------------------

_WORKSPACE_OBS_COLS = (
    "wo.workspace_name, wo.status, wo.opencode_status, wo.observed_at"
)

_OPENCODE_INSTANCE_OBS_COLS = (
    "oi.instance_name, oi.version, oi.status, oi.observed_at"
)

_WORKSPACE_OBS_QUERY = (
    f"SELECT {_WORKSPACE_OBS_COLS} "
    "FROM workspace_observations wo "
    "WHERE wo.runner_id = $1 "
    "ORDER BY wo.observed_at DESC "
    "LIMIT 50"
)

_OPENCODE_INSTANCE_OBS_QUERY = (
    f"SELECT {_OPENCODE_INSTANCE_OBS_COLS} "
    "FROM opencode_instance_observations oi "
    "WHERE oi.runner_id = $1 "
    "ORDER BY oi.observed_at DESC "
    "LIMIT 50"
)


def _row_to_workspace_obs_item(row: asyncpg.Record) -> WorkspaceObservationItem:
    """Convert an asyncpg Record to a WorkspaceObservationItem."""
    return WorkspaceObservationItem(
        workspace_name=row["workspace_name"],
        status=row.get("status"),
        opencode_status=row.get("opencode_status"),
        observed_at=row["observed_at"],
    )


def _row_to_opencode_instance_obs_item(
    row: asyncpg.Record,
) -> OpenCodeInstanceObservationItem:
    """Convert an asyncpg Record to an OpenCodeInstanceObservationItem."""
    return OpenCodeInstanceObservationItem(
        instance_name=row["instance_name"],
        version=row.get("version"),
        status=row.get("status"),
        observed_at=row["observed_at"],
    )


# ---------------------------------------------------------------------------
# GET /runners
# ---------------------------------------------------------------------------


@router.get("/runners", response_model=list[RunnerResponse])
async def list_runners(
    conn: asyncpg.Connection = Depends(get_session),
) -> list[RunnerResponse]:
    """List all registered Runner VMs with their latest observation summary.

    Returns a JSON array of runners ordered by creation date (newest first).
    Each entry includes a ``latest_observation`` field that contains the most
    recent resource-utilisation snapshot, or ``null`` when no observations
    have been recorded yet.
    """
    rows = await conn.fetch(_RUNNER_LIST_QUERY)
    return [_row_to_runner_response(row) for row in rows]


# ---------------------------------------------------------------------------
# GET /runners/{runner_id}
# ---------------------------------------------------------------------------


@router.get("/runners/{runner_id}", response_model=RunnerDetailResponse)
async def get_runner_detail(
    runner_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> RunnerDetailResponse:
    """Retrieve a single runner with its observation history.

    Returns the runner's details along with the last 50 workspace
    observations and last 50 OpenCode Serve instance observations,
    each ordered by ``observed_at`` descending.

    Returns 404 if the runner ID does not exist.
    """
    # Fetch the runner row
    row = await conn.fetchrow(
        f"SELECT {_RUNNER_COLS} FROM runners r WHERE r.id = $1",
        runner_id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Runner {runner_id} not found",
        )

    # Build the base RunnerResponse
    base = _row_to_runner_response(row)

    # Fetch workspace observations
    ws_rows = await conn.fetch(_WORKSPACE_OBS_QUERY, runner_id)
    workspace_observations = [_row_to_workspace_obs_item(r) for r in ws_rows]

    # Fetch OpenCode instance observations
    oi_rows = await conn.fetch(_OPENCODE_INSTANCE_OBS_QUERY, runner_id)
    opencode_instance_observations = [
        _row_to_opencode_instance_obs_item(r) for r in oi_rows
    ]

    # Fetch the latest observation summary separately (the list query
    # uses a lateral join; for the detail endpoint we re-query)
    obs_row = await conn.fetchrow(
        "SELECT disk_used_percent, memory_used_percent, load_1m, observed_at "
        "FROM runner_observations "
        "WHERE runner_id = $1 "
        "ORDER BY observed_at DESC "
        "LIMIT 1",
        runner_id,
    )
    latest_observation: RunnerObservationSummary | None = None
    if obs_row is not None:
        latest_observation = RunnerObservationSummary(
            disk_used_percent=obs_row.get("disk_used_percent"),
            memory_used_percent=obs_row.get("memory_used_percent"),
            load_1m=obs_row.get("load_1m"),
            observed_at=obs_row["observed_at"],
        )

    return RunnerDetailResponse(
        id=base.id,
        runner_id=base.runner_id,
        hostname=base.hostname,
        status=base.status,
        executor_type=base.executor_type,
        labels=base.labels,
        created_at=base.created_at,
        updated_at=base.updated_at,
        latest_observation=latest_observation,
        workspace_observations=workspace_observations,
        opencode_instance_observations=opencode_instance_observations,
    )
