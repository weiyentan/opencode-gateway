#!/usr/bin/env python3
"""Nightly ``afk_dashboard_daily`` parity verification (issue #718).

Compares the Gateway's derived ``afk_dashboard_daily`` rollup against the
canonical source tables the recomputation engine
(``app.core.afk_dashboard_daily``) projects from, over a configurable recent
window, and reports every disagreement by ``(day, provider, repository)`` with
a per-metric breakdown.  It is strictly **read-only** — it never inserts,
updates, deletes, or otherwise mutates a rollup or a canonical row.  Its SQL is
SELECT-only by construction.

This script checks **only** AFK Dashboard rollup parity.  Reporting aggregate
checks have been moved to ``verify_reporting_resource_aggregates.py`` (issue
#729) so each script has an independent exit status — a reporting-aggregate
drift cannot fail the AFK Dashboard verification job.

Exit code is ``0`` when every rollup matches its canonical source and non-zero
when any mismatch is found, so a Kubernetes CronJob can alert on the job status.
There is no ``--fix`` mode: verification reports only; correcting a rollup
remains the job of the recompute tooling.

Usage:
    python scripts/verify_afk_dashboard_daily.py [--window-days N]
    python scripts/verify_afk_dashboard_daily.py --from-date 2026-09-01 --to-date 2026-09-15
    python scripts/verify_afk_dashboard_daily.py --window-days 3 --json

Flags:
    --window-days N   Verify the last N days including today (default: 7).
    --from-date DATE  Explicit inclusive window start (YYYY-MM-DD); requires
                      --to-date.  Overrides --window-days.
    --to-date DATE    Explicit inclusive window end (YYYY-MM-DD).
    --json            Emit the report as JSON on stdout (for CronJob alerting).
    --dry-run         Accepted for CronJob symmetry; always read-only and logs
                      an explicit no-write confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from typing import Any, Mapping, Sequence

import asyncpg

# Allow running from any location by resolving the repo root relative to this script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

# Shared helpers (issue #729): comparison types, window parsing, pure helpers.
from scripts.verify_helpers import (  # noqa: E402
    Mismatch,
    VerificationWindow,
    compare_afk_dashboard_daily_rows as _compare_afk_rows,
    exit_code as _exit_code,
    format_mismatch as _format_mismatch,
    group_mismatches,
    parse_window,
)

from app.core.afk_dashboard_daily import (  # noqa: E402
    CR_EVENT_FILTER,
    METRIC_COLUMNS as AFK_DASHBOARD_METRIC_COLUMNS,
    UNAMBIGUOUS_CTE,
)

logger = logging.getLogger("verify_afk_dashboard_daily")

SOURCE_AFK_DASHBOARD_DAILY = "afk_dashboard_daily"
# Backward-compatible alias for the historical source label.
SOURCE_USAGE_ROLLUP = SOURCE_AFK_DASHBOARD_DAILY

# ---------------------------------------------------------------------------
# SQL (SELECT-only)
#
# The canonical side recomputes every metric of the ``afk_dashboard_daily``
# rollup from the same source tables the recompute engine
# (``app.core.afk_dashboard_daily``) reads, grouped by
# ``(day, provider, repository)`` for the whole window in one round trip.
# A FULL OUTER JOIN against ``afk_dashboard_daily`` then flags three
# disagreement shapes — a rollup row whose metrics differ, a rollup row with no
# matching canonical group (stale), and a canonical group with no rollup row
# (missing).  Both sides are restricted to the verification window by the UTC
# calendar day.
# ---------------------------------------------------------------------------

_AFK_ROLLUP_COLUMNS_SQL = ",\n           ".join(
    f"d.{column} AS rollup_{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)
_AFK_CANONICAL_COLUMNS_SQL = ",\n           ".join(
    f"c.{column} AS canonical_{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)
_AFK_MISMATCH_PREDICATE = "\n         OR ".join(
    f"d.{column} != c.{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)

AFK_DASHBOARD_DAILY_MISMATCH_SQL = f"""
    SELECT COALESCE(d.day, c.day) AS day,
           COALESCE(d.provider, c.provider) AS provider,
           COALESCE(d.repository, c.repository) AS repository,
           {_AFK_ROLLUP_COLUMNS_SQL},
           {_AFK_CANONICAL_COLUMNS_SQL}
    FROM afk_dashboard_daily d
    FULL OUTER JOIN (
        WITH runs_agg AS (
            SELECT (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider,
                   r.repository,
                   COUNT(*)::int AS runs_started
            FROM afk_runs r
            WHERE r.repository IS NOT NULL
              AND (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        cr_agg AS (
            SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day,
                   e.provider,
                   e.repository,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.opened')::int
                       AS change_requests_opened,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.merged')::int
                       AS change_requests_merged,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.closed')::int
                       AS change_requests_closed
            FROM engineering_events e
            WHERE {CR_EVENT_FILTER}
              AND e.repository IS NOT NULL
              AND (e.occurred_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
            GROUP BY day, e.provider, e.repository
        ),
        exec_agg AS (
            SELECT (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   b.provider,
                   b.repository_url AS repository,
                   COUNT(*)::int AS execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'completed')::int
                       AS successful_execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'failed')::int
                       AS failed_execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'cancelled')::int
                       AS cancelled_execution_count
            FROM execution_bindings b
            WHERE b.repository_url IS NOT NULL
              AND (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, b.provider, b.repository_url
        ),
        unambiguous AS (
{UNAMBIGUOUS_CTE}
        ),
        sess_agg AS (
            SELECT (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider,
                   r.repository,
                   COUNT(*)::int AS session_count
            FROM afk_run_sessions ars
            JOIN unambiguous u ON u.session_id = ars.session_id
            JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
            WHERE r.repository IS NOT NULL
              AND (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        usage_agg AS (
            SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
                   r.provider,
                   r.repository,
                   COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
                   COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
                   COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
                   COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
                   COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd
            FROM usage_events ue
            JOIN unambiguous u ON u.session_id = ue.session_id
            JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
            WHERE r.repository IS NOT NULL
              AND (ue.reported_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        canonical AS (
            SELECT COALESCE(r.day, cr.day, ex.day, se.day, ug.day) AS day,
                   COALESCE(r.provider, cr.provider, ex.provider, se.provider,
                            ug.provider) AS provider,
                   COALESCE(r.repository, cr.repository, ex.repository,
                            se.repository, ug.repository) AS repository,
                   COALESCE(r.runs_started, 0)::int AS runs_started,
                   COALESCE(cr.change_requests_opened, 0)::int
                       AS change_requests_opened,
                   COALESCE(cr.change_requests_merged, 0)::int
                       AS change_requests_merged,
                   COALESCE(cr.change_requests_closed, 0)::int
                       AS change_requests_closed,
                   COALESCE(ex.execution_count, 0)::int AS execution_count,
                   COALESCE(ex.successful_execution_count, 0)::int
                       AS successful_execution_count,
                   COALESCE(ex.failed_execution_count, 0)::int
                       AS failed_execution_count,
                   COALESCE(ex.cancelled_execution_count, 0)::int
                       AS cancelled_execution_count,
                   COALESCE(se.session_count, 0)::int AS session_count,
                   COALESCE(ug.input_tokens, 0)::int AS input_tokens,
                   COALESCE(ug.output_tokens, 0)::int AS output_tokens,
                   COALESCE(ug.cache_read_tokens, 0)::int AS cache_read_tokens,
                   COALESCE(ug.cache_write_tokens, 0)::int AS cache_write_tokens,
                   COALESCE(ug.estimated_cost_usd, 0) AS estimated_cost_usd
            FROM runs_agg r
            FULL OUTER JOIN cr_agg cr
              ON cr.day = r.day
             AND cr.provider = r.provider
             AND cr.repository = r.repository
            FULL OUTER JOIN exec_agg ex
              ON ex.day = COALESCE(r.day, cr.day)
             AND ex.provider = COALESCE(r.provider, cr.provider)
             AND ex.repository = COALESCE(r.repository, cr.repository)
            FULL OUTER JOIN sess_agg se
              ON se.day = COALESCE(r.day, cr.day, ex.day)
             AND se.provider = COALESCE(r.provider, cr.provider, ex.provider)
             AND se.repository = COALESCE(r.repository, cr.repository,
                                          ex.repository)
            FULL OUTER JOIN usage_agg ug
              ON ug.day = COALESCE(r.day, cr.day, ex.day, se.day)
             AND ug.provider = COALESCE(r.provider, cr.provider, ex.provider,
                                        se.provider)
             AND ug.repository = COALESCE(r.repository, cr.repository,
                                          ex.repository, se.repository)
        )
        SELECT * FROM canonical
    ) c
      ON d.day = c.day
     AND d.provider = c.provider
     AND d.repository = c.repository
    WHERE COALESCE(d.day, c.day) BETWEEN $1 AND $2
      AND ( d.day IS NULL
         OR c.day IS NULL
         OR {_AFK_MISMATCH_PREDICATE} )
    ORDER BY day, provider, repository
"""

# Backward-compatible alias: the comparison now targets ``afk_dashboard_daily``
# but the module's historical constant name is retained for callers/tests.
USAGE_ROLLUP_MISMATCH_SQL = AFK_DASHBOARD_DAILY_MISMATCH_SQL


# ---------------------------------------------------------------------------
# Pure comparison (AFK-specific wrapper around shared helper)
# ---------------------------------------------------------------------------


def compare_afk_dashboard_daily_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mismatch]:
    """Map SQL mismatch rows (already filtered) into grouped mismatch records.

    Thin wrapper around the shared helper that supplies the AFK-specific
    metric columns and source label.
    """
    return _compare_afk_rows(
        rows,
        metric_columns=AFK_DASHBOARD_METRIC_COLUMNS,
        source_label=SOURCE_AFK_DASHBOARD_DAILY,
    )


# Backward-compatible alias for the historical helper name.
compare_usage_rollup_rows = compare_afk_dashboard_daily_rows


# ---------------------------------------------------------------------------
# Database fetch paths
# ---------------------------------------------------------------------------


async def _get_pool() -> asyncpg.Pool:
    """Create a database connection pool from application settings."""
    settings = get_settings()
    return await asyncpg.create_pool(
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
        user=settings.database_user,
        password=settings.database_password,
        min_size=1,
        max_size=2,
    )


async def _fetch_afk_dashboard_mismatches(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the ``afk_dashboard_daily`` mismatch query for the window."""
    rows = await conn.fetch(
        AFK_DASHBOARD_DAILY_MISMATCH_SQL, window.from_date, window.to_date,
    )
    return compare_afk_dashboard_daily_rows(rows)


# Backward-compatible alias for the historical fetch helper name.
_fetch_usage_mismatches = _fetch_afk_dashboard_mismatches


async def _run_verification(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the AFK dashboard daily rollup-parity comparison and return grouped mismatches."""
    return await _fetch_afk_dashboard_mismatches(conn, window)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _emit_report(
    window: VerificationWindow,
    mismatches: Sequence[Mismatch],
    *,
    as_json: bool = False,
) -> None:
    """Log a human-readable report (or emit JSON on stdout)."""
    if as_json:
        print(
            json.dumps(
                {
                    "window": {
                        "from_date": window.from_date.isoformat(),
                        "to_date": window.to_date.isoformat(),
                        "days": window.day_count,
                    },
                    "mismatch_count": len(mismatches),
                    "mismatches": [m.as_dict() for m in mismatches],
                },
                default=str,
            )
        )
        return

    logger.info(
        "AFK dashboard daily parity verification window %s..%s (%d day(s)): "
        "%d mismatch(es).",
        window.from_date,
        window.to_date,
        window.day_count,
        len(mismatches),
    )
    for (day, provider, repository), group in group_mismatches(mismatches).items():
        logger.warning(
            "Mismatch group day=%s provider=%s repository=%s (%d bucket(s)):",
            day,
            provider,
            repository,
            len(group),
        )
        for mismatch in group:
            logger.warning("  %s", _format_mismatch(mismatch))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only afk_dashboard_daily parity verification against "
        "the canonical AFK/usage/engineering source tables.",
    )
    parser.add_argument(
        "--window-days",
        dest="days",
        type=int,
        default=7,
        help="Verify the last N days including today (default: 7).",
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        default=None,
        help="Explicit inclusive window start (YYYY-MM-DD); requires --to-date.",
    )
    parser.add_argument(
        "--to-date",
        type=date.fromisoformat,
        default=None,
        help="Explicit inclusive window end (YYYY-MM-DD); requires --from-date.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON on stdout (for CronJob alerting).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Accepted for CronJob symmetry: this tool is always read-only, "
        "so dry-run only logs an explicit no-write confirmation.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, verify, report; never write."""
    args = _parse_args(argv)
    window = parse_window(
        days=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    if args.dry_run:
        logger.info(
            "Read-only verification (dry-run): no rollup or canonical row "
            "will be modified, deleted, or updated.",
        )

    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            mismatches = await _run_verification(conn, window)
    finally:
        await pool.close()

    _emit_report(window, mismatches, as_json=args.json)
    return _exit_code(mismatches)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
