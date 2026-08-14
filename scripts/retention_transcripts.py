#!/usr/bin/env python3
"""Execution-transcript retention job (issue #470, ADR 0016 Redaction and Privacy).

Deletes rows older than the configured per-table retention window from the
append-only transcript tables — ``observed_messages``, ``observed_parts``,
``observed_tool_calls``.  Transcript data is higher-volume and
lower-longevity than accounting data; accounting aggregates (usage events,
rollup) keep their existing, longer retention and are NEVER touched by this
job.

Retention is keyed on the transcript timestamps (``source_created_at_tz``),
never ingest time.  Rows without a source timestamp have unknown age and are
retained (never prematurely deleted).  A row exactly at the retention edge
(cutoff = now - window) is retained; only rows strictly older than the
cutoff are deleted.

The job is **idempotent** — safe to run on a schedule: re-running deletes
nothing further, empty tables are a no-op, and concurrent runs are safe
because each bounded DELETE batch is atomic and never conflicts.

Bounded batches, no unbounded single transaction: rows are deleted in
``--batch-size`` chunks (default {default_batch}), each batch an
independent autocommitted statement, so a run never holds one giant
transaction open.  Tables are processed children-first
(``observed_tool_calls`` → ``observed_parts`` → ``observed_messages``) so
the per-table counts reflect the explicit policy; the FK ``ON DELETE
CASCADE`` chain is only a safety net for rows whose parent was already
removed.

Usage::

    python scripts/retention_transcripts.py [--dry-run] [--limit N]
        [--batch-size N]

Flags:
    --dry-run       print the full per-table report (cutoffs + would-be
                    deletion counts) and delete nothing.
    --limit N       delete at most N rows across all tables (phased runs).
    --batch-size N  rows per DELETE batch (default 1000).

Retention windows come from the ``GATEWAY_`` settings (see
``app/core/config.py`` / ``.env.example``):

    GATEWAY_TRANSCRIPT_RETENTION_MESSAGES_DAYS    (default 365)
    GATEWAY_TRANSCRIPT_RETENTION_PARTS_DAYS       (default 90)
    GATEWAY_TRANSCRIPT_RETENTION_TOOL_CALLS_DAYS  (default 90)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger("retention_transcripts")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

# Children-first so the per-table counts reflect the explicit policy; the
# FK cascade (parts → messages, tool_calls → parts) is only a safety net.
TRANSCRIPT_TABLES = (
    "observed_tool_calls",
    "observed_parts",
    "observed_messages",
)

DEFAULT_BATCH_SIZE = 1000


# ── SQL ──────────────────────────────────────────────────────────────────────


def _count_sql(table: str) -> str:
    """Count rows eligible for retention in ``table`` (strictly older than
    the cutoff; NULL source timestamps are never eligible)."""
    return (
        f"SELECT count(*) AS cnt FROM {table}"
        " WHERE source_created_at_tz < $1"
    )


def _delete_sql(table: str) -> str:
    """Delete one bounded batch of eligible rows from ``table``.

    The ``LIMIT`` subselect keeps each statement's transaction small; the
    job loops until a batch comes back short.  Only ``source_created_at_tz``
    is compared — ingest-side timestamps are never used.
    """
    return (
        f"DELETE FROM {table} WHERE id IN ("
        f" SELECT id FROM {table}"
        "  WHERE source_created_at_tz < $1"
        "  LIMIT $2"
        ")"
    )


# ── Report ───────────────────────────────────────────────────────────────────


@dataclass
class RetentionReport:
    """The full report printed by the CLI (and asserted by tests)."""

    now: datetime
    dry_run: bool
    cutoffs: dict[str, datetime] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


def format_report(report: RetentionReport) -> str:
    """Render the report (dry-run and write runs share the same form)."""
    lines = [
        "Execution-transcript retention report",
        f"as-of: {report.now.isoformat()}",
        f"mode: {'dry-run' if report.dry_run else 'write'}",
    ]
    for table in TRANSCRIPT_TABLES:
        cutoff = report.cutoffs.get(table)
        cutoff_text = cutoff.isoformat() if cutoff else "-"
        lines.append(
            f"{table}: {report.deleted.get(table, 0)} row(s)"
            f" older than {cutoff_text}"
        )
    lines.append(f"total: {report.total} row(s)")
    if report.dry_run:
        lines.append(
            "dry-run: no rows were deleted; re-run without --dry-run to apply."
        )
    return "\n".join(lines)


# ── Settings wiring ──────────────────────────────────────────────────────────


def _windows_from_settings(settings) -> dict[str, timedelta]:
    """Per-table retention windows from the ``GATEWAY_`` settings."""
    return {
        "observed_messages": timedelta(
            days=settings.transcript_retention_messages_days
        ),
        "observed_parts": timedelta(days=settings.transcript_retention_parts_days),
        "observed_tool_calls": timedelta(
            days=settings.transcript_retention_tool_calls_days
        ),
    }


# ── Core job ─────────────────────────────────────────────────────────────────


def _parse_delete_count(status: str) -> int:
    """Parse an asyncpg ``DELETE n`` status string into an int."""
    return int(status.rsplit(" ", 1)[-1])


async def run_retention(
    conn: asyncpg.Connection,
    *,
    windows: Mapping[str, timedelta],
    now: datetime | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> RetentionReport:
    """Enforce per-table retention on the transcript tables.

    ``windows`` maps each transcript table name to its retention window;
    the cutoff for a table is ``now - windows[table]`` and rows strictly
    older than that cutoff (``source_created_at_tz < cutoff``) are deleted.
    In ``dry_run`` mode only the would-be counts are computed.  ``limit``
    caps the total rows deleted (or counted) across all tables; deletions
    proceed in ``batch_size`` bounded batches, each its own transaction.
    """
    now = now if now is not None else datetime.now(UTC)
    cutoffs = {table: now - windows[table] for table in TRANSCRIPT_TABLES}
    deleted: dict[str, int] = {}
    remaining = limit

    for table in TRANSCRIPT_TABLES:
        cutoff = cutoffs[table]

        if dry_run:
            row = await conn.fetchrow(_count_sql(table), cutoff)
            eligible = row["cnt"] if row is not None else 0
            if remaining is not None:
                eligible = min(eligible, remaining)
                remaining -= eligible
            deleted[table] = eligible
            continue

        count = 0
        while True:
            if remaining is not None and remaining <= 0:
                break
            take = batch_size
            if remaining is not None:
                take = min(take, remaining)
            status = await conn.execute(_delete_sql(table), cutoff, take)
            n = _parse_delete_count(status)
            count += n
            if remaining is not None:
                remaining -= n
            # A short batch means the table is exhausted (or the limit is
            # hit, which the next loop check turns into a stop).
            if n < take:
                break
        deleted[table] = count

    return RetentionReport(now=now, dry_run=dry_run, cutoffs=cutoffs, deleted=deleted)


# ── CLI wiring ───────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enforce per-table retention on the execution-transcript tables"
            " (observed_messages / observed_parts / observed_tool_calls),"
            " keyed on source_created_at_tz."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full per-table report without deleting any rows.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Delete at most N rows across all tables (phased runs).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=f"Rows per DELETE batch (default {DEFAULT_BATCH_SIZE}).",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    return args


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


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, compute windows from settings, run, report."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    settings = get_settings()
    windows = _windows_from_settings(settings)

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            report = await run_retention(
                conn,
                windows=windows,
                dry_run=args.dry_run,
                limit=args.limit,
                batch_size=args.batch_size or DEFAULT_BATCH_SIZE,
            )
            print(format_report(report))
    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
