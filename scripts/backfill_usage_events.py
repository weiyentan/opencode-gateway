#!/usr/bin/env python3
"""Backfill canonical ``usage_events`` from legacy ``opencode_usage_records``.

The canonical-event layer (migration 0021) starts EMPTY after deploy.
Legacy records that already exist in ``opencode_usage_records`` are never
automatically promoted into ``usage_events`` because a replay of a
pre-existing record returns ``"Duplicate (idempotent)"`` at the legacy
layer, which skips canonical event creation (PR #396, finding #8).

This one-time script backfills the canonical event and ingest attempt
rows for every ``opencode_usage_records`` row that has no corresponding
``usage_events`` row for ``(canonical_source_identity_id, source_record_id)``.
It is **idempotent** — safe to re-run; already-backfilled keys are
skipped.

**When to run:** Post-deploy, once, after migration 0021 has been applied.
This script is the migration of pre-canonical data into the canonical
event layer.

Usage:
    python scripts/backfill_usage_events.py [--dry-run] [--limit N]

Flags:
    --dry-run  Count records needing backfill without inserting.
    --limit N  Process at most N records (useful for phased backfill).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.core.identity import is_quarantined, resolve_canonical_identity  # noqa: E402

logger = logging.getLogger("backfill_usage_events")


# ── Queries ────────────────────────────────────────────────────────────────

CANONICAL_EVENT_EXISTS_SQL = """
    SELECT 1
    FROM usage_events ue
    WHERE ue.canonical_source_identity_id = $1
      AND ue.source_record_id = $2
"""

LEGACY_RECORDS_QUERY = """
    SELECT our.*, s.project_id, s.workspace_id, s.agent, s.parent_session_id
    FROM opencode_usage_records our
    LEFT JOIN sessions s ON s.id = our.session_id
    WHERE our.session_id IS NOT NULL
    ORDER BY our.id
"""

LEGACY_RECORDS_BATCH_QUERY = LEGACY_RECORDS_QUERY + " LIMIT $1 OFFSET $2"

INSERT_USAGE_EVENT_SQL = """
    INSERT INTO usage_events
        (id, canonical_source_identity_id, source_record_id,
         client_id, session_id, model_id,
         input_tokens, output_tokens, cached_tokens,
         reasoning_tokens, cache_read_tokens, cache_write_tokens,
         estimated_cost_usd, reported_at,
         provider, mode, finish_reason,
         project_id, workspace_id, agent, parent_session_id,
         first_ingested_at, last_ingested_at)
    VALUES ($1, $2, $3,
            $4, $5, $6,
            $7, $8, $9,
            $10, $11, $12,
            $13, $14,
            $15, $16, $17,
            $18, $19, $20, $21,
            $22, $22)
"""

INSERT_ATTEMPT_SQL = """
    INSERT INTO usage_ingest_attempts
        (id, usage_event_id, source_identity_id,
         original_source_record_id, record_jsonb,
         ingest_batch_id, outcome, replay_id, delivered_at)
    VALUES ($1, $2, $3,
            $4, $5,
            $6, $7, $8, $9)
"""

MISMATCH_COUNT_SQL = """
    SELECT COUNT(*) AS cnt
    FROM opencode_usage_records our
    LEFT JOIN source_identities si
        ON si.client_id = our.client_id
       AND si.collector_source_id = our.source_database_id::text
    LEFT JOIN usage_events ue
        ON ue.canonical_source_identity_id = COALESCE(si.canonical_parent_id, si.id)
       AND ue.source_record_id = our.source_record_id
    WHERE our.session_id IS NOT NULL
      AND ue.id IS NULL
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill canonical usage_events from legacy opencode_usage_records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count records needing backfill without inserting.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N records (useful for phased backfill).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Commit progress after this many records (default: 1000).",
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


def _effective_cached_tokens(
    cached_tokens: int,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
) -> int:
    """Compute effective cached_tokens matching the v1.2 ingest logic."""
    if cache_read_tokens is not None and cache_write_tokens is not None:
        return cache_read_tokens + cache_write_tokens
    return int(cached_tokens)


def _build_record_jsonb(row: asyncpg.Record) -> dict:
    """Build a minimal JSONB payload from a legacy usage record for the ingest attempt."""
    return {
        "source_record_id": row["source_record_id"],
        "session_id": str(row["session_id"]) if row["session_id"] else None,
        "model": "backfilled",  # model_id is available but model_name is not stored
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "cached_tokens": int(row["cached_tokens"] or 0),
        "estimated_cost_usd": str(row["estimated_cost_usd"]) if row["estimated_cost_usd"] is not None else None,
        "reported_at": row["reported_at"].isoformat() if row["reported_at"] else None,
        "reasoning_tokens": row["reasoning_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "_backfill": True,
    }


async def _count_mismatches(conn: asyncpg.Connection) -> int:
    """Return the number of legacy records needing backfill."""
    row = await conn.fetchrow(MISMATCH_COUNT_SQL)
    return row["cnt"] if row else 0


async def _create_synthetic_batch(conn: asyncpg.Connection) -> str:
    """Create a synthetic ingest_batches row for the backfill's attempt rows.

    Returns the batch UUID.  The batch's client_id and credential come from
    the first active credential found.
    """
    # Find ANY active credential to satisfy the FK
    cred = await conn.fetchrow(
        "SELECT cc.id, cc.client_id FROM collector_credentials cc"
        " WHERE cc.revoked_at IS NULL LIMIT 1"
    )
    if cred is None:
        raise RuntimeError(
            "No active collector credentials found — cannot create synthetic"
            " ingest batch for backfill attempts"
        )

    import uuid as _uuid

    batch_id = _uuid.uuid4()
    await conn.execute(
        "INSERT INTO ingest_batches"
        " (id, collector_credential_id, client_id, record_count,"
        "  accepted_count, rejected_count, ingested_at)"
        " VALUES ($1, $2, $3, 0, 0, 0, now())",
        batch_id,
        cred["id"],
        cred["client_id"],
    )
    logger.info("Created synthetic ingest batch %s for backfill.", batch_id)
    return str(batch_id)


# ── Main backfill logic ─────────────────────────────────────────────────────


async def _backfill_record(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    batch_id: str,
    *,
    quarantine_skip_count: list[int],
    records_processed: list[int],
) -> bool:
    """Backfill one legacy record into a canonical event + ingest attempt.

    Returns True when a canonical event was inserted, False when the record
    was skipped (already backfilled, quarantined, or no session).
    ``quarantine_skip_count`` and ``records_processed`` are mutable lists
    used as out-parameters (shared counters).
    """
    import json as _json
    import uuid as _uuid

    source_record_id = row["source_record_id"]

    # ── Resolve canonical source identity ─────────────────────────
    # collector_source_id = str(source_database_id), matching the
    # ingest handler's convention (ingest.py line 1720).
    client_id = row["client_id"]
    canonical_identity_id = await resolve_canonical_identity(
        conn, client_id, str(row["source_database_id"]),
    )

    # ── Skip quarantined identities ───────────────────────────────
    if await is_quarantined(conn, canonical_identity_id):
        quarantine_skip_count[0] += 1
        logger.debug(
            "Skipping record %s: identity %s is quarantined.",
            source_record_id, canonical_identity_id,
        )
        return False

    # ── Idempotency: skip if canonical event already exists ────────
    existing = await conn.fetchrow(
        CANONICAL_EVENT_EXISTS_SQL,
        canonical_identity_id,
        source_record_id,
    )
    if existing is not None:
        logger.debug(
            "Skipping record %s: canonical event already exists.", source_record_id,
        )
        return False

    # ── Insert canonical event ────────────────────────────────────
    event_id = _uuid.uuid4()
    now = datetime.now(timezone.utc)

    effective_cached = _effective_cached_tokens(
        row["cached_tokens"], row["cache_read_tokens"], row["cache_write_tokens"],
    )

    await conn.execute(
        INSERT_USAGE_EVENT_SQL,
        event_id,
        canonical_identity_id,
        source_record_id,
        client_id,
        row["session_id"],
        row["model_id"],
        int(row["input_tokens"] or 0),
        int(row["output_tokens"] or 0),
        effective_cached,
        row["reasoning_tokens"],
        row["cache_read_tokens"],
        row["cache_write_tokens"],
        row["estimated_cost_usd"],
        row["reported_at"],
        row["provider"],
        row["mode"],
        row["finish_reason"],
        row["project_id"],
        row["workspace_id"],
        row["agent"],
        row["parent_session_id"],
        now,
    )

    # ── Insert ingest attempt ─────────────────────────────────────
    record_jsonb = _build_record_jsonb(row)
    attempt_id = _uuid.uuid4()

    await conn.execute(
        INSERT_ATTEMPT_SQL,
        attempt_id,
        event_id,
        canonical_identity_id,
        source_record_id,
        _json.dumps(record_jsonb),
        _uuid.UUID(batch_id),
        "accepted",
        None,  # replay_id
        now,
    )

    records_processed[0] += 1
    logger.debug("Backfilled record %s → event %s.", source_record_id, event_id)
    return True


# ── Entry point ─────────────────────────────────────────────────────────────


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, count, optionally backfill."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            # ── Step 1: Count records needing backfill ───────────────
            mismatch_count = await _count_mismatches(conn)
            logger.info(
                "Found %d legacy record(s) without a canonical event.",
                mismatch_count,
            )

            if mismatch_count == 0:
                logger.info("No backfill needed — all records have canonical events.")
                return 0

            # ── Step 2: Dry-run → report and exit ────────────────────
            if args.dry_run:
                limit_note = f" (limit: {args.limit})" if args.limit else ""
                logger.info(
                    "DRY-RUN: Would backfill %d record(s)%s."
                    " Re-run without --dry-run to apply.",
                    min(mismatch_count, args.limit) if args.limit else mismatch_count,
                    limit_note,
                )
                return 0

            # ── Step 3: Create synthetic ingest batch ────────────────
            batch_id = await _create_synthetic_batch(conn)

            # ── Step 4: Fetch and backfill in committed batches ───────
            limit = args.limit
            if args.batch_size < 1:
                raise ValueError("--batch-size must be at least 1")
            quarantine_skip_count = [0]  # mutable to share across calls
            records_processed = [0]
            records_skipped = 0

            offset = 0
            target = limit if limit is not None else mismatch_count
            while True:
                batch = await conn.fetch(
                    LEGACY_RECORDS_BATCH_QUERY,
                    args.batch_size if limit is None else min(args.batch_size, target - offset),
                    offset,
                )
                if not batch:
                    break
                async with conn.transaction():
                    for row in batch:
                        inserted = await _backfill_record(
                            conn, row, batch_id,
                            quarantine_skip_count=quarantine_skip_count,
                            records_processed=records_processed,
                        )
                        if not inserted:
                            records_skipped += 1

                total = records_processed[0] + records_skipped
                logger.info(
                    "Committed batch: %d/%d records; %d created, %d skipped, %d quarantined.",
                    total, target, records_processed[0], records_skipped,
                    quarantine_skip_count[0],
                )
                offset += len(batch)
                if limit is not None and offset >= target:
                    break

            # ── Step 5: Summary ─────────────────────────────────────
            logger.info(
                "Backfill complete: %d canonical events created,"
                " %d records skipped (already backfilled or no session),"
                " %d records skipped (quarantined identity).",
                records_processed[0], records_skipped, quarantine_skip_count[0],
            )

            # ── Step 6: Re-verify ────────────────────────────────────
            remaining = await _count_mismatches(conn)
            if remaining == 0:
                logger.info("Verification passed — all records now have canonical events.")
            else:
                logger.warning(
                    "Verification: %d record(s) still without a canonical event"
                    " (may be quarantined identities).",
                    remaining,
                )

    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
