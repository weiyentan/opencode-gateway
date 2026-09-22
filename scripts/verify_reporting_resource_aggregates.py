#!/usr/bin/env python3
"""Reporting aggregate consistency verification (issue #729).

Compares ``reporting_resource_aggregates`` against the canonical
``reporting_deliveries`` rows per **stable resource identity**
 ``(provider, repository_url, resource_type, resource_number)`` (ADR 0018).
The current aggregate's forward-advanced ``last_delivery_id`` /
``last_occurred_at`` pointer must resolve to the resource's newest delivery;
a resource with window deliveries but no aggregate (or an aggregate with no
backing delivery in the window) is a mismatch.  The canonical delivery count
for the identity is reported as context.

This script checks **only** reporting aggregate consistency.  AFK Dashboard
rollup parity checks have been moved to ``verify_afk_dashboard_daily.py``
(issue #729) so each script has an independent exit status — an AFK rollup
drift cannot fail the reporting verification job.

Exit code is ``0`` when every aggregate matches its canonical source and
non-zero when any mismatch is found, so a Kubernetes CronJob can alert on
the job status.  There is no ``--fix`` mode: verification reports only;
correcting an aggregate remains the job of the recompute tooling.

Usage:
    python scripts/verify_reporting_resource_aggregates.py [--window-days N]
    python scripts/verify_reporting_resource_aggregates.py --from-date 2026-09-01 --to-date 2026-09-15
    python scripts/verify_reporting_resource_aggregates.py --window-days 3 --json

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
from typing import Sequence

import asyncpg

# Allow running from any location by resolving the repo root relative to this script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

# Shared helpers (issue #729): comparison types, window parsing, pure helpers.
from scripts.verify_helpers import (  # noqa: E402
    Mismatch,
    VerificationWindow,
    compare_reporting_aggregates,
    exit_code as _exit_code,
    format_mismatch as _format_mismatch,
    group_mismatches,
    parse_window,
)

logger = logging.getLogger("verify_reporting_resource_aggregates")

SOURCE_REPORTING_AGGREGATES = "reporting_resource_aggregates"

# ---------------------------------------------------------------------------
# SQL (SELECT-only)
#
# The aggregate table is the *current* state (one row per stable resource
# identity), so it is read in full and window-guarded in Python; the
# canonical delivery rows are windowed by their UTC day.
# ---------------------------------------------------------------------------

REPORTING_AGGREGATES_SQL = """
    SELECT provider,
           repository_url,
           resource_type,
           resource_number,
           last_occurred_at,
           last_delivery_id
    FROM reporting_resource_aggregates
    ORDER BY provider, repository_url, resource_type, resource_number
"""

REPORTING_DELIVERIES_SQL = """
    SELECT provider,
           delivery_id,
           occurred_at,
           payload
    FROM reporting_deliveries
    WHERE (occurred_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
    ORDER BY provider, occurred_at, delivery_id
"""


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


async def _fetch_reporting_mismatches(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the reporting-aggregate comparison for the window."""
    aggregate_rows = await conn.fetch(REPORTING_AGGREGATES_SQL)
    delivery_rows = await conn.fetch(
        REPORTING_DELIVERIES_SQL, window.from_date, window.to_date,
    )
    return compare_reporting_aggregates(aggregate_rows, delivery_rows, window)


async def _run_verification(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the reporting-aggregate consistency check and return mismatches."""
    return await _fetch_reporting_mismatches(conn, window)


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
        "Reporting aggregate consistency verification window %s..%s (%d day(s)): "
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
        description="Read-only reporting aggregate consistency verification "
        "against reporting_deliveries source data.",
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
            "Read-only verification (dry-run): no aggregate or delivery row "
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
