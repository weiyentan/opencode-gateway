#!/usr/bin/env python3
# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Backfill observed_tool_calls rows from observed_parts (issue #468).

``observed_tool_calls`` is a *derived query surface*, not an independent
source of truth (ADR 0016 §4): the authoritative verbatim store is
``observed_parts.data``, and the projection's normalized columns are
extracted from it at ingest.  A disagreement is repaired by
re-extraction — this script recomputes ``observed_tool_calls`` from
``observed_parts`` rows with ``part_type = 'tool'`` using the SAME
extraction logic as live ingest (the ADR 0015 backfill↔live-equivalence
pattern).

The extraction pipeline is SHARED with the live ingest path: the script
imports ``_extract_tool_call_facts``, ``_redact_json_value``,
``_truncate_json_field`` and ``_serialize_jsonb`` from ``app.api.ingest``
directly, so backfill and live extraction call the same function objects
and cannot drift.  Each stored part goes through the identical
extract → redact → truncate → serialize pipeline live ingest applies
(``app/api/ingest.py:1575`` and ``1632-1637``), with the same
``GATEWAY_TOOL_PAYLOAD_MAX_CHARS`` cap (default 4096) on
``tool_input`` / ``tool_output``.

Rows are upserted on the projection's unique key
``(client_id, source_database_id, external_part_id)`` with the same
conflict semantics as live ingest: ``tool_name`` is overwritten (NOT
NULL), the nullable derived columns are COALESCE-filled (a
re-extraction never erases a stored value), and ``first_seen_at`` is
preserved on conflict.  Re-running is therefore idempotent and safe —
identical rows are never rewritten, and a re-run produces no duplicate
or divergent rows.

Tool parts whose stored ``data`` is a truncation marker (payload was cut
at ingest beyond ``GATEWAY_PART_DATA_MAX_CHARS``) or is otherwise not
re-extractable are skipped and counted — the full payload was bounded at
ingest, so no re-extraction is possible; the projection row written in
the same ingest transaction is left untouched.  ``observed_tool_calls``
rows with no backing ``tool`` part (stale) are flagged and deleted — no
tool part, no derived row (mirroring the sibling rollup backfill's stale
correction).

Usage:
    python scripts/backfill_tool_calls.py [--dry-run] [--limit N] [--since ISO]

Flags:
    --dry-run    Report the re-extraction diff (missing / divergent /
                 stale rows) without updating.
    --limit N    Repair at most N rows per run (in scan order); rows
                 beyond the budget are counted as deferred and left for
                 a later run.  Re-run to converge; each run is
                 idempotent.
    --since ISO  Only scan tool parts first seen at/after this
                 UTC-offset timestamp (e.g. 2026-08-01T00:00:00+00:00)
                 — phased backfill by ingest window.

The default (no flags) scans every tool part, repairs all disagreements,
deletes stale rows, and re-verifies that a fresh re-extraction diff is
empty.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Shared with live ingest — the SAME extraction functions (ADR 0015
# backfill↔live-equivalence): backfill and live extraction call the same
# function objects, so the projection can never drift from the
# ingest-time derivation.
from app.api.ingest import (  # noqa: E402
    _extract_tool_call_facts,
    _redact_json_value,
    _serialize_jsonb,
    _truncate_json_field,
)
from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger("backfill_tool_calls")

# ---------------------------------------------------------------------------
# SQL
#
# The scan reads every observed_parts row with part_type = 'tool' and
# LEFT JOINs its current observed_tool_calls projection on the unique
# source key (client_id, source_database_id, external_part_id).  The
# stored projection columns (t.tool_*) are the pre-backfill values the
# re-extraction diff compares against; NULL for a missing row.  The scan
# is deterministic (ORDER BY first_seen_at, id) so a --limit run
# advances through the store in a stable order.  The __SINCE__ marker is
# replaced by _scan_sql() with a first_seen_at bound for phased runs.
# ---------------------------------------------------------------------------

SCAN_SQL = """
    SELECT p.id AS part_id,
           p.client_id,
           p.source_database_id,
           p.external_part_id,
           p.message_id,
           p.session_id,
           p.external_session_id,
           p.source_created_at,
           p.source_updated_at,
           p.source_created_at_tz,
           p.source_updated_at_tz,
           p.first_seen_at,
           p.data,
           t.tool_name,
           t.tool_status,
           t.tool_input,
           t.tool_output
    FROM observed_parts p
    LEFT JOIN observed_tool_calls t
      ON t.client_id = p.client_id
     AND t.source_database_id = p.source_database_id
     AND t.external_part_id = p.external_part_id
    WHERE p.part_type = 'tool'
      /* __SINCE__ */
    ORDER BY p.first_seen_at, p.id
"""

_SINCE_MARKER = "/* __SINCE__ */"
_SINCE_CLAUSE = "AND p.first_seen_at >= $1"

# Recomputed rows are written with an INSERT ... ON CONFLICT upsert
# mirroring the live-ingest observed_tool_calls write exactly: tool_name
# is overwritten (NOT NULL), the nullable derived columns are
# COALESCE-filled so re-extraction never erases a stored value, and
# first_seen_at is preserved on conflict (ADR 0016 idempotent-projection
# convention).  On insert, first_seen_at carries over the part's
# first-observation time (the part and its tool call were first observed
# together); last_seen_at is refreshed to the backfill run time — the
# projection was (re)confirmed now.
UPSERT_TOOL_CALL_SQL = """
    INSERT INTO observed_tool_calls
        (id, client_id, source_database_id, part_id,
         external_part_id, message_id, session_id, external_session_id,
         tool_name, tool_status, tool_input, tool_output,
         source_created_at, source_updated_at,
         source_created_at_tz, source_updated_at_tz,
         first_seen_at, last_seen_at, data)
    VALUES ($1, $2, $3, $4,
            $5, $6, $7, $8,
            $9, $10, $11, $12,
            $13, $14, $15, $16,
            $17, $18, $19)
    ON CONFLICT (client_id, source_database_id, external_part_id)
    DO UPDATE SET
        part_id = EXCLUDED.part_id,
        message_id = COALESCE(EXCLUDED.message_id, observed_tool_calls.message_id),
        session_id = COALESCE(EXCLUDED.session_id, observed_tool_calls.session_id),
        external_session_id = COALESCE(
            EXCLUDED.external_session_id, observed_tool_calls.external_session_id),
        tool_name = EXCLUDED.tool_name,
        tool_status = COALESCE(EXCLUDED.tool_status, observed_tool_calls.tool_status),
        tool_input = COALESCE(EXCLUDED.tool_input, observed_tool_calls.tool_input),
        tool_output = COALESCE(EXCLUDED.tool_output, observed_tool_calls.tool_output),
        source_created_at = COALESCE(
            EXCLUDED.source_created_at, observed_tool_calls.source_created_at),
        source_updated_at = COALESCE(
            EXCLUDED.source_updated_at, observed_tool_calls.source_updated_at),
        source_created_at_tz = COALESCE(
            EXCLUDED.source_created_at_tz, observed_tool_calls.source_created_at_tz),
        source_updated_at_tz = COALESCE(
            EXCLUDED.source_updated_at_tz, observed_tool_calls.source_updated_at_tz),
        last_seen_at = EXCLUDED.last_seen_at,
        data = COALESCE(EXCLUDED.data, observed_tool_calls.data)
"""

# A tool-call row with NO backing tool part cannot be recomputed from
# observed_parts — either the part row is gone entirely, or the part was
# re-ingested with a different type (a part upsert overwrites
# part_type).  Both shapes are stale and are corrected by deletion:
# no tool part, no derived row (mirroring the sibling rollup backfill).
STALE_TOOL_CALL_SQL = """
    SELECT t.client_id, t.source_database_id, t.external_part_id
    FROM observed_tool_calls t
    LEFT JOIN observed_parts p
      ON t.client_id = p.client_id
     AND t.source_database_id = p.source_database_id
     AND t.external_part_id = p.external_part_id
    WHERE p.id IS NULL
       OR p.part_type <> 'tool'
    ORDER BY t.client_id, t.source_database_id, t.external_part_id
"""

STALE_TOOL_CALL_DELETE_SQL = """
    DELETE FROM observed_tool_calls t
    WHERE NOT EXISTS (
        SELECT 1
        FROM observed_parts p
        WHERE p.client_id = t.client_id
          AND p.source_database_id = t.source_database_id
          AND p.external_part_id = t.external_part_id
          AND p.part_type = 'tool'
    )
"""

STALE_TOOL_CALL_COUNT_SQL = f"""
SELECT COUNT(*) AS cnt
FROM (
{STALE_TOOL_CALL_SQL}
) sub;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_since(raw: str) -> datetime:
    """Parse ``--since`` into a UTC-offset-aware datetime (argparse type).

    A naive timestamp is rejected — bounding the scan on an ambiguous
    timezone would silently mis-window the backfill.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --since timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "--since must include a UTC offset (e.g. 2026-08-01T00:00:00+00:00)"
        )
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute observed_tool_calls rows from observed_parts "
        "with the live-ingest extraction logic (ADR 0016 §4: "
        "observed_parts.data is the authoritative store).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the re-extraction diff (missing / divergent / stale "
        "rows) without updating.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Repair at most N rows per run (in scan order) — useful for "
        "phased backfill. Re-run to converge.",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Only scan tool parts first seen at/after this UTC-offset "
        "timestamp (e.g. 2026-08-01T00:00:00+00:00).",
    )
    return parser.parse_args(argv)


def _scan_sql(since: datetime | None) -> str:
    """Return the tool-part scan, optionally bounded to parts first seen
    at/after ``since`` (a UTC-offset-aware datetime)."""
    if since is None:
        return SCAN_SQL.replace(_SINCE_MARKER, "")
    return SCAN_SQL.replace(_SINCE_MARKER, _SINCE_CLAUSE)


def _recompute_tool_call(
    data: object,
    *,
    tool_payload_max_chars: int,
) -> tuple[str, str | None, object, object]:
    """Recompute the expected ``observed_tool_calls`` columns from stored part data.

    Applies the exact live-ingest pipeline — ``_extract_tool_call_facts`` →
    ``_redact_json_value`` → ``_truncate_json_field`` — using the same
    function objects live ingest calls (``app/api/ingest.py:1575`` +
    ``1632-1637``), so the backfill derivation is identical by construction
    (ADR 0015 equivalence).  ``observed_parts.data`` was already redacted
    before persistence, so re-applying ``_redact_json_value`` is a no-op on
    the input/output values — but it is applied anyway so the backfill path
    is byte-for-byte the same sequence live ingest runs on the raw payload.

    Returns ``(tool_name, tool_status, tool_input, tool_output)`` with
    ``tool_input`` / ``tool_output`` redacted and truncated to
    ``tool_payload_max_chars`` per field.

    Raises ``ValueError`` when the stored data cannot produce a tool-call
    row — a truncation marker (the payload was cut at ingest and is not
    re-extractable), a non-tool type, or a missing tool name.  Live ingest
    would never have written such a projection row, so the backfill cannot
    either; the caller counts the part skipped.
    """
    if data is None:
        raise ValueError("stored part data is null")
    if isinstance(data, str):
        data = json.loads(data)
    tool_name, tool_status, tool_input, tool_output = _extract_tool_call_facts(data)
    if tool_name is None:
        raise ValueError("stored part data is not a re-extractable tool call")
    tool_input = _truncate_json_field(_redact_json_value(tool_input), tool_payload_max_chars)
    tool_output = _truncate_json_field(_redact_json_value(tool_output), tool_payload_max_chars)
    return tool_name, tool_status, tool_input, tool_output


def _tool_call_matches(row: asyncpg.Record, expected: tuple) -> bool:
    """Return ``True`` when the stored projection row already equals the
    re-extracted values (no repair needed).

    ``tool_input`` / ``tool_output`` are JSONB columns read back as JSON
    strings (asyncpg's default codec); they are decoded and compared
    structurally so JSONB's canonical key ordering never reports a false
    divergence.
    """
    tool_name, tool_status, tool_input, tool_output = expected
    stored_input = json.loads(row["tool_input"]) if row["tool_input"] is not None else None
    stored_output = json.loads(row["tool_output"]) if row["tool_output"] is not None else None
    return (
        row["tool_name"] == tool_name
        and row["tool_status"] == tool_status
        and stored_input == tool_input
        and stored_output == tool_output
    )


async def _upsert_tool_call(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    expected: tuple,
    *,
    now: datetime,
) -> None:
    """Upsert one recomputed ``observed_tool_calls`` row on the source key.

    ``first_seen_at`` on insert carries over the part's first-observation
    time (the part and its tool call were first observed together); on
    conflict it is preserved untouched.  ``last_seen_at`` is refreshed to
    ``now`` — the projection was (re)confirmed at backfill time.  The JSONB
    fields are serialized to JSON strings via ``_serialize_jsonb`` (asyncpg's
    JSONB codec accepts ``str`` only), matching live ingest exactly.
    """
    tool_name, tool_status, tool_input, tool_output = expected
    await conn.execute(
        UPSERT_TOOL_CALL_SQL,
        uuid.uuid4(),
        row["client_id"],
        row["source_database_id"],
        row["part_id"],
        row["external_part_id"],
        row["message_id"],
        row["session_id"],
        row["external_session_id"],
        tool_name,
        tool_status,
        _serialize_jsonb(tool_input),
        _serialize_jsonb(tool_output),
        row["source_created_at"],
        row["source_updated_at"],
        row["source_created_at_tz"],
        row["source_updated_at_tz"],
        row["first_seen_at"],
        now,
        row["data"],
    )


def _parse_row_count(tag: str) -> int:
    """Parse the row count out of an asyncpg execute tag."""
    parts = tag.split()
    if len(parts) == 2:
        return int(parts[1])
    return 0


async def _run_backfill(
    conn: asyncpg.Connection,
    *,
    tool_payload_max_chars: int,
    since: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Scan tool parts and repair disagreeing ``observed_tool_calls`` rows.

    Streams tool parts in deterministic order and, for each, recomputes
    the expected projection columns with the live-ingest extraction
    logic.  Rows whose stored projection already matches are counted
    unchanged and never rewritten (idempotency).  Missing or divergent
    rows are upserted — unless ``dry_run`` (counted only) or beyond the
    ``limit`` budget (counted deferred, left for a later run).

    Returns a counters dict: ``scanned``, ``unchanged``, ``repaired``,
    ``deferred``, ``skipped``.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")

    sql = _scan_sql(since)
    params = [since] if since is not None else []
    counters = {"scanned": 0, "unchanged": 0, "repaired": 0, "deferred": 0, "skipped": 0}
    now = datetime.now(timezone.utc)

    async for row in conn.cursor(sql, *params):
        counters["scanned"] += 1
        try:
            expected = _recompute_tool_call(
                row["data"], tool_payload_max_chars=tool_payload_max_chars,
            )
        except (ValueError, TypeError) as exc:
            counters["skipped"] += 1
            logger.warning(
                "Skipping tool part %s: cannot re-extract (%s).",
                row["external_part_id"], exc,
            )
            continue

        if _tool_call_matches(row, expected):
            counters["unchanged"] += 1
            continue

        if limit is not None and counters["repaired"] >= limit:
            counters["deferred"] += 1
            continue

        counters["repaired"] += 1
        if not dry_run:
            await _upsert_tool_call(conn, row, expected, now=now)

        if counters["scanned"] % 1000 == 0:
            logger.info(
                "Progress: %d tool part(s) scanned, %d repaired.",
                counters["scanned"], counters["repaired"],
            )

    return counters


async def _count_stale_tool_calls(conn: asyncpg.Connection) -> int:
    """Return the number of ``observed_tool_calls`` rows with no backing
    tool part."""
    row = await conn.fetchrow(STALE_TOOL_CALL_COUNT_SQL)
    return row["cnt"] if row else 0


async def _delete_stale_tool_calls(conn: asyncpg.Connection) -> int:
    """Delete ``observed_tool_calls`` rows with no backing tool part;
    return the deleted count."""
    result = await conn.execute(STALE_TOOL_CALL_DELETE_SQL)
    return _parse_row_count(result)


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


def _log_summary(counters: dict, stale: int) -> None:
    """Log the re-extraction diff counters in a human-readable format."""
    logger.info(
        "Tool-part re-extraction diff: %d scanned, %d unchanged, "
        "%d repaired, %d deferred (beyond --limit), %d skipped "
        "(unrecoverable data); %d stale observed_tool_calls row(s).",
        counters["scanned"],
        counters["unchanged"],
        counters["repaired"],
        counters["deferred"],
        counters["skipped"],
        stale,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, diff, optionally repair, re-verify."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    settings = get_settings()
    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            # ── Step 1: Re-extraction diff (dry-run or repair) ───────
            counters = await _run_backfill(
                conn,
                tool_payload_max_chars=settings.tool_payload_max_chars,
                since=args.since,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            stale = await _count_stale_tool_calls(conn)
            _log_summary(counters, stale)

            if args.dry_run:
                limit_note = f" (limit: {args.limit})" if args.limit else ""
                logger.info(
                    "DRY-RUN: would repair %d observed_tool_calls row(s)%s. "
                    "Re-run without --dry-run to apply.",
                    counters["repaired"],
                    limit_note,
                )
                return 0

            if counters["repaired"] == 0 and stale == 0:
                logger.info(
                    "No backfill needed — observed_tool_calls matches "
                    "re-extraction for every tool part.",
                )
                return 0

            # ── Step 2: Stale rows — no tool part, no derived row ────
            if stale > 0:
                deleted = await _delete_stale_tool_calls(conn)
                logger.warning(
                    "Deleted %d stale observed_tool_calls row(s) with no "
                    "backing tool part.",
                    deleted,
                )

            # ── Step 3: Re-verify (fresh re-extraction diff) ─────────
            remaining = await _run_backfill(
                conn,
                tool_payload_max_chars=settings.tool_payload_max_chars,
                since=args.since,
                limit=None,
                dry_run=True,
            )
            remaining_stale = await _count_stale_tool_calls(conn)
            total_remaining = remaining["repaired"] + remaining_stale
            if total_remaining == 0:
                logger.info(
                    "Verification passed — observed_tool_calls now matches "
                    "re-extraction for every tool part.",
                )
                return 0

            logger.error(
                "Verification FAILED — %d tool part(s) still disagree and "
                "%d stale row(s) remain. Re-run the script to converge "
                "remaining groups.",
                remaining["repaired"],
                remaining_stale,
            )
            return 1

    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
