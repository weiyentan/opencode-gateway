"""Tests for the usage dashboard daily recomputation engine (issue #735).

Each metric is exercised against a canonical fixture row: the fake
connection dispatches by SQL identity, so a category whose query/filters
are wrong cannot silently return the expected value.  SQL-shape assertions
pin the canonical source table, the event-time bucketing, and the full-
replacement upsert contract.

The tests are pure unit tests (mock asyncpg connection) — no live database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

from app.core.usage_dashboard_daily import (
    COMPUTE_QUERIES,
    METRIC_COLUMNS,
    USAGE_DASHBOARD_LOCK_CLASS,
    USAGE_DASHBOARD_ROLLUP_VERSION,
    USAGE_SQL,
    UPSERT_SQL,
    acquire_bucket_lock,
    bucket_lock_key,
    compute_bucket_metrics,
    recompute_bucket,
    upsert_bucket,
)

UTC = timezone.utc

_DAY = date(2026, 9, 23)
_PROVIDER = "anthropic"


# ---------------------------------------------------------------------------
# Canonical fixtures
# ---------------------------------------------------------------------------


def _fixture_row() -> dict[str, object]:
    """Return the canonical fixture row for the usage query."""
    return {
        USAGE_SQL: {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_tokens": 50,
            "cache_write_tokens": 25,
            "reasoning_tokens": 10,
            "cached_tokens": 60,
            "estimated_cost_usd": Decimal("1.23"),
            "record_count": 5,
        },
    }


def _expected_metrics() -> dict[str, object]:
    expected: dict[str, object] = {}
    for row in _fixture_row().values():
        expected.update(row)
    return expected


def _expected_values() -> list[object]:
    metrics = _expected_metrics()
    return [metrics[column] for column in METRIC_COLUMNS]


def _mock_conn(*, upsert_row: dict | None = None) -> AsyncMock:
    """Build a mock conn that dispatches per-category queries by SQL identity."""
    fixtures = _fixture_row()
    conn = AsyncMock()
    conn.lock_args = None
    conn.upsert_args = None
    conn.fetchrow_calls = []

    async def _fetchrow(sql, *args):
        conn.fetchrow_calls.append(sql)
        if sql == UPSERT_SQL:
            conn.upsert_args = args
            if upsert_row is None:
                return None
            return dict(upsert_row)
        return dict(fixtures[sql])

    async def _fetchval(sql, *args):
        conn.lock_args = (sql, args)
        return None

    conn.fetchrow.side_effect = _fetchrow
    conn.fetchval.side_effect = _fetchval
    return conn


# ---------------------------------------------------------------------------
# Recomputation behaviour
# ---------------------------------------------------------------------------


async def test_recompute_returns_persisted_row():
    """The persisted row (RETURNING *) is returned for verification."""
    persisted = {
        "day": _DAY,
        "provider": _PROVIDER,
        **_expected_metrics(),
        "derived_at": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        "rollup_version": USAGE_DASHBOARD_ROLLUP_VERSION,
        "updated_at": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    }
    conn = _mock_conn(upsert_row=persisted)

    result = await recompute_bucket(conn, _DAY, _PROVIDER)

    assert result == persisted


async def test_recompute_merges_all_metrics():
    """Every metric flows into the returned metric row."""
    conn = _mock_conn()

    metrics = await compute_bucket_metrics(conn, _DAY, _PROVIDER)

    assert metrics == _expected_metrics()


async def test_compute_queries_once():
    """One query for the usage metrics, no more."""
    conn = _mock_conn()

    await compute_bucket_metrics(conn, _DAY, _PROVIDER)

    assert conn.fetchrow_calls == list(COMPUTE_QUERIES)


async def test_upsert_binds_computed_metrics_in_column_order():
    """The upsert receives the computed metrics in METRIC_COLUMNS order."""
    conn = _mock_conn()

    await recompute_bucket(conn, _DAY, _PROVIDER)

    expected = (_DAY, _PROVIDER, *_expected_values(), USAGE_DASHBOARD_ROLLUP_VERSION)
    assert conn.upsert_args == expected


async def test_missing_metric_values_default_to_zero():
    """A NULL category result is written as zero, never omitted."""
    conn = AsyncMock()

    async def _fetchrow(sql, *args):
        if sql == UPSERT_SQL:
            return None
        return None

    conn.fetchrow.side_effect = _fetchrow

    await upsert_bucket(conn, _DAY, _PROVIDER, {})

    # Params: SQL, day, provider, 8 metrics (all zero), rollup_version
    assert list(conn.fetchrow.call_args.args[3:11]) == [0] * len(METRIC_COLUMNS)


async def test_rollup_version_is_configurable():
    """Callers may stamp a different rule version per recompute."""
    conn = _mock_conn()

    await recompute_bucket(conn, _DAY, _PROVIDER, rollup_version="7")

    assert conn.upsert_args[-1] == "7"


# ---------------------------------------------------------------------------
# Metric sourcing + event-time bucketing
# ---------------------------------------------------------------------------


def test_usage_sourced_from_usage_events():
    for token in [
        "usage_events",
        "ue.input_tokens",
        "ue.output_tokens",
        "ue.cache_read_tokens",
        "ue.cache_write_tokens",
        "ue.reasoning_tokens",
        "ue.cached_tokens",
        "ue.estimated_cost_usd",
        "ue.reported_at",
    ]:
        assert token in USAGE_SQL, f"expected {token!r} in query"


def test_event_time_bucketed_in_utc():
    for sql in COMPUTE_QUERIES:
        assert "AT TIME ZONE 'UTC'" in sql
        assert "::date = $1" in sql


def test_upsert_replaces_every_column():
    """Full replacement: DO UPDATE SET col = EXCLUDED.col for every metric."""
    assert "ON CONFLICT (day, provider) DO UPDATE SET" in UPSERT_SQL
    for column in METRIC_COLUMNS:
        assert f"{column} = EXCLUDED.{column}" in UPSERT_SQL
    # No additive increments — that would double-count on retry.
    assert "+ EXCLUDED" not in UPSERT_SQL


def test_upsert_sets_freshness_metadata():
    assert "now()" in UPSERT_SQL
    assert "derived_at = EXCLUDED.derived_at" in UPSERT_SQL
    assert "rollup_version = EXCLUDED.rollup_version" in UPSERT_SQL
    assert "updated_at = EXCLUDED.updated_at" in UPSERT_SQL
    assert "RETURNING *" in UPSERT_SQL


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


def test_bucket_lock_key_is_deterministic():
    assert bucket_lock_key(_DAY, _PROVIDER) == bucket_lock_key(_DAY, _PROVIDER)


def test_bucket_lock_key_distinguishes_buckets():
    base = bucket_lock_key(_DAY, _PROVIDER)
    assert bucket_lock_key(date(2026, 9, 24), _PROVIDER) != base
    assert bucket_lock_key(_DAY, "github") != base


def test_bucket_lock_key_uses_lock_namespace():
    lock_class, _ = bucket_lock_key(_DAY, _PROVIDER)
    assert lock_class == USAGE_DASHBOARD_LOCK_CLASS


async def test_acquire_bucket_lock_uses_transactional_advisory_lock():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    await acquire_bucket_lock(conn, _DAY, _PROVIDER)

    sql, args = conn.fetchval.call_args.args[0], conn.fetchval.call_args.args[1:]
    assert "pg_advisory_xact_lock" in sql
    assert args == bucket_lock_key(_DAY, _PROVIDER)
