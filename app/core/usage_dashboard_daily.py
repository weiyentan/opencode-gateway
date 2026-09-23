"""Usage dashboard daily rollup recomputation engine (issue #735).

The single writer of the ``usage_dashboard_daily`` read-model (migration 0048).
For one ``(day, provider)`` bucket it recomputes every additive metric from
the canonical ``usage_events`` table and replaces the whole row with an
``INSERT ... ON CONFLICT DO UPDATE SET col = EXCLUDED.col`` upsert.

Follows the ``afk_dashboard_daily`` precedent (migration 0046): raw asyncpg
SQL, a per-bucket advisory lock, and an idempotent, full-row upsert.  The
rollup is a derived convenience; the canonical ``usage_events`` table remains
the source of truth, and the upsert *replaces* rather than increments, so a
retried recomputation can never double-count.

Metric sources and event-time bucketing (all UTC calendar days)
---------------------------------------------------------------

====================================  ==========================  ====================================
Metric                                Canonical table             Event-time column
====================================  ==========================  ====================================
``input_tokens``                      ``usage_events``            ``reported_at``
``output_tokens``                     ``usage_events``            ``reported_at``
``cache_read_tokens``                 ``usage_events``            ``reported_at``
``cache_write_tokens``                ``usage_events``            ``reported_at``
``reasoning_tokens``                  ``usage_events``            ``reported_at``
``cached_tokens``                     ``usage_events``            ``reported_at``
``estimated_cost_usd``                ``usage_events``            ``reported_at``
``record_count``                      ``usage_events``            ``reported_at``
====================================  ==========================  ====================================

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

USAGE_DASHBOARD_ROLLUP_VERSION = "1"
"""Rule version stamped on every recomputed bucket (``rollup_version``).

Bump this whenever any metric's source table, filter, or event-time
bucketing changes, so consumers can detect version-skewed buckets.  Callers
may override it per call via the ``rollup_version`` argument, but the
default is this configurable constant — never a hardcoded SQL literal.
"""

USAGE_DASHBOARD_LOCK_CLASS = 47_008
"""High 32 bits of the two-arg per-bucket recomputation advisory lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002, ``REPLAY_LOCK_CLASS`` = 47_003,
``RECONCILE_LOCK_CLASS`` = 47_004, ``CANONICAL_EVENT_LOCK_CLASS`` = 47_005,
``AGGREGATE_LOCK_CLASS`` = 47_006) and the ``afk_dashboard_daily``
convention (``AFK_DASHBOARD_LOCK_CLASS`` = 47_007).  The low 32 bits are
derived from a signed-int32 hash of ``(day, provider)`` so concurrent
recomputations of the *same* bucket serialise while different buckets never
contend.
"""

METRIC_COLUMNS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "estimated_cost_usd",
    "record_count",
)
"""The additive metric columns, in the exact order the upsert binds them."""


# ---------------------------------------------------------------------------
# Per-bucket recomputation SQL
#
# Every query takes the same two parameters:
#   $1 day (date)  $2 provider (text)
# and buckets on the UTC calendar day of the event's own event time.
# ---------------------------------------------------------------------------

USAGE_SQL = """
    SELECT
        COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
        COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
        COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
        COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
        COALESCE(SUM(ue.reasoning_tokens), 0)::int AS reasoning_tokens,
        COALESCE(SUM(ue.cached_tokens), 0)::int AS cached_tokens,
        COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd,
        COUNT(*)::int AS record_count
    FROM usage_events ue
    WHERE ue.provider = $2
      AND (ue.reported_at AT TIME ZONE 'UTC')::date = $1
"""

COMPUTE_QUERIES: tuple[str, ...] = (
    USAGE_SQL,
)
"""Every per-category recomputation query, one round trip each."""

# The full-row replacement.  ``ON CONFLICT ... DO UPDATE SET col = EXCLUDED.col``
# overwrites every metric plus the freshness metadata, so a retried or
# corrected recomputation replaces the bucket rather than incrementing it.
# ``RETURNING *`` hands the persisted row back for verification.
UPSERT_SQL = """
    INSERT INTO usage_dashboard_daily
        (day, provider,
         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         reasoning_tokens, cached_tokens, estimated_cost_usd, record_count,
         derived_at, rollup_version, updated_at)
    VALUES ($1, $2,
            $3, $4, $5, $6, $7, $8, $9, $10,
            now(), $11, now())
    ON CONFLICT (day, provider) DO UPDATE SET
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cache_read_tokens = EXCLUDED.cache_read_tokens,
        cache_write_tokens = EXCLUDED.cache_write_tokens,
        reasoning_tokens = EXCLUDED.reasoning_tokens,
        cached_tokens = EXCLUDED.cached_tokens,
        estimated_cost_usd = EXCLUDED.estimated_cost_usd,
        record_count = EXCLUDED.record_count,
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
) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for one bucket.

    The low 32 bits are the first four bytes of the MD5 of
    ``"<day>|<provider>"``, interpreted as a SIGNED int32 so the key always
    binds to the ``int4`` arguments of ``pg_advisory_xact_lock(int, int)``
    — an unsigned interpretation can exceed ``INT32_MAX`` and asyncpg raises
    at bind time.  The key is deterministic: the same bucket always maps to
    the same lock.
    """
    text = f"{day.isoformat()}|{provider}"
    digest = hashlib.md5(text.encode("utf-8")).digest()[:4]
    key = int.from_bytes(digest, byteorder="big", signed=True)
    return (USAGE_DASHBOARD_LOCK_CLASS, key)


async def acquire_bucket_lock(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
) -> None:
    """Acquire the transaction-scoped advisory lock for one bucket.

    Serialises concurrent recomputations of the same
    ``(day, provider)``.  The lock is released on the caller's commit or
    rollback, so the caller MUST wrap the lock acquisition and the following
    recompute in an explicit transaction.
    """
    lock_class, lock_key = bucket_lock_key(day, provider)
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
) -> dict[str, Any]:
    """Compute the additive metrics for one bucket from canonical tables.

    Runs one query per metric category and merges the results.  Columns not
    produced by any query default to zero, so the returned mapping always
    carries every :data:`METRIC_COLUMNS` key.
    """
    metrics: dict[str, Any] = {column: 0 for column in METRIC_COLUMNS}
    for sql in COMPUTE_QUERIES:
        row = await conn.fetchrow(sql, day, provider)
        if row is not None:
            metrics.update(dict(row))
    return metrics


async def upsert_bucket(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    metrics: dict[str, Any],
    *,
    rollup_version: str = USAGE_DASHBOARD_ROLLUP_VERSION,
) -> dict[str, Any]:
    """Replace the whole bucket row; return the persisted row.

    Missing/``None`` metric values are written as zero.  The upsert uses
    ``ON CONFLICT (day, provider) DO UPDATE SET col = EXCLUDED.col`` so
    the stored row is fully replaced — never incremented.
    """
    values = [metrics.get(column) or 0 for column in METRIC_COLUMNS]
    row = await conn.fetchrow(
        UPSERT_SQL,
        day,
        provider,
        *values,
        rollup_version,
    )
    return {} if row is None else dict(row)


async def recompute_bucket(
    conn: asyncpg.Connection,
    day: date,
    provider: str,
    *,
    rollup_version: str = USAGE_DASHBOARD_ROLLUP_VERSION,
) -> dict[str, Any]:
    """Recompute and persist one ``(day, provider)`` bucket.

    Returns the persisted row (the ``RETURNING *`` projection), which
    includes every computed metric plus ``derived_at``, ``rollup_version``
    and ``updated_at`` for verification.
    """
    metrics = await compute_bucket_metrics(conn, day, provider)
    return await upsert_bucket(
        conn,
        day,
        provider,
        metrics,
        rollup_version=rollup_version,
    )
