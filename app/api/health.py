"""Health check endpoint — reports application status, database connectivity,
and collector/source-database health.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.db.session import DatabasePool

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["health"])



def _get_version() -> str:
    """Return the installed package version, or a sensible fallback."""
    try:
        return version("opencode-gateway")
    except PackageNotFoundError:
        return "0.1.0-dev"


# ── Sub-models ────────────────────────────────────────────────────────────


class CollectorHealth(BaseModel):
    """Health status for a single collector credential."""

    credential_id: str = Field(description="UUID of the collector_credentials row")
    client_name: str = Field(description="Name of the associated OpenCode client")
    last_heartbeat: Optional[datetime] = Field(
        default=None, description="Most recent ingest timestamp"
    )
    total_records_ingested: int = Field(
        default=0, description="Total records ingested via this credential"
    )
    health: str = Field(description="healthy | stale | unknown")


class SourceDatabaseHealth(BaseModel):
    """Health status for a single source database."""

    source_database_id: str = Field(description="UUID of the source_databases row")
    client_name: str = Field(description="Name of the associated OpenCode client")
    last_push: Optional[datetime] = Field(
        default=None, description="Most recent push (last_seen_at)"
    )
    record_count: int = Field(default=0, description="Total records ingested")
    health: str = Field(description="healthy | stale | unknown")


class HealthResponse(BaseModel):
    """Response model for the GET /health endpoint."""

    status: str = Field(default="ok", description="Application health status")
    version: str = Field(description="Installed package version")
    database: str = Field(
        default="disconnected", description="Database connectivity status"
    )
    last_ingest_timestamp: Optional[datetime] = Field(
        default=None, description="Most recent ingest across all collectors"
    )
    collectors: list[CollectorHealth] = Field(
        default_factory=list, description="Per-collector health summary"
    )
    source_databases: list[SourceDatabaseHealth] = Field(
        default_factory=list, description="Per-source-database health summary"
    )


# ── Helpers ───────────────────────────────────────────────────────────────


def _derive_health(
    last_ts: datetime | None,
    now: datetime,
    threshold: int = settings.heartbeat_threshold,

) -> str:
    """Return 'healthy', 'stale', or 'unknown' based on last activity timestamp."""
    if last_ts is None:
        return "unknown"
    delta = (now - last_ts).total_seconds()
    if delta <= threshold:
        return "healthy"
    return "stale"


async def _collector_health_summary(
    db_pool: DatabasePool,
    now: datetime,
    threshold: int = settings.heartbeat_threshold,

) -> list[CollectorHealth]:
    """Query collectors and their most recent ingest-batch activity."""
    try:
        conn = await db_pool.acquire()
        try:
            rows = await conn.fetch("""
                SELECT
                    cc.id AS credential_id,
                    c.name  AS client_name,
                    (
                        SELECT MAX(ib.ingested_at)
                        FROM ingest_batches ib
                        WHERE ib.collector_credential_id = cc.id
                    ) AS last_heartbeat,
                    COALESCE((
                        SELECT SUM(ib.record_count)
                        FROM ingest_batches ib
                        WHERE ib.collector_credential_id = cc.id
                    ), 0) AS total_records_ingested
                FROM collector_credentials cc
                JOIN opencode_clients c ON c.id = cc.client_id
                WHERE cc.revoked_at IS NULL
                ORDER BY cc.id
            """)
        finally:
            await db_pool.release(conn)
    except Exception:
        logger.debug("Health: collector summary query failed", exc_info=True)
        return []

    return [
        CollectorHealth(
            credential_id=str(r["credential_id"]),
            client_name=r["client_name"],
            last_heartbeat=r["last_heartbeat"],
            total_records_ingested=r["total_records_ingested"],
            health=_derive_health(r["last_heartbeat"], now, threshold),
        )
        for r in rows
    ]


async def _source_db_health_summary(
    db_pool: DatabasePool,
    now: datetime,
    threshold: int = settings.heartbeat_threshold,

) -> list[SourceDatabaseHealth]:
    """Query source databases and their last-seen / record-count activity."""
    try:
        conn = await db_pool.acquire()
        try:
            rows = await conn.fetch("""
                SELECT
                    sd.id            AS source_database_id,
                    c.name           AS client_name,
                    sd.last_seen_at  AS last_push,
                    sd.record_count  AS record_count
                FROM source_databases sd
                JOIN opencode_clients c ON c.id = sd.client_id
                WHERE sd.is_active = true
                ORDER BY sd.id
            """)
        finally:
            await db_pool.release(conn)
    except Exception:
        logger.debug("Health: source-database summary query failed", exc_info=True)
        return []

    return [
        SourceDatabaseHealth(
            source_database_id=str(r["source_database_id"]),
            client_name=r["client_name"],
            last_push=r["last_push"],
            record_count=r["record_count"],
            health=_derive_health(r["last_push"], now, threshold),
        )
        for r in rows
    ]


async def _last_ingest_timestamp(db_pool: DatabasePool) -> datetime | None:
    """Return the most recent ingest timestamp across all batches."""
    try:
        conn = await db_pool.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT MAX(ingested_at) AS last_ts FROM ingest_batches"
            )
        finally:
            await db_pool.release(conn)
        return row["last_ts"] if row else None
    except Exception:
        logger.debug("Health: last-ingest-timestamp query failed", exc_info=True)
        return None


# ── Endpoint ──────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Return application health including database connectivity,
    collector status, and source-database health.
    """
    db_pool: DatabasePool | None = getattr(request.app.state, "pool", None)
    now = datetime.now(timezone.utc)
    threshold = get_settings().heartbeat_threshold

    if db_pool is None:
        return HealthResponse(
            version=_get_version(),
            database="disconnected",
        )

    # Check basic connectivity
    try:
        conn = await db_pool.acquire()
        await db_pool.release(conn)
        db_status = "connected"
    except Exception:
        logger.warning("Health endpoint: database acquire failed", exc_info=True)
        return HealthResponse(version=_get_version(), database="disconnected")

    # Enrich with collector / source-database health when connected
    collectors = await _collector_health_summary(db_pool, now, threshold)
    source_dbs = await _source_db_health_summary(db_pool, now, threshold)
    last_ingest = await _last_ingest_timestamp(db_pool)

    return HealthResponse(
        version=_get_version(),
        database=db_status,
        last_ingest_timestamp=last_ingest,
        collectors=collectors,
        source_databases=source_dbs,
    )
