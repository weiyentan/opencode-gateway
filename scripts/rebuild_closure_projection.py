#!/usr/bin/env python3
"""Closure projection rebuild CLI (issue #539).

A stdlib-argparse entry point that rebuilds the closure-episode projection
from the committed ``engineering_events`` facts.  It reuses the same
pure-domain projector (:func:`afk_outcomes.closure_episodes.project_closure_episodes`)
and the same repository reconcile path
(:meth:`afk_outcomes.repository.AsyncpgOutcomeRepository.rebuild_closure_projection`)
as the incremental recompute, so a full rebuild converges on the identical
projection state the live path would produce.

The CLI is a thin orchestration layer: it never duplicates or forks the
projection logic, and all app-dependent wiring (config, DB pool, the
repository-URL normalizer) lives here, keeping ``afk_outcomes/`` pure domain.

Usage::

    python scripts/rebuild_closure_projection.py [--since ISO] [--until ISO] \\
        [--confirm] [--dry-run]

Flags:
    --since/--until  optional time bounds (ISO 8601; naive values assumed
                     UTC).  When given, only issues whose ENTIRE
                     closure-relevant fact history falls within
                     ``[since, until]`` are written; issues whose lifecycle
                     is cut by the window are excluded from the write set
                     (never regressed from a partial fact set) and no
                     projection rows are deleted.  Defaults to a full
                     rebuild over every closure-relevant fact.
    --confirm        explicitly confirm the rebuild.  A full rebuild refuses
                     to write anything without this flag (or ``--yes``).
    --dry-run        print the full report (facts processed, event range,
                     and the resulting closure_links / closure_episodes /
                     closure_unresolved counts) and write nothing.

A full rebuild converges on identical projection state: the projector is
deterministic, the reconcile writes are conflict-updates, and stale
closure_links / closure_unresolved rows absent from the fresh projection
are removed.  A windowed rebuild only ever writes whole-window issues and
never deletes — it is a bounded repair primitive, not a convergence run.

This is a CLI/AWX-only operation — no public API endpoint is added, no
schema migration and no automatic reconciliation scheduler are introduced.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from afk_outcomes import AsyncpgOutcomeRepository  # noqa: E402
from afk_outcomes.repository import ClosureRebuildResult  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.repository import normalize_repository_url  # noqa: E402

logger = logging.getLogger("rebuild_closure_projection")


class _ReadOnlyConnection:
    """Wrap an asyncpg connection so writes are blocked (``--dry-run``).

    Forwards reads (``fetch`` and any other attribute) to the underlying
    connection so the rebuild can compute the projection, but turns every
    write (``execute``) into a no-op.  This guarantees ``--dry-run`` never
    touches the database while still producing an accurate "what WOULD be
    written" report.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        # Forward any attribute not explicitly overridden (e.g. ``fetch``) to
        # the underlying connection.
        return getattr(self._conn, name)

    async def execute(self, *args, **kwargs) -> str:
        # Block every write.  Return a benign status string so callers that
        # inspect the result (none do in the rebuild path) still work.
        return "DRY RUN 0 0"


@dataclass
class RebuildReport:
    """The full report printed by the CLI (and asserted by tests)."""

    since: datetime | None
    until: datetime | None
    dry_run: bool
    confirmed: bool
    facts_processed: int
    event_range_start: datetime | None
    event_range_end: datetime | None
    closure_links: int
    closure_episodes: int
    closure_unresolved: int


def format_report(report: RebuildReport) -> str:
    """Render the full report (dry-run and write runs share the same form)."""
    window = (
        f"{report.since.isoformat()} .. {report.until.isoformat()}"
        if report.since is not None or report.until is not None
        else "full"
    )
    event_range = (
        f"{report.event_range_start.isoformat()} .. {report.event_range_end.isoformat()}"
        if report.event_range_start is not None and report.event_range_end is not None
        else "(none)"
    )
    lines = [
        "closure projection rebuild report",
        f"window: {window}",
        f"mode: {'dry-run' if report.dry_run else 'write'}",
        f"confirmed: {'yes' if report.confirmed else 'no'}",
        f"facts processed: {report.facts_processed}",
        f"event range: {event_range}",
        f"closure_links: {report.closure_links}",
        f"closure_episodes: {report.closure_episodes}",
        f"closure_unresolved: {report.closure_unresolved}",
    ]
    if report.dry_run:
        lines.append("dry-run: no rows were written; re-run with --confirm to persist.")
    return "\n".join(lines)


async def run_rebuild(
    conn: asyncpg.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> RebuildReport:
    """Rebuild the closure projection and return the report.

    A full rebuild (``dry_run=False``) requires explicit confirmation
    (``confirm=True``) before writing anything.  In ``dry_run`` mode nothing
    is written and no confirmation is required; the returned report is
    identical to what a real write would produce for the same bounds.
    """
    if not dry_run and not confirm:
        raise SystemExit(
            "refusing to write without explicit confirmation; "
            "re-run with --confirm (or --yes) to persist, or use --dry-run."
        )

    # In dry-run mode, wrap the connection so every write is blocked: the
    # repository still computes the projection (reads) so the report reflects
    # what WOULD be written, but no row is ever upserted.
    repository = AsyncpgOutcomeRepository(
        _ReadOnlyConnection(conn) if dry_run else conn
    )
    result: ClosureRebuildResult = await repository.rebuild_closure_projection(
        since=since,
        until=until,
        normalize_repository=normalize_repository_url,
    )

    return RebuildReport(
        since=since,
        until=until,
        dry_run=dry_run,
        confirmed=confirm,
        facts_processed=result.facts_processed,
        event_range_start=result.event_range_start,
        event_range_end=result.event_range_end,
        closure_links=len(result.projection.links),
        closure_episodes=len(result.projection.episodes),
        closure_unresolved=len(result.projection.unresolved),
    )


def _parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 bound (naive values are assumed UTC)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid datetime: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the closure-episode projection from committed "
            "engineering_events facts (operator-only; CLI/AWX)."
        ),
    )
    parser.add_argument(
        "--since",
        type=_parse_datetime,
        default=None,
        help="Process only facts at/after this time (ISO 8601; naive assumed UTC). Only issues whose entire closure-relevant fact history falls within [since, until] are written; issues cut by the window are excluded and never regressed.",
    )
    parser.add_argument(
        "--until",
        type=_parse_datetime,
        default=None,
        help="Process only facts at/before this time (ISO 8601; naive assumed UTC). Only issues whose entire closure-relevant fact history falls within [since, until] are written; issues cut by the window are excluded and never regressed.",
    )
    parser.add_argument(
        "--confirm",
        "--yes",
        action="store_true",
        dest="confirm",
        help="Explicitly confirm the rebuild before writing any rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report without writing any rows.",
    )
    args = parser.parse_args(argv)
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since must not be after --until")
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
    """Entry point — parse args, rebuild the projection, persist or dry-run."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    pool = await _get_pool()

    try:
        async with pool.acquire() as conn:
            report = await run_rebuild(
                conn,
                since=args.since,
                until=args.until,
                dry_run=args.dry_run,
                confirm=args.confirm,
            )
            print(format_report(report))
    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
