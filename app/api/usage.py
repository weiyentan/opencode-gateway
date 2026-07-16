"""Usage reporting API — aggregates, records, and session summaries.

All endpoints require API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).  Responses are automatically
wrapped in the standard envelope format.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.config import get_settings
from app.core.loki import build_loki_search_url
from app.core.schemas.usage import (
    AggregateRow,
    PaginatedResponse,
    RecordRow,
    SessionSummary,
)
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usage"])

# ── Valid group-by dimensions ─────────────────────────────────────────────

VALID_GROUP_BY: frozenset[str] = frozenset(
    {"client", "model", "session", "day", "week", "month"}
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


def _validate_date_range(start_date: datetime, end_date: datetime) -> None:
    """Raise 400 if start is after end."""
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date must not be after end_date",
        )


def _parse_group_by(raw: str | None) -> list[str]:
    """Parse and validate a comma-separated group_by string.

    Returns an empty list when *raw* is ``None`` or empty.
    Raises ``HTTPException(400)`` on invalid values.
    """
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in VALID_GROUP_BY]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by value(s): {', '.join(invalid)}. "
            f"Valid values: {', '.join(sorted(VALID_GROUP_BY))}",
        )
    return parts


def _group_expression(parts: list[str]) -> str:
    """Build a SQL GROUP BY expression and corresponding select expression.

    Each part contributes a fragment.  When there are multiple parts the
    group value is concatenated with a pipe separator.
    """
    fragments: list[str] = []
    for part in parts:
        if part == "client":
            fragments.append("oc.name")
        elif part == "model":
            fragments.append("om.model_name")
        elif part == "session":
            fragments.append("CAST(our.session_id AS text)")
        elif part == "day":
            fragments.append("date_trunc('day', our.reported_at)::text")
        elif part == "week":
            fragments.append("date_trunc('week', our.reported_at)::text")
        elif part == "month":
            fragments.append("date_trunc('month', our.reported_at)::text")

    if len(fragments) == 1:
        return fragments[0]
    return " || '|' || ".join(fragments)


# ── Aggregate helpers ─────────────────────────────────────────────────────


def _build_aggregate_filters(
    client_id: uuid.UUID | None,
    model: str | None,
    session_id: uuid.UUID | None,
) -> tuple[str, list]:
    """Build WHERE clause fragments and parameter list for aggregate queries.

    Returns ``(where_clause, params)``.  The WHERE clause always includes
    the date-range placeholders ``$1`` and ``$2``, followed by optional
    filters.
    """
    params: list = []
    filters: list[str] = []

    # Date range is always present (positional: $1, $2)
    filters.append("our.reported_at >= $1")
    filters.append("our.reported_at <= $2")

    if client_id is not None:
        filters.append(f"our.client_id = ${len(params) + 3}")
        params.append(client_id)

    if model is not None:
        filters.append(f"om.model_name = ${len(params) + 3}")
        params.append(model)

    if session_id is not None:
        filters.append(f"our.session_id = ${len(params) + 3}")
        params.append(session_id)

    return " AND ".join(filters), params


async def _fetch_aggregates(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    client_id: uuid.UUID | None,
    model: str | None,
    session_id: uuid.UUID | None,
    group_parts: list[str],
) -> list[AggregateRow]:
    """Execute the aggregates query and return typed rows."""
    where_clause, params = _build_aggregate_filters(
        client_id, model, session_id
    )
    query_params = [start_date, end_date, *params]

    if not group_parts:
        # Single total row
        sql = f"""
            SELECT
                'total' AS group_value,
                COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
                SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
                COUNT(*) AS record_count
            FROM opencode_usage_records our
            JOIN observed_models om ON om.id = our.model_id
            LEFT JOIN opencode_clients oc ON oc.id = our.client_id
            WHERE {where_clause}
        """
        row = await conn.fetchrow(sql, *query_params)
        return [
            AggregateRow(
                group_value="total",
                total_input_tokens=row["total_input_tokens"] if row else 0,
                total_output_tokens=row["total_output_tokens"] if row else 0,
                total_cached_tokens=row["total_cached_tokens"] if row else 0,
                total_estimated_cost_usd=(
                    row["total_estimated_cost_usd"] if row else Decimal("0")
                ),
                record_count=row["record_count"] if row else 0,
            )
        ]

    group_expr = _group_expression(group_parts)
    sql = f"""
        SELECT
            {group_expr} AS group_value,
            COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
            SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
            COUNT(*) AS record_count
        FROM opencode_usage_records our
        JOIN observed_models om ON om.id = our.model_id
        LEFT JOIN opencode_clients oc ON oc.id = our.client_id
        WHERE {where_clause}
        GROUP BY {group_expr}
        ORDER BY group_value
    """
    rows = await conn.fetch(sql, *query_params)
    return [
        AggregateRow(
            group_value=str(r["group_value"]),
            total_input_tokens=r["total_input_tokens"],
            total_output_tokens=r["total_output_tokens"],
            total_cached_tokens=r["total_cached_tokens"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            record_count=r["record_count"],
        )
        for r in rows
    ]


# ── Records helpers ────────────────────────────────────────────────────────


def _build_record_filters(
    client_id: uuid.UUID | None,
    model: str | None,
    session_id: uuid.UUID | None,
) -> tuple[str, list]:
    """Build WHERE clause and params for record-level queries.

    Uses 1-indexed placeholders with an offset for the two date params
    that are always prepended (``$1``, ``$2``).
    """
    params: list = []
    filters: list[str] = []
    filters.append("our.reported_at >= $1")
    filters.append("our.reported_at <= $2")

    if client_id is not None:
        filters.append(f"our.client_id = ${len(params) + 3}")
        params.append(client_id)
    if model is not None:
        filters.append(f"om.model_name = ${len(params) + 3}")
        params.append(model)
    if session_id is not None:
        filters.append(f"our.session_id = ${len(params) + 3}")
        params.append(session_id)

    return " AND ".join(filters), params


def _validate_sort(sort_by: str, sort_dir: str) -> tuple[str, str]:
    """Validate and normalise sort parameters; raise 400 on invalid values."""
    sort_by = sort_by.strip().lower()
    if sort_by not in ("reported_at", "ingested_at"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort_by: '{sort_by}'. "
            f"Must be 'reported_at' or 'ingested_at'.",
        )
    sort_dir = sort_dir.strip().lower()
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort_dir: '{sort_dir}'. "
            f"Must be 'asc' or 'desc'.",
        )
    return sort_by, sort_dir


async def _fetch_records(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    client_id: uuid.UUID | None,
    model: str | None,
    session_id: uuid.UUID | None,
    limit: int,
    offset: int,
    sort_by: str,
    sort_dir: str,
    grafana_base_url: str,
) -> PaginatedResponse[RecordRow]:
    """Execute count + data queries and return a paginated response."""
    where_clause, params = _build_record_filters(
        client_id, model, session_id
    )
    query_params = [start_date, end_date, *params]

    # Total count
    count_sql = f"""
        SELECT COUNT(*)
        FROM opencode_usage_records our
        JOIN observed_models om ON om.id = our.model_id
        WHERE {where_clause}
    """
    total = await conn.fetchval(count_sql, *query_params)

    # Data query
    order_col = "our.reported_at" if sort_by == "reported_at" else "our.ingested_at"
    data_sql = f"""
        SELECT
            our.id,
            our.client_id,
            our.source_database_id,
            our.session_id,
            om.model_name,
            our.input_tokens,
            our.output_tokens,
            our.cached_tokens,
            our.estimated_cost_usd,
            our.reported_at,
            our.ingested_at
        FROM opencode_usage_records our
        JOIN observed_models om ON om.id = our.model_id
        WHERE {where_clause}
        ORDER BY {order_col} {sort_dir}
        LIMIT ${len(query_params) + 1}
        OFFSET ${len(query_params) + 2}
    """
    rows = await conn.fetch(data_sql, *query_params, limit, offset)

    items = [
        RecordRow(
            id=r["id"],
            client_id=r["client_id"],
            source_database_id=r["source_database_id"],
            session_id=r["session_id"],
            model_name=r["model_name"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            cached_tokens=r["cached_tokens"],
            estimated_cost_usd=r["estimated_cost_usd"],
            reported_at=r["reported_at"],
            ingested_at=r["ingested_at"],
            loki_search_url=build_loki_search_url(
                client_id=r["client_id"],
                source_database_id=r["source_database_id"],
                session_id=r["session_id"],
                start_time=start_date,
                end_time=end_date,
                grafana_base_url=grafana_base_url,
            ),
        )
        for r in rows
    ]

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Session helpers ────────────────────────────────────────────────────────


async def _fetch_sessions(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    client_id: uuid.UUID | None,
    limit: int,
    offset: int,
    grafana_base_url: str,
) -> PaginatedResponse[SessionSummary]:
    """Return sessions whose interval overlaps *start_date*–*end_date*.

    Uses interval-overlap semantics: a session is included when
    ``first_message_at <= end_date`` **and** ``last_message_at >= start_date``.
    This captures sessions that started before the query range but were still
    active during it.
    """
    params: list = []

    filters: list[str] = []
    # Overlap: session started before or at range end
    filters.append("s.first_message_at <= $2")
    # Session ended after range start, or is still ongoing
    filters.append("(s.last_message_at >= $1 OR s.last_message_at IS NULL)")

    if client_id is not None:
        filters.append(f"s.client_id = ${len(params) + 3}")
        params.append(client_id)

    where_clause = " AND ".join(filters)
    query_params = [start_date, end_date, *params]

    # Total count
    count_sql = f"SELECT COUNT(*) FROM sessions s WHERE {where_clause}"
    total = await conn.fetchval(count_sql, *query_params)

    # Data query
    data_sql = f"""
        SELECT
            s.id,
            s.client_id,
            s.source_database_id,
            s.first_message_at,
            s.last_message_at,
            s.message_count,
            s.total_input_tokens,
            s.total_output_tokens,
            s.total_cached_tokens,
            s.total_estimated_cost_usd
        FROM sessions s
        WHERE {where_clause}
        ORDER BY s.last_message_at DESC
        LIMIT ${len(query_params) + 1}
        OFFSET ${len(query_params) + 2}
    """
    rows = await conn.fetch(data_sql, *query_params, limit, offset)

    items = [
        SessionSummary(
            id=r["id"],
            client_id=r["client_id"],
            source_database_id=r["source_database_id"],
            first_message_at=r["first_message_at"],
            last_message_at=r["last_message_at"],
            message_count=r["message_count"],
            total_input_tokens=r["total_input_tokens"],
            total_output_tokens=r["total_output_tokens"],
            total_cached_tokens=r["total_cached_tokens"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            loki_search_url=build_loki_search_url(
                client_id=r["client_id"],
                source_database_id=r["source_database_id"],
                session_id=r["id"],
                start_time=start_date,
                end_time=end_date,
                grafana_base_url=grafana_base_url,
            ),
        )
        for r in rows
    ]

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/aggregates")
async def get_aggregates(
    request: Request,
    start_date: datetime = Query(..., description="ISO-8601 start date (inclusive)"),
    end_date: datetime = Query(..., description="ISO-8601 end date (inclusive)"),
    client_id: uuid.UUID | None = Query(default=None),
    model: str | None = Query(default=None),
    session_id: uuid.UUID | None = Query(default=None),
    group_by: str | None = Query(
        default=None,
        description="Comma-separated group-by dimensions: "
        "client,model,session,day,week,month",
    ),
    conn: asyncpg.Connection = Depends(get_session),
) -> list[AggregateRow]:
    """Return aggregated token/cost values with optional filtering and grouping.

    Without ``group_by``, returns a single total row.  With one or more
    valid dimensions, returns one row per group.
    """
    _validate_date_range(start_date, end_date)
    group_parts = _parse_group_by(group_by)
    return await _fetch_aggregates(
        conn, start_date, end_date, client_id, model, session_id, group_parts
    )


@router.get("/records")
async def get_records(
    request: Request,
    start_date: datetime = Query(..., description="ISO-8601 start date (inclusive)"),
    end_date: datetime = Query(..., description="ISO-8601 end date (inclusive)"),
    client_id: uuid.UUID | None = Query(default=None),
    model: str | None = Query(default=None),
    session_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort_by: str = Query(default="reported_at"),
    sort_dir: str = Query(default="desc"),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[RecordRow]:
    """Return paginated individual usage records with Loki drill-down URLs.

    Each record includes a ``loki_search_url`` pointing to the Grafana
    Explore view filtered to the record's client, source database, and
    session.
    """
    _validate_date_range(start_date, end_date)
    sort_by, sort_dir = _validate_sort(sort_by, sort_dir)
    settings = get_settings()
    return await _fetch_records(
        conn,
        start_date,
        end_date,
        client_id,
        model,
        session_id,
        limit,
        offset,
        sort_by,
        sort_dir,
        settings.grafana_base_url,
    )


@router.get("/sessions")
async def get_sessions(
    request: Request,
    start_date: datetime = Query(..., description="ISO-8601 start date (inclusive)"),
    end_date: datetime = Query(..., description="ISO-8601 end date (inclusive)"),
    client_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[SessionSummary]:
    """Return paginated session-level summaries with Loki drill-down URLs.

    Each session summary includes a ``loki_search_url`` for drill-down
    into that session's logs.
    """
    _validate_date_range(start_date, end_date)
    settings = get_settings()
    return await _fetch_sessions(
        conn,
        start_date,
        end_date,
        client_id,
        limit,
        offset,
        settings.grafana_base_url,
    )
