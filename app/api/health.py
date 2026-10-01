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
from app.core.telemetry import timed_operation
from app.db.session import DatabasePool

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["health"])

# The collector-health summary is restricted to OpenCode Clients whose
# registered name begins with this prefix. Integration identities (e.g.
# "awx-execution-bindings", "watcher-dispatcher") are excluded by
# construction — an include-prefix rule filters new integration clients
# without maintaining a denylist.
REMOTE_COLLECTOR_CLIENT_PREFIX = "remote-collector"



def _get_version() -> str:
    """Return the installed package version, or a sensible fallback."""
    try:
        return version("opencode-gateway")
    except PackageNotFoundError:
        return "0.1.0-dev"


# ── Sub-models ────────────────────────────────────────────────────────────


class CollectorHealth(BaseModel):
    """Health status for one remote-collector client (client-level row).

    The monitored identity is the OpenCode Client, not any individual
    Collector Credential: liveness and record totals are aggregated across
    the client's non-revoked credentials and watched source databases
    (issue #750).
    """

    client_id: str = Field(description="UUID of the opencode_clients row")
    client_name: str = Field(description="Name of the OpenCode client")
    last_heartbeat: Optional[datetime] = Field(
        default=None,
        description="Most recent ingest activity across the client's "
        "credentials and watched source databases",
    )
    total_records_ingested: int = Field(
        default=0,
        description="Total records ingested via the client's credentials",
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
    """Aggregate collector health at client level (issue #750).

    One row per qualifying remote-collector client. The signal rows carry,
    per non-revoked Collector Credential, that credential's most recent
    heartbeat (Remote Collector Heartbeat = empty ingest batch, or
    data-bearing ingest) and record total; UNION ALL one row per active
    watched source database carrying its last_seen_at. Aggregation is
    read-time: last_heartbeat is the MAX across all of the client's
    signals, total_records_ingested is the SUM across the client's
    credentials.
    """
    try:
        conn = await db_pool.acquire()
        try:
            async with timed_operation("db.query.health.collectors", "db"):
                rows = await conn.fetch("""
                    SELECT
                        cc.id            AS credential_id,
                        cc.client_id     AS client_id,
                        c.name           AS client_name,
                        (
                            SELECT MAX(ib.ingested_at)
                            FROM ingest_batches ib
                            WHERE ib.collector_credential_id = cc.id
                        ) AS credential_last_heartbeat,
                        COALESCE((
                            SELECT SUM(ib.record_count)
                            FROM ingest_batches ib
                            WHERE ib.collector_credential_id = cc.id
                        ), 0) AS credential_records,
                        NULL::timestamptz AS source_last_seen
                    FROM collector_credentials cc
                    JOIN opencode_clients c ON c.id = cc.client_id
                    WHERE cc.revoked_at IS NULL
                    UNION ALL
                    SELECT
                        NULL::uuid       AS credential_id,
                        sd.client_id     AS client_id,
                        c.name           AS client_name,
                        NULL::timestamptz AS credential_last_heartbeat,
                        0                AS credential_records,
                        sd.last_seen_at  AS source_last_seen
                    FROM source_databases sd
                    JOIN opencode_clients c ON c.id = sd.client_id
                    WHERE sd.is_active = true
                """)
        finally:
            await db_pool.release(conn)
    except Exception:
        logger.debug("Health: collector summary query failed", exc_info=True)
        return []

    # Group signal rows by client, keeping only qualifying remote-collector
    # clients (issue #749 prefix rule, unchanged).
    by_client: dict[str, dict] = {}
    for r in rows:
        if not str(r["client_name"]).startswith(REMOTE_COLLECTOR_CLIENT_PREFIX):
            continue
        client_key = str(r["client_id"])
        entry = by_client.setdefault(
            client_key,
            {
                "client_id": client_key,
                "client_name": r["client_name"],
                "last_heartbeat": None,
                "total_records_ingested": 0,
            },
        )
        for signal in (r["credential_last_heartbeat"], r["source_last_seen"]):
            if signal is not None and (
                entry["last_heartbeat"] is None or signal > entry["last_heartbeat"]
            ):
                entry["last_heartbeat"] = signal
        entry["total_records_ingested"] += r["credential_records"]

    return [
        CollectorHealth(
            client_id=e["client_id"],
            client_name=e["client_name"],
            last_heartbeat=e["last_heartbeat"],
            total_records_ingested=e["total_records_ingested"],
            health=_derive_health(e["last_heartbeat"], now, threshold),
        )
        for e in sorted(by_client.values(), key=lambda e: e["client_name"])
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
            async with timed_operation("db.query.health.source_databases", "db"):
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
            async with timed_operation("db.query.health.last_ingest", "db"):
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
