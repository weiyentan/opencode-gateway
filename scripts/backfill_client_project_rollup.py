#!/usr/bin/env python3
"""Backfill client_project_rollup rows from canonical usage_events.

The Client Project Rollup (migration 0022, ADR 0015) is a pre-aggregated
read-model of the canonical ``usage_events`` table keyed by
``(client_id, project_id, day)``, maintained at ingest time (#403).  Rows
can drift from their source of truth — e.g. events ingested before the
ingest-time maintenance deployed, or non-canonical events removed by
historical reconciliation without a matching rollup correction.

This script recomputes every rollup row from ``usage_events`` with the
same additive math as ingest-time maintenance (the five additive fields of
``app.core.reconciliation.ROLLUP_FIELDS``: input, output, cache read,
cache write tokens plus estimated cost; ``cached_tokens`` and
``reasoning_tokens`` have no rollup column and are never summed), and
verifies the result against ``SUM(usage_events)`` per
``(client_id, project_id, day)``.  Rollup rows whose key has no backing
``usage_events`` group (stale) are deleted — no events, no derived row.

``usage_events`` remain the accounting truth (ADR 0015): on disagreement
the ROLLUP is corrected toward the event sums — never the reverse.

It is idempotent and safe to run multiple times.  Runs in the write-only
window after the rollup migration deploys, before the hybrid read path
(#405) depends on the table (straight-switch deployment, no feature flag).

Usage:
    python scripts/backfill_client_project_rollup.py [--dry-run] [--limit N]

Flags:
    --dry-run  Show disagreeing (client_id, project_id, day) groups
               without updating.
    --limit N  Recomputed at most N groups (ordered by client_id,
               project_id, day) — useful for phased backfill of large
               tables.  Re-run to converge; each run is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger("backfill_client_project_rollup")

# ---------------------------------------------------------------------------
# SQL
#
# The backfill's recompute source is a grouped SUM of the five additive
# rollup fields over canonical usage_events with a non-NULL project_id
# (the rollup PK is all NOT NULL — NULL-project events cannot be keyed),
# bucketed by the UTC date of reported_at.  This is the SQL counterpart of
# the ingest-time maintenance math in app/core/reconciliation.py
# (ROLLUP_FIELDS + _rollup_day).
# ---------------------------------------------------------------------------

EVENT_AGGREGATE_SQL = """
    SELECT ue.client_id,
           ue.project_id,
           (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
           COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
           COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
           COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
           COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
           COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd
    FROM usage_events ue
    WHERE ue.project_id IS NOT NULL
    GROUP BY ue.client_id, ue.project_id,
             (ue.reported_at AT TIME ZONE 'UTC')::date
"""

# Rollup rows vs event sums, per (client_id, project_id, day).  A FULL
# OUTER JOIN flags three disagreement shapes: a rollup row whose totals
# differ from SUM(usage_events), a rollup row with no matching events
# (stale), and an event group with no rollup row (missing).  The rollup
# side is never NULL in its columns (all NOT NULL); NULL columns only
# appear for the missing-row shapes, which the IS NULL predicates catch.
DISAGREEMENT_JOIN_SQL = f"""
    SELECT COALESCE(r.client_id, g.client_id) AS client_id,
           COALESCE(r.project_id, g.project_id) AS project_id,
           COALESCE(r.day, g.day) AS day,
           r.input_tokens AS rollup_input_tokens,
           r.output_tokens AS rollup_output_tokens,
           r.cache_read_tokens AS rollup_cache_read_tokens,
           r.cache_write_tokens AS rollup_cache_write_tokens,
           r.estimated_cost_usd AS rollup_estimated_cost_usd,
           g.input_tokens AS event_input_tokens,
           g.output_tokens AS event_output_tokens,
           g.cache_read_tokens AS event_cache_read_tokens,
           g.cache_write_tokens AS event_cache_write_tokens,
           g.estimated_cost_usd AS event_estimated_cost_usd
    FROM client_project_rollup r
    FULL OUTER JOIN (
{EVENT_AGGREGATE_SQL}
    ) g
      ON r.client_id = g.client_id
     AND r.project_id = g.project_id
     AND r.day = g.day
    WHERE r.client_id IS NULL
       OR g.client_id IS NULL
       OR r.input_tokens != g.input_tokens
       OR r.output_tokens != g.output_tokens
       OR r.cache_read_tokens != g.cache_read_tokens
       OR r.cache_write_tokens != g.cache_write_tokens
       OR r.estimated_cost_usd != g.estimated_cost_usd
"""

VERIFICATION_QUERY = (
    DISAGREEMENT_JOIN_SQL
    + "\nORDER BY client_id, project_id, day;"
)

MISMATCH_COUNT_SQL = f"""
SELECT COUNT(*) AS cnt
FROM (
{DISAGREEMENT_JOIN_SQL}
) sub;
"""

# Recomputed rows are written with an INSERT ... ON CONFLICT upsert:
# missing rollup rows are inserted, existing rows are overwritten with the
# recomputed sums (EXCLUDED.*), correcting the rollup toward usage_events.
# The SELECT is restricted to groups that disagree with the current rollup
# (LEFT JOIN + mismatch predicates), so only genuine corrections are
# written and a --limit run spends its budget on disagreeing groups only.
# The __BACKFILL_LIMIT__ marker is replaced by _backfill_sql() with an
# ORDER BY ... LIMIT clause for phased runs.
BACKFILL_UPDATE_SQL = f"""WITH grouped AS (
{EVENT_AGGREGATE_SQL}
)
INSERT INTO client_project_rollup
    (client_id, project_id, day,
     input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
     estimated_cost_usd)
SELECT g.client_id, g.project_id, g.day,
       g.input_tokens, g.output_tokens, g.cache_read_tokens, g.cache_write_tokens,
       g.estimated_cost_usd
FROM grouped g
LEFT JOIN client_project_rollup r
  ON r.client_id = g.client_id
 AND r.project_id = g.project_id
 AND r.day = g.day
WHERE r.client_id IS NULL
   OR r.input_tokens != g.input_tokens
   OR r.output_tokens != g.output_tokens
   OR r.cache_read_tokens != g.cache_read_tokens
   OR r.cache_write_tokens != g.cache_write_tokens
   OR r.estimated_cost_usd != g.estimated_cost_usd
    /* __BACKFILL_LIMIT__ */
ON CONFLICT (client_id, project_id, day)
DO UPDATE SET
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cache_read_tokens = EXCLUDED.cache_read_tokens,
    cache_write_tokens = EXCLUDED.cache_write_tokens,
    estimated_cost_usd = EXCLUDED.estimated_cost_usd
"""

_BACKFILL_LIMIT_MARKER = "/* __BACKFILL_LIMIT__ */"
_BACKFILL_LIMIT_CLAUSE = (
    "ORDER BY g.client_id, g.project_id, g.day\n    LIMIT $1"
)

# A rollup row whose (client_id, project_id, day) has NO backing
# usage_events group cannot be recomputed from events — the row is stale
# and is corrected by deletion (usage_events are the accounting truth; no
# events for the key, no derived row).  The day match uses the same UTC
# bucketing and NULL-project exclusion as the recompute.
STALE_ROLLUP_DELETE_SQL = """
DELETE FROM client_project_rollup r
WHERE NOT EXISTS (
    SELECT 1
    FROM usage_events ue
    WHERE ue.client_id = r.client_id
      AND ue.project_id = r.project_id
      AND ue.project_id IS NOT NULL
      AND (ue.reported_at AT TIME ZONE 'UTC')::date = r.day
)
"""


def _backfill_sql(limit: int | None) -> str:
    """Return the backfill upsert, optionally bounded to ``limit`` groups.

    ``limit`` bounds the number of ``(client_id, project_id, day)`` groups
    recomputed per run (ordered by key) for phased backfills; ``None``
    recomputes every group (the safe default).  A limit below 1 is
    rejected.
    """
    if limit is None:
        return BACKFILL_UPDATE_SQL.replace(_BACKFILL_LIMIT_MARKER, "")
    if limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")
    return BACKFILL_UPDATE_SQL.replace(
        _BACKFILL_LIMIT_MARKER, _BACKFILL_LIMIT_CLAUSE,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute client_project_rollup rows from usage_events "
        "(ADR 0015: usage_events are the accounting truth).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show disagreeing (client_id, project_id, day) groups without updating.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Recompute at most N groups (ordered by client_id, project_id, day) "
        "— useful for phased backfill.",
    )
    return parser.parse_args(argv)


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


def _parse_row_count(tag: str) -> int:
    """Parse the row count out of an asyncpg execute tag.

    Handles both "UPDATE N" and the upsert's "INSERT 0 N".
    """
    parts = tag.split()
    if len(parts) == 3 and parts[0] == "INSERT":
        return int(parts[2])
    if len(parts) == 2:
        return int(parts[1])
    return 0


async def _run_verification(
    conn: asyncpg.Connection,
) -> list[asyncpg.Record]:
    """Run the verification query and return disagreeing rows."""
    return await conn.fetch(VERIFICATION_QUERY)


async def _count_mismatches(conn: asyncpg.Connection) -> int:
    """Return the number of groups whose rollup disagrees with usage_events."""
    row = await conn.fetchrow(MISMATCH_COUNT_SQL)
    return row["cnt"] if row else 0


async def _run_backfill(
    conn: asyncpg.Connection,
    limit: int | None = None,
) -> int:
    """Recompute rollup rows from usage_events; return rows upserted."""
    sql = _backfill_sql(limit)
    if limit is None:
        result = await conn.execute(sql)
    else:
        result = await conn.execute(sql, int(limit))
    return _parse_row_count(result)


async def _delete_stale_rows(conn: asyncpg.Connection) -> int:
    """Delete rollup rows with no backing usage_events group; return count."""
    result = await conn.execute(STALE_ROLLUP_DELETE_SQL)
    return _parse_row_count(result)


async def _show_mismatches(rows: list[asyncpg.Record]) -> None:
    """Log disagreeing groups in a human-readable format."""
    if not rows:
        logger.info(
            "No disagreements found — client_project_rollup matches "
            "SUM(usage_events) for every (client_id, project_id, day).",
        )
        return

    logger.warning(
        "Found %d (client_id, project_id, day) group(s) whose rollup "
        "disagrees with SUM(usage_events):",
        len(rows),
    )
    for r in rows:
        rollup = (
            f"rollup: in={r['rollup_input_tokens']} out={r['rollup_output_tokens']} "
            f"cr={r['rollup_cache_read_tokens']} cw={r['rollup_cache_write_tokens']} "
            f"cost={r['rollup_estimated_cost_usd']}"
        )
        events = (
            f"events: in={r['event_input_tokens']} out={r['event_output_tokens']} "
            f"cr={r['event_cache_read_tokens']} cw={r['event_cache_write_tokens']} "
            f"cost={r['event_estimated_cost_usd']}"
        )
        logger.warning(
            "  client=%s project=%s day=%s  %s | %s",
            r["client_id"], r["project_id"], r["day"], rollup, events,
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
            # ── Step 1: Count and show disagreements ─────────────────
            mismatch_count = await _count_mismatches(conn)
            logger.info(
                "Found %d (client_id, project_id, day) group(s) whose "
                "rollup disagrees with SUM(usage_events).",
                mismatch_count,
            )

            mismatched_rows = await _run_verification(conn)
            await _show_mismatches(mismatched_rows)

            if mismatch_count == 0:
                logger.info("No backfill needed — rollup is consistent with usage_events.")
                return 0

            # ── Step 2: Dry-run or apply ─────────────────────────────
            if args.dry_run:
                limit_note = f" (limit: {args.limit})" if args.limit else ""
                logger.info(
                    "DRY-RUN: Would recompute %d rollup row(s)%s. "
                    "Re-run without --dry-run to apply.",
                    min(mismatch_count, args.limit) if args.limit else mismatch_count,
                    limit_note,
                )
                return 0

            updated = await _run_backfill(conn, limit=args.limit)
            logger.info("Recomputed %d rollup row(s) from usage_events.", updated)

            stale_deleted = await _delete_stale_rows(conn)
            if stale_deleted:
                logger.warning(
                    "Deleted %d stale rollup row(s) with no backing usage_events.",
                    stale_deleted,
                )

            # ── Step 3: Re-verify ────────────────────────────────────
            remaining = await _count_mismatches(conn)
            if remaining == 0:
                logger.info(
                    "Verification passed — client_project_rollup now matches "
                    "SUM(usage_events) for every group.",
                )
            else:
                logger.error(
                    "Verification FAILED — %d group(s) still disagree. "
                    "Re-run the script to converge remaining groups.",
                    remaining,
                )
                return 1

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
