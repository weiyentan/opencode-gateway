"""AFK Dashboard summary API (issue #719).

One GET endpoint under ``/api/v1/afk/dashboard/summary`` answers the AFK
Dashboard's "what happened over this window?" question by reading the
pre-aggregated ``afk_dashboard_daily`` rollup (issue #714) and shaping it
into a stable summary contract:

- Filters: ``provider`` and ``repository`` (both optional), a date range
  (``from_date`` / ``to_date``, defaulting to the last 30 calendar days), and
  an ``interval`` of ``daily`` or ``monthly``.
- ``monthly`` aggregates the daily rows **at query time** (a plain sum per
  ``(provider, repository)`` and calendar month) — there is no materialised
  monthly rollup table.
- The response carries ``derived_at`` freshness metadata: the latest rollup
  recompute across the returned buckets (``None`` when the window is empty).

The endpoint hides the internal table structure — the summary is the
contract.  It follows the ``app/api/afk_outcomes.py`` /
``app/api/afk_runs.py`` convention: raw asyncpg via ``Depends(get_session)``,
explicit-column SELECTs, parameterised filters with 400 on invalid
enum/date/interval values, and the ``_db_timeout`` / ``_request_timeout``
helpers.  All responses use the shared ``{status, data, error}`` envelope and
are protected by the global :class:`~app.core.auth.ApiKeyMiddleware`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from afk_outcomes.models import Provider
from app.core.config import get_settings
from app.core.schemas.afk_dashboard import (
    AFKDashboardSummary,
    AFKDashboardSummaryBucket,
    SummaryInterval,
)
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

router = APIRouter(tags=["afk-dashboard"])

# ── Valid filter values (locked domain vocabulary) ───────────────────────────

_VALID_PROVIDERS = frozenset(m.value for m in Provider)
_VALID_INTERVALS = frozenset(m.value for m in SummaryInterval)

# "Last 30 days" is an inclusive calendar window: to_date - 29 days → to_date.
_DEFAULT_WINDOW_DAYS = 30

# ── Shared SQL fragments ─────────────────────────────────────────────────────

# Additive metric columns — monthly aggregation is a plain SUM over them.
_METRIC_COLUMNS = """
    COALESCE(SUM(runs_started), 0)::bigint AS runs_started,
    COALESCE(SUM(change_requests_opened), 0)::bigint AS change_requests_opened,
    COALESCE(SUM(change_requests_merged), 0)::bigint AS change_requests_merged,
    COALESCE(SUM(change_requests_closed), 0)::bigint AS change_requests_closed,
    COALESCE(SUM(execution_count), 0)::bigint AS execution_count,
    COALESCE(SUM(successful_execution_count), 0)::bigint
        AS successful_execution_count,
    COALESCE(SUM(failed_execution_count), 0)::bigint AS failed_execution_count,
    COALESCE(SUM(cancelled_execution_count), 0)::bigint
        AS cancelled_execution_count,
    COALESCE(SUM(session_count), 0)::bigint AS session_count,
    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::bigint AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0)::bigint AS cache_write_tokens,
    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
    MAX(derived_at) AS derived_at
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_date(raw: str | None, param_name: str) -> date | None:
    """Parse an ISO-8601 date query param, raising 400 on malformed values."""
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {param_name}: {raw!r} is not a valid ISO-8601 date",
        ) from None


def _require_enum_value(raw: str | None, valid: frozenset[str], param_name: str) -> None:
    """Raise 400 when *raw* is not a member of *valid*."""
    if raw is not None and raw not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid {param_name}: {raw!r}. "
                f"Valid values: {', '.join(sorted(valid))}"
            ),
        )


def _resolve_date_range(
    from_raw: date | None,
    to_raw: date | None,
    *,
    today: date,
) -> tuple[date, date]:
    """Resolve the effective ``(from_date, to_date)`` window.

    An omitted ``to_date`` defaults to today; an omitted ``from_date``
    defaults to a 30-calendar-day inclusive window ending at the resolved
    ``to_date``.  Both sides are validated in order (from <= to).
    """
    to_date = to_raw or today
    from_date = from_raw or (to_date - timedelta(days=_DEFAULT_WINDOW_DAYS - 1))
    if from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date range: from_date must not be after to_date",
        )
    return from_date, to_date


def _period_expression(interval: str) -> str:
    """The SQL expression producing a bucket's start day for *interval*."""
    if interval == SummaryInterval.MONTHLY.value:
        # Aggregate the daily rows per calendar month at query time.
        return "date_trunc('month', day)::date"
    return "day"


def _build_summary_filters(
    from_date: date,
    to_date: date,
    provider: str | None,
    repository: str | None,
) -> tuple[str, list[object]]:
    """Build the WHERE clause and parameter list for the summary query."""
    params: list[object] = [from_date, to_date]
    filters: list[str] = ["day >= $1", "day <= $2"]
    if provider is not None:
        filters.append(f"provider = ${len(params) + 1}")
        params.append(provider)
    if repository is not None:
        filters.append(f"repository = ${len(params) + 1}")
        params.append(repository)
    return " AND ".join(filters), params


def _bucket(row: asyncpg.Record) -> AFKDashboardSummaryBucket:
    """Build an :class:`AFKDashboardSummaryBucket` from an aggregated row."""
    return AFKDashboardSummaryBucket(
        period_start=row["period_start"],
        provider=row["provider"],
        repository=row["repository"],
        runs_started=row["runs_started"],
        change_requests_opened=row["change_requests_opened"],
        change_requests_merged=row["change_requests_merged"],
        change_requests_closed=row["change_requests_closed"],
        execution_count=row["execution_count"],
        successful_execution_count=row["successful_execution_count"],
        failed_execution_count=row["failed_execution_count"],
        cancelled_execution_count=row["cancelled_execution_count"],
        session_count=row["session_count"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        estimated_cost_usd=row["estimated_cost_usd"],
        derived_at=row["derived_at"],
    )


async def _fetch_summary(
    conn: asyncpg.Connection,
    interval: str,
    from_date: date,
    to_date: date,
    provider: str | None,
    repository: str | None,
    *,
    db_timeout_seconds: int,
) -> AFKDashboardSummary:
    """Run the grouped, parameterised summary query and shape the response."""
    period = _period_expression(interval)
    where_clause, params = _build_summary_filters(
        from_date, to_date, provider, repository
    )
    sql = f"""
        SELECT
            {period} AS period_start,
            provider,
            repository,
            {_METRIC_COLUMNS}
        FROM afk_dashboard_daily
        WHERE {where_clause}
        GROUP BY {period}, provider, repository
        ORDER BY period_start ASC, provider ASC, repository ASC
    """
    async with timed_operation("db.query.afk_dashboard.summary", "db"):
        async with _db_timeout(
            "db.query.afk_dashboard.summary", db_timeout_seconds
        ):
            rows = await conn.fetch(sql, *params)

    buckets = [_bucket(row) for row in rows]
    derived_values = [b.derived_at for b in buckets if b.derived_at is not None]
    return AFKDashboardSummary(
        interval=interval,
        from_date=from_date,
        to_date=to_date,
        provider=provider,
        repository=repository,
        buckets=buckets,
        derived_at=max(derived_values) if derived_values else None,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/summary")
async def get_afk_dashboard_summary(
    request: Request,
    provider: str | None = Query(default=None),
    repository: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    interval: str = Query(default=SummaryInterval.DAILY.value),
    conn: asyncpg.Connection = Depends(get_session),
) -> AFKDashboardSummary:
    """Return the AFK Dashboard summary for the requested window and interval.

    Filters: ``provider`` (validated against the locked provider vocabulary),
    ``repository``, and the date range ``from_date`` / ``to_date``
    (``start_date`` / ``end_date`` are accepted as aliases; an explicit
    ``from_date`` / ``to_date`` wins).  The range defaults to the last 30
    calendar days, and ``interval`` (``daily`` | ``monthly``) selects the
    bucket granularity — ``monthly`` sums the daily rows at query time.

    The response is an :class:`AFKDashboardSummary` with the ordered buckets
    and the ``derived_at`` freshness marker (latest rollup recompute across
    the returned buckets, ``None`` when empty).
    """
    _require_enum_value(provider, _VALID_PROVIDERS, "provider")
    _require_enum_value(interval, _VALID_INTERVALS, "interval")

    from_date_dt = _parse_date(from_date if from_date is not None else start_date,
                               "from_date")
    to_date_dt = _parse_date(to_date if to_date is not None else end_date,
                             "to_date")
    resolved_from, resolved_to = _resolve_date_range(
        from_date_dt,
        to_date_dt,
        today=datetime.now(timezone.utc).date(),
    )

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_summary(
            conn,
            interval,
            resolved_from,
            resolved_to,
            provider,
            repository,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
