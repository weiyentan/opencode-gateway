"""Usage reporting API — aggregates, records, and session summaries.

All endpoints require API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).  Responses are automatically
wrapped in the standard envelope format.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

from app.core.config import Settings, get_settings
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
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usage"])

# ── Timeout helpers ────────────────────────────────────────────────────────


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


# ── Valid group-by dimensions ─────────────────────────────────────────────

VALID_GROUP_BY: frozenset[str] = frozenset(
    {"client", "model", "session", "day", "week", "month", "project", "agent"}
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017


def _text_or_none(row, key: str) -> str | None:
    """Read an optional text column from a query row, defaulting to None.

    Legacy test row factories predate the v1.2 enrichment columns.  Returns
    None when the column is NULL, empty, or absent from the row.
    """
    try:
        value = row[key]
    except (KeyError, TypeError):
        return None
    return value or None


def _json_breakdown(row, key: str) -> dict[str, int]:
    """Read a JSON breakdown column (asyncpg jsonb arrives as a JSON string).

    Returns an empty dict when the column is NULL or absent from the row
    (legacy test row factories) so the response shape stays stable.
    """
    try:
        raw = row[key]
    except (KeyError, TypeError):
        return {}
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return {str(k): int(v) for k, v in parsed.items()}
    return {str(k): int(v) for k, v in raw.items()}


def _cache_hit_ratio(cache_read_tokens: int, input_tokens: int) -> float | None:
    """Compute the cache hit ratio for an aggregate row.

    ``cache_read / (input + cache_read)`` — the fraction of model input
    served from cache — rounded to 4 decimals.  Returns None when the
    denominator is zero (no input activity to measure against).
    """
    denominator = input_tokens + cache_read_tokens
    if denominator <= 0:
        return None
    return round(cache_read_tokens / denominator, 4)


def _deprecation_header_value(
    sunset: datetime | None, now: datetime | None = None
) -> str | None:
    """Return the Deprecation header value while within the sunset window.

    The legacy ``active_tokens`` field is deprecated (issue #557): while the
    current instant is strictly before *sunset*, usage query responses carry
    ``Deprecation: active_tokens; sunset=<ISO-8601>``.  Returns None when
    *sunset* is unset or the window has passed (boundary-inclusive: a request
    exactly at the sunset instant no longer carries the header).
    """
    if sunset is None:
        return None
    if now is None:
        now = _utcnow()
    if now >= sunset:
        return None
    return "active_tokens; sunset=" + sunset.isoformat()


def _apply_deprecation_header(response: Response, settings: Settings) -> None:
    """Attach the ``Deprecation`` header while the deprecation window is open."""
    value = _deprecation_header_value(settings.active_tokens_deprecation_sunset)
    if value is not None:
        response.headers["Deprecation"] = value


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
        elif part == "agent":
            fragments.append("COALESCE(s.agent, 'unknown')")

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
# Like the metadata branches, the worktree-basename fallback and the
# ``r.project_id`` fallback are normalized at read time: trailing
# ``-\d+$`` workspace numeric suffixes are stripped, and empty strip
# results cascade to 'unknown'.
_ROLLUP_PROJECT_LABEL_SQL = """
    COALESCE(
        NULLIF(regexp_replace(osp.display_name, '-\\d+$', ''), ''),
        NULLIF(regexp_replace(osp.name, '-\\d+$', ''), ''),
        CASE
            WHEN osp.worktree IS NULL
                  OR osp.worktree = ''
                  OR osp.worktree = '/' THEN NULL
            ELSE NULLIF(
                regexp_replace(substring(osp.worktree, '([^/]+)$'), '-\\d+$', ''),
                ''
            )
        END,
        CASE
            WHEN r.project_id IS NULL THEN NULL
            WHEN length(r.project_id) > 12 THEN substring(r.project_id, 1, 12) || '…'
            ELSE NULLIF(regexp_replace(r.project_id, '-\\d+$', ''), '')
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
        "r.day >= ($1 AT TIME ZONE 'UTC')::date",
        "r.day <= ($2 AT TIME ZONE 'UTC')::date",
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
        "(r.reported_at AT TIME ZONE 'UTC')::date >= ($1 AT TIME ZONE 'UTC')::date",
        "(r.reported_at AT TIME ZONE 'UTC')::date <= ($2 AT TIME ZONE 'UTC')::date",
        "r.project_id IS NOT NULL",
    ]
    if client_id is not None:
        count_filters.append(f"r.client_id = ${len(count_params) + 1}")
        count_params.append(client_id)
    count_where = " AND ".join(count_filters)

    # PERF: provider_counts CTE re-declares the main query's FROM/JOINs against
    # usage_events, doubling I/O for this query path. Acceptable at current
    # scale; if this becomes a bottleneck, refactor to a single pass with a
    # two-level GROUP BY or window function.
    count_sql = f"""
        WITH usage_with_label AS (
            SELECT
                r.client_id,
                r.session_id,
                r.model_id,
                ({_ROLLUP_PROJECT_LABEL_SQL}) AS project_label,
                COALESCE(NULLIF(r.provider, ''), 'unknown') AS provider_key
            FROM usage_events r
            LEFT JOIN LATERAL (
                SELECT osp.display_name, osp.name, osp.worktree
                FROM opencode_source_projects osp
                WHERE osp.client_id = r.client_id
                  AND osp.external_project_id = r.project_id
                LIMIT 1
            ) osp ON true
            WHERE {count_where}
        ),
        provider_counts AS (
            SELECT
                ul.client_id,
                ul.project_label,
                ul.provider_key,
                COUNT(*) AS cnt
            FROM usage_with_label ul
            GROUP BY ul.client_id, ul.project_label, ul.provider_key
        ),
        provider_breakdown AS (
            SELECT
                client_id,
                project_label,
                jsonb_object_agg(provider_key, cnt) AS provider_breakdown
            FROM provider_counts
            GROUP BY client_id, project_label
        )
        SELECT
            COALESCE(oc.canonical_name, oc.name) || '|' || ul.project_label AS group_value,
            COUNT(*) AS record_count,
            COUNT(DISTINCT ul.session_id) AS session_count,
            COUNT(DISTINCT om.model_name) AS model_count,
            COALESCE(
                MAX(pb.provider_breakdown), '{{}}'::jsonb
            ) AS provider_breakdown
        FROM usage_with_label ul
        JOIN observed_models om ON om.id = ul.model_id
        JOIN opencode_clients oc ON oc.id = ul.client_id
        LEFT JOIN provider_breakdown pb
            ON pb.client_id = ul.client_id
           AND pb.project_label = ul.project_label
        GROUP BY COALESCE(oc.canonical_name, oc.name), ul.project_label
    """
    async with timed_operation("db.query.aggregates.client_project_counts", "db"):
        async with _db_timeout(
            "db.query.aggregates.client_project_counts", db_timeout_seconds
        ):
            count_rows = await conn.fetch(count_sql, *count_params)

    # Build a lookup of counts by group_value
    count_by_group: dict[str, dict] = {}
    for cr in count_rows:
        gv = str(cr["group_value"])
        count_by_group[gv] = {
            "record_count": cr["record_count"],
            "session_count": cr["session_count"],
            "model_count": cr["model_count"],
            "provider_breakdown": _json_breakdown(cr, "provider_breakdown"),
        }

    # ── Merge counts into rollup rows ────────────────────────────────
    result: list[AggregateRow] = []
    for r in rollup_rows:
        gv = str(r["group_value"])
        counts = count_by_group.get(gv, {
            "record_count": 0,
            "session_count": 0,
            "model_count": 0,
            "provider_breakdown": {},
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
                cache_hit_ratio=_cache_hit_ratio(
                    r["total_cache_read_tokens"] or 0,
                    r["total_input_tokens"] or 0,
                ),
                provider_breakdown=counts["provider_breakdown"],
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
        # The provider breakdown rides the SAME statement as a scalar
        # subquery (jsonb_object_agg), reusing the date/filter placeholders
        # ($1..$5) at identical indices, so the endpoint stays at one query.
        provider_filters: list[str] = [
            "p2.reported_at >= $1",
            "p2.reported_at <= $2",
        ]
        if client_id is not None:
            provider_filters.append("p2.client_id = $3")
        if model is not None:
            provider_filters.append("om2.model_name = $4")
        if session_id is not None:
            provider_filters.append("p2.session_id = $5")
        provider_where = " AND ".join(provider_filters)

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
                COUNT(DISTINCT om.model_name) AS model_count,
                COALESCE(
                    (SELECT jsonb_object_agg(pb.provider_key, pb.cnt) FROM (
                        SELECT
                            COALESCE(NULLIF(p2.provider, ''), 'unknown')
                                AS provider_key,
                            COUNT(*) AS cnt
                        FROM usage_events p2
                        JOIN observed_models om2 ON om2.id = p2.model_id
                        WHERE {provider_where}
                        GROUP BY COALESCE(NULLIF(p2.provider, ''), 'unknown')
                    ) pb),
                    '{{}}'::jsonb
                ) AS provider_breakdown
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
                cache_hit_ratio=(
                    _cache_hit_ratio(
                        row["total_cache_read_tokens"] or 0,
                        row["total_input_tokens"] or 0,
                    )
                    if row
                    else None
                ),
                provider_breakdown=(
                    _json_breakdown(row, "provider_breakdown") if row else {}
                ),
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
    has_agent = "agent" in group_parts

    # Conditionally join sessions when the project or agent dimension is in
    # use (both resolve columns from the ``sessions`` table), and join
    # source_projects when the project dimension is in use.
    sessions_join = (
        "LEFT JOIN sessions s ON s.id = our.session_id"
        if has_project or has_agent
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
    agent_col = (
        ",\n            COALESCE(s.agent, 'unknown') AS agent"
        if has_agent
        else ""
    )

    group_by_clause = f"GROUP BY {group_expr}"
    if has_project:
        group_by_clause += f",{_PROJECT_LABEL_SQL}"
    if has_agent and len(group_parts) > 1:
        group_by_clause += ",COALESCE(s.agent, 'unknown')"

    # The provider breakdown rides the SAME statement via a CTE keyed on the
    # group expression (one query budget preserved — see test_query_counts).
    # The CTE replicates the main query's joins so the group expression
    # (including project-label and agent COALESCE fragments) resolves
    # identically, then collapses per-provider counts into one JSON object
    # per group.
    # PERF: provider_counts CTE re-declares the main query's FROM/JOINs against
    # usage_events, doubling I/O for this query path. Acceptable at current
    # scale; if this becomes a bottleneck, refactor to a single pass with a
    # two-level GROUP BY or window function.
    sql = f"""
        WITH provider_counts AS (
            SELECT
                {group_expr} AS group_value,
                COALESCE(NULLIF(our.provider, ''), 'unknown') AS provider_key,
                COUNT(*) AS cnt
            FROM usage_events our
            JOIN observed_models om ON om.id = our.model_id
            LEFT JOIN opencode_clients oc ON oc.id = our.client_id
            {sessions_join}
            {project_join}
            WHERE {where_clause}
            GROUP BY {group_expr}, COALESCE(NULLIF(our.provider, ''), 'unknown')
        ),
        provider_breakdown AS (
            SELECT
                group_value,
                jsonb_object_agg(provider_key, cnt) AS provider_breakdown
            FROM provider_counts
            GROUP BY group_value
        )
        SELECT
            {group_expr} AS group_value{project_label_col}{agent_col},
            COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
            COALESCE(SUM(our.reasoning_tokens), 0) AS total_reasoning_tokens,
            COALESCE(SUM(our.cache_read_tokens), 0) AS total_cache_read_tokens,
            COALESCE(SUM(our.cache_write_tokens), 0) AS total_cache_write_tokens,
            SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
            COUNT(*) AS record_count,
            COUNT(DISTINCT our.session_id) AS session_count,
            COUNT(DISTINCT om.model_name) AS model_count,
            COALESCE(pb.provider_breakdown, '{{}}'::jsonb) AS provider_breakdown
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        LEFT JOIN opencode_clients oc ON oc.id = our.client_id
        {sessions_join}
        {project_join}
        LEFT JOIN provider_breakdown pb ON pb.group_value = {group_expr}
        WHERE {where_clause}
        -- JSONB in GROUP BY: PostgreSQL requires non-aggregated LEFT JOIN
        -- columns to appear in GROUP BY; COALESCE(...,'{{}}'::jsonb) keeps
        -- groups with empty provider_breakdown distinct.
        {group_by_clause}, COALESCE(pb.provider_breakdown, '{{}}'::jsonb)
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
            cache_hit_ratio=_cache_hit_ratio(
                r["total_cache_read_tokens"] or 0,
                r["total_input_tokens"] or 0,
            ),
            provider_breakdown=_json_breakdown(r, "provider_breakdown"),
            project_label=r["project_label"] if has_project else None,
            agent=r["agent"] if has_agent else None,
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
            NULLIF(our.provider, '') AS provider,
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


def _int_or_zero(row, key: str) -> int:
    """Read an integer column from a query row, defaulting to 0.

    The code-change projection columns are nullable (a missing Session
    Context row yields NULL), and a few test row factories predate these
    columns.  Returns 0 when the column is NULL or absent from the row.
    """
    try:
        value = row[key]
    except (KeyError, TypeError):
        return 0
    return value or 0


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

    # Data query — the page of sessions is hoisted into a ``page`` CTE so
    # the read-time usage aggregations (reasoning total + primary provider)
    # are scoped to the returned page's sessions only: one hash aggregate
    # over ``usage_events`` rows belonging to those sessions, no N+1, no
    # full-table scan of every session in the deployment.  ``sessions`` has
    # no reasoning/provider columns (ADR 0012 keeps reasoning off the
    # session aggregate), so these fields are derived from ``usage_events``
    # at read time.  ``total_cache_read_tokens`` / ``total_cache_write_tokens``
    # remain the stored session aggregates.
    data_sql = f"""
        WITH page AS (
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
                osc.code_change_count,
                osc.code_change_additions,
                osc.code_change_deletions,
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
        ),
        usage_agg AS (
            SELECT
                ue.session_id,
                COALESCE(SUM(ue.reasoning_tokens), 0)::bigint
                    AS total_reasoning_tokens
            FROM usage_events ue
            JOIN page p ON p.id = ue.session_id
            GROUP BY ue.session_id
        ),
        provider_agg AS (
            SELECT session_id, provider
            FROM (
                SELECT
                    ue.session_id,
                    ue.provider,
                    ROW_NUMBER() OVER (
                        PARTITION BY ue.session_id
                        ORDER BY COUNT(*) DESC, ue.provider ASC
                    ) AS rn
                FROM usage_events ue
                JOIN page p ON p.id = ue.session_id
                WHERE ue.provider IS NOT NULL AND ue.provider <> ''
                GROUP BY ue.session_id, ue.provider
            ) ranked
            WHERE rn = 1
        )
        SELECT
            p.*,
            COALESCE(ua.total_reasoning_tokens, 0) AS total_reasoning_tokens,
            pa.provider AS primary_provider
        FROM page p
        LEFT JOIN usage_agg ua ON ua.session_id = p.id
        LEFT JOIN provider_agg pa ON pa.session_id = p.id
        ORDER BY p.last_message_at DESC
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
            total_reasoning_tokens=_int_or_zero(r, "total_reasoning_tokens"),
            primary_provider=_text_or_none(r, "primary_provider"),
            project_id=r["project_id"],
            project_label=r["project_label"],
            workspace_id=r["workspace_id"],
            agent=r["agent"],
            parent_session_id=r["parent_session_id"],
            total_estimated_cost_usd=r["total_estimated_cost_usd"],
            session_title=r["session_title"],
            code_change_count=_int_or_zero(r, "code_change_count"),
            code_change_additions=_int_or_zero(r, "code_change_additions"),
            code_change_deletions=_int_or_zero(r, "code_change_deletions"),
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
    response: Response,
    start_date: datetime = Query(..., description="ISO-8601 start date (inclusive)"),
    end_date: datetime = Query(..., description="ISO-8601 end date (inclusive)"),
    client_id: uuid.UUID | None = Query(default=None),
    model: str | None = Query(default=None),
    session_id: uuid.UUID | None = Query(default=None),
    group_by: str | None = Query(
        default=None,
        description="Comma-separated group-by dimensions: "
        "client,model,session,day,week,month,project,agent",
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
    _apply_deprecation_header(response, settings)
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_aggregates(
            conn, start_date, end_date, client_id, model, session_id,
            group_parts,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/records")
async def get_records(
    request: Request,
    response: Response,
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
    back to the collector-reported time.  "Most recent" therefore means
    most recently created at the source, not most recently ingested.
    """
    _validate_date_range(start_date, end_date)
    sort_by, sort_dir = _validate_sort(sort_by, sort_dir)
    settings = get_settings()
    _apply_deprecation_header(response, settings)
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
    response: Response,
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
    _apply_deprecation_header(response, settings)
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
#  (SQL) implement exactly the table below. The thresholds are configurable
#  via :class:`app.core.config.Settings` (``quiet_threshold_minutes``,
#  ``stale_threshold_hours``, ``unknown_threshold_hours``); the module-level
#  constants beneath this block hold the defaults and are what the pure
#  helpers fall back to when a caller supplies no explicit thresholds. The
#  endpoints resolve the runtime values from Settings and pass them down.
#  If you edit either function's branch logic, update this table to match.
#  Tests in tests/test_agent_runs.py pin both implementations to this table.
#
#  Branch priority (first match wins), with ``age`` = now - last_message_at:
#    1. unknown    — message_count == 0 OR last_message_at IS NULL
#    2. running    — age < quiet threshold (15 minutes)
#    3. completed  — quiet <= age < stale threshold (2 hours), no parent
#    4. blocked    — quiet <= age < stale threshold (2 hours), has parent
#    5. stale      — stale <= age < unknown threshold (48 hours) — an
#                    observability gap, not a proven terminal state
#    6. unknown    — age >= unknown threshold (48 hours)
#
#  Boundary inclusivity is identical in both implementations (strict):
#     * exactly 15 minutes old → completed/blocked (running is ``<``)
#     * exactly 2 hours old    → stale           (completed is ``<``)
#     * exactly 48 hours old   → unknown         (stale is ``<``)
# ────────────────────────────────────────────────────────────────────────────

# Default quiet threshold in minutes — a session whose last message is
# within this window is considered "running". Mirrors the Settings default
# ``Settings.quiet_threshold_minutes``.
QUIET_THRESHOLD_MINUTES: int = 15

# Default stale threshold in hours — a session whose last message is older
# than this (but younger than the unknown threshold) is considered "stale"
# (observability gap) rather than "completed"/"blocked". Mirrors the
# Settings default ``Settings.stale_threshold_hours``.
STALE_THRESHOLD_HOURS: int = 2

# Default unknown threshold in hours — a session whose last message is
# older than this is considered "unknown" rather than "stale". Mirrors the
# Settings default ``Settings.unknown_threshold_hours``.
UNKNOWN_THRESHOLD_HOURS: int = 48


def _compute_status(
    last_message_at: datetime | None,
    message_count: int,
    has_parent: bool,
    *,
    now: datetime | None = None,
    quiet_threshold_minutes: int = QUIET_THRESHOLD_MINUTES,
    stale_threshold_hours: int = STALE_THRESHOLD_HOURS,
    unknown_threshold_hours: int = UNKNOWN_THRESHOLD_HOURS,
) -> str:
    """Compute agent run status from available session facts.

    **Status derivation (in priority order):**

    1. **unknown** — No messages recorded (``message_count == 0``) OR
       ``last_message_at`` is ``None``.

    2. **running** — ``last_message_at`` is within the quiet threshold
       (``quiet_threshold_minutes``), indicating the session may still be
       producing telemetry.

    3. **completed** — beyond the quiet threshold but within the stale
       threshold (``stale_threshold_hours``), has messages, no parent. A
       confidently-recently-quiet, best-effort terminal status.

    4. **blocked** — beyond the quiet threshold but within the stale
       threshold, has messages, and has a ``parent_session_id``, suggesting
       it is intentionally waiting on a parent.

    5. **stale** — beyond the stale threshold but within the unknown
       threshold (``unknown_threshold_hours``). Liveness is no longer
       trusted without a terminal signal — an observability gap, not a
       known termination.

    6. **unknown** — beyond the unknown threshold (too old to classify
       meaningfully).

    The thresholds are configurable via ``app.core.config.Settings`` and
    default to the module-level constants (``QUIET_THRESHOLD_MINUTES``,
    ``STALE_THRESHOLD_HOURS``, ``UNKNOWN_THRESHOLD_HOURS``). The canonical
    status table (branch priority, threshold values, boundary inclusivity)
    is documented once above the constants block; this function and the SQL
    CASE expression in :func:`_status_case_expression` implement it
    identically.
    """
    if now is None:
        now = _utcnow()

    if message_count == 0 or last_message_at is None:
        return "unknown"

    age_minutes = (now - last_message_at).total_seconds() / 60.0

    if age_minutes < quiet_threshold_minutes:
        return "running"

    if age_minutes < stale_threshold_hours * 60:
        return "blocked" if has_parent else "completed"

    if age_minutes < unknown_threshold_hours * 60:
        return "stale"

    return "unknown"


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


def _status_case_expression(
    *,
    quiet_threshold_minutes: int = QUIET_THRESHOLD_MINUTES,
    stale_threshold_hours: int = STALE_THRESHOLD_HOURS,
    unknown_threshold_hours: int = UNKNOWN_THRESHOLD_HOURS,
    now_param: str = "now()",
) -> str:
    """Return a SQL CASE expression that computes status from session columns.

    Implements the canonical status table documented above the
    ``QUIET_THRESHOLD_MINUTES`` constants block — the single authoritative
    source for thresholds, branch priority, and boundary inclusivity.

    Mirrors the logic in :func:`_compute_status` but expressed in SQL
    so the database can filter rows by computed status. The reference time
    is rendered from *now_param*, which defaults to SQL ``now()``.  Callers
    that embed this expression in more than one statement of a single
    request (the agent-runs list count and data queries) must pass a bound
    parameter placeholder (e.g. ``"$3"``) instead so every statement derives
    status against the same reference timestamp rather than two independent
    ``now()`` calls that could straddle a threshold boundary.  The intervals
    are rendered from the thresholds it is passed (the same configurable
    values the Python implementation consumes), keeping the same branch
    order (unknown → running → completed/blocked → stale → unknown-old) and
    the same strict boundary inclusivity.
    """
    quiet_interval = f"interval '{quiet_threshold_minutes} minutes'"
    stale_interval = f"interval '{stale_threshold_hours} hours'"
    unknown_interval = f"interval '{unknown_threshold_hours} hours'"
    return f"""
        CASE
            WHEN s.message_count = 0 OR s.last_message_at IS NULL THEN 'unknown'
            WHEN s.last_message_at > {now_param} - {quiet_interval} THEN 'running'
            WHEN s.last_message_at > {now_param} - {stale_interval}
                 AND s.parent_session_id IS NULL THEN 'completed'
            WHEN s.last_message_at > {now_param} - {stale_interval} THEN 'blocked'
            WHEN s.last_message_at > {now_param} - {unknown_interval} THEN 'stale'
            ELSE 'unknown'
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
    quiet_threshold_minutes: int,
    stale_threshold_hours: int,
    unknown_threshold_hours: int,
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

    # ── Build the status filter as a CTE wrapper ────────────────────
    if status_filter is not None:
        # Resolve a single reference timestamp for status derivation.  The
        # count and data queries each embed the status CASE expression;
        # binding one Python-side instant (rather than two independent SQL
        # ``now()`` calls) guarantees both derive statuses against the same
        # clock reading, so ``total`` can never disagree with the rows
        # actually returned for a session sitting on a threshold boundary.
        status_now = _utcnow()
        # The status filter occupies the next param slot and the reference
        # timestamp the slot after that; both the count and data queries
        # reference them at the same indices.
        params.append(status_filter)
        status_filter_placeholder = f"${len(params)}"
        params.append(status_now)
        now_placeholder = f"${len(params)}"
        status_expr = _status_case_expression(
            quiet_threshold_minutes=quiet_threshold_minutes,
            stale_threshold_hours=stale_threshold_hours,
            unknown_threshold_hours=unknown_threshold_hours,
            now_param=now_placeholder,
        )
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
                    osc.code_change_count,
                    osc.code_change_additions,
                    osc.code_change_deletions,
                    {_PROJECT_LABEL_SQL} AS project_label
                FROM sessions s
                LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
                LEFT JOIN opencode_source_projects osp
                    ON osp.source_database_id = s.source_database_id
                    AND osp.external_project_id = s.project_id
                WHERE {where_clause}
            ) sub
            WHERE sub._status = {status_filter_placeholder}
        """
        count_from = f"""
            FROM sessions s
            WHERE {where_clause} AND ({status_expr}) = {status_filter_placeholder}
        """
    else:
        status_expr = _status_case_expression(
            quiet_threshold_minutes=quiet_threshold_minutes,
            stale_threshold_hours=stale_threshold_hours,
            unknown_threshold_hours=unknown_threshold_hours,
        )
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
                osc.code_change_count,
                osc.code_change_additions,
                osc.code_change_deletions,
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
    # ``base`` is hoisted into a CTE so both the main result and the
    # ``todo_counts`` aggregation share a single evaluation of the filtered
    # session universe.  ``todo_counts`` is scoped to that universe via an
    # INNER JOIN, so the todo aggregation cost tracks the filtered result
    # set (and any client_id/date/agent/project/status predicate) rather
    # than the full ``opencode_session_todos`` table.  ``usage_agg`` and
    # ``provider_agg`` (issue #557) follow the same scoping pattern for the
    # read-time reasoning total and primary provider — the sessions table
    # carries no reasoning/provider columns (ADR 0012), so these derive
    # from ``usage_events`` for the filtered universe only.
    data_sql = f"""
        WITH base AS (
            {base_query}
        ),
        usage_agg AS (
            SELECT
                ue.session_id,
                COALESCE(SUM(ue.reasoning_tokens), 0)::bigint
                    AS total_reasoning_tokens
            FROM usage_events ue
            JOIN base b ON b.id = ue.session_id
            GROUP BY ue.session_id
        ),
        provider_agg AS (
            SELECT session_id, provider
            FROM (
                SELECT
                    ue.session_id,
                    ue.provider,
                    ROW_NUMBER() OVER (
                        PARTITION BY ue.session_id
                        ORDER BY COUNT(*) DESC, ue.provider ASC
                    ) AS rn
                FROM usage_events ue
                JOIN base b ON b.id = ue.session_id
                WHERE ue.provider IS NOT NULL AND ue.provider <> ''
                GROUP BY ue.session_id, ue.provider
            ) ranked
            WHERE rn = 1
        ),
        child_counts AS (
            SELECT parent_session_id, COUNT(*) AS cnt
            FROM sessions
            WHERE parent_session_id IS NOT NULL
            GROUP BY parent_session_id
        ),
        todo_counts AS (
            SELECT t.source_database_id, t.external_session_id,
                   COUNT(*) AS todo_total,
                   COUNT(*) FILTER (WHERE t.status = 'completed') AS todo_completed,
                   COUNT(*) FILTER (WHERE t.status = 'blocked') AS todo_blocked
            FROM opencode_session_todos t
            JOIN base s
              ON s.source_database_id = t.source_database_id
             AND s.external_session_id = t.external_session_id
            GROUP BY t.source_database_id, t.external_session_id
        )
        SELECT s.*,
               COALESCE(ua.total_reasoning_tokens, 0) AS total_reasoning_tokens,
               pa.provider AS primary_provider,
               COALESCE(cc.cnt, 0) AS child_run_count,
               COALESCE(tc.todo_total, 0) AS todo_total,
               COALESCE(tc.todo_completed, 0) AS todo_completed,
               COALESCE(tc.todo_blocked, 0) AS todo_blocked
        FROM base s
        LEFT JOIN usage_agg ua ON ua.session_id = s.id
        LEFT JOIN provider_agg pa ON pa.session_id = s.id
        LEFT JOIN child_counts cc
            ON cc.parent_session_id = s.external_session_id
        LEFT JOIN todo_counts tc
            ON tc.source_database_id = s.source_database_id
            AND tc.external_session_id = s.external_session_id
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
                todo_total=_int_or_zero(r, "todo_total"),
                todo_completed=_int_or_zero(r, "todo_completed"),
                todo_blocked=_int_or_zero(r, "todo_blocked"),
                code_changes_total=_int_or_zero(r, "code_change_count"),
                code_change_count=_int_or_zero(r, "code_change_count"),
                code_change_additions=_int_or_zero(r, "code_change_additions"),
                code_change_deletions=_int_or_zero(r, "code_change_deletions"),
                total_input_tokens=r["total_input_tokens"],
                total_output_tokens=r["total_output_tokens"],
                total_cached_tokens=r["total_cached_tokens"],
                total_cache_read_tokens=r["total_cache_read_tokens"],
                total_cache_write_tokens=r["total_cache_write_tokens"],
                total_reasoning_tokens=_int_or_zero(r, "total_reasoning_tokens"),
                primary_provider=_text_or_none(r, "primary_provider"),
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
    quiet_threshold_minutes: int,
    stale_threshold_hours: int,
    unknown_threshold_hours: int,
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
                    osc.source_reasoning_tokens AS ctx_source_reasoning_tokens,
                    -- Issue #557 read-time usage derivations: the sessions
                    -- table carries no reasoning/provider columns, so the
                    -- reasoning total and primary provider (most frequent
                    -- provider by record count, alphabetical tie-break) are
                    -- aggregated from usage_events for this session only.
                    COALESCE(ua.total_reasoning_tokens, 0)
                        AS total_reasoning_tokens,
                    pr.provider AS primary_provider
                FROM sessions s
                LEFT JOIN opencode_source_projects osp
                    ON osp.source_database_id = s.source_database_id
                    AND osp.external_project_id = s.project_id
                LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(ue.reasoning_tokens), 0)::bigint
                        AS total_reasoning_tokens
                    FROM usage_events ue
                    WHERE ue.session_id = s.id
                ) ua ON true
                LEFT JOIN LATERAL (
                    SELECT ue.provider
                    FROM usage_events ue
                    WHERE ue.session_id = s.id
                      AND ue.provider IS NOT NULL
                      AND ue.provider <> ''
                    GROUP BY ue.provider
                    ORDER BY COUNT(*) DESC, ue.provider ASC
                    LIMIT 1
                ) pr ON true
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
                    quiet_threshold_minutes=quiet_threshold_minutes,
                    stale_threshold_hours=stale_threshold_hours,
                    unknown_threshold_hours=unknown_threshold_hours,
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
                quiet_threshold_minutes=quiet_threshold_minutes,
                stale_threshold_hours=stale_threshold_hours,
                unknown_threshold_hours=unknown_threshold_hours,
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
            content=tr["content"],
            status=tr["status"] or "pending",
            priority=tr["priority"],
            position=tr["position"],
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
        code_change_count=_int_or_zero(session_row, "code_change_count"),
        code_change_additions=_int_or_zero(session_row, "code_change_additions"),
        code_change_deletions=_int_or_zero(session_row, "code_change_deletions"),
        session_context=session_context,
        message_count=session_row["message_count"],
        total_input_tokens=session_row["total_input_tokens"],
        total_output_tokens=session_row["total_output_tokens"],
        total_cached_tokens=session_row["total_cached_tokens"],
        total_cache_read_tokens=session_row["total_cache_read_tokens"],
        total_cache_write_tokens=session_row["total_cache_write_tokens"],
        total_reasoning_tokens=_int_or_zero(session_row, "total_reasoning_tokens"),
        primary_provider=_text_or_none(session_row, "primary_provider"),
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
    response: Response,
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

    **Status derivation** uses a configurable quiet-threshold heuristic
    (15 min by default), a stale-threshold heuristic (2 h by default), and
    an unknown-threshold heuristic (48 h by default) to produce one of five
    values: ``running``, ``completed``, ``blocked``, ``stale``, or
    ``unknown``. See :func:`_compute_status` for the full derivation rules.
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
    _apply_deprecation_header(response, settings)
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
            quiet_threshold_minutes=settings.quiet_threshold_minutes,
            stale_threshold_hours=settings.stale_threshold_hours,
            unknown_threshold_hours=settings.unknown_threshold_hours,
        )


@router.get("/agent-runs/{session_id}")
async def get_agent_run_detail(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    conn: asyncpg.Connection = Depends(get_session),
) -> AgentRunDetail:
    """Return a full agent run detail view keyed by internal Gateway session UUID.

    Includes parent identifiers, child summaries, project details,
    Session Context (placeholder), and usage totals.

    No OpenCode event, transcript, or message-part replay data is
    included — this endpoint returns aggregated facts only.
    """
    settings = get_settings()
    _apply_deprecation_header(response, settings)
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_agent_run_detail(
            conn, session_id, settings.grafana_base_url,
            db_timeout_seconds=settings.database_timeout_seconds,
            status_timeout_seconds=settings.status_computation_timeout_seconds,
            quiet_threshold_minutes=settings.quiet_threshold_minutes,
            stale_threshold_hours=settings.stale_threshold_hours,
            unknown_threshold_hours=settings.unknown_threshold_hours,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Records-with-context helpers
# ═══════════════════════════════════════════════════════════════════════════

# Valid group-by dimensions for the records-with-context endpoint
RECORDS_WITH_CONTEXT_GROUP_BY: frozenset[str] = frozenset(
    {"project", "session", "agent", "model"}
)

# SQL expression for project label resolution
# COALESCE(display_name w/o workspace suffix, name w/o workspace suffix,
#          worktree basename w/o workspace suffix,
#          external_project_id w/o workspace suffix, 'unknown')
# Every branch strips a trailing workspace numeric suffix (``-\d+$``);
# empty strip results cascade to 'unknown'.
_PROJECT_LABEL_SQL = """
    COALESCE(
        NULLIF(regexp_replace(osp.display_name, '-\\d+$', ''), ''),
        NULLIF(regexp_replace(osp.name, '-\\d+$', ''), ''),
        CASE
            WHEN osp.worktree IS NULL
                  OR osp.worktree = ''
                  OR osp.worktree = '/' THEN NULL
            ELSE NULLIF(
                regexp_replace(substring(osp.worktree, '([^/]+)$'), '-\\d+$', ''),
                ''
            )
        END,
        CASE
            WHEN s.project_id IS NULL THEN NULL
            WHEN length(s.project_id) > 12 THEN substring(s.project_id, 1, 12) || '…'
            ELSE NULLIF(regexp_replace(s.project_id, '-\\d+$', ''), '')
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
            NULLIF(our.provider, '') AS provider,
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
    response: Response,
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
    _apply_deprecation_header(response, settings)

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
