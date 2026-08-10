"""Usage reporting API — aggregates, records, and session summaries.

All endpoints require API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).  Responses are automatically
wrapped in the standard envelope format.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.config import get_settings
from app.core.formatting import format_model_output
from app.core.loki import build_loki_search_url
from app.core.schemas.usage import (
    AgentRunDetail,
    AgentRunSummary,
    AggregateRow,
    PaginatedResponse,
    RecordRow,
    RecordWithContextGroupedRow,
    SessionSummary,
)
from app.core.telemetry import timed_operation, timeout_operation
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usage"])

# ── Timeout helpers ────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _db_timeout(
    event_name: str,
    db_timeout_seconds: int,
) -> contextlib.AbstractAsyncContextManager[None]:
    """Wrap a database query with the configured per-query timeout budget."""
    async with timeout_operation(
        event_name, "db", budget_ms=db_timeout_seconds * 1000
    ):
        yield


@contextlib.asynccontextmanager
async def _status_timeout(
    status_timeout_seconds: int,
) -> contextlib.AbstractAsyncContextManager[None]:
    """Wrap a _compute_status call with its configured timeout budget.

    .. note::
        ``_compute_status`` is a pure, synchronous, O(1) function — it
        contains no await points.  Timeout machinery (``asyncio.timeout``
        and the 3.9 backport) can only cancel at an await point, so the
        status budget can never actually fire.  The wrapper satisfies the
        letter of AC4 and provides a place to drop an await point later
        (e.g. if status derivation moves into the database).
    """
    async with timeout_operation(
        "status.compute", "compute", budget_ms=status_timeout_seconds * 1000
    ):
        yield


@contextlib.asynccontextmanager
async def _request_timeout(
    total_request_timeout_seconds: int,
) -> contextlib.AbstractAsyncContextManager[None]:
    """Wrap an endpoint handler body with the total request timeout budget."""
    async with timeout_operation(
        "request.total", "request",
        budget_ms=total_request_timeout_seconds * 1000,
    ):
        yield


# ── Valid group-by dimensions ─────────────────────────────────────────────

VALID_GROUP_BY: frozenset[str] = frozenset(
    {"client", "model", "session", "day", "week", "month", "project"}
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
        elif part == "project":
            fragments.append(f"({_PROJECT_LABEL_SQL})")

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


# Project-label resolution SQL for the rollup read path.  Mirrors
# ``_PROJECT_LABEL_SQL`` but adapted for ``client_project_rollup``, which
# has no ``sessions`` join — ``r.project_id`` is the external project ID,
# and ``opencode_source_projects`` is matched via a LATERAL subquery.
_ROLLUP_PROJECT_LABEL_SQL = """
    COALESCE(
        osp.display_name,
        osp.name,
        CASE
            WHEN osp.worktree IS NULL
                  OR osp.worktree = ''
                  OR osp.worktree = '/' THEN NULL
            ELSE substring(osp.worktree, '([^/]+)$')
        END,
        CASE
            WHEN r.project_id IS NULL THEN NULL
            WHEN length(r.project_id) > 12 THEN substring(r.project_id, 1, 12) || '…'
            ELSE r.project_id
        END,
        'unknown'
    )
"""


async def _fetch_aggregates_rollup(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    client_id: uuid.UUID | None,
    db_timeout_seconds: int,
) -> list[AggregateRow]:
    """Execute the client,project aggregates as a hybrid read.

    Reads pre-aggregated additive totals from ``client_project_rollup``
    (ADR 0015) and resolves the human-readable project label at read time
    from ``opencode_source_projects`` via a LATERAL join.  Counts
    (record, session, model) are derived from a second distinct-count
    query over raw ``usage_events`` grouped by the same
    (client, project-label) dimension, then merged in Python.
    """
    # ── Rollup query (additive token/cost totals) ────────────────────
    params: list = [start_date, end_date]
    filters: list[str] = [
        "r.day >= $1",
        "r.day <= $2",
    ]

    if client_id is not None:
        filters.append(f"r.client_id = ${len(params) + 1}")
        params.append(client_id)

    where_clause = " AND ".join(filters)

    rollup_sql = f"""
        WITH rollup_with_label AS (
            SELECT
                r.client_id,
                r.input_tokens,
                r.output_tokens,
                r.cache_read_tokens,
                r.cache_write_tokens,
                r.estimated_cost_usd,
                ({_ROLLUP_PROJECT_LABEL_SQL}) AS project_label
            FROM client_project_rollup r
            LEFT JOIN LATERAL (
                SELECT osp.display_name, osp.name, osp.worktree
                FROM opencode_source_projects osp
                WHERE osp.client_id = r.client_id
                  AND osp.external_project_id = r.project_id
                LIMIT 1
            ) osp ON true
            WHERE {where_clause}
        )
        SELECT
            COALESCE(oc.canonical_name, oc.name) || '|' || rl.project_label AS group_value,
            SUM(rl.input_tokens)::bigint AS total_input_tokens,
            SUM(rl.output_tokens)::bigint AS total_output_tokens,
            0::bigint AS total_cached_tokens,
            0::bigint AS total_reasoning_tokens,
            SUM(rl.cache_read_tokens)::bigint AS total_cache_read_tokens,
            SUM(rl.cache_write_tokens)::bigint AS total_cache_write_tokens,
            SUM(rl.estimated_cost_usd) AS total_estimated_cost_usd,
            0 AS record_count,
            0 AS session_count,
            0 AS model_count,
            rl.project_label
        FROM rollup_with_label rl
        JOIN opencode_clients oc ON oc.id = rl.client_id
        GROUP BY COALESCE(oc.canonical_name, oc.name), rl.project_label
        ORDER BY group_value
    """
    async with timed_operation("db.query.aggregates.client_project_rollup", "db"):
        async with _db_timeout(
            "db.query.aggregates.client_project_rollup", db_timeout_seconds
        ):
            rollup_rows = await conn.fetch(rollup_sql, *params)

    # ── Count query (distinct counts over raw usage_events) ──────────
    # The rollup stores only additive token/cost totals (ADR 0015);
    # record_count, session_count, and model_count are derived from a
    # separate distinct-count scan over raw ``usage_events``, grouped by
    # the same (client, project-label) dimension.
    #
    # The count query MUST resolve the project label through the SAME
    # ``(client_id, external_project_id)`` LATERAL lookup the rollup
    # query uses.  Keying on ``usage_events.project_id`` (NOT
    # ``sessions.project_id``) and matching against
    # ``opencode_source_projects`` via ``(client_id, external_project_id)``
    # produces byte-identical ``group_value`` strings to the rollup query.
    # This prevents count-zeroing when one client reports the same project
    # from multiple source databases (the canonical-client scenario) or
    # when ``usage_events.project_id`` differs from
    # ``sessions.project_id``.  The constant ``_ROLLUP_PROJECT_LABEL_SQL``
    # references ``r.project_id`` and ``osp`` — aliasing ``usage_events``
    # as ``r`` lets the same constant serve both queries.
    count_params: list = [start_date, end_date]
    count_filters: list[str] = [
        "r.reported_at >= $1",
        "r.reported_at <= $2",
        "r.project_id IS NOT NULL",
    ]
    if client_id is not None:
        count_filters.append(f"r.client_id = ${len(count_params) + 1}")
        count_params.append(client_id)
    count_where = " AND ".join(count_filters)

    count_sql = f"""
        WITH usage_with_label AS (
            SELECT
                r.client_id,
                r.session_id,
                r.model_id,
                ({_ROLLUP_PROJECT_LABEL_SQL}) AS project_label
            FROM usage_events r
            LEFT JOIN LATERAL (
                SELECT osp.display_name, osp.name, osp.worktree
                FROM opencode_source_projects osp
                WHERE osp.client_id = r.client_id
                  AND osp.external_project_id = r.project_id
                LIMIT 1
            ) osp ON true
            WHERE {count_where}
        )
        SELECT
            COALESCE(oc.canonical_name, oc.name) || '|' || ul.project_label AS group_value,
            COUNT(*) AS record_count,
            COUNT(DISTINCT ul.session_id) AS session_count,
            COUNT(DISTINCT om.model_name) AS model_count
        FROM usage_with_label ul
        JOIN observed_models om ON om.id = ul.model_id
        JOIN opencode_clients oc ON oc.id = ul.client_id
        GROUP BY COALESCE(oc.canonical_name, oc.name), ul.project_label
    """
    async with timed_operation("db.query.aggregates.client_project_counts", "db"):
        async with _db_timeout(
            "db.query.aggregates.client_project_counts", db_timeout_seconds
        ):
            count_rows = await conn.fetch(count_sql, *count_params)

    # Build a lookup of counts by group_value
    count_by_group: dict[str, dict[str, int]] = {}
    for cr in count_rows:
        gv = str(cr["group_value"])
        count_by_group[gv] = {
            "record_count": cr["record_count"],
            "session_count": cr["session_count"],
            "model_count": cr["model_count"],
        }

    # ── Merge counts into rollup rows ────────────────────────────────
    result: list[AggregateRow] = []
    for r in rollup_rows:
        gv = str(r["group_value"])
        counts = count_by_group.get(gv, {
            "record_count": 0,
            "session_count": 0,
            "model_count": 0,
        })
        result.append(
            AggregateRow(
                group_value=gv,
                total_input_tokens=r["total_input_tokens"],
                total_output_tokens=r["total_output_tokens"],
                total_cached_tokens=r["total_cached_tokens"],
                total_reasoning_tokens=r["total_reasoning_tokens"],
                total_cache_read_tokens=r["total_cache_read_tokens"],
                total_cache_write_tokens=r["total_cache_write_tokens"],
                total_estimated_cost_usd=r["total_estimated_cost_usd"],
                record_count=counts["record_count"],
                session_count=counts["session_count"],
                model_count=counts["model_count"],
                project_label=r["project_label"],
            )
        )
    return result


async def _fetch_aggregates(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    client_id: uuid.UUID | None,
    model: str | None,
    session_id: uuid.UUID | None,
    group_parts: list[str],
    *,
    db_timeout_seconds: int,
) -> list[AggregateRow]:
    """Execute the aggregates query and return typed rows."""
    where_clause, params = _build_aggregate_filters(
        client_id, model, session_id
    )
    query_params = [start_date, end_date, *params]

    if not group_parts:
        # Single total row.  Audit: 1 query (fetchrow) — already optimal.
        sql = f"""
            SELECT
                'total' AS group_value,
                COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
                COALESCE(SUM(our.reasoning_tokens), 0) AS total_reasoning_tokens,
                COALESCE(SUM(our.cache_read_tokens), 0) AS total_cache_read_tokens,
                COALESCE(SUM(our.cache_write_tokens), 0) AS total_cache_write_tokens,
                SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
                COUNT(*) AS record_count,
                COUNT(DISTINCT our.session_id) AS session_count,
                COUNT(DISTINCT om.model_name) AS model_count
            FROM usage_events our
            JOIN observed_models om ON om.id = our.model_id
            LEFT JOIN opencode_clients oc ON oc.id = our.client_id
            WHERE {where_clause}
        """
        async with timed_operation("db.query.aggregates.total", "db"):
            async with _db_timeout(
                "db.query.aggregates.total", db_timeout_seconds
            ):
                row = await conn.fetchrow(sql, *query_params)
        return [
            AggregateRow(
                group_value="total",
                total_input_tokens=row["total_input_tokens"] if row else 0,
                total_output_tokens=row["total_output_tokens"] if row else 0,
                total_cached_tokens=row["total_cached_tokens"] if row else 0,
                total_reasoning_tokens=row["total_reasoning_tokens"] if row else 0,
                total_cache_read_tokens=row["total_cache_read_tokens"] if row else 0,
                total_cache_write_tokens=row["total_cache_write_tokens"] if row else 0,
                total_estimated_cost_usd=(
                    row["total_estimated_cost_usd"] if row else Decimal("0")
                ),
                record_count=row["record_count"] if row else 0,
                session_count=row["session_count"] if row else 0,
                model_count=row["model_count"] if row else 0,
            )
        ]

    # ── Hybrid read dispatch: client,project → rollup path ─────────
    # Only dispatch to the rollup when no filter that the rollup cannot
    # express is present.  The rollup stores only (client_id, project_id, day)
    # and cannot filter by model or session_id — falling back to the raw
    # usage_events scan preserves those filters (Finding 2 / PR #407 review).
    _is_client_project = set(group_parts) == {"client", "project"}
    _rollup_safe = model is None and session_id is None

    if _is_client_project and _rollup_safe:
        return await _fetch_aggregates_rollup(
            conn, start_date, end_date, client_id, db_timeout_seconds
        )

    # ── All other dimensions: raw usage_events scan ───────────────
    group_expr = _group_expression(group_parts)
    has_project = "project" in group_parts

    # Conditionally join sessions and source_projects when the
    # project dimension is in use
    sessions_join = (
        "LEFT JOIN sessions s ON s.id = our.session_id"
        if has_project
        else ""
    )
    project_join = (
        "LEFT JOIN opencode_source_projects osp "
        "  ON osp.source_database_id = s.source_database_id "
        "  AND osp.external_project_id = s.project_id"
        if has_project
        else ""
    )
    project_label_col = (
        f",\n            {_PROJECT_LABEL_SQL} AS project_label"
        if has_project
        else ""
    )

    group_by_clause = f"GROUP BY {group_expr}"
    if has_project:
        group_by_clause += f",{_PROJECT_LABEL_SQL}"

    sql = f"""
        SELECT
            {group_expr} AS group_value{project_label_col},
            COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
            COALESCE(SUM(our.reasoning_tokens), 0) AS total_reasoning_tokens,
            COALESCE(SUM(our.cache_read_tokens), 0) AS total_cache_read_tokens,
            COALESCE(SUM(our.cache_write_tokens), 0) AS total_cache_write_tokens,
            SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
            COUNT(*) AS record_count,
            COUNT(DISTINCT our.session_id) AS session_count,
            COUNT(DISTINCT om.model_name) AS model_count
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        LEFT JOIN opencode_clients oc ON oc.id = our.client_id
        {sessions_join}
        {project_join}
        WHERE {where_clause}
        {group_by_clause}
        ORDER BY group_value
    """
    async with timed_operation("db.query.aggregates.grouped", "db"):
        async with _db_timeout(
            "db.query.aggregates.grouped", db_timeout_seconds
        ):
            rows = await conn.fetch(sql, *query_params)
    return [
        AggregateRow(
            group_value=str(r["group_value"]),
            total_input_tokens=r["total_input_tokens"],
            total_output_tokens=r["total_output_tokens"],
            total_cached_tokens=r["total_cached_tokens"],
            total_reasoning_tokens=r["total_reasoning_tokens"],
            total_cache_read_tokens=r["total_cache_read_tokens"],
            total_cache_write_tokens=r["total_cache_write_tokens"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            record_count=r["record_count"],
            session_count=r["session_count"],
            model_count=r["model_count"],
            project_label=r["project_label"] if has_project else None,
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
    if sort_by not in ("reported_at", "ingested_at", "source_created_at"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort_by: '{sort_by}'. "
            f"Must be 'reported_at', 'ingested_at', or 'source_created_at'.",
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
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[RecordRow]:
    """Execute count + data queries and return a paginated response."""
    where_clause, params = _build_record_filters(
        client_id, model, session_id
    )
    query_params = [start_date, end_date, *params]

    # Total count
    count_sql = f"""
        SELECT COUNT(*)
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        WHERE {where_clause}
    """
    async with timed_operation("db.query.records.count", "db"):
        async with _db_timeout(
            "db.query.records.count", db_timeout_seconds
        ):
            total = await conn.fetchval(count_sql, *query_params)

    # Data query
    if sort_by == "source_created_at":
        order_col = "COALESCE(osc.source_created_at_tz, our.reported_at)"
    elif sort_by == "ingested_at":
        order_col = "our.first_ingested_at"
    else:
        order_col = "our.reported_at"

    data_sql = f"""
        SELECT
            our.id,
            our.client_id,
            s.source_database_id,
            our.session_id,
            om.model_name,
            our.input_tokens,
            our.output_tokens,
            our.cached_tokens,
            our.provider,
            our.mode,
            our.finish_reason,
            our.reasoning_tokens,
            our.cache_read_tokens,
            our.cache_write_tokens,
            our.estimated_cost_usd,
            our.reported_at,
            our.first_ingested_at AS ingested_at
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        JOIN sessions s ON s.id = our.session_id
        LEFT JOIN opencode_session_contexts osc
            ON osc.source_database_id = s.source_database_id
            AND osc.external_session_id = s.external_session_id
        WHERE {where_clause}
        ORDER BY {order_col} {sort_dir}
        LIMIT ${len(query_params) + 1}
        OFFSET ${len(query_params) + 2}
    """
    async with timed_operation("db.query.records.data", "db"):
        async with _db_timeout(
            "db.query.records.data", db_timeout_seconds
        ):
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
            provider=r["provider"],
            mode=r["mode"],
            finish_reason=r["finish_reason"],
            reasoning_tokens=r["reasoning_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_write_tokens=r["cache_write_tokens"],
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
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[SessionSummary]:
    """Return sessions whose interval overlaps *start_date*–*end_date*.

    Uses interval-overlap semantics: a session is included when
    ``first_message_at <= end_date`` **and** ``last_message_at >= start_date``.
    This captures sessions that started before the query range but were still
    active during it.

    Audit: 2 queries (count + data), both with LEFT JOIN for context and
    project label — no N+1 pattern found.  Columns are explicitly listed
    (``SELECT s.id, s.client_id, …``) rather than ``SELECT *``.
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
    # Total count
    count_sql = f"SELECT COUNT(*) FROM sessions s WHERE {where_clause}"
    async with timed_operation("db.query.sessions.count", "db"):
        async with _db_timeout(
            "db.query.sessions.count", db_timeout_seconds
        ):
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
            s.total_cache_read_tokens,
            s.total_cache_write_tokens,
            s.project_id,
            s.workspace_id,
            s.agent,
            s.parent_session_id,
            s.total_estimated_cost_usd,
            osc.title AS session_title,
            {_PROJECT_LABEL_SQL} AS project_label
        FROM sessions s
        LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
        LEFT JOIN opencode_source_projects osp
            ON osp.source_database_id = s.source_database_id
            AND osp.external_project_id = s.project_id
        WHERE {where_clause}
        ORDER BY s.last_message_at DESC
        LIMIT ${len(query_params) + 1}
        OFFSET ${len(query_params) + 2}
    """
    async with timed_operation("db.query.sessions.data", "db"):
        async with _db_timeout(
            "db.query.sessions.data", db_timeout_seconds
        ):
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
            total_cache_read_tokens=r["total_cache_read_tokens"],
            total_cache_write_tokens=r["total_cache_write_tokens"],
            project_id=r["project_id"],
            project_label=r["project_label"],
            workspace_id=r["workspace_id"],
            agent=r["agent"],
            parent_session_id=r["parent_session_id"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            session_title=r["session_title"],
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
        "client,model,session,day,week,month,project",
    ),
    conn: asyncpg.Connection = Depends(get_session),
) -> list[AggregateRow]:
    """Return aggregated token/cost values with optional filtering and grouping.

    Without ``group_by``, returns a single total row.  With one or more
    valid dimensions, returns one row per group.
    """
    _validate_date_range(start_date, end_date)
    group_parts = _parse_group_by(group_by)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_aggregates(
            conn, start_date, end_date, client_id, model, session_id,
            group_parts,
            db_timeout_seconds=settings.database_timeout_seconds,
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
    sort_by: str = Query(default="source_created_at"),
    sort_dir: str = Query(default="desc"),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[RecordRow]:
    """Return paginated individual usage records with Loki drill-down URLs.

    Each record includes a ``loki_search_url`` pointing to the Grafana
    Explore view filtered to the record's client, source database, and
    session.

    **Sort options**: ``source_created_at`` (default), ``reported_at``,
    ``ingested_at``.  When sorting by ``source_created_at``, the query
    uses ``COALESCE(source_created_at_tz, reported_at)`` — preferring
    the timezone-aware source-created timestamp when available, falling
    back to the collector-reported time.
    """
    _validate_date_range(start_date, end_date)
    sort_by, sort_dir = _validate_sort(sort_by, sort_dir)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
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
            db_timeout_seconds=settings.database_timeout_seconds,
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
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_sessions(
            conn,
            start_date,
            end_date,
            client_id,
            limit,
            offset,
            settings.grafana_base_url,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Agent Run helpers
# ═══════════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────────────
#  Canonical agent run status thresholds — SINGLE AUTHORITATIVE SOURCE
# ────────────────────────────────────────────────────────────────────────────
#  Both :func:`_compute_status` (Python) and :func:`_status_case_expression`
#  (SQL) implement exactly the table below. The SQL function renders its
#  interval literals from the constants beneath this block, so a threshold
#  change is made here — and only here — and both implementations pick it
#  up automatically. If you edit either function's branch logic, update
#  this table to match. Tests in tests/test_agent_runs.py pin both
#  implementations to this table.
#
#  Branch priority (first match wins):
#    1. unknown    — message_count == 0 OR last_message_at IS NULL
#    2. running    — last_message_at >= now() - 60 minutes
#    3. unknown    — last_message_at < now() - 24 hours
#    4. blocked    — parent_session_id IS NOT NULL AND beyond quiet
#                    (reached only when branches 2 and 3 missed, so the
#                    "beyond quiet" part is implied by branch order)
#    5. stale      — last_message_at >= now() - 6 hours AND no parent
#    6. completed  — fallback (beyond stale, no parent, has messages)
#
#  Boundary inclusivity is identical in both implementations:
#     * exactly 60 minutes old → running  (inclusive: ``<=`` / ``>=``)
#     * exactly 24 hours old   → NOT unknown (strict: ``>`` / ``<``)
#     * exactly 6 hours old    → stale    (inclusive: ``<=`` / ``>=``)
# ────────────────────────────────────────────────────────────────────────────

# Default quiet threshold in minutes — a session whose last message is
# within this window is considered "running" rather than "completed".
# Exposed as a module-level constant so tests can reference it.
_QUIET_THRESHOLD_MINUTES: int = 60

# Default stale threshold in hours — a session whose last message is
# older than the quiet threshold but younger than this is considered
# "stale" (observability gap) rather than "completed".
_STALE_THRESHOLD_HOURS: int = 6

# Default unknown threshold in hours — a session whose last message is
# older than this is considered "unknown" rather than "completed".
_UNKNOWN_THRESHOLD_HOURS: int = 24


def _compute_status(
    last_message_at: datetime | None,
    message_count: int,
    has_parent: bool,
    *,
    now: datetime | None = None,
) -> str:
    """Compute agent run status from available session facts.

    **Status derivation (in priority order):**

    1. **unknown** — No messages recorded (``message_count == 0``)
       OR ``last_message_at`` is ``None`` OR the session is so old it
       exceeds the unknown threshold (``_UNKNOWN_THRESHOLD_HOURS``).

    2. **running** — ``last_message_at`` is within the quiet threshold
       (``_QUIET_THRESHOLD_MINUTES``), indicating the session may still
       be producing telemetry.

    3. **blocked** — The session is beyond the quiet threshold AND has a
       ``parent_session_id``, suggesting it may be waiting on a parent.

    4. **stale** — The session is beyond the quiet threshold, has no
       parent dependency, and is not yet old enough to be considered
       completed or unknown (between quiet threshold and stale threshold,
       ``_STALE_THRESHOLD_HOURS``). Represents an observability gap
       rather than a known termination.

    5. **completed** — The session is beyond the stale threshold, has no
       parent, and has recorded messages. A best-effort terminal status.

    The quiet, stale, and unknown thresholds are module-level constants
    (``_QUIET_THRESHOLD_MINUTES``, ``_STALE_THRESHOLD_HOURS``,
    ``_UNKNOWN_THRESHOLD_HOURS``) that can be adjusted as the system's
    behaviour is tuned.

    The canonical status table (branch priority, threshold values, boundary
    inclusivity) is documented once above the constants block; this function
    and the SQL CASE expression in :func:`_status_case_expression` must
    implement it identically.
    """
    if now is None:
        now = _utcnow()

    if message_count == 0 or last_message_at is None:
        return "unknown"

    age_minutes = (now - last_message_at).total_seconds() / 60.0

    if age_minutes <= _QUIET_THRESHOLD_MINUTES:
        return "running"

    if age_minutes > _UNKNOWN_THRESHOLD_HOURS * 60:
        return "unknown"

    if has_parent:
        return "blocked"

    if age_minutes <= _STALE_THRESHOLD_HOURS * 60:
        return "stale"

    return "completed"


def _derive_title(
    agent: str | None,
    external_session_id: str | None,
) -> str | None:
    """Derive a human-readable title from agent name and external session ID.

    If neither is available, returns ``None``.  The external session ID
    is truncated to 12 characters for readability.
    """
    if agent and external_session_id:
        return f"{agent} — {external_session_id[:12]}"
    if agent:
        return agent
    if external_session_id:
        return external_session_id[:12]
    return None


def _build_agent_run_filters(
    client_id: uuid.UUID | None,
    from_date: datetime | None,
    to_date: datetime | None,
    agent: str | None,
    external_project_id: str | None,
) -> tuple[str, list]:
    """Build WHERE clause and params for agent run list queries.

    Returns ``(where_clause, params)``.  Placeholders start at ``$1``
    and increment with each filter.
    """
    params: list = []
    filters: list[str] = ["TRUE"]

    if client_id is not None:
        filters.append(f"s.client_id = ${len(params) + 1}")
        params.append(client_id)

    if from_date is not None:
        filters.append(f"s.last_message_at >= ${len(params) + 1}")
        params.append(from_date)

    if to_date is not None:
        filters.append(f"s.last_message_at <= ${len(params) + 1}")
        params.append(to_date)

    if agent is not None:
        filters.append(f"s.agent = ${len(params) + 1}")
        params.append(agent)

    if external_project_id is not None:
        filters.append(f"s.project_id = ${len(params) + 1}")
        params.append(external_project_id)

    return " AND ".join(filters), params


def _status_case_expression() -> str:
    """Return a SQL CASE expression that computes status from session columns.

    Implements the canonical status table documented above the
    ``_QUIET_THRESHOLD_MINUTES`` constants block — the single authoritative
    source for thresholds, branch priority, and boundary inclusivity.

    Mirrors the logic in :func:`_compute_status` but expressed in SQL
    so the database can filter rows by computed status. Uses ``now()`` as
    the reference time and renders its intervals from the same module-level
    constants as the Python implementation, keeping the same branch order
    (unknown → running → unknown → blocked → stale → completed) and the
    same boundary inclusivity (running/stale inclusive, unknown-old strict).
    """
    quiet_interval = f"interval '{_QUIET_THRESHOLD_MINUTES} minutes'"
    stale_interval = f"interval '{_STALE_THRESHOLD_HOURS} hours'"
    unknown_interval = f"interval '{_UNKNOWN_THRESHOLD_HOURS} hours'"
    return f"""
        CASE
            WHEN s.message_count = 0 OR s.last_message_at IS NULL THEN 'unknown'
            WHEN s.last_message_at >= now() - {quiet_interval} THEN 'running'
            WHEN s.last_message_at < now() - {unknown_interval} THEN 'unknown'
            WHEN s.parent_session_id IS NOT NULL THEN 'blocked'
            WHEN s.last_message_at >= now() - {stale_interval} THEN 'stale'
            ELSE 'completed'
        END
    """


# ── Agent Run list query ──────────────────────────────────────────────────


async def _fetch_agent_runs(
    conn: asyncpg.Connection,
    client_id: uuid.UUID | None,
    from_date: datetime | None,
    to_date: datetime | None,
    agent: str | None,
    external_project_id: str | None,
    status_filter: str | None,
    limit: int,
    offset: int,
    grafana_base_url: str,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[AgentRunSummary]:
    """Query sessions table, compute status, join child counts, return paginated.

    Audit: removed N+1 pattern for ``child_run_count`` — replaced the
    per-row correlated subquery ``(SELECT COUNT(*) FROM sessions child
    WHERE child.parent_session_id = ...)`` with a pre-computed CTE
    ``child_counts`` that aggregates once before the main query.
    2 queries (count + data), no N+1.

    Query-plan rationale: the correlated subquery version forces a nested
    loop — the inner ``SELECT COUNT(*) … WHERE parent_session_id = …``
    runs once for every row in the result set (50–1000 times per page).
    The CTE ``child_counts`` does a single ``GROUP BY parent_session_id``
    pass over ``sessions`` and the main query LEFT JOINs it at the outer
    level.  For N result rows the old plan does O(N) index scans; the new
    plan does O(1) hash-aggregate + hash-join.  ``parent_session_id`` is
    indexed, so the CTE can use an index-only scan.
    """
    from app.core.schemas.usage import AgentRunSummary

    where_clause, params = _build_agent_run_filters(
        client_id, from_date, to_date, agent, external_project_id
    )
    status_expr = _status_case_expression()

    # ── Build the status filter as a CTE wrapper ────────────────────
    if status_filter is not None:
        # Wrap in a subquery that computes status, then filter
        base_query = f"""
            SELECT * FROM (
                SELECT
                    s.id,
                    s.client_id,
                    s.source_database_id,
                    s.external_session_id,
                    s.project_id,
                    s.workspace_id,
                    s.agent,
                    s.parent_session_id,
                    s.message_count,
                    s.total_input_tokens,
                    s.total_output_tokens,
                    s.total_cached_tokens,
                    s.total_cache_read_tokens,
                    s.total_cache_write_tokens,
                    s.total_estimated_cost_usd,
                    s.last_message_at,
                    ({status_expr}) AS _status,
                    osc.title AS session_title,
                    osc.session_model AS session_model,
                    {_PROJECT_LABEL_SQL} AS project_label
                FROM sessions s
                LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
                LEFT JOIN opencode_source_projects osp
                    ON osp.source_database_id = s.source_database_id
                    AND osp.external_project_id = s.project_id
                WHERE {where_clause}
            ) sub
            WHERE sub._status = ${len(params) + 1}
        """
        params.append(status_filter)
        count_from = f"""
            FROM sessions s
            WHERE {where_clause} AND ({status_expr}) = ${len(params)}
        """
    else:
        base_query = f"""
            SELECT
                s.id,
                s.client_id,
                s.source_database_id,
                s.external_session_id,
                s.project_id,
                s.workspace_id,
                s.agent,
                s.parent_session_id,
                s.message_count,
                s.total_input_tokens,
                s.total_output_tokens,
                s.total_cached_tokens,
                s.total_cache_read_tokens,
                s.total_cache_write_tokens,
                s.total_estimated_cost_usd,
                s.last_message_at,
                ({status_expr}) AS _status,
                osc.title AS session_title,
                osc.session_model AS session_model,
                {_PROJECT_LABEL_SQL} AS project_label
            FROM sessions s
            LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
            LEFT JOIN opencode_source_projects osp
                ON osp.source_database_id = s.source_database_id
                AND osp.external_project_id = s.project_id
            WHERE {where_clause}
        """
        count_from = f"FROM sessions s WHERE {where_clause}"

    # ── Count query ─────────────────────────────────────────────────
    # ── Count query ─────────────────────────────────────────────────
    count_sql = f"SELECT COUNT(*) {count_from}"
    async with timed_operation("db.query.agent_runs.count", "db"):
        async with _db_timeout(
            "db.query.agent_runs.count", db_timeout_seconds
        ):
            total = await conn.fetchval(count_sql, *params)

    # ── Data query with CTE for child counts (no N+1) ──────────────
    data_sql = f"""
        WITH child_counts AS (
            SELECT parent_session_id, COUNT(*) AS cnt
            FROM sessions
            WHERE parent_session_id IS NOT NULL
            GROUP BY parent_session_id
        )
        SELECT s.*, COALESCE(cc.cnt, 0) AS child_run_count
        FROM ({base_query}) s
        LEFT JOIN child_counts cc
            ON cc.parent_session_id = s.external_session_id
        ORDER BY s.last_message_at DESC NULLS LAST
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.agent_runs.data", "db"):
        async with _db_timeout(
            "db.query.agent_runs.data", db_timeout_seconds
        ):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items: list[AgentRunSummary] = []
    for r in rows:
        status_val = r["_status"]
        items.append(
            AgentRunSummary(
                id=r["id"],
                external_session_id=r["external_session_id"],
                client_id=r["client_id"],
                source_database_id=r["source_database_id"],
                title=_derive_title(r["agent"], r["external_session_id"]),
                status=status_val,
                currentStatus=status_val,
                agent=r["agent"],
                project_id=r["project_id"],
                project_label=r["project_label"],
                workspace_id=r["workspace_id"],
                todo_total=0,
                todo_completed=0,
                todo_blocked=0,
                code_changes_total=0,
                total_input_tokens=r["total_input_tokens"],
                total_output_tokens=r["total_output_tokens"],
                total_cached_tokens=r["total_cached_tokens"],
                total_cache_read_tokens=r["total_cache_read_tokens"],
                total_cache_write_tokens=r["total_cache_write_tokens"],
                total_estimated_cost_usd=r["total_estimated_cost_usd"],
                message_count=r["message_count"],
                last_updated_at=r["last_message_at"],
                child_run_count=r["child_run_count"],
                session_title=r["session_title"],
                model=format_model_output(r["session_model"]),
            )
        )

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Agent Run detail query ────────────────────────────────────────────────


async def _fetch_agent_run_detail(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    grafana_base_url: str,
    *,
    db_timeout_seconds: int,
    status_timeout_seconds: int,
) -> AgentRunDetail:
    """Fetch a single agent run detail by internal session UUID.

    Optimized from 5 sequential queries to 3:
    1. Session row + context (LEFT JOIN) + parent internal ID (correlated subquery)
    2. Child sessions
    3. Todo snapshots

    Query-plan rationale:
    - LEFT JOINing ``opencode_session_contexts`` into the session fetchrow
      eliminates a separate index-lookup on ``session_id`` (the FK); the join
      is 1:1 per session and resolved in the same heap scan.
    - The parent-internal-ID correlated subquery ``(SELECT p.id FROM sessions p
      WHERE p.external_session_id = s.parent_session_id LIMIT 1)`` is only
      evaluated when ``parent_session_id IS NOT NULL``; when NULL the
      subquery short-circuits.  ``external_session_id`` is indexed, so each
      invocation is a single index seek — cheaper than a separate round-trip.
    - This reduces the per-request query count from 5 to 3 (2 round-trips
      saved), eliminating the latency tail of the extra network exchanges.
    """
    from app.core.schemas.usage import (
        AgentRunDetail,
        ChildRunSummary,
        TodoRow,
    )

    # ── Fetch session + context + parent_internal_id (merged) ───────
    async with timed_operation("db.query.agent_run_detail.session", "db"):
        async with _db_timeout(
            "db.query.agent_run_detail.session", db_timeout_seconds
        ):
            session_row = await conn.fetchrow(
                """SELECT
                    s.id,
                    s.client_id,
                    s.source_database_id,
                    s.external_session_id,
                    s.first_message_at,
                    s.last_message_at,
                    s.message_count,
                    s.total_input_tokens,
                    s.total_output_tokens,
                    s.total_cached_tokens,
                    s.total_cache_read_tokens,
                    s.total_cache_write_tokens,
                    s.total_estimated_cost_usd,
                    s.project_id,
                    s.workspace_id,
                    s.agent,
                    s.parent_session_id,
                    """ + _PROJECT_LABEL_SQL + """ AS project_label,
                    -- Parent resolution via correlated subquery
                    (SELECT p.id FROM sessions p
                     WHERE p.external_session_id = s.parent_session_id LIMIT 1
                    ) AS parent_internal_id,
                    -- Session context columns
                    osc.id AS ctx_present,  -- non-null PK → reliable row-presence signal
                    osc.code_change_count,
                    osc.code_change_additions,
                    osc.code_change_deletions,
                    osc.session_model AS ctx_session_model,
                    osc.session_cost AS ctx_session_cost,
                    osc.title AS ctx_title,
                    osc.source_directory AS ctx_source_directory,
                    osc.source_path AS ctx_source_path,
                    osc.source_input_tokens AS ctx_source_input_tokens,
                    osc.source_output_tokens AS ctx_source_output_tokens,
                    osc.source_cached_tokens AS ctx_source_cached_tokens,
                    osc.source_reasoning_tokens AS ctx_source_reasoning_tokens
                FROM sessions s
                LEFT JOIN opencode_source_projects osp
                    ON osp.source_database_id = s.source_database_id
                    AND osp.external_project_id = s.project_id
                LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
                WHERE s.id = $1""",
                session_id,
            )

    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent run not found: {session_id}",
        )

    # ── Fetch child sessions ─────────────────────────────────────────
    async with timed_operation("db.query.agent_run_detail.children", "db"):
        async with _db_timeout(
            "db.query.agent_run_detail.children", db_timeout_seconds
        ):
            child_rows = await conn.fetch(
                """SELECT
                    id, external_session_id, agent, message_count,
                    last_message_at
                FROM sessions
                WHERE parent_session_id = $1
                ORDER BY last_message_at DESC""",
                session_row["external_session_id"],
            )

    child_summaries: list[ChildRunSummary] = []
    for cr in child_rows:
        async with timed_operation("compute.status.child", "compute"):
            async with _status_timeout(status_timeout_seconds):
                child_status = _compute_status(
                    last_message_at=cr["last_message_at"],
                    message_count=cr["message_count"],
                    has_parent=True,
                )
        child_summaries.append(
            ChildRunSummary(
                id=cr["id"],
                external_session_id=cr["external_session_id"],
                status=child_status,
                currentStatus=child_status,
                agent=cr["agent"],
                message_count=cr["message_count"],
            )
        )

    # ── Compute status ────────────────────────────────────────────────
    async with timed_operation("compute.status.session", "compute"):
        async with _status_timeout(status_timeout_seconds):
            computed_status = _compute_status(
                last_message_at=session_row["last_message_at"],
                message_count=session_row["message_count"],
                has_parent=session_row["parent_session_id"] is not None,
            )

    # ── Fetch todo snapshots ──────────────────────────────────────
    async with timed_operation("db.query.agent_run_detail.todos", "db"):
        async with _db_timeout(
            "db.query.agent_run_detail.todos", db_timeout_seconds
        ):
            todo_rows_raw = await conn.fetch(
                """SELECT content, status, priority, position
                FROM opencode_session_todos
                WHERE source_database_id = $1 AND external_session_id = $2
                ORDER BY position""",
                session_row["source_database_id"],
                session_row["external_session_id"],
            )

    # ── Compute todo aggregates ────────────────────────────────────
    todos: list[TodoRow] = [
        TodoRow(
            description=tr["content"],
            status=tr["status"] or "pending",
            priority=tr["priority"],
        )
        for tr in todo_rows_raw
    ]
    todo_total = len(todos)
    todo_completed = sum(
        1 for tr in todo_rows_raw if tr["status"] == "completed"
    )
    todo_blocked = sum(
        1 for tr in todo_rows_raw if tr["status"] == "blocked"
    )

    # ── Extract context from merged row ──────────────────────────────
    parent_external_id: str | None = session_row["parent_session_id"]
    parent_internal_id: uuid.UUID | None = session_row["parent_internal_id"]

    has_context = session_row["ctx_present"] is not None
    code_changes_total: int = (
        session_row["code_change_count"] if has_context else 0
    ) or 0

    # ── Build session context dict ──────────────────────────────────
    session_context: dict[str, object] | None = None
    if has_context:
        session_context = {
            "session_model": format_model_output(session_row["ctx_session_model"]),
            "title": session_row["ctx_title"],
            "source_directory": session_row["ctx_source_directory"],
            "source_path": session_row["ctx_source_path"],
            "code_change_additions": session_row["code_change_additions"],
            "code_change_deletions": session_row["code_change_deletions"],
        }

    # ── Build detail ─────────────────────────────────────────────────
    return AgentRunDetail(
        id=session_row["id"],
        external_session_id=session_row["external_session_id"],
        client_id=session_row["client_id"],
        source_database_id=session_row["source_database_id"],
        title=_derive_title(
            session_row["agent"], session_row["external_session_id"]
        ),
        status=computed_status,
        currentStatus=computed_status,
        agent=session_row["agent"],
        project_id=session_row["project_id"],
        project_label=session_row["project_label"],
        workspace_id=session_row["workspace_id"],
        parent_session_id=parent_external_id,
        parent_internal_id=parent_internal_id,
        child_summaries=child_summaries,
        todo_rows=todos,
        todo_total=todo_total,
        todo_completed=todo_completed,
        todo_blocked=todo_blocked,
        code_changes_total=code_changes_total,
        session_context=session_context,
        message_count=session_row["message_count"],
        total_input_tokens=session_row["total_input_tokens"],
        total_output_tokens=session_row["total_output_tokens"],
        total_cached_tokens=session_row["total_cached_tokens"],
        total_cache_read_tokens=session_row["total_cache_read_tokens"],
        total_cache_write_tokens=session_row["total_cache_write_tokens"],
        total_estimated_cost_usd=session_row["total_estimated_cost_usd"],
        first_message_at=session_row["first_message_at"],
        last_message_at=session_row["last_message_at"],
        loki_search_url=build_loki_search_url(
            client_id=session_row["client_id"],
            source_database_id=session_row["source_database_id"],
            session_id=session_row["id"],
            start_time=session_row["first_message_at"],
            end_time=session_row["last_message_at"] or _utcnow(),
            grafana_base_url=grafana_base_url,
        ),
    )


# ── Agent Run endpoints ───────────────────────────────────────────────────


@router.get("/agent-runs")
async def get_agent_runs(
    request: Request,
    client_id: uuid.UUID | None = Query(default=None),
    from_date: datetime | None = Query(
        default=None,
        description="ISO-8601 start date — filter sessions last active on or after this date",
    ),
    to_date: datetime | None = Query(
        default=None,
        description="ISO-8601 end date — filter sessions last active on or before this date",
    ),
    agent: str | None = Query(
        default=None,
        description="Filter by agent name (exact match)",
    ),
    external_project_id: str | None = Query(
        default=None,
        description="Filter by external project identifier (exact match on project_id)",
    ),
    filter_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by computed status: running, stale, completed, blocked, unknown",
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[AgentRunSummary]:
    """Return paginated Agent Run Summary rows computed on read.

    Each row includes internal/external session IDs, a derived title,
    calculated status, agent name, project/worktree identity, usage
    totals, and child run count.

    **Computed fields**: ``status`` and ``child_run_count`` are never
    stored — they are calculated at query time from session facts and
    parent/child relationships.

    **Status derivation** uses a quiet-threshold heuristic (60 min by
    default) and a stale-threshold heuristic (6 hours by default) to
    produce one of five values:
    ``running``, ``stale``, ``completed``, ``blocked``, or ``unknown``.
    See :func:`_compute_status` for the full derivation rules.
    """
    from app.core.schemas.usage import VALID_AGENT_RUN_STATUSES

    # Validate date range if both provided
    if from_date is not None and to_date is not None:
        _validate_date_range(from_date, to_date)

    # Validate status filter
    if filter_status is not None and filter_status not in VALID_AGENT_RUN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status: '{filter_status}'. "
            f"Valid values: {', '.join(sorted(VALID_AGENT_RUN_STATUSES))}",
        )

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_agent_runs(
            conn,
            client_id,
            from_date,
            to_date,
            agent,
            external_project_id,
            filter_status,
            limit,
            offset,
            settings.grafana_base_url,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/agent-runs/{session_id}")
async def get_agent_run_detail(
    session_id: uuid.UUID,
    request: Request,
    conn: asyncpg.Connection = Depends(get_session),
) -> AgentRunDetail:
    """Return a full agent run detail view keyed by internal Gateway session UUID.

    Includes parent identifiers, child summaries, project details,
    Session Context (placeholder), and usage totals.

    No OpenCode event, transcript, or message-part replay data is
    included — this endpoint returns aggregated facts only.
    """
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_agent_run_detail(
            conn, session_id, settings.grafana_base_url,
            db_timeout_seconds=settings.database_timeout_seconds,
            status_timeout_seconds=settings.status_computation_timeout_seconds,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Records-with-context helpers
# ═══════════════════════════════════════════════════════════════════════════

# Valid group-by dimensions for the records-with-context endpoint
RECORDS_WITH_CONTEXT_GROUP_BY: frozenset[str] = frozenset(
    {"project", "session", "agent", "model"}
)

# SQL expression for project label resolution
# COALESCE(display_name, name, basename(NULLIF(worktree, '/')), external_project_id, 'unknown')
_PROJECT_LABEL_SQL = """
    COALESCE(
        osp.display_name,
        osp.name,
        CASE
            WHEN osp.worktree IS NULL
                  OR osp.worktree = ''
                  OR osp.worktree = '/' THEN NULL
            ELSE substring(osp.worktree, '([^/]+)$')
        END,
        CASE
            WHEN s.project_id IS NULL THEN NULL
            WHEN length(s.project_id) > 12 THEN substring(s.project_id, 1, 12) || '…'
            ELSE s.project_id
        END,
        'unknown'
    )
"""

# Common join fragments for records-with-context queries
_RWC_SESSION_JOIN = "JOIN sessions s ON s.id = our.session_id"
_RWC_CONTEXT_JOIN = """
    LEFT JOIN opencode_session_contexts osc
        ON osc.source_database_id = s.source_database_id
        AND osc.external_session_id = s.external_session_id
"""
_RWC_PROJECT_JOIN = """
    LEFT JOIN opencode_source_projects osp
        ON osp.source_database_id = s.source_database_id
        AND osp.external_project_id = s.project_id
"""


def _parse_records_with_context_group_by(raw: str | None) -> list[str]:
    """Parse and validate a comma-separated group_by string.

    Returns an empty list when *raw* is ``None`` or empty.
    Raises ``HTTPException(400)`` on invalid values.
    """
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in RECORDS_WITH_CONTEXT_GROUP_BY]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by value(s): {', '.join(invalid)}. "
            f"Valid values: {', '.join(sorted(RECORDS_WITH_CONTEXT_GROUP_BY))}",
        )
    return parts


def _build_records_with_context_filters(
    start_date: datetime,
    end_date: datetime,
    project_id: str | None,
    session_id: uuid.UUID | None,
    agent: str | None,
    model: str | None,
) -> tuple[str, list]:
    """Build WHERE clause fragments and parameter list.

    Date-range params are placed first (``$1``, ``$2``) and included
    in the returned param list so callers do not need to prepend them.
    Additional filters use incrementing placeholders (``$3``, ``$4``, …).
    """
    filters: list[str] = []
    params: list = [start_date, end_date]

    filters.append("our.reported_at >= $1")
    filters.append("our.reported_at <= $2")

    if project_id is not None:
        filters.append(f"s.project_id = ${len(params) + 1}")
        params.append(project_id)

    if session_id is not None:
        filters.append(f"our.session_id = ${len(params) + 1}")
        params.append(session_id)

    if agent is not None:
        filters.append(f"s.agent = ${len(params) + 1}")
        params.append(agent)

    if model is not None:
        filters.append(f"om.model_name = ${len(params) + 1}")
        params.append(model)

    return " AND ".join(filters), params


async def _fetch_records_with_context(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    project_id: str | None,
    session_id: uuid.UUID | None,
    agent: str | None,
    model: str | None,
    limit: int,
    offset: int,
    grafana_base_url: str,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse:
    """Execute count + data queries for raw (non-grouped) mode.

    Returns paginated records enriched with project label, session title,
    and agent.
    """
    from app.core.schemas.usage import RecordWithContextRow

    where_clause, query_params = _build_records_with_context_filters(
        start_date, end_date, project_id, session_id, agent, model
    )

    # ── Total count ─────────────────────────────────────────────────
    count_sql = f"""
        SELECT COUNT(*)
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        {_RWC_SESSION_JOIN}
        WHERE {where_clause}
    """
    async with timed_operation("db.query.records_with_context.count", "db"):
        async with _db_timeout(
            "db.query.records_with_context.count", db_timeout_seconds
        ):
            total = await conn.fetchval(count_sql, *query_params)

    # ── Data query ──────────────────────────────────────────────────
    data_sql = f"""
        SELECT
            our.id,
            our.client_id,
            s.source_database_id,
            our.session_id,
            om.model_name,
            our.input_tokens,
            our.output_tokens,
            our.cached_tokens,
            our.provider,
            our.mode,
            our.finish_reason,
            our.reasoning_tokens,
            our.cache_read_tokens,
            our.cache_write_tokens,
            our.estimated_cost_usd,
            our.reported_at,
            our.first_ingested_at AS ingested_at,
            s.agent,
            osc.title AS session_title,
            {_PROJECT_LABEL_SQL} AS project_label
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        {_RWC_SESSION_JOIN}
        {_RWC_CONTEXT_JOIN}
        {_RWC_PROJECT_JOIN}
        WHERE {where_clause}
        ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) DESC
        LIMIT ${len(query_params) + 1}
        OFFSET ${len(query_params) + 2}
    """
    async with timed_operation("db.query.records_with_context.data", "db"):
        async with _db_timeout(
            "db.query.records_with_context.data", db_timeout_seconds
        ):
            rows = await conn.fetch(data_sql, *query_params, limit, offset)

    items = [
        RecordWithContextRow(
            id=r["id"],
            client_id=r["client_id"],
            source_database_id=r["source_database_id"],
            session_id=r["session_id"],
            model_name=r["model_name"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            cached_tokens=r["cached_tokens"],
            provider=r["provider"],
            mode=r["mode"],
            finish_reason=r["finish_reason"],
            reasoning_tokens=r["reasoning_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_write_tokens=r["cache_write_tokens"],
            estimated_cost_usd=r["estimated_cost_usd"],
            reported_at=r["reported_at"],
            ingested_at=r["ingested_at"],
            agent=r["agent"],
            session_title=r["session_title"],
            project_label=r["project_label"],
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


def _rwc_group_expression(parts: list[str]) -> str:
    """Build a SQL GROUP BY expression and corresponding select expression.

    Each part contributes a SQL fragment.  When there are multiple parts
    the group value is concatenated with a pipe separator.
    """
    fragments: list[str] = []
    for part in parts:
        if part == "project":
            fragments.append(_PROJECT_LABEL_SQL)
        elif part == "session":
            fragments.append("CAST(our.session_id AS text)")
        elif part == "agent":
            fragments.append("COALESCE(s.agent, 'unknown')")
        elif part == "model":
            fragments.append("om.model_name")

    if len(fragments) == 1:
        return fragments[0]
    return " || '|' || ".join(fragments)


def _rwc_group_by_columns(parts: list[str]) -> list[str]:
    """Return the list of output column names needed for this grouping.

    Each dimension may contribute a context column to the result row.
    """
    cols: list[str] = []
    if "project" in parts:
        cols.append(f"{_PROJECT_LABEL_SQL} AS project_label")
    if "session" in parts:
        cols.append("osc.title AS session_title")
    if "agent" in parts:
        cols.append("COALESCE(s.agent, 'unknown') AS agent")
    if "model" in parts:
        cols.append("om.model_name AS model_name")
    return cols


async def _fetch_records_with_context_grouped(
    conn: asyncpg.Connection,
    start_date: datetime,
    end_date: datetime,
    project_id: str | None,
    session_id: uuid.UUID | None,
    agent: str | None,
    model: str | None,
    group_parts: list[str],
    *,
    db_timeout_seconds: int,
) -> list:
    """Execute aggregated query for grouped mode.

    Returns aggregated rows grouped by the requested dimensions.
    """
    where_clause, query_params = _build_records_with_context_filters(
        start_date, end_date, project_id, session_id, agent, model
    )

    group_expr = _rwc_group_expression(group_parts)
    extra_cols = _rwc_group_by_columns(group_parts)
    extra_cols_sql = ",\n            ".join(extra_cols) if extra_cols else ""

    sql = f"""
        SELECT
            {group_expr} AS group_value,
            {extra_cols_sql}{',' if extra_cols else ''}
            COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
            COALESCE(SUM(our.reasoning_tokens), 0) AS total_reasoning_tokens,
            COALESCE(SUM(our.cache_read_tokens), 0) AS total_cache_read_tokens,
            COALESCE(SUM(our.cache_write_tokens), 0) AS total_cache_write_tokens,
            SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
            COUNT(*) AS record_count
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        {_RWC_SESSION_JOIN}
        {_RWC_CONTEXT_JOIN}
        {_RWC_PROJECT_JOIN}
        WHERE {where_clause}
        GROUP BY {group_expr}
        ORDER BY group_value
    """
    async with timed_operation("db.query.records_with_context.grouped", "db"):
        async with _db_timeout(
            "db.query.records_with_context.grouped", db_timeout_seconds
        ):
            rows = await conn.fetch(sql, *query_params)

    return [
        RecordWithContextGroupedRow(
            group_value=str(r["group_value"]),
            project_label=r.get("project_label"),
            session_title=r.get("session_title"),
            agent=r.get("agent"),
            model_name=r.get("model_name"),
            total_input_tokens=r["total_input_tokens"],
            total_output_tokens=r["total_output_tokens"],
            total_cached_tokens=r["total_cached_tokens"],
            total_reasoning_tokens=r["total_reasoning_tokens"],
            total_cache_read_tokens=r["total_cache_read_tokens"],
            total_cache_write_tokens=r["total_cache_write_tokens"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            record_count=r["record_count"],
        )
        for r in rows
    ]


# ── Records-with-context endpoint ─────────────────────────────────────────


@router.get("/records-with-context", response_model=Union[PaginatedResponse, list[RecordWithContextGroupedRow]])
async def get_records_with_context(
    request: Request,
    start_date: datetime = Query(..., description="ISO-8601 start date (inclusive)"),
    end_date: datetime = Query(..., description="ISO-8601 end date (inclusive)"),
    project_id: str | None = Query(
        default=None,
        description="Filter by external project ID (exact match on sessions.project_id)",
    ),
    session_id: uuid.UUID | None = Query(default=None),
    agent: str | None = Query(
        default=None,
        description="Filter by agent name (exact match on sessions.agent)",
    ),
    model: str | None = Query(
        default=None,
        description="Filter by model name (exact match on observed_models.model_name)",
    ),
    group_by: str | None = Query(
        default=None,
        description="Comma-separated group-by dimensions: "
        "project,session,agent,model",
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse | list[RecordWithContextGroupedRow]:
    """Return usage records enriched with project label, session title, and agent.

    **Raw mode** (no ``group_by``): returns paginated per-message rows with
    context fields.  Supports ``limit`` and ``offset`` for pagination.

    **Grouped mode** (with ``group_by``): returns aggregated subtotals for
    the requested dimensions.  ``limit`` and ``offset`` are not applied.
    """
    _validate_date_range(start_date, end_date)
    group_parts = _parse_records_with_context_group_by(group_by)
    settings = get_settings()

    async with _request_timeout(settings.total_request_timeout_seconds):
        if group_parts:
            return await _fetch_records_with_context_grouped(
                conn,
                start_date,
                end_date,
                project_id,
                session_id,
                agent,
                model,
                group_parts,
                db_timeout_seconds=settings.database_timeout_seconds,
            )

        return await _fetch_records_with_context(
            conn,
            start_date,
            end_date,
            project_id,
            session_id,
            agent,
            model,
            limit,
            offset,
            settings.grafana_base_url,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
