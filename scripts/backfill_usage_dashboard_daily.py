#!/usr/bin/env python3
"""Backfill ``usage_dashboard_daily`` rows from canonical usage_events (issue #737).

The usage dashboard daily rollup (``usage_dashboard_daily``, migration 0048) is
a pre-aggregated read-model of the canonical ``usage_events`` table, keyed by
``(day, provider)`` and maintained by the recomputation engine in
``app.core.usage_dashboard_daily`` (issue #735).  Rows can drift from their
sources — e.g. buckets written before the engine deployed, corrections that
arrived without a matching recompute, or buckets left behind by a source
correction/retraction.

This operator CLI recomputes every active bucket in an inclusive UTC day
window by delegating each one to the shared engine
(:func:`~app.core.usage_dashboard_daily.recompute_bucket`) under the engine's
per-bucket transaction-scoped advisory lock
(:func:`~app.core.usage_dashboard_daily.acquire_bucket_lock`).  Because the
engine *replaces* the whole row (``INSERT ... ON CONFLICT DO UPDATE SET col =
EXCLUDED.col``) rather than incrementing it, the backfill is **idempotent**:
rerunning over the same window produces identical rows.

A rollup row whose ``(day, provider)`` has no backing canonical bucket cannot
be recomputed from source — it is stale and is corrected by deletion.  The
canonical ``usage_events`` table remains the accounting truth; on
disagreement the ROLLUP is corrected toward the canonical sums, never the
reverse.

``--verify`` compares the stored rollups against canonical SUM/COUNT
recomputation over the window and reports every disagreement (differing
metric, stale rollup row, or missing rollup row) without writing.  The
default flow recomputes, deletes stale rows, and re-verifies.

Usage:
    python scripts/backfill_usage_dashboard_daily.py \
        --from 2026-01-01 --to 2026-01-31 \
        [--dry-run | --verify]

Flags:
    --from DATE        Inclusive UTC start day (YYYY-MM-DD, required).
    --to DATE          Inclusive UTC end day (YYYY-MM-DD, required).
    --dry-run          Show the buckets that would be recomputed and the
                       disagreements found, without writing.
    --verify           Compare stored rollups against canonical SUM/COUNT
                       recomputation and report mismatches; never writes.
                       Exits 1 when any mismatch is found.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_client_project_rollup import (  # noqa: E402
    _get_pool,
    _parse_row_count,
)

logger = logging.getLogger("backfill_usage_dashboard_daily")

# ---------------------------------------------------------------------------
# Recomputation engine (issue #735)
#
# The engine is the single writer of ``usage_dashboard_daily``.  It is imported
# at module scope so tests (and callers) can patch the engine calls on this
# module.
# ---------------------------------------------------------------------------

from app.core.usage_dashboard_daily import (  # noqa: E402
    METRIC_COLUMNS,
    acquire_bucket_lock,
    recompute_bucket,
)


# ---------------------------------------------------------------------------
# SQL
#
# ``CANONICAL_BUCKETS_SQL`` is the bucket inventory: every ``(day, provider)``
# pair that carries canonical activity in the ``usage_events`` table, bucketed
# by ``reported_at`` into UTC calendar days.
# ---------------------------------------------------------------------------

CANONICAL_BUCKETS_SQL = """
    SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
           ue.provider AS provider
    FROM usage_events ue
"""

# The buckets the backfill recomputes: those with canonical activity in the
# inclusive ``$1..$2`` UTC day window.  A ``NULL`` filter matches everything.
DISCOVERY_SQL = f"""
    SELECT DISTINCT b.day, b.provider
    FROM ({CANONICAL_BUCKETS_SQL}) b
    WHERE b.day BETWEEN $1 AND $2
    ORDER BY b.day, b.provider
"""

# ---------------------------------------------------------------------------
# Verification SQL
#
# The canonical side recomputes every additive metric from ``usage_events``,
# grouped by ``(day, provider)`` over the window in one round trip.  A FULL
# OUTER JOIN against ``usage_dashboard_daily`` then flags three disagreement
# shapes — a rollup row whose metrics differ, a rollup row with no matching
# canonical group (stale), and a canonical group with no rollup row (missing).
# ---------------------------------------------------------------------------

_ROLLUP_COLUMNS_SQL = ",\n           ".join(
    f"d.{column} AS rollup_{column}" for column in METRIC_COLUMNS
)
_CANONICAL_COLUMNS_SQL = ",\n           ".join(
    f"c.{column} AS canonical_{column}" for column in METRIC_COLUMNS
)
_MISMATCH_PREDICATE = "\n         OR ".join(
    f"d.{column} != c.{column}" for column in METRIC_COLUMNS
)

CANONICAL_AGGREGATE_SQL = f"""
    SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
           ue.provider AS provider,
           COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
           COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
           COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
           COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
           COALESCE(SUM(ue.reasoning_tokens), 0)::int AS reasoning_tokens,
           COALESCE(SUM(ue.cached_tokens), 0)::int AS cached_tokens,
           COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd,
           COUNT(*)::int AS record_count
    FROM usage_events ue
    WHERE (ue.reported_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
    GROUP BY day, ue.provider
"""

DISAGREEMENT_JOIN_SQL = f"""
    SELECT COALESCE(d.day, c.day) AS day,
           COALESCE(d.provider, c.provider) AS provider,
           {_ROLLUP_COLUMNS_SQL},
           {_CANONICAL_COLUMNS_SQL}
    FROM usage_dashboard_daily d
    FULL OUTER JOIN (
{CANONICAL_AGGREGATE_SQL}
    ) c
      ON d.day = c.day
     AND d.provider = c.provider
    WHERE COALESCE(d.day, c.day) BETWEEN $1 AND $2
      AND ( d.day IS NULL
         OR c.day IS NULL
         OR {_MISMATCH_PREDICATE} )
"""

VERIFICATION_QUERY = (
    DISAGREEMENT_JOIN_SQL
    + "\nORDER BY day, provider;"
)

MISMATCH_COUNT_SQL = f"""
SELECT COUNT(*) AS cnt
FROM (
{DISAGREEMENT_JOIN_SQL}
) sub;
"""

# A rollup row whose ``(day, provider)`` has NO backing canonical bucket cannot
# be recomputed from source — the row is stale and is corrected by deletion.
STALE_ROLLUP_DELETE_SQL = f"""
    DELETE FROM usage_dashboard_daily d
    WHERE d.day BETWEEN $1 AND $2
      AND NOT EXISTS (
          SELECT 1
          FROM ({CANONICAL_BUCKETS_SQL}) b
          WHERE b.day = d.day
            AND b.provider = d.provider
      )
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_date(value: str) -> date:
    """argparse type: parse a ``YYYY-MM-DD`` UTC calendar day or fail fast."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute usage_dashboard_daily buckets from the canonical "
        "usage_events table (the rollup is a rebuildable projection of source "
        "facts).",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        type=_parse_iso_date,
        required=True,
        help="Inclusive UTC start day (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        type=_parse_iso_date,
        required=True,
        help="Inclusive UTC end day (YYYY-MM-DD).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the buckets that would be recomputed and the disagreements "
        "found, without writing.",
    )
    mode.add_argument(
        "--verify",
        action="store_true",
        help="Compare stored rollups against canonical SUM/COUNT recomputation "
        "and report mismatches; never writes.",
    )
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must not be after --to")
    return args


async def _discover_buckets(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
) -> list[asyncpg.Record]:
    """Return the active ``(day, provider)`` buckets in the window."""
    return await conn.fetch(DISCOVERY_SQL, from_date, to_date)


async def _run_backfill(
    conn: asyncpg.Connection,
    buckets: list[asyncpg.Record],
) -> int:
    """Recompute each bucket through the shared engine; return buckets written.

    Every bucket is recomputed in its own transaction: the engine's
    transaction-scoped per-bucket advisory lock is acquired first, then
    :func:`~app.core.usage_dashboard_daily.recompute_bucket` replaces the whole
    row.  Because the engine replaces rather than increments, rerunning the
    same bucket is idempotent.
    """
    if recompute_bucket is None or acquire_bucket_lock is None:
        raise RuntimeError(
            "app.core.usage_dashboard_daily is unavailable (issue #735 not "
            "merged); cannot recompute usage_dashboard_daily."
        )

    recomputed = 0
    for bucket in buckets:
        day = bucket["day"]
        provider = bucket["provider"]
        async with conn.transaction():
            await acquire_bucket_lock(conn, day, provider)
            await recompute_bucket(conn, day, provider)
        recomputed += 1
    return recomputed


async def _delete_stale_rows(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
) -> int:
    """Delete rollup rows in the window with no backing canonical bucket."""
    result = await conn.execute(
        STALE_ROLLUP_DELETE_SQL, from_date, to_date
    )
    return _parse_row_count(result)


async def _count_mismatches(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
) -> int:
    """Return how many buckets disagree with canonical recomputation."""
    row = await conn.fetchrow(
        MISMATCH_COUNT_SQL, from_date, to_date
    )
    return row["cnt"] if row else 0


async def _run_verification(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
) -> list[asyncpg.Record]:
    """Run the canonical comparison query and return disagreeing rows."""
    return await conn.fetch(
        VERIFICATION_QUERY, from_date, to_date
    )


async def _show_mismatches(rows: list[asyncpg.Record]) -> None:
    """Log disagreeing buckets in a human-readable format."""
    if not rows:
        logger.info(
            "No disagreements found — usage_dashboard_daily matches canonical "
            "SUM/COUNT for every bucket in the window.",
        )
        return

    logger.warning(
        "Found %d bucket(s) whose rollup disagrees with canonical "
        "recomputation:",
        len(rows),
    )
    for r in rows:
        rollup = " ".join(
            f"{column}={r[f'rollup_{column}']}" for column in METRIC_COLUMNS
        )
        canonical = " ".join(
            f"{column}={r[f'canonical_{column}']}" for column in METRIC_COLUMNS
        )
        logger.warning(
            "  day=%s provider=%s  rollup[%s] | canonical[%s]",
            r["day"], r["provider"], rollup, canonical,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, verify, optionally recompute."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            # ── Step 1: Discover buckets and count disagreements ─────
            buckets = await _discover_buckets(
                conn, args.from_date, args.to_date,
            )
            mismatch_count = await _count_mismatches(
                conn, args.from_date, args.to_date,
            )
            logger.info(
                "Window %s .. %s: %d active bucket(s), %d bucket(s) whose "
                "rollup disagrees with canonical recomputation.",
                args.from_date, args.to_date, len(buckets), mismatch_count,
            )

            mismatched_rows = await _run_verification(
                conn, args.from_date, args.to_date,
            )
            await _show_mismatches(mismatched_rows)

            # ── Step 2: Verify-only mode ─────────────────────────────
            if args.verify:
                if mismatch_count == 0:
                    logger.info(
                        "Verification passed — usage_dashboard_daily matches "
                        "canonical recomputation for every bucket.",
                    )
                    return 0
                logger.error(
                    "Verification FAILED — %d bucket(s) disagree. Re-run "
                    "without --verify to backfill.",
                    mismatch_count,
                )
                return 1

            # ── Step 3: Dry-run — report, never write ────────────────
            if args.dry_run:
                logger.info(
                    "DRY-RUN: Would recompute %d bucket(s) and delete stale "
                    "rollup rows in window %s .. %s. Re-run without --dry-run "
                    "to apply.",
                    len(buckets), args.from_date, args.to_date,
                )
                return 0

            # ── Step 4: Recompute, delete stale rows, re-verify ──────
            recomputed = await _run_backfill(conn, buckets)
            logger.info(
                "Recomputed %d usage_dashboard_daily bucket(s) from canonical "
                "usage_events.",
                recomputed,
            )

            stale_deleted = await _delete_stale_rows(
                conn, args.from_date, args.to_date,
            )
            if stale_deleted:
                logger.warning(
                    "Deleted %d stale usage_dashboard_daily row(s) with no "
                    "backing canonical activity.",
                    stale_deleted,
                )

            remaining = await _count_mismatches(
                conn, args.from_date, args.to_date,
            )
            if remaining == 0:
                logger.info(
                    "Verification passed — usage_dashboard_daily now matches "
                    "canonical recomputation for every bucket.",
                )
            else:
                logger.error(
                    "Verification FAILED — %d bucket(s) still disagree. "
                    "Re-run the script to converge remaining buckets.",
                    remaining,
                )
                return 1

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
