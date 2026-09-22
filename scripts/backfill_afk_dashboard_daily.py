#!/usr/bin/env python3
"""Backfill ``afk_dashboard_daily`` rows from canonical source tables (issue #716).

The AFK dashboard daily rollup (``afk_dashboard_daily``, migration 0046) is
a pre-aggregated read-model of the AFK outcome source tables, keyed by
``(day, provider, repository)`` and maintained by the recomputation engine
in ``app.core.afk_dashboard_daily`` (issue #715).  Rows can drift from their
sources — e.g. buckets written before the engine deployed, corrections that
arrived without a matching recompute, or buckets left behind by a source
correction/retraction.

This operator CLI recomputes every active bucket in an inclusive UTC day
window by delegating each one to the shared engine
(:func:`~app.core.afk_dashboard_daily.recompute_bucket`) under the engine's
per-bucket transaction-scoped advisory lock
(:func:`~app.core.afk_dashboard_daily.acquire_bucket_lock`).  Because the
engine *replaces* the whole row (``INSERT ... ON CONFLICT DO UPDATE SET col =
EXCLUDED.col``) rather than incrementing it, the backfill is **idempotent**:
rerunning over the same window produces identical rows.

A rollup row whose ``(day, provider, repository)`` has no backing canonical
bucket cannot be recomputed from source — it is stale and is corrected by
deletion.  The canonical source tables remain the accounting truth; on
disagreement the ROLLUP is corrected toward the canonical sums, never the
reverse.

``--verify`` compares the stored rollups against canonical SUM/COUNT
recomputation over the window and reports every disagreement (differing
metric, stale rollup row, or missing rollup row) without writing.  The
default flow recomputes, deletes stale rows, and re-verifies.

``--provider`` and ``--repository`` narrow every operation (discovery,
recompute, stale deletion, and verification) to one provider and/or one
repository.

Usage:
    python scripts/backfill_afk_dashboard_daily.py \
        --from 2026-01-01 --to 2026-01-31 \
        [--dry-run | --verify] \
        [--provider gitlab] [--repository cloudnative-pg]

Flags:
    --from DATE        Inclusive UTC start day (YYYY-MM-DD, required).
    --to DATE          Inclusive UTC end day (YYYY-MM-DD, required).
    --dry-run          Show the buckets that would be recomputed and the
                       disagreements found, without writing.
    --verify           Compare stored rollups against canonical SUM/COUNT
                       recomputation and report mismatches; never writes.
                       Exits 1 when any mismatch is found.
    --provider NAME    Restrict the backfill scope to one provider.
    --repository NAME  Restrict the backfill scope to one repository.

The shared engine (issue #715) pulls the additive metrics from the canonical
tables: ``afk_runs`` (runs started), ``engineering_events`` (change requests
opened/merged/closed), ``execution_bindings`` (executions), ``afk_run_sessions``
(sessions), and ``usage_events`` (tokens + estimated cost).  Rows without a
repository identity, and sessions mapped to more than one AFK run, are
excluded — never guessed.
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

logger = logging.getLogger("backfill_afk_dashboard_daily")

# ---------------------------------------------------------------------------
# Recomputation engine (issue #715)
#
# The engine is the single writer of ``afk_dashboard_daily``.  It is imported
# at module scope so tests (and callers) can patch the engine calls on this
# module.  The shared vocabulary (METRIC_COLUMNS, UNAMBIGUOUS_CTE,
# CR_EVENT_FILTER) is defined in the engine and imported here.
# ---------------------------------------------------------------------------

from app.core.afk_dashboard_daily import (  # noqa: E402
    CR_EVENT_FILTER,
    METRIC_COLUMNS,
    UNAMBIGUOUS_CTE,
    acquire_bucket_lock,
    recompute_bucket,
)


# ---------------------------------------------------------------------------
# SQL
#
# ``CANONICAL_BUCKETS_SQL`` is the shared bucket inventory: every
# ``(day, provider, repository)`` triple that carries canonical activity in
# any of the five source tables, bucketed by that source's own event-time
# column (never by ingest time) into UTC calendar days.  Rows without a
# repository identity cannot be keyed into a bucket and are skipped.  Like
# the verification aggregate (``CANONICAL_AGGREGATE_SQL``), the two
# session-derived branches (``afk_run_sessions`` and ``usage_events``) route
# through an ``unambiguous`` CTE that excludes sessions mapped to more than
# one AFK run, so an ambiguous session can never keep a stale bucket alive.
# It is the SQL counterpart of the engine's per-category queries
# (``app.core.afk_dashboard_daily``).
# ---------------------------------------------------------------------------

CANONICAL_BUCKETS_SQL = f"""
    WITH unambiguous AS (
{UNAMBIGUOUS_CTE}
    )
    SELECT (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
               AS day,
           r.provider AS provider,
           r.repository AS repository
    FROM afk_runs r
    WHERE r.repository IS NOT NULL
    UNION
    SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day,
           e.provider AS provider,
           e.repository AS repository
    FROM engineering_events e
    WHERE {CR_EVENT_FILTER}
      AND e.repository IS NOT NULL
    UNION
    SELECT (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
               AS day,
           b.provider AS provider,
           b.repository_url AS repository
    FROM execution_bindings b
    WHERE b.repository_url IS NOT NULL
    UNION
    SELECT (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
               AS day,
           r.provider AS provider,
           r.repository AS repository
    FROM afk_run_sessions ars
    JOIN unambiguous u ON u.session_id = ars.session_id
    JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
    WHERE r.repository IS NOT NULL
    UNION
    SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
           r.provider AS provider,
           r.repository AS repository
    FROM usage_events ue
    JOIN unambiguous u ON u.session_id = ue.session_id
    JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
    WHERE r.repository IS NOT NULL
"""

# The buckets the backfill recomputes: those with canonical activity in the
# inclusive ``$1..$2`` UTC day window, optionally narrowed by provider ($3)
# and repository ($4).  A ``NULL`` filter matches everything.
DISCOVERY_SQL = f"""
    SELECT DISTINCT b.day, b.provider, b.repository
    FROM ({CANONICAL_BUCKETS_SQL}) b
    WHERE b.day BETWEEN $1 AND $2
      AND ($3::text IS NULL OR b.provider = $3)
      AND ($4::text IS NULL OR b.repository = $4)
    ORDER BY b.day, b.provider, b.repository
"""

# ---------------------------------------------------------------------------
# Verification SQL
#
# The canonical side recomputes every additive metric from the same source
# tables the engine reads, grouped by ``(day, provider, repository)`` over
# the window in one round trip.  A FULL OUTER JOIN against
# ``afk_dashboard_daily`` then flags three disagreement shapes — a rollup row
# whose metrics differ, a rollup row with no matching canonical group
# (stale), and a canonical group with no rollup row (missing).
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
        WITH runs_agg AS (
            SELECT (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider AS provider,
                   r.repository AS repository,
                   COUNT(*)::int AS runs_started
            FROM afk_runs r
            WHERE r.repository IS NOT NULL
              AND (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
              AND ($3::text IS NULL OR r.provider = $3)
              AND ($4::text IS NULL OR r.repository = $4)
            GROUP BY day, r.provider, r.repository
        ),
        cr_agg AS (
            SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day,
                   e.provider AS provider,
                   e.repository AS repository,
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
              AND ($3::text IS NULL OR e.provider = $3)
              AND ($4::text IS NULL OR e.repository = $4)
            GROUP BY day, e.provider, e.repository
        ),
        exec_agg AS (
            SELECT (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   b.provider AS provider,
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
              AND ($3::text IS NULL OR b.provider = $3)
              AND ($4::text IS NULL OR b.repository_url = $4)
            GROUP BY day, b.provider, b.repository_url
        ),
        unambiguous AS (
{UNAMBIGUOUS_CTE}
        ),
        sess_agg AS (
            SELECT (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider AS provider,
                   r.repository AS repository,
                   COUNT(*)::int AS session_count
            FROM afk_run_sessions ars
            JOIN unambiguous u ON u.session_id = ars.session_id
            JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
            WHERE r.repository IS NOT NULL
              AND (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
              AND ($3::text IS NULL OR r.provider = $3)
              AND ($4::text IS NULL OR r.repository = $4)
            GROUP BY day, r.provider, r.repository
        ),
        usage_agg AS (
            SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
                   r.provider AS provider,
                   r.repository AS repository,
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
              AND ($3::text IS NULL OR r.provider = $3)
              AND ($4::text IS NULL OR r.repository = $4)
            GROUP BY day, r.provider, r.repository
        )
        SELECT COALESCE(r.day, cr.day, ex.day, se.day, ug.day) AS day,
               COALESCE(r.provider, cr.provider, ex.provider, se.provider,
                        ug.provider) AS provider,
               COALESCE(r.repository, cr.repository, ex.repository, se.repository,
                        ug.repository) AS repository,
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
         AND se.repository = COALESCE(r.repository, cr.repository, ex.repository)
        FULL OUTER JOIN usage_agg ug
          ON ug.day = COALESCE(r.day, cr.day, ex.day, se.day)
         AND ug.provider = COALESCE(r.provider, cr.provider, ex.provider,
                                    se.provider)
         AND ug.repository = COALESCE(r.repository, cr.repository, ex.repository,
                                      se.repository)
"""

DISAGREEMENT_JOIN_SQL = f"""
    SELECT COALESCE(d.day, c.day) AS day,
           COALESCE(d.provider, c.provider) AS provider,
           COALESCE(d.repository, c.repository) AS repository,
           {_ROLLUP_COLUMNS_SQL},
           {_CANONICAL_COLUMNS_SQL}
    FROM afk_dashboard_daily d
    FULL OUTER JOIN (
{CANONICAL_AGGREGATE_SQL}
    ) c
      ON d.day = c.day
     AND d.provider = c.provider
     AND d.repository = c.repository
    WHERE COALESCE(d.day, c.day) BETWEEN $1 AND $2
      AND ($3::text IS NULL OR COALESCE(d.provider, c.provider) = $3)
      AND ($4::text IS NULL OR COALESCE(d.repository, c.repository) = $4)
      AND ( d.day IS NULL
         OR c.day IS NULL
         OR {_MISMATCH_PREDICATE} )
"""

VERIFICATION_QUERY = (
    DISAGREEMENT_JOIN_SQL
    + "\nORDER BY day, provider, repository;"
)

MISMATCH_COUNT_SQL = f"""
SELECT COUNT(*) AS cnt
FROM (
{DISAGREEMENT_JOIN_SQL}
) sub;
"""

# A rollup row whose bucket has NO canonical activity in the window cannot be
# recomputed from source — the row is stale and is corrected by deletion (the
# canonical tables are the accounting truth; no source bucket, no derived
# row).  Scope ($1/$2 window, $3 provider, $4 repository) matches discovery.
STALE_ROLLUP_DELETE_SQL = f"""
    DELETE FROM afk_dashboard_daily d
    WHERE d.day BETWEEN $1 AND $2
      AND ($3::text IS NULL OR d.provider = $3)
      AND ($4::text IS NULL OR d.repository = $4)
      AND NOT EXISTS (
          SELECT 1
          FROM ({CANONICAL_BUCKETS_SQL}) b
          WHERE b.day = d.day
            AND b.provider = d.provider
            AND b.repository = d.repository
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
        description="Recompute afk_dashboard_daily buckets from the canonical "
        "AFK source tables (the rollup is a rebuildable projection of source "
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
    parser.add_argument(
        "--provider",
        default=None,
        help="Restrict the backfill scope to one provider.",
    )
    parser.add_argument(
        "--repository",
        default=None,
        help="Restrict the backfill scope to one repository.",
    )
    args = parser.parse_args(argv)
    if args.from_date > args.to_date:
        parser.error("--from must not be after --to")
    return args


async def _discover_buckets(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
    provider: str | None = None,
    repository: str | None = None,
) -> list[asyncpg.Record]:
    """Return the active ``(day, provider, repository)`` buckets in the window."""
    return await conn.fetch(
        DISCOVERY_SQL, from_date, to_date, provider, repository
    )


async def _run_backfill(
    conn: asyncpg.Connection,
    buckets: list[asyncpg.Record],
) -> int:
    """Recompute each bucket through the shared engine; return buckets written.

    Every bucket is recomputed in its own transaction: the engine's
    transaction-scoped per-bucket advisory lock is acquired first, then
    :func:`~app.core.afk_dashboard_daily.recompute_bucket` replaces the whole
    row.  Because the engine replaces rather than increments, rerunning the
    same bucket is idempotent.
    """
    if recompute_bucket is None or acquire_bucket_lock is None:
        raise RuntimeError(
            "app.core.afk_dashboard_daily is unavailable (issue #714/#715 not "
            "merged); cannot recompute afk_dashboard_daily."
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


async def _delete_stale_rows(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
    provider: str | None = None,
    repository: str | None = None,
) -> int:
    """Delete rollup rows in the window with no backing canonical bucket."""
    result = await conn.execute(
        STALE_ROLLUP_DELETE_SQL, from_date, to_date, provider, repository
    )
    return _parse_row_count(result)


async def _count_mismatches(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
    provider: str | None = None,
    repository: str | None = None,
) -> int:
    """Return how many buckets disagree with canonical recomputation."""
    row = await conn.fetchrow(
        MISMATCH_COUNT_SQL, from_date, to_date, provider, repository
    )
    return row["cnt"] if row else 0


async def _run_verification(
    conn: asyncpg.Connection,
    from_date: date,
    to_date: date,
    provider: str | None = None,
    repository: str | None = None,
) -> list[asyncpg.Record]:
    """Run the canonical comparison query and return disagreeing rows."""
    return await conn.fetch(
        VERIFICATION_QUERY, from_date, to_date, provider, repository
    )


async def _show_mismatches(rows: list[asyncpg.Record]) -> None:
    """Log disagreeing buckets in a human-readable format."""
    if not rows:
        logger.info(
            "No disagreements found — afk_dashboard_daily matches canonical "
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
            "  day=%s provider=%s repository=%s  rollup[%s] | canonical[%s]",
            r["day"], r["provider"], r["repository"], rollup, canonical,
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
                args.provider, args.repository,
            )
            mismatch_count = await _count_mismatches(
                conn, args.from_date, args.to_date,
                args.provider, args.repository,
            )
            logger.info(
                "Window %s .. %s: %d active bucket(s), %d bucket(s) whose "
                "rollup disagrees with canonical recomputation.",
                args.from_date, args.to_date, len(buckets), mismatch_count,
            )

            mismatched_rows = await _run_verification(
                conn, args.from_date, args.to_date,
                args.provider, args.repository,
            )
            await _show_mismatches(mismatched_rows)

            # ── Step 2: Verify-only mode ─────────────────────────────
            if args.verify:
                if mismatch_count == 0:
                    logger.info(
                        "Verification passed — afk_dashboard_daily matches "
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
                "Recomputed %d afk_dashboard_daily bucket(s) from canonical "
                "source tables.",
                recomputed,
            )

            stale_deleted = await _delete_stale_rows(
                conn, args.from_date, args.to_date,
                args.provider, args.repository,
            )
            if stale_deleted:
                logger.warning(
                    "Deleted %d stale afk_dashboard_daily row(s) with no "
                    "backing canonical activity.",
                    stale_deleted,
                )

            remaining = await _count_mismatches(
                conn, args.from_date, args.to_date,
                args.provider, args.repository,
            )
            if remaining == 0:
                logger.info(
                    "Verification passed — afk_dashboard_daily now matches "
                    "canonical recomputation for every bucket.",
                )
            else:
                logger.error(
                    "Verification FAILED — %d bucket(s) still disagree. "
                    "Re-run without the scope filters or investigate the "
                    "remaining buckets.",
                    remaining,
                )
                return 1

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
