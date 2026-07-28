#!/usr/bin/env python3
"""Backfill sessions.total_cache_write_tokens from raw usage records.

Historical sessions created before the ingest pipeline was updated to
increment total_cache_write_tokens have this column stuck at 0 even
when opencode_usage_records contain non-zero cache_write_tokens.

This script recomputes the session-level totals from the raw records
and updates the sessions table.  It is idempotent and safe to run
multiple times.

Usage:
    python scripts/backfill_cache_write_tokens.py [--dry-run]

Flags:
    --dry-run  Preview changes without applying them.
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

logger = logging.getLogger("backfill_cache_write_tokens")


VERIFICATION_QUERY = """
SELECT s.id, s.external_session_id,
       s.total_cache_read_tokens AS session_cache_read,
       s.total_cache_write_tokens AS session_cache_write,
       COALESCE(SUM(r.cache_read_tokens), 0) AS raw_cache_read_sum,
       COALESCE(SUM(r.cache_write_tokens), 0) AS raw_cache_write_sum
FROM sessions s
JOIN opencode_usage_records r ON r.session_id = s.id
GROUP BY s.id, s.external_session_id,
         s.total_cache_read_tokens, s.total_cache_write_tokens
HAVING s.total_cache_write_tokens != COALESCE(SUM(r.cache_write_tokens), 0)
    OR s.total_cache_read_tokens != COALESCE(SUM(r.cache_read_tokens), 0)
ORDER BY s.id;
"""

BACKFILL_UPDATE_SQL = """
UPDATE sessions
SET total_cache_write_tokens = sub.raw_write_sum,
    total_cache_read_tokens = sub.raw_read_sum
FROM (
    SELECT r.session_id,
           COALESCE(SUM(r.cache_write_tokens), 0) AS raw_write_sum,
           COALESCE(SUM(r.cache_read_tokens), 0) AS raw_read_sum
    FROM opencode_usage_records r
    GROUP BY r.session_id
) sub
WHERE sessions.id = sub.session_id
  AND (sessions.total_cache_write_tokens != sub.raw_write_sum
       OR sessions.total_cache_read_tokens != sub.raw_read_sum);
"""

MISMATCH_COUNT_SQL = """
SELECT COUNT(*) AS cnt
FROM (
    SELECT s.id
    FROM sessions s
    JOIN opencode_usage_records r ON r.session_id = s.id
    GROUP BY s.id, s.total_cache_write_tokens, s.total_cache_read_tokens
    HAVING s.total_cache_write_tokens != COALESCE(SUM(r.cache_write_tokens), 0)
        OR s.total_cache_read_tokens != COALESCE(SUM(r.cache_read_tokens), 0)
) sub;
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill sessions.total_cache_write_tokens "
        "from opencode_usage_records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show mismatched sessions without updating.",
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


async def _run_verification(
    conn: asyncpg.Connection,
) -> list[asyncpg.Record]:
    """Run the verification query and return mismatched rows."""
    rows = await conn.fetch(VERIFICATION_QUERY)
    return rows


async def _count_mismatches(conn: asyncpg.Connection) -> int:
    """Return the number of sessions with mismatched cache-write totals."""
    row = await conn.fetchrow(MISMATCH_COUNT_SQL)
    return row["cnt"] if row else 0


async def _run_backfill(conn: asyncpg.Connection) -> int:
    """Update session cache-write totals to match raw usage record sums.

    Returns the number of rows updated.
    """
    result = await conn.execute(BACKFILL_UPDATE_SQL)
    # asyncpg execute returns a tag like "UPDATE 42"
    parts = result.split()
    return int(parts[1]) if len(parts) == 2 else 0


async def _show_mismatches(rows: list[asyncpg.Record]) -> None:
    """Log mismatched sessions in a human-readable format."""
    if not rows:
        logger.info("No mismatched sessions found — all totals are correct.")
        return

    logger.warning("Found %d session(s) with mismatched cache token totals:", len(rows))
    for r in rows:
        sid = r["id"]
        ext = r["external_session_id"] or "(none)"
        logger.warning(
            "  Session %s (external=%s): "
            "session_cache_write=%s vs raw_sum=%s, "
            "session_cache_read=%s vs raw_sum=%s",
            sid,
            ext,
            r["session_cache_write"],
            r["raw_cache_write_sum"],
            r["session_cache_read"],
            r["raw_cache_read_sum"],
        )


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, verify, optionally update."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            # ── Step 1: Count and show mismatches ─────────────────────
            mismatch_count = await _count_mismatches(conn)
            logger.info(
                "Found %d session(s) with mismatched cache token totals.",
                mismatch_count,
            )

            mismatched_rows = await _run_verification(conn)
            await _show_mismatches(mismatched_rows)

            if mismatch_count == 0:
                logger.info("No backfill needed — all totals are consistent.")
                return 0

            # ── Step 2: Dry-run or apply ──────────────────────────────
            if args.dry_run:
                logger.info(
                    "DRY-RUN: Would update %d session(s). "
                    "Re-run without --dry-run to apply.",
                    mismatch_count,
                )
                return 0

            updated = await _run_backfill(conn)
            logger.info("Updated %d session(s).", updated)

            # ── Step 3: Re-verify ─────────────────────────────────────
            remaining = await _count_mismatches(conn)
            if remaining == 0:
                logger.info("Verification passed — all totals are now consistent.")
            else:
                logger.error(
                    "Verification FAILED — %d session(s) still have mismatched totals.",
                    remaining,
                )
                return 1

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
