"""AFK dashboard daily rollup recomputation engine (issue #715).

The single writer of the ``afk_dashboard_daily`` read-model (migration 0046).
For one ``(day, provider, repository)`` bucket it recomputes every additive
metric from the canonical source tables and replaces the whole row with an
``INSERT ... ON CONFLICT DO UPDATE SET col = EXCLUDED.col`` upsert.

Follows the ``client_project_rollup`` precedent (migration 0023, ADR 0014/
0015): raw asyncpg SQL, a per-bucket advisory lock, and an idempotent,
full-row upsert.  The rollup is a derived convenience; the canonical tables
remain the source of truth, and the upsert *replaces* rather than increments,
so a retried recomputation can never double-count.

Metric sources and event-time bucketing (all UTC calendar days)
---------------------------------------------------------------

====================================  ==========================  ====================================
Metric                                Canonical table             Event-time column
====================================  ==========================  ====================================
``runs_started``                      ``afk_runs``                ``COALESCE(started_at, first_seen_at)``
``change_requests_opened``            ``engineering_events``      ``occurred_at``
``change_requests_merged``            ``engineering_events``      ``occurred_at``
``change_requests_closed``            ``engineering_events``      ``occurred_at``
``execution_count``                   ``execution_bindings``      ``COALESCE(started_at, created_at)``
``successful_execution_count``        ``execution_bindings``      ``COALESCE(started_at, created_at)``
``failed_execution_count``            ``execution_bindings``      ``COALESCE(started_at, created_at)``
``cancelled_execution_count``         ``execution_bindings``      ``COALESCE(started_at, created_at)``
``session_count``                     ``afk_run_sessions``        ``COALESCE(started_at, first_seen_at)``
``input_tokens``                      ``usage_events``            ``reported_at``
``output_tokens``                     ``usage_events``            ``reported_at``
``cache_read_tokens``                 ``usage_events``            ``reported_at``
``cache_write_tokens``                ``usage_events``            ``reported_at``
``estimated_cost_usd``                ``usage_events``            ``reported_at``
====================================  ==========================  ====================================

Attribution policy — unresolved and ambiguous data is excluded, never
guessed:

* A metric whose canonical row carries a ``NULL`` repository identity
  (``afk_runs.repository IS NULL``, ``execution_bindings.repository_url IS
  NULL``) cannot be keyed into a bucket and is skipped.  The bucket
  ``repository`` is the normalized repository identity the rest of the
  gateway uses.
* Sessions and usage are attributed to a run through ``afk_run_sessions``
  (the internal ``session_id``).  When one session maps to more than one
  AFK run the attribution is ambiguous, so the session — and its usage —
  is excluded rather than split or arbitrarily assigned.

Change-request counts come from ``engineering_events`` — the immutable
facts — not from ``afk_run_entities`` (derived links that may be provisional
or superseded).  This keeps the rollup a direct projection of source facts.

The module opens no transaction of its own (mirroring
``app.core.reconciliation``): the caller owns the transaction, so
:func:`acquire_bucket_lock` (``pg_advisory_xact_lock``) spans the
compute-then-upsert sequence and is released exactly at the caller's
commit/rollback.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version + lock namespace
# ---------------------------------------------------------------------------

AFK_DASHBOARD_ROLLUP_VERSION = "2"
"""Rule version stamped on every recomputed bucket (``rollup_version``).

Bump this whenever any metric's source table, filter, or event-time
bucketing changes, so consumers can detect version-skewed buckets.  Callers
may override it per call via the ``rollup_version`` argument, but the
default is this configurable constant — never a hardcoded SQL literal.
"""

AFK_DASHBOARD_LOCK_CLASS = 47_007
"""High 32 bits of the two-arg per-bucket recomputation advisory lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002, ``REPLAY_LOCK_CLASS`` = 47_003,
``RECONCILE_LOCK_CLASS`` = 47_004, ``CANONICAL_EVENT_LOCK_CLASS`` = 47_005,
``AGGREGATE_LOCK_CLASS`` = 47_006).  The low 32 bits are derived from a
signed-int32 hash of ``(day, provider, repository)`` so concurrent
recomputations of the *same* bucket serialise while different buckets never
contend.
"""

METRIC_COLUMNS: tuple[str, ...] = (
    "runs_started",
    "change_requests_opened",
    "change_requests_merged",
    "change_requests_closed",
    "execution_count",
    "successful_execution_count",
    "failed_execution_count",
    "cancelled_execution_count",
    "session_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
)
"""The additive metric columns, in the exact order the upsert binds them."""

# ---------------------------------------------------------------------------
# Per-category recomputation SQL
#
# Every query takes the same three parameters:
#   $1 day (date)  $2 provider (text)  $3 repository (text)
# and buckets on the UTC calendar day of the metric's own event time.
# ---------------------------------------------------------------------------

RUNS_SQL = """
    SELECT COUNT(*)::int AS runs_started
    FROM afk_runs r
    WHERE r.provider = $2
      AND r.repository = $3
      AND (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date = $1
"""

CHANGE_REQUEST_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE e.event_type = 'change_request.opened')::int
            AS change_requests_opened,
        COUNT(*) FILTER (WHERE e.event_type = 'change_request.merged')::int
            AS change_requests_merged,
        COUNT(*) FILTER (WHERE e.event_type = 'change_request.closed')::int
            AS change_requests_closed
    FROM engineering_events e
    WHERE e.provider = $2
      AND e.repository = $3
      AND e.entity_type = 'change_request'
      AND (e.occurred_at AT TIME ZONE 'UTC')::date = $1
"""

EXECUTION_SQL = """
    SELECT
        COUNT(*)::int AS execution_count,
        COUNT(*) FILTER (WHERE b.outcome = 'completed')::int
            AS successful_execution_count,
        COUNT(*) FILTER (WHERE b.outcome = 'failed')::int
            AS failed_execution_count,
        COUNT(*) FILTER (WHERE b.outcome = 'cancelled')::int
            AS cancelled_execution_count
    FROM execution_bindings b
    WHERE b.provider = $2
      AND b.repository_url = $3
      AND (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date = $1
"""

SESSION_SQL = """
    WITH unambiguous_sessions AS (
        SELECT ars.session_id AS gateway_session_id,
               MIN(ars.afk_run_id) AS afk_run_id
        FROM afk_run_sessions ars
        WHERE ars.session_id IS NOT NULL
        GROUP BY ars.session_id
        HAVING COUNT(DISTINCT ars.afk_run_id) = 1
    )
    SELECT COUNT(*)::int AS session_count
    FROM afk_run_sessions ars
    JOIN unambiguous_sessions us ON us.gateway_session_id = ars.session_id
    JOIN afk_runs r ON r.afk_run_id = us.afk_run_id
    WHERE r.provider = $2
      AND r.repository = $3
      AND (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date = $1
"""

USAGE_SQL = """
    WITH unambiguous_sessions AS (
        SELECT ars.session_id AS gateway_session_id,
               MIN(ars.afk_run_id) AS afk_run_id
        FROM afk_run_sessions ars
        WHERE ars.session_id IS NOT NULL
        GROUP BY ars.session_id
        HAVING COUNT(DISTINCT ars.afk_run_id) = 1
    )
    SELECT
        COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
        COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
        COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
        COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
        COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd
    FROM usage_events ue
    JOIN unambiguous_sessions us ON us.gateway_session_id = ue.session_id
    JOIN afk_runs r ON r.afk_run_id = us.afk_run_id
    WHERE r.provider = $2
      AND r.repository = $3
      AND r.repository IS NOT NULL
      AND (ue.reported_at AT TIME ZONE 'UTC')::date = $1
"""

COMPUTE_QUERIES: tuple[str, ...] = (
    RUNS_SQL,
    CHANGE_REQUEST_SQL,
    EXECUTION_SQL,
    SESSION_SQL,
    USAGE_SQL,
)
"""Every per-category recomputation query, one round trip each."""

# The full-row replacement.  ``ON CONFLICT ... DO UPDATE SET col = EXCLUDED.col``
# overwrites every metric plus the freshness metadata, so a retried or
# corrected recomputation replaces the bucket rather than incrementing it.
# ``RETURNING *`` hands the persisted row back for verification.
UPSERT_SQL = """
    INSERT INTO afk_dashboard_daily
        (day, provider, repository,
         runs_started, change_requests_opened, change_requests_merged,
         change_requests_closed, execution_count,
         successful_execution_count, failed_execution_count,
         cancelled_execution_count, session_count,
         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         estimated_cost_usd, derived_at, rollup_version, updated_at)
    VALUES ($1, $2, $3,
            $4, $5, $6, $7, $8, $9, $10, $11, $12,
            $13, $14, $15, $16, $17, now(), $18, now())
    ON CONFLICT (day, provider, repository) DO UPDATE SET
        runs_started = EXCLUDED.runs_started,
        change_requests_opened = EXCLUDED.change_requests_opened,
        change_requests_merged = EXCLUDED.change_requests_merged,
        change_requests_closed = EXCLUDED.change_requests_closed,
        execution_count = EXCLUDED.execution_count,
        successful_execution_count = EXCLUDED.successful_execution_count,
        failed_execution_count = EXCLUDED.failed_execution_count,
        cancelled_execution_count = EXCLUDED.cancelled_execution_count,
        session_count = EXCLUDED.session_count,
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cache_read_tokens = EXCLUDED.cache_read_tokens,
        cache_write_tokens = EXCLUDED.cache_write_tokens,
        estimated_cost_usd = EXCLUDED.estimated_cost_usd,
        derived_at = EXCLUDED.derived_at,
        rollup_version = EXCLUDED.rollup_version,
        updated_at = EXCLUDED.updated_at
    RETURNING *
"""


# ---------------------------------------------------------------------------
# Per-bucket advisory lock
# ---------------------------------------------------------------------------


def bucket_lock_key(
    day: date,
    provider: str,
    repository: str,
) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for one bucket.

    The low 32 bits are the first four bytes of the MD5 of
    ``"<day>|<provider>|<repository>"``, interpreted as a SIGNED int32 so the
    key always binds to the ``int4`` arguments of
    ``pg_advisory_xact_lock(int, int)`` — an unsigned interpretation can
    exceed ``INT32_MAX`` and asyncpg raises at bind time.  The key is
    deterministic: the same bucket always maps to the same lock.
    """
    text = f"{day.isoformat()}|{provider}|{repository}"
    digest = hashlib.md5(text.encode("utf-8")).digest()[:4]
    key = int.from_bytes(digest, byteorder="big", signed=True)
    return (AFK_DASHBOARD_LOCK_CLASS, key)


async def acquire_bucket_lock(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    repository: str,
) -> None:
    """Acquire the transaction-scoped advisory lock for one bucket.

    Serialises concurrent recomputations of the same
    ``(day, provider, repository)``.  The lock is released on the caller's
    commit or rollback, so the caller MUST wrap the lock acquisition and the
    following recompute in an explicit transaction.
    """
    lock_class, lock_key = bucket_lock_key(day, provider, repository)
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1, $2)",
        lock_class,
        lock_key,
    )


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------


async def compute_bucket_metrics(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    repository: str,
) -> dict[str, Any]:
    """Compute the additive metrics for one bucket from canonical tables.

    Runs one query per metric category and merges the results.  Columns not
    produced by any query default to zero, so the returned mapping always
    carries every :data:`METRIC_COLUMNS` key.
    """
    metrics: dict[str, Any] = {column: 0 for column in METRIC_COLUMNS}
    for sql in COMPUTE_QUERIES:
        row = await conn.fetchrow(sql, day, provider, repository)
        if row is not None:
            metrics.update(dict(row))
    return metrics


async def upsert_bucket(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    repository: str,
    metrics: dict[str, Any],
    *,
    rollup_version: str = AFK_DASHBOARD_ROLLUP_VERSION,
) -> dict[str, Any]:
    """Replace the whole bucket row; return the persisted row.

    Missing/``None`` metric values are written as zero.  The upsert uses
    ``ON CONFLICT (day, provider, repository) DO UPDATE SET col = EXCLUDED.col``
    so the stored row is fully replaced — never incremented.
    """
    values = [metrics.get(column) or 0 for column in METRIC_COLUMNS]
    row = await conn.fetchrow(
        UPSERT_SQL,
        day,
        provider,
        repository,
        *values,
        rollup_version,
    )
    return {} if row is None else dict(row)


async def recompute_bucket(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    repository: str,
    *,
    rollup_version: str = AFK_DASHBOARD_ROLLUP_VERSION,
) -> dict[str, Any]:
    """Recompute and persist one ``(day, provider, repository)`` bucket.

    Returns the persisted row (the ``RETURNING *`` projection), which
    includes every computed metric plus ``derived_at``, ``rollup_version``
    and ``updated_at`` for verification.
    """
    metrics = await compute_bucket_metrics(conn, day, provider, repository)
    return await upsert_bucket(
        conn,
        day,
        provider,
        repository,
        metrics,
        rollup_version=rollup_version,
    )
