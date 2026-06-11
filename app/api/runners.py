"""Runner API endpoints — list runners with latest observations."""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends
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
    """Response model for a single runner returned by the API."""

    id: uuid.UUID
    runner_id: str
    hostname: str
    status: str
    executor_type: str
    labels: dict | None = None
    created_at: datetime
    updated_at: datetime
    latest_observation: RunnerObservationSummary | None = None


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
