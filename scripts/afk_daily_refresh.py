#!/usr/bin/env python3
"""Daily refresh of recent ``client_project_rollup`` buckets (issue #717).

Kubernetes CronJob entry point for the AFK dashboard daily refresh.  It
recomputes the Client Project Rollup (migration 0023, ADR 0015) for a small,
configurable window of recent UTC days — by default **today and yesterday** —
by delegating to the recomputation engine in
``scripts/backfill_client_project_rollup.py``: the same grouped additive
``SUM(usage_events)`` over the five :data:`~app.core.reconciliation.ROLLUP_FIELDS`
(input, output, cache read, cache write tokens plus estimated cost) and the
same ``INSERT ... ON CONFLICT DO UPDATE`` correction toward the event sums.

Unlike the one-shot backfill, this refresh is bounded to the configured day
window and never touches rows outside it.  Only ``(client_id, project_id,
day)`` groups that genuinely disagree with ``SUM(usage_events)`` are written.

Concurrency is prevented with a database advisory lock in the
:data:`~app.core.reporting_aggregates.AGGREGATE_LOCK_CLASS` (``47_006``)
namespace, acquired with ``pg_try_advisory_lock``.  If another refresh already
holds the lock, this process logs and exits **0** immediately — a CronJob
overlap is a normal, non-error condition.

Usage:
    python scripts/afk_daily_refresh.py [--days N] [--as-of YYYY-MM-DD]

Flags:
    --days N          Number of days to recompute, ending at --as-of
                      (default: 2 = today and yesterday).
    --as-of DATE      UTC date anchoring the end of the window
                      (default: today, UTC).  Useful for replaying a
                      specific day.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.reporting_aggregates import (  # noqa: E402
    AGGREGATE_LOCK_CLASS,
)
from scripts.backfill_client_project_rollup import (  # noqa: E402
    EVENT_AGGREGATE_SQL,
    _get_pool,
    _parse_row_count,
)

logger = logging.getLogger("afk_daily_refresh")

DEFAULT_WINDOW_DAYS = 2
"""Default window size: today plus yesterday (a single-day-safe default)."""

DAILY_REFRESH_LOCK_KEY = 0
"""Low 32 bits of the daily-refresh advisory lock.

The high 32 bits are :data:`AGGREGATE_LOCK_CLASS` (``47_006``), the same
class used by the per-resource aggregate enrichment lock.  A fixed low key
serialises the scheduled refresh against any concurrently running refresh;
per-resource keys are derived from a resource hash and are essentially never
``0``, so the two usages do not contend in practice.
"""

# ---------------------------------------------------------------------------
# SQL
#
# The recompute source is the engine's grouped additive SUM over canonical
# usage_events (``EVENT_AGGREGATE_SQL`` in the backfill script) — reused
# verbatim so ingest-time and refresh-time math can never drift.  The window
# predicate filters the *grouped* rows by their UTC day bucket, so only
# buckets inside the configured window are ever written.  The LEFT JOIN +
# mismatch predicates restrict the upsert to genuinely disagreeing groups,
# exactly as the engine does.
# ---------------------------------------------------------------------------

DAILY_BACKFILL_SQL = f"""WITH grouped AS (
{EVENT_AGGREGATE_SQL}
)
INSERT INTO client_project_rollup
    (client_id, project_id, day,
     input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
     estimated_cost_usd)
SELECT g.client_id, g.project_id, g.day,
       g.input_tokens, g.output_tokens, g.cache_read_tokens,
       g.cache_write_tokens, g.estimated_cost_usd
FROM grouped g
LEFT JOIN client_project_rollup r
  ON r.client_id = g.client_id
 AND r.project_id = g.project_id
 AND r.day = g.day
WHERE g.day >= $1
  AND g.day <= $2
  AND (
        r.client_id IS NULL
     OR r.input_tokens != g.input_tokens
     OR r.output_tokens != g.output_tokens
     OR r.cache_read_tokens != g.cache_read_tokens
     OR r.cache_write_tokens != g.cache_write_tokens
     OR r.estimated_cost_usd != g.estimated_cost_usd
  )
ON CONFLICT (client_id, project_id, day)
DO UPDATE SET
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cache_read_tokens = EXCLUDED.cache_read_tokens,
    cache_write_tokens = EXCLUDED.cache_write_tokens,
    estimated_cost_usd = EXCLUDED.estimated_cost_usd
"""


# ---------------------------------------------------------------------------
# Window + argument parsing
# ---------------------------------------------------------------------------


def _parse_iso_date(value: str) -> date:
    """argparse type: parse a ``YYYY-MM-DD`` date or fail fast."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute recent client_project_rollup buckets from "
        "usage_events (default: today and yesterday, UTC).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="Number of days to recompute, ending at --as-of "
        "(default: 2 = today and yesterday).",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_iso_date,
        default=None,
        help="UTC date anchoring the end of the window (default: today, UTC).",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")
    return args


def _resolve_window(
    days: int,
    as_of: date | None = None,
) -> tuple[date, date]:
    """Return the inclusive ``(start_day, end_day)`` window.

    ``days`` is the window size ending at ``as_of`` (default: today, UTC).
    A ``days`` below 1 is rejected — the refresh must never produce an
    inverted or empty window.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    end_day = as_of if as_of is not None else datetime.now(timezone.utc).date()
    start_day = end_day - timedelta(days=days - 1)
    return start_day, end_day


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


async def _try_acquire_refresh_lock(conn: asyncpg.Connection) -> bool:
    """Try to acquire the daily-refresh advisory lock.

    Uses ``pg_try_advisory_lock`` in the :data:`AGGREGATE_LOCK_CLASS`
    namespace so the caller can distinguish "another refresh is running"
    from other failures without blocking.
    """
    locked: bool = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)",
        AGGREGATE_LOCK_CLASS,
        DAILY_REFRESH_LOCK_KEY,
    )
    return bool(locked)


async def _release_refresh_lock(conn: asyncpg.Connection) -> None:
    """Release the daily-refresh advisory lock (session-level)."""
    await conn.execute(
        "SELECT pg_advisory_unlock($1, $2)",
        AGGREGATE_LOCK_CLASS,
        DAILY_REFRESH_LOCK_KEY,
    )


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------


async def _run_windowed_recompute(
    conn: asyncpg.Connection,
    start_day: date,
    end_day: date,
) -> int:
    """Recompute disagreeing rollup buckets inside the window.

    Returns the number of rollup rows upserted.
    """
    result = await conn.execute(DAILY_BACKFILL_SQL, start_day, end_day)
    return _parse_row_count(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, acquire lock, recompute window."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    start_day, end_day = _resolve_window(args.days, args.as_of)
    logger.info(
        "Daily rollup refresh window: %s .. %s (%d day(s)).",
        start_day,
        end_day,
        args.days,
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            if not await _try_acquire_refresh_lock(conn):
                logger.warning(
                    "Another rollup refresh holds the aggregate lock "
                    "(class=%s key=%s); exiting cleanly.",
                    AGGREGATE_LOCK_CLASS,
                    DAILY_REFRESH_LOCK_KEY,
                )
                return 0

            try:
                updated = await _run_windowed_recompute(conn, start_day, end_day)
                logger.info(
                    "Recomputed %d rollup row(s) for window %s .. %s.",
                    updated,
                    start_day,
                    end_day,
                )
            finally:
                await _release_refresh_lock(conn)

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
