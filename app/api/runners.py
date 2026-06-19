"""Runner API endpoints — list runners, retrieve runner details, and manage runner status."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field

from app.db.session import get_session
from app.policy.observation import (
    RUNNER_STATUS_BLOCKED_DISK,
    RUNNER_STATUS_BLOCKED_MEMORY,
    RUNNER_STATUS_HEALTHY,
    RUNNER_STATUS_MAINTENANCE,
    RUNNER_STATUS_OFFLINE,
    RUNNER_STATUS_ONLINE,
    RUNNER_STATUS_UNKNOWN,
)

logger = logging.getLogger(__name__)

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
    admin_status: str | None = None
    health_status: str | None = None
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
    policy_status: str = "HEALTHY"
    policy_reason: str = "Runner is healthy"


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

_RUNNER_COLS = (
    "r.id, r.runner_id, r.hostname, r.status, "
    "r.admin_status, r.health_status, r.executor_type, "
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
        admin_status=row.get("admin_status"),
        health_status=row.get("health_status"),
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


def _derive_policy_status(
    admin_status: str | None,
    health_status: str | None,
) -> tuple[str, str]:
    """Derive policy_status and policy_reason from the runner's admin and health statuses.

    Admin status (operator-set) takes priority: offline → OFFLINE,
    maintenance → MAINTENANCE, online → ONLINE.
    Otherwise the observation-derived health_status determines the result.
    """
    if admin_status == RUNNER_STATUS_OFFLINE:
        return ("OFFLINE", "Runner is manually set offline")
    if admin_status == RUNNER_STATUS_MAINTENANCE:
        return ("MAINTENANCE", "Runner is in maintenance mode")
    if admin_status == RUNNER_STATUS_ONLINE:
        return ("ONLINE", "Runner is manually set online")
    if health_status == RUNNER_STATUS_BLOCKED_DISK:
        return ("BLOCKED_DISK_PRESSURE", "Runner has disk pressure")
    if health_status == RUNNER_STATUS_BLOCKED_MEMORY:
        return ("BLOCKED_MEMORY_PRESSURE", "Runner has memory pressure")
    if health_status == RUNNER_STATUS_UNKNOWN:
        return ("UNKNOWN", "Runner observations are stale")
    return ("HEALTHY", "Runner is healthy")


# ---------------------------------------------------------------------------
# Status transition validation
# ---------------------------------------------------------------------------

# Valid status transitions for POST /runners/{runner_id}/status.
# The *keys* are the target statuses; the *values* are the set of
# allowed source statuses.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    RUNNER_STATUS_OFFLINE: frozenset({
        RUNNER_STATUS_HEALTHY,
        RUNNER_STATUS_BLOCKED_DISK,
        RUNNER_STATUS_BLOCKED_MEMORY,
        RUNNER_STATUS_UNKNOWN,
        RUNNER_STATUS_ONLINE,
        RUNNER_STATUS_MAINTENANCE,
    }),
    RUNNER_STATUS_MAINTENANCE: frozenset({
        RUNNER_STATUS_HEALTHY,
        RUNNER_STATUS_BLOCKED_DISK,
        RUNNER_STATUS_BLOCKED_MEMORY,
        RUNNER_STATUS_UNKNOWN,
        RUNNER_STATUS_ONLINE,
        RUNNER_STATUS_OFFLINE,
    }),
    RUNNER_STATUS_ONLINE: frozenset({
        RUNNER_STATUS_OFFLINE,
        RUNNER_STATUS_MAINTENANCE,
    }),
}

_VALID_TARGET_STATUSES: frozenset[str] = frozenset(_VALID_TRANSITIONS.keys())


def _validate_status_transition(
    current_status: str,
    target_status: str,
    runner_id: uuid.UUID,
) -> None:
    """Validate that *target_status* is a valid transition from *current_status*.

    Raises :class:`HTTPException` (422) when the transition is not allowed.
    """
    if target_status not in _VALID_TARGET_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid target status '{target_status}'. "
                f"Allowed values: {', '.join(sorted(_VALID_TARGET_STATUSES))}"
            ),
        )

    allowed_sources = _VALID_TRANSITIONS[target_status]
    if current_status not in allowed_sources:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot transition from '{current_status}' to '{target_status}'. "
                f"Allowed source statuses for '{target_status}': "
                f"{', '.join(sorted(allowed_sources))}"
            ),
        )


# ---------------------------------------------------------------------------
# Request / response models for POST /runners/{runner_id}/status
# ---------------------------------------------------------------------------


class RunnerStatusUpdateRequest(BaseModel):
    """Request body for POST /runners/{runner_id}/status."""

    status: str = Field(
        description="Target runner status. One of: offline, online, maintenance.",
    )
    reason: str = Field(
        default="",
        description="Human-readable reason for the status change.",
    )


class RunnerStatusUpdateResponse(BaseModel):
    """Response body for POST /runners/{runner_id}/status."""

    id: uuid.UUID
    runner_id: str
    hostname: str
    previous_status: str
    current_status: str
    reason: str
    updated_at: datetime


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

    policy_status, policy_reason = _derive_policy_status(
        base.admin_status, base.health_status
    )

    return RunnerDetailResponse(
        id=base.id,
        runner_id=base.runner_id,
        hostname=base.hostname,
        status=base.status,
        admin_status=base.admin_status,
        health_status=base.health_status,
        executor_type=base.executor_type,
        labels=base.labels,
        created_at=base.created_at,
        updated_at=base.updated_at,
        latest_observation=latest_observation,
        workspace_observations=workspace_observations,
        opencode_instance_observations=opencode_instance_observations,
        policy_status=policy_status,
        policy_reason=policy_reason,
    )


# ---------------------------------------------------------------------------
# POST /runners/{runner_id}/status
# ---------------------------------------------------------------------------


@router.post(
    "/runners/{runner_id}/status",
    response_model=RunnerStatusUpdateResponse,
    status_code=http_status.HTTP_200_OK,
)
async def set_runner_status(
    runner_id: uuid.UUID,
    body: RunnerStatusUpdateRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> RunnerStatusUpdateResponse:
    """Manually set a runner's status.

    Allows operators to set a runner to ``offline``, ``online``, or
    ``maintenance``.  The transition is validated against the allowed
    state machine, the runner record is updated, and the change is
    logged in the ``runner_events`` table.

    Returns 404 if the runner ID does not exist, and 422 if the
    requested transition is not allowed.
    """
    # Fetch the current runner row — use admin_status for transition validation
    row = await conn.fetchrow(
        "SELECT id, runner_id, hostname, admin_status, status FROM runners WHERE id = $1",
        runner_id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Runner {runner_id} not found",
        )

    previous_status: str = row["admin_status"] or row["status"]
    target_status: str = body.status

    # Validate the transition
    _validate_status_transition(previous_status, target_status, runner_id)

    # Update both admin_status and the legacy status column
    now = datetime.now(timezone.utc)
    await conn.execute(
        "UPDATE runners SET admin_status = $1, status = $1, updated_at = $2 WHERE id = $3",
        target_status,
        now,
        runner_id,
    )

    # Log the status change in runner_events
    event_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO runner_events "
        "(id, runner_id, event_type, old_status, new_status, reason, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        event_id,
        runner_id,
        f"runner_status_{target_status}",
        previous_status,
        target_status,
        body.reason or f"Runner status changed to {target_status}",
        now,
    )

    logger.info(
        "Runner %s status changed: %s → %s (reason: %s)",
        runner_id,
        previous_status,
        target_status,
        body.reason,
    )

    return RunnerStatusUpdateResponse(
        id=row["id"],
        runner_id=row["runner_id"],
        hostname=row["hostname"],
        previous_status=previous_status,
        current_status=target_status,
        reason=body.reason,
        updated_at=now,
    )
