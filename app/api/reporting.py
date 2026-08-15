"""Reporting read-only REST API (issue #484) — the first report.

Three GET endpoints under the versioned namespace expose the reporting
read-model persisted by ``app/api/reporting_ingest.py`` (issue #479,
migration 0031):

- ``GET /resources``        — list ingested resources, filterable by stable
  resource identity (``provider`` + ``repository_url`` + ``resource_type``
  + ``resource_number``); paginated; each item carries the current
  aggregate (verbatim payload + delivery counts).
- ``GET /resources/detail`` — full detail for one resource addressed by the
  four identity components: current aggregate + per-delivery state trail
  + session links.
- ``GET /session-links``    — the session links that currently exist
  (``afk_run_sessions``), surfaced as provisional/inferred with empty
  ``source_references`` until exact correlation (#481) lands.

Guarantees (PRD #478 Implementation Decision 13):

- **Strictly read-only** — no mutating endpoints; this router only serves
  ``GET``.  The write path remains ``app/api/reporting_ingest.py``.
- **No completion claims** — resource response shapes carry the resource's
  verbatim current payload and delivery lifecycle states; the Gateway never
  derives or asserts a "completed"/"finished"/outcome state for a resource.
- **Exact session links degrade explicitly** — until #481 provides exact
  many-to-many correlation, every session link is marked ``provisional``
  with an empty ``source_references`` list.  No link is ever invented.

Resource identity is currently derived at read time from each delivery's
``payload["resource"]`` object (the verbatim producer payload).  When the
current-aggregate layer (#480) lands with a normalized
``reporting_resource_aggregates`` table, this read path is structured so
those aggregates slot in (the ``ResourceSummary`` shape already carries
``payload`` / ``last_delivery_id`` / ``last_ingested_at``).

All responses use the ``{status, data, error}`` envelope and are protected
by the global :class:`~app.core.auth.ApiKeyMiddleware`, following the
``app/api/afk_outcomes.py`` convention: raw asyncpg via
``Depends(get_session)``, explicit-column SELECTs, parameterised filters,
and the ``_db_timeout`` / ``_request_timeout`` helpers.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.config import get_settings
from app.core.schemas.reporting import (
    ReportingSessionLink,
    ResourceDetail,
    ResourceSummary,
    StateTrailEntry,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation, timeout_operation
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/reporting", tags=["reporting"])

# ── JSONB extraction expressions (stable resource identity from payload) ─────

_RES_REPO = "jsonb_extract_path_text(d.payload, 'resource', 'repository_url')"
_RES_TYPE = "jsonb_extract_path_text(d.payload, 'resource', 'resource_type')"
_RES_NUM = "jsonb_extract_path_text(d.payload, 'resource', 'resource_number')"

# Every resource-bearing delivery must carry a complete ``resource`` object.
_BASE_RESOURCE_CONDITION = (
    "d.payload ? 'resource'"
    f" AND {_RES_REPO} IS NOT NULL"
    f" AND {_RES_TYPE} IS NOT NULL"
    f" AND {_RES_NUM} IS NOT NULL"
)


# ── Timeout helpers (mirror app/api/afk_outcomes.py) ─────────────────────────


@contextlib.asynccontextmanager
async def _db_timeout(
    event_name: str, db_timeout_seconds: int
) -> AsyncIterator[None]:
    """Wrap a database query with the configured per-query timeout budget."""
    async with timeout_operation(
        event_name, "db", budget_ms=db_timeout_seconds * 1000
    ):
        yield


@contextlib.asynccontextmanager
async def _request_timeout(
    total_request_timeout_seconds: int,
) -> AsyncIterator[None]:
    """Wrap an endpoint handler body with the total request timeout budget."""
    async with timeout_operation(
        "request.total", "request",
        budget_ms=total_request_timeout_seconds * 1000,
    ):
        yield


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_identity_filters(
    provider: str | None,
    repository_url: str | None,
    resource_type: str | None,
    resource_number: str | None,
) -> tuple[str, list[object]]:
    """Build a WHERE clause and parameter list for resource identity filters.

    The optional identity components are appended in the fixed order
    ``provider``, ``repository_url``, ``resource_type``,
    ``resource_number`` so parameter positions are deterministic.
    """
    params: list[object] = []
    filters: list[str] = [_BASE_RESOURCE_CONDITION]

    if provider is not None:
        filters.append(f"d.provider = ${len(params) + 1}")
        params.append(provider)
    if repository_url is not None:
        filters.append(f"{_RES_REPO} = ${len(params) + 1}")
        params.append(repository_url)
    if resource_type is not None:
        filters.append(f"{_RES_TYPE} = ${len(params) + 1}")
        params.append(resource_type)
    if resource_number is not None:
        filters.append(f"{_RES_NUM} = ${len(params) + 1}")
        params.append(resource_number)

    return " AND ".join(filters), params


def _resource_id(
    provider: str, repository_url: str, resource_type: str, resource_number: str
) -> str:
    """The composite stable resource identity key (producer partition-key)."""
    return f"{provider}:{repository_url}:{resource_type}:{resource_number}"


def _summary_from_row(row: asyncpg.Record) -> ResourceSummary:
    """Build a :class:`ResourceSummary` from a delivery-derived row."""
    provider = row["provider"]
    repository_url = row["repository_url"]
    resource_type = row["resource_type"]
    resource_number = row["resource_number"]
    return ResourceSummary(
        resource_id=_resource_id(
            provider, repository_url, resource_type, resource_number
        ),
        provider=provider,
        repository_url=repository_url,
        resource_type=resource_type,
        resource_number=resource_number,
        delivery_count=row["delivery_count"],
        last_delivery_id=row["last_delivery_id"],
        last_ingested_at=row["last_ingested_at"],
        payload=row["payload"] or {},
    )


# ── Query helpers ────────────────────────────────────────────────────────────


async def _fetch_resources(
    conn: asyncpg.Connection,
    provider: str | None,
    repository_url: str | None,
    resource_type: str | None,
    resource_number: str | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[ResourceSummary]:
    """Execute count + data queries for the list-resources endpoint."""
    where_clause, params = _build_identity_filters(
        provider, repository_url, resource_type, resource_number
    )

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT d.provider, {_RES_REPO}, {_RES_TYPE}, {_RES_NUM}
            FROM reporting_deliveries d
            WHERE {where_clause}
        ) grouped_resources
    """
    async with timed_operation("db.query.reporting.resources.count", "db"):
        async with _db_timeout("db.query.reporting.resources.count", db_timeout_seconds):
            total = await conn.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT DISTINCT ON (
                 d.provider, {_RES_REPO}, {_RES_TYPE}, {_RES_NUM}
               )
               d.provider,
               {_RES_REPO} AS repository_url,
               {_RES_TYPE} AS resource_type,
               {_RES_NUM} AS resource_number,
               d.delivery_id AS last_delivery_id,
               d.received_at AS last_ingested_at,
               d.payload,
               (SELECT COUNT(*) FROM reporting_deliveries d2
                 WHERE d2.provider = d.provider
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'repository_url')
                       = {_RES_REPO}
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'resource_type')
                       = {_RES_TYPE}
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'resource_number')
                       = {_RES_NUM}
               ) AS delivery_count
        FROM reporting_deliveries d
        WHERE {where_clause}
        ORDER BY d.provider, {_RES_REPO}, {_RES_TYPE}, {_RES_NUM}, d.received_at DESC
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.reporting.resources.data", "db"):
        async with _db_timeout("db.query.reporting.resources.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [_summary_from_row(r) for r in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_resource_detail(
    conn: asyncpg.Connection,
    provider: str,
    repository_url: str,
    resource_type: str,
    resource_number: str,
    *,
    db_timeout_seconds: int,
) -> ResourceDetail | None:
    """Compose the full detail for one resource (2 queries, no N+1)."""
    where_clause, params = _build_identity_filters(
        provider, repository_url, resource_type, resource_number
    )

    resource_sql = f"""
        SELECT d.provider,
               {_RES_REPO} AS repository_url,
               {_RES_TYPE} AS resource_type,
               {_RES_NUM} AS resource_number,
               d.delivery_id AS last_delivery_id,
               d.received_at AS last_ingested_at,
               d.payload,
               (SELECT COUNT(*) FROM reporting_deliveries d2
                 WHERE d2.provider = d.provider
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'repository_url')
                       = {_RES_REPO}
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'resource_type')
                       = {_RES_TYPE}
                   AND jsonb_extract_path_text(d2.payload, 'resource', 'resource_number')
                       = {_RES_NUM}
               ) AS delivery_count
        FROM reporting_deliveries d
        WHERE {where_clause}
        ORDER BY d.received_at DESC
        LIMIT 1
    """
    async with timed_operation("db.query.reporting.resource.detail.resource", "db"):
        async with _db_timeout(
            "db.query.reporting.resource.detail.resource", db_timeout_seconds
        ):
            resource_row = await conn.fetchrow(resource_sql, *params)

    if resource_row is None:
        return None

    trail_sql = """
        SELECT t.provider, t.delivery_id, t.state, t.occurred_at, t.detail,
               t.created_at
        FROM delivery_state_trails t
        JOIN reporting_deliveries d
          ON d.provider = t.provider AND d.delivery_id = t.delivery_id
        WHERE d.provider = $1
          AND jsonb_extract_path_text(d.payload, 'resource', 'repository_url') = $2
          AND jsonb_extract_path_text(d.payload, 'resource', 'resource_type') = $3
          AND jsonb_extract_path_text(d.payload, 'resource', 'resource_number') = $4
        ORDER BY t.occurred_at, t.created_at, t.state
    """
    async with timed_operation("db.query.reporting.resource.detail.trail", "db"):
        async with _db_timeout(
            "db.query.reporting.resource.detail.trail", db_timeout_seconds
        ):
            trail_rows = await conn.fetch(
                trail_sql, provider, repository_url, resource_type, resource_number
            )

    state_trail = [
        StateTrailEntry(
            provider=r["provider"],
            delivery_id=r["delivery_id"],
            state=r["state"],
            occurred_at=r["occurred_at"],
            detail=r["detail"],
            created_at=r["created_at"],
        )
        for r in trail_rows
    ]

    return ResourceDetail(
        resource=_summary_from_row(resource_row),
        state_trail=state_trail,
        # No provable resource↔session link exists yet (exact correlation
        # #481 is not implemented) — the field is present but empty, never
        # fabricated from heuristics.
        session_links=[],
    )


async def _fetch_session_links(
    conn: asyncpg.Connection,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[ReportingSessionLink]:
    """Execute count + data queries for the list-session-links endpoint."""
    async with timed_operation("db.query.reporting.session_links.count", "db"):
        async with _db_timeout(
            "db.query.reporting.session_links.count", db_timeout_seconds
        ):
            total = await conn.fetchval("SELECT COUNT(*) FROM afk_run_sessions")

    data_sql = """
        SELECT ars.afk_run_id, ars.session_id, ars.external_session_id,
               ars.started_at, ars.finished_at, s.agent
        FROM afk_run_sessions ars
        LEFT JOIN sessions s ON s.id = ars.session_id
        ORDER BY ars.started_at NULLS LAST
        LIMIT $1 OFFSET $2
    """
    async with timed_operation("db.query.reporting.session_links.data", "db"):
        async with _db_timeout(
            "db.query.reporting.session_links.data", db_timeout_seconds
        ):
            rows = await conn.fetch(data_sql, limit, offset)

    items = [
        ReportingSessionLink(
            session_id=str(r["session_id"]) if r["session_id"] else None,
            external_session_id=r["external_session_id"],
            afk_run_id=r["afk_run_id"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            agent=r["agent"],
            provisional=True,
            source_references=[],
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/resources")
async def list_resources(
    request: Request,
    provider: str | None = Query(default=None),
    repository_url: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_number: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ResourceSummary]:
    """List ingested resources, filterable by stable resource identity."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_resources(
            conn,
            provider,
            repository_url,
            resource_type,
            resource_number,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/resources/detail")
async def get_resource_detail(
    request: Request,
    provider: str = Query(...),
    repository_url: str = Query(...),
    resource_type: str = Query(...),
    resource_number: str = Query(...),
    conn: asyncpg.Connection = Depends(get_session),
) -> ResourceDetail:
    """Return the full detail for one resource addressed by stable identity."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        detail = await _fetch_resource_detail(
            conn,
            provider,
            repository_url,
            resource_type,
            resource_number,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Reporting resource not found: {provider}:{repository_url}:"
                f"{resource_type}:{resource_number}"
            ),
        )
    return detail


@router.get("/session-links")
async def list_session_links(
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ReportingSessionLink]:
    """List session links (provisional until exact correlation #481 lands)."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_session_links(
            conn,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
