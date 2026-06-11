"""Observations ingestion endpoint — POST /observations.

Runner VMs POST resource-utilisation snapshots here.  The endpoint
upserts the runner record and stores observations for the runner,
its workspaces, and its OpenCode Serve instances.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observations"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class WorkspaceEntry(BaseModel):
    """A single workspace observation attached to a runner heartbeat."""

    workspace_name: str = Field(description="Name of the workspace directory")
    status: str | None = Field(None, description="Workspace status (e.g. running, stopped)")
    opencode_status: str | None = Field(
        None, description="OpenCode Serve status for this workspace"
    )


class OpenCodeInstanceEntry(BaseModel):
    """A single OpenCode Serve instance observation attached to a runner heartbeat."""

    instance_name: str = Field(description="Name of the OpenCode Serve instance")
    version: str | None = Field(None, description="Version of OpenCode Serve")
    status: str | None = Field(None, description="Instance status (e.g. running, stopped)")


class ObservationsIngestRequest(BaseModel):
    """Request body for POST /observations."""

    runner_id: str = Field(description="Unique identifier for the Runner VM")
    hostname: str = Field(description="Hostname of the Runner VM")
    executor_type: str = Field(description="Executor plugin type (e.g. awx, local)")
    labels: dict | None = Field(None, description="Arbitrary key-value labels for the runner")

    # Runner-level resource observations
    disk_used_percent: float | None = Field(None, ge=0, le=100)
    memory_used_percent: float | None = Field(None, ge=0, le=100)
    load_1m: float | None = Field(None, ge=0)
    load_5m: float | None = Field(None, ge=0)
    load_15m: float | None = Field(None, ge=0)

    # Per-workpace and per-instance observations
    workspaces: list[WorkspaceEntry] | None = Field(None)
    opencode_instances: list[OpenCodeInstanceEntry] | None = Field(None)


class ObservationsIngestResponse(BaseModel):
    """Response returned on successful observation ingestion."""

    status: str = Field(default="ok")
    runner_id: str = Field(description="The runner_id that was upserted")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.post(
    "/observations",
    response_model=ObservationsIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_observations(
    body: ObservationsIngestRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> ObservationsIngestResponse:
    """Ingest runner heartbeat observations.

    1. Upserts the runner record (create if ``runner_id`` is new, update
       hostname/executor_type/labels otherwise).
    2. Sets runner status to ``HEALTHY``.
    3. Creates a ``RunnerObservation`` row with disk/memory/load metrics.
    4. Creates ``WorkspaceObservation`` rows for each workspace entry.
    5. Creates ``OpenCodeInstanceObservation`` rows for each instance entry.
    """
    now = _utcnow()

    # ------------------------------------------------------------------
    # 1. Upsert runner (INSERT … ON CONFLICT)
    # ------------------------------------------------------------------
    runner_row = await conn.fetchrow(
        """
        INSERT INTO runners (id, runner_id, hostname, executor_type, labels, status,
                             created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3, $4, 'HEALTHY', $5, $5)
        ON CONFLICT (runner_id)
        DO UPDATE SET
            hostname       = EXCLUDED.hostname,
            executor_type  = EXCLUDED.executor_type,
            labels         = EXCLUDED.labels,
            status         = 'HEALTHY',
            updated_at     = EXCLUDED.updated_at
        RETURNING id
        """,
        body.runner_id,
        body.hostname,
        body.executor_type,
        body.labels,
        now,
    )

    resolved_id: uuid.UUID = runner_row["id"]

    # ------------------------------------------------------------------
    # 2. Store runner-level observation
    # ------------------------------------------------------------------
    obs_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO runner_observations
            (id, runner_id, disk_used_percent, memory_used_percent,
             load_1m, load_5m, load_15m, observed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        obs_id,
        resolved_id,
        body.disk_used_percent,
        body.memory_used_percent,
        body.load_1m,
        body.load_5m,
        body.load_15m,
        now,
    )

    # ------------------------------------------------------------------
    # 3. Store workspace observations
    # ------------------------------------------------------------------
    if body.workspaces:
        for ws in body.workspaces:
            ws_obs_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO workspace_observations
                    (id, runner_id, workspace_name, status, opencode_status, observed_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                ws_obs_id,
                resolved_id,
                ws.workspace_name,
                ws.status,
                ws.opencode_status,
                now,
            )

    # ------------------------------------------------------------------
    # 4. Store OpenCode instance observations
    # ------------------------------------------------------------------
    if body.opencode_instances:
        for inst in body.opencode_instances:
            inst_obs_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO opencode_instance_observations
                    (id, runner_id, instance_name, version, status, observed_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                inst_obs_id,
                resolved_id,
                inst.instance_name,
                inst.version,
                inst.status,
                now,
            )

    logger.info(
        "Ingested observations for runner %s (uuid=%s): %d workspace(s), %d instance(s)",
        body.runner_id,
        resolved_id,
        len(body.workspaces) if body.workspaces else 0,
        len(body.opencode_instances) if body.opencode_instances else 0,
    )

    return ObservationsIngestResponse(status="ok", runner_id=body.runner_id)
