#!/usr/bin/env python3
"""Correct an execution binding wrongly recorded as ``cancelled`` (issue #654).

The execution binding for PR #653 (AWX job 9293) records
``outcome = 'cancelled'`` even though the run actually succeeded.  The
root cause is a prose-substring heuristic in the
``openclaw_ansible_playbooks`` AWX integration that matched "cancell" in
the coordinator's closing summary.

**Why a script and not the API** — the execution-binding write contract
(issue #590) guarantees *terminal history is never overwritten*: an
already-terminal row is only re-observed idempotently or rejected as a
conflict.  The erroneous ``cancelled`` record cannot be corrected through
the API without violating that contract, so the disposition chosen for
issue #654 is option 3: a least-privilege, auditable admin-path script.

**Why this is auditable** — every correction writes a row to the
``execution_outcome_corrections`` audit table (migration 0043) in the
same transaction as the flip: the previous and new outcome, the previous
failure metadata, the operator-supplied reason, and the correction time.

**Why narrow** — only ``cancelled`` rows may be corrected, only to
``completed``.  ``running`` rows are refused (the normal two-phase
lifecycle owns that transition); ``failed`` rows are refused (different
root cause class); already-corrected rows are idempotent no-ops.  Failure
metadata is cleared on correction: a ``completed`` execution carries no
Failure Summary, and the stored summary text is the prose that caused the
false cancellation in the first place.

Usage:
    python scripts/correct_execution_outcome.py audit
    python scripts/correct_execution_outcome.py correct --awx-job-id 9293 --reason "..."

Subcommands:
    audit     Read-only: list every ``cancelled`` execution binding with
              its failure metadata so the operator can verify whether the
              ``cancelled`` entries for change-requests #619 and #608
              stem from the same prose-substring root cause.
    correct   Flip one ``cancelled`` execution to ``completed`` (must be
              explicitly identified by AWX job id and carry an operator
              reason for the audit trail).
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

logger = logging.getLogger("correct_execution_outcome")

CORRECTION_REASON_MAX_LENGTH = 1000

# Bounded, redacted failure metadata columns carried on a cancelled row.
# Cleared on correction: a completed execution carries no Failure Summary.
_FAILURE_METADATA_COLUMNS = ("failure_reason", "failure_summary")

LIST_CANCELLED_SQL = """
SELECT id, awx_job_id, entity_number, title, failure_reason, failure_summary,
       finished_at
FROM execution_bindings
WHERE outcome = 'cancelled'
ORDER BY awx_job_id;
"""

FETCH_BINDING_SQL = """
SELECT id, outcome, failure_reason, failure_summary
FROM execution_bindings
WHERE awx_job_id = $1
"""

AUDIT_INSERT_SQL = """
INSERT INTO execution_outcome_corrections (
    id, execution_binding_id, awx_job_id, previous_outcome, new_outcome,
    previous_failure_reason, previous_failure_summary, reason, corrected_at
) VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8)
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Correct execution bindings wrongly recorded as cancelled (issue #654).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "audit",
        help="List every cancelled execution binding with its failure metadata.",
    )

    p_correct = sub.add_parser(
        "correct",
        help="Flip one cancelled execution to completed (audited).",
    )
    p_correct.add_argument(
        "--awx-job-id",
        required=True,
        help="AWX job id of the execution binding to correct (e.g. 9293).",
    )
    p_correct.add_argument(
        "--reason",
        required=True,
        help="Operator reason recorded in the audit trail (max "
        f"{CORRECTION_REASON_MAX_LENGTH} chars).",
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


async def _list_cancelled_executions(
    conn: asyncpg.Connection,
) -> list[asyncpg.Record]:
    """Return every ``cancelled`` execution binding with failure metadata."""
    return await conn.fetch(LIST_CANCELLED_SQL)


async def _validate_reason(reason: str) -> str:
    """Bound the operator reason to the same 1000-char limit as the
    Failure Summary model contract."""
    if not reason or not reason.strip():
        raise ValueError("A non-empty --reason is required for the audit trail")
    if len(reason) > CORRECTION_REASON_MAX_LENGTH:
        raise ValueError(f"--reason exceeds the {CORRECTION_REASON_MAX_LENGTH}-char limit")
    return reason


async def _correct(
    conn: asyncpg.Connection,
    *,
    awx_job_id: int,
    reason: str,
) -> str:
    """Flip one ``cancelled`` execution to ``completed`` with an audit row.

    Returns a status string: ``corrected`` / ``not_found`` / ``refused`` /
    ``already_completed``.
    """
    reason = await _validate_reason(reason)
    async with conn.transaction():
        row = await conn.fetchrow(FETCH_BINDING_SQL, awx_job_id)
        if row is None:
            return "not_found"
        if row["outcome"] != "cancelled":
            return "already_completed" if row["outcome"] == "completed" else "refused"

        await conn.execute(
            AUDIT_INSERT_SQL,
            row["id"],
            awx_job_id,
            "cancelled",
            "completed",
            row["failure_reason"],
            row["failure_summary"],
            reason,
            _utcnow(),
        )
        await conn.execute(
            """
            UPDATE execution_bindings
            SET outcome = 'completed',
                failure_reason = NULL,
                failure_summary = NULL,
                updated_at = $2
            WHERE id = $1
            """,
            row["id"],
            _utcnow(),
        )
    return "corrected"


async def main(argv: list[str] | None = None) -> int:
    """Entry point — dispatch to the audit (read-only) or correct (write) path."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    if args.command == "audit":
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await _list_cancelled_executions(conn)
        finally:
            await pool.close()
        if not rows:
            print("No cancelled execution bindings found.")
            return 0
        print(
            f"{len(rows)} cancelled execution binding(s) — verify against the "
            "prose-substring root cause (issue #654):"
        )
        for r in rows:
            title = r["title"] or "(no title)"
            summary = r["failure_summary"] or "(no failure summary)"
            print(
                f"  awx_job_id={r['awx_job_id']} change_request=#{r['entity_number'] or '?'} "
                f"title={title!r} failure_summary={summary!r}"
            )
        return 0

    # correct
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            status = await _correct(
                conn,
                awx_job_id=int(args.awx_job_id),
                reason=args.reason,
            )
    finally:
        await pool.close()
    logger.info("Correction result for AWX job %s: %s", args.awx_job_id, status)
    return 0 if status in {"corrected", "already_completed"} else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
