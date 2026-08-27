"""Durable job store for the API-triggered AFK backfill.

Owns every SQL statement against ``afk_backfill_jobs`` so the API (producer),
the dedicated worker (consumer), and the retention sweep never drift.  Rows
are returned as plain dicts keyed by column name; the API layer maps them onto
the Pydantic response models in :mod:`app.core.schemas.backfill`.

State machine (mirrors the migration's CHECK constraint):

    queued -> running -> completed
                     -> failed
    queued -> cancelled

Cancellation is queued-only by construction: the cancel UPDATE matches
``status = 'queued'`` atomically, so a job the worker has already claimed
(``running``) can never be flipped — running work is not forcibly interrupted
in v1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from app.core.logging import redact_text
from scripts.afk_backfill import BackfillReport

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})
ALL_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
)

# Bound on a single persisted evidence line (opt-in evidence is also capped
# at ``GATEWAY_BACKFILL_MAX_EVIDENCE_LINES`` lines by the callers).
MAX_EVIDENCE_LINE_CHARS = 1000

_JOB_COLUMNS = """
    id, status, provider, repository, window_from, window_until,
    dry_run, show_evidence, requested_by, retry_count,
    failure_category, failure_message, evidence,
    change_requests_scanned, issues_scanned, sessions_considered,
    explicit_matches, high_matches, inferred_matches, ambiguous, unmatched,
    created_at, started_at, completed_at
"""

INSERT_JOB_SQL = f"""
    INSERT INTO afk_backfill_jobs
        (provider, repository, window_from, window_until, dry_run,
         show_evidence, requested_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    RETURNING {_JOB_COLUMNS}
"""

SELECT_JOB_SQL = f"SELECT {_JOB_COLUMNS} FROM afk_backfill_jobs WHERE id = $1"

COUNT_JOBS_SQL = "SELECT COUNT(*) FROM afk_backfill_jobs"

COMPLETE_JOB_SQL = f"""
    UPDATE afk_backfill_jobs
    SET status = '{STATUS_COMPLETED}', completed_at = now(),
        retry_count = $2,
        change_requests_scanned = $3, issues_scanned = $4,
        sessions_considered = $5, explicit_matches = $6, high_matches = $7,
        inferred_matches = $8, ambiguous = $9, unmatched = $10,
        evidence = $11
    WHERE id = $1
    RETURNING {_JOB_COLUMNS}
"""

FAIL_JOB_SQL = f"""
    UPDATE afk_backfill_jobs
    SET status = '{STATUS_FAILED}', completed_at = now(),
        retry_count = $2, failure_category = $3, failure_message = $4
    WHERE id = $1
    RETURNING {_JOB_COLUMNS}
"""

INCREMENT_RETRY_SQL = """
    UPDATE afk_backfill_jobs SET retry_count = retry_count + 1 WHERE id = $1
"""

CANCEL_JOB_SQL = f"""
    UPDATE afk_backfill_jobs
    SET status = '{STATUS_CANCELLED}', completed_at = now()
    WHERE id = $1 AND status = '{STATUS_QUEUED}'
    RETURNING {_JOB_COLUMNS}
"""

CLAIM_JOB_SQL = f"""
    UPDATE afk_backfill_jobs
    SET status = '{STATUS_RUNNING}', started_at = now()
    WHERE id = (
        SELECT id FROM afk_backfill_jobs
        WHERE status = '{STATUS_QUEUED}'
          AND provider = $1 AND repository = $2
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    AND status = '{STATUS_QUEUED}'
    RETURNING {_JOB_COLUMNS}
"""

QUEUED_REPOSITORIES_SQL = """
    SELECT DISTINCT provider, repository
    FROM afk_backfill_jobs
    WHERE status = 'queued'
    ORDER BY provider, repository
"""

PRUNE_JOBS_SQL = """
    DELETE FROM afk_backfill_jobs
    WHERE status = ANY($1::text[]) AND completed_at < $2
    RETURNING id
"""

RECLAIM_STALE_SQL = f"""
    UPDATE afk_backfill_jobs
    SET status = '{STATUS_FAILED}', completed_at = now(),
        failure_category = 'interrupted', failure_message = $2
    WHERE status = '{STATUS_RUNNING}' AND started_at < $1
    RETURNING {_JOB_COLUMNS}
"""


def bounded_evidence(lines: list[str], *, max_lines: int) -> list[str]:
    """Bound and redact opt-in evidence lines before they are persisted.

    Evidence is opt-in (``show_evidence``), bounded (at most ``max_lines``
    lines, each capped at :data:`MAX_EVIDENCE_LINE_CHARS` characters), and
    passed through the shared redaction path before it is ever stored.
    """
    return [redact_text(line)[:MAX_EVIDENCE_LINE_CHARS] for line in lines[:max_lines]]


class BackfillJobStore:
    """Stateless SQL access to ``afk_backfill_jobs`` on one connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def create(
        self,
        *,
        provider: str,
        repository: str,
        window_from: datetime,
        window_until: datetime,
        dry_run: bool,
        show_evidence: bool,
        requested_by: str,
    ) -> dict[str, Any]:
        """Insert a new job row (``status = queued``)."""
        row = await self._conn.fetchrow(
            INSERT_JOB_SQL,
            provider,
            repository,
            window_from,
            window_until,
            dry_run,
            show_evidence,
            requested_by,
        )
        return dict(row)

    async def get(self, job_id: str | Any) -> dict[str, Any] | None:
        """Fetch one job row by id (``None`` when unknown)."""
        row = await self._conn.fetchrow(SELECT_JOB_SQL, job_id)
        return dict(row) if row is not None else None

    async def list_jobs(
        self,
        *,
        status_filter: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Count + page job rows, optionally filtered by status."""
        params: list[Any] = []
        where = "TRUE"
        if status_filter is not None:
            where = f"status = ${len(params) + 1}"
            params.append(status_filter)
        total = await self._conn.fetchval(
            f"SELECT COUNT(*) FROM afk_backfill_jobs WHERE {where}", *params
        )
        data_sql = f"""
            SELECT {_JOB_COLUMNS} FROM afk_backfill_jobs
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        rows = await self._conn.fetch(data_sql, *params, limit, offset)
        return int(total), [dict(row) for row in rows]

    async def cancel(self, job_id: str | Any) -> dict[str, Any] | None:
        """Cancel a queued job; returns the updated row or ``None``.

        ``None`` means the job is not queued (missing, already running,
        finished, or already cancelled) — the caller distinguishes 404 from
        409 by fetching the row first.
        """
        row = await self._conn.fetchrow(CANCEL_JOB_SQL, job_id)
        return dict(row) if row is not None else None

    async def claim(self, provider: str, repository: str) -> dict[str, Any] | None:
        """Claim the oldest queued job for a repository (``SKIP LOCKED``).

        Callers must hold the (provider, repository) advisory lock so the
        claim is serialized per repository; the ``FOR UPDATE SKIP LOCKED``
        guard additionally protects against a cancel UPDATE racing the claim.
        """
        row = await self._conn.fetchrow(CLAIM_JOB_SQL, provider, repository)
        return dict(row) if row is not None else None

    async def complete(
        self,
        job_id: str | Any,
        *,
        report: BackfillReport,
        evidence: list[str] | None,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """Mark a job completed with the reused :class:`BackfillReport` counters."""
        row = await self._conn.fetchrow(
            COMPLETE_JOB_SQL,
            job_id,
            retry_count,
            report.change_requests_scanned,
            report.issues_scanned,
            report.sessions_considered,
            report.explicit_matches,
            report.high_matches,
            report.inferred_matches,
            report.ambiguous,
            report.unmatched,
            evidence,
        )
        return dict(row)

    async def fail(
        self,
        job_id: str | Any,
        *,
        category: str,
        message: str,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """Mark a job failed with a safe failure category and message."""
        row = await self._conn.fetchrow(
            FAIL_JOB_SQL, job_id, retry_count, category, message
        )
        return dict(row)

    async def increment_retry(self, job_id: str | Any) -> None:
        """Bump the durable retry counter before a bounded retry attempt."""
        await self._conn.execute(INCREMENT_RETRY_SQL, job_id)

    async def queued_repositories(self) -> list[tuple[str, str]]:
        """Return distinct (provider, repository) pairs with queued work."""
        rows = await self._conn.fetch(QUEUED_REPOSITORIES_SQL)
        return [(row["provider"], row["repository"]) for row in rows]

    async def prune(self, cutoff: datetime) -> int:
        """Delete terminal rows older than ``cutoff``; returns the count."""
        rows = await self._conn.fetch(
            PRUNE_JOBS_SQL, sorted(TERMINAL_STATUSES), cutoff
        )
        return len(rows)

    async def reclaim_stale(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Fail ``running`` jobs whose claim predates ``cutoff`` (crash recovery)."""
        rows = await self._conn.fetch(
            RECLAIM_STALE_SQL, cutoff, "Worker interrupted; re-submit to retry."
        )
        return [dict(row) for row in rows]
