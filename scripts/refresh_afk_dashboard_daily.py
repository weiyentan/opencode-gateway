#!/usr/bin/env python3
"""Daily refresh of recent ``afk_dashboard_daily`` buckets (issue #717).

Kubernetes CronJob entry point for the AFK dashboard daily refresh.  It
recomputes the pre-aggregated AFK dashboard rollup (``afk_dashboard_daily``,
migration 0046) for a small, configurable window of recent UTC days — by
default **today and yesterday** — by delegating the per-bucket metric
computation to the shared recomputation engine in
``app.core.afk_dashboard_daily`` (issue #715): it discovers every
``(day, provider, repository)`` bucket that carries activity in the window,
then calls :func:`~app.core.afk_dashboard_daily.recompute_bucket` for each.

The engine derives every additive metric from the canonical source tables,
so the rollup stays a rebuildable projection of source facts.  Unlike the
one-shot backfill, this refresh is bounded to the configured day window and
never touches buckets outside it.

Each bucket is recomputed in its own transaction: the script takes the
engine's per-bucket advisory lock
(:func:`~app.core.afk_dashboard_daily.acquire_bucket_lock`, a
transaction-scoped ``pg_advisory_xact_lock`` in the
:data:`~app.core.afk_dashboard_daily.AFK_DASHBOARD_LOCK_CLASS` namespace)
before calling :func:`~app.core.afk_dashboard_daily.recompute_bucket`, so
concurrent recomputations of the *same* bucket serialise while different
buckets never contend.

Concurrency between refresh runs is prevented with a **session-level**
database advisory lock in the :data:`~app.core.reporting_aggregates.AGGREGATE_LOCK_CLASS`
(``47_006``) namespace, acquired with ``pg_try_advisory_lock``.  If another
refresh already holds the lock, this process logs and exits **0** immediately
— a CronJob overlap is a normal, non-error condition.

Usage:
    python scripts/refresh_afk_dashboard_daily.py [--days-back N] [--as-of YYYY-MM-DD]

Flags:
    --days-back N     Number of days to recompute, ending at --as-of
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

from app.core.afk_dashboard_daily import (  # noqa: E402
    AFK_DASHBOARD_LOCK_CLASS,
    acquire_bucket_lock,
    recompute_bucket,
)
from app.core.reporting_aggregates import (  # noqa: E402
    AGGREGATE_LOCK_CLASS,
)
from scripts.backfill_client_project_rollup import (  # noqa: E402
    _get_pool,
)

logger = logging.getLogger("refresh_afk_dashboard_daily")

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
# Bucket discovery.  The canonical source tables each bucket their own
# event-time column by UTC calendar day; the five ``UNION ALL`` branches
# collect every ``(day, provider, repository)`` triple that has activity in
# the configured window ($1 start, $2 end).  ``DISTINCT ON`` collapses the
# per-source rows to the distinct triples the engine recomputes.  The actual
# metric computation is the engine's responsibility — this query only decides
# *which* buckets are worth recomputing.
#
# Buckets are keyed by the same repository identity the engine uses:
# ``afk_runs.repository``, ``engineering_events.repository`` and
# ``execution_bindings.repository_url`` (aliased to ``repository``).  Rows
# without a repository identity cannot be keyed and are skipped.
#
# The sessions and usage-events branches are filtered through the
# ``unambiguous`` CTE (the same pattern the recompute engine and the verify
# tool use): a session mapped to multiple ``afk_run_ids`` is ambiguous and the
# engine excludes it, so discovery must not surface buckets whose only
# activity is ambiguous — otherwise the engine recomputes them to all-zero
# and the verify tool flags a stale mismatch.
# ---------------------------------------------------------------------------

DISCOVERY_SQL = """
    WITH unambiguous AS (
        SELECT ars.session_id, MIN(ars.afk_run_id) AS afk_run_id
        FROM afk_run_sessions ars
        WHERE ars.session_id IS NOT NULL
        GROUP BY ars.session_id
        HAVING COUNT(DISTINCT ars.afk_run_id) = 1
    )
    SELECT DISTINCT ON (day, provider, repository)
           day, provider, repository
    FROM (
        -- Runs
        SELECT (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date AS day,
               r.provider,
               r.repository
        FROM afk_runs r
        WHERE r.repository IS NOT NULL
          AND (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
        UNION ALL
        -- Engineering events (change requests)
        SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day,
               e.provider,
               e.repository
        FROM engineering_events e
        WHERE e.entity_type = 'change_request'
          AND e.repository IS NOT NULL
          AND (e.occurred_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
        UNION ALL
        -- Execution bindings
        SELECT (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date AS day,
               b.provider,
               b.repository_url AS repository
        FROM execution_bindings b
        WHERE b.repository_url IS NOT NULL
          AND (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
        UNION ALL
        -- Sessions (unambiguous only)
        SELECT (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date AS day,
               r.provider,
               r.repository
        FROM afk_run_sessions ars
        JOIN unambiguous u ON u.session_id = ars.session_id
        JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
        WHERE r.repository IS NOT NULL
          AND (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
        UNION ALL
        -- Usage events (via unambiguous sessions)
        SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
               r.provider,
               r.repository
        FROM usage_events ue
        JOIN unambiguous u ON u.session_id = ue.session_id
        JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
        WHERE r.repository IS NOT NULL
          AND (ue.reported_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
    ) all_buckets
    ORDER BY day, provider, repository
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
        description="Recompute recent afk_dashboard_daily buckets from the "
        "canonical source tables (default: today and yesterday, UTC).",
    )
    parser.add_argument(
        "--days-back",
        dest="days",
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
        parser.error("--days-back must be >= 1")
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
    """Recompute every active ``afk_dashboard_daily`` bucket in the window.

    Discovers the distinct ``(day, provider, repository)`` triples with
    activity in ``[start_day, end_day]`` and recomputes each one through the
    shared engine.  Every bucket is recomputed in its own transaction, under
    the engine's transaction-scoped per-bucket advisory lock, so concurrent
    refreshes of the same bucket serialise.

    Returns the number of buckets recomputed.
    """
    buckets = await conn.fetch(DISCOVERY_SQL, start_day, end_day)
    logger.debug(
        "Discovered %d bucket(s) in %s .. %s (per-bucket lock class %s).",
        len(buckets),
        start_day,
        end_day,
        AFK_DASHBOARD_LOCK_CLASS,
    )

    recomputed = 0
    for bucket in buckets:
        day = bucket["day"]
        provider = bucket["provider"]
        repository = bucket["repository"]
        async with conn.transaction():
            await acquire_bucket_lock(conn, day, provider, repository)
            await recompute_bucket(conn, day, provider, repository)
        recomputed += 1

    return recomputed


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
        "AFK dashboard daily refresh window: %s .. %s (%d day(s)).",
        start_day,
        end_day,
        args.days,
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            if not await _try_acquire_refresh_lock(conn):
                logger.warning(
                    "Another AFK dashboard daily refresh holds the aggregate "
                    "lock (class=%s key=%s); exiting cleanly.",
                    AGGREGATE_LOCK_CLASS,
                    DAILY_REFRESH_LOCK_KEY,
                )
                return 0

            try:
                recomputed = await _run_windowed_recompute(
                    conn, start_day, end_day
                )
                logger.info(
                    "Recomputed %d afk_dashboard_daily bucket(s) for window "
                    "%s .. %s.",
                    recomputed,
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
