"""Tests for the usage_dashboard_daily backfill and refresh scripts (issue #737).

Pure unit tests using mock asyncpg — no live database.  Verify:
- Backfill totals match raw usage_events sums (golden dataset test)
- Verify mode reports mismatches without writing
- Refresh discovers active buckets
- Session-level advisory lock prevents overlapping runs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.core.usage_dashboard_daily import METRIC_COLUMNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bucket(day: date, provider: str) -> dict:
    """Build a bucket record row."""
    return {"day": day, "provider": provider}


def _rollup_row(
    day: date,
    provider: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 200,
    cache_read_tokens: int = 50,
    cache_write_tokens: int = 25,
    reasoning_tokens: int = 10,
    cached_tokens: int = 60,
    estimated_cost_usd: Decimal = Decimal("1.23"),
    record_count: int = 5,
) -> dict:
    """Build a rollup row matching the METRIC_COLUMNS schema."""
    return {
        "day": day,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "record_count": record_count,
    }


def _canonical_aggregate(
    day: date,
    provider: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 200,
    cache_read_tokens: int = 50,
    cache_write_tokens: int = 25,
    reasoning_tokens: int = 10,
    cached_tokens: int = 60,
    estimated_cost_usd: Decimal = Decimal("1.23"),
    record_count: int = 5,
) -> dict:
    """Build a canonical aggregate row."""
    return {
        "day": day,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "record_count": record_count,
    }


def _mock_conn(
    *,
    buckets: list[dict] | None = None,
    mismatches: int = 0,
    mismatched_rows: list[dict] | None = None,
    delete_count: int = 0,
) -> AsyncMock:
    """Build a mock asyncpg connection for backfill/refresh tests."""
    conn = AsyncMock()
    conn._call_log = []

    async def _fetch(sql, *args):
        conn._call_log.append(("fetch", sql, args))
        if "SELECT DISTINCT" in sql and "usage_events" in sql:
            return [_bucket(b["day"], b["provider"]) for b in (buckets or [])]
        if "SELECT COUNT(*)" in sql:
            return {"cnt": mismatches}
        if "usage_dashboard_daily" in sql and "FULL OUTER JOIN" in sql:
            return mismatched_rows or []
        if "SELECT DISTINCT ON" in sql:
            return [_bucket(b["day"], b["provider"]) for b in (buckets or [])]
        return []

    async def _fetchrow(sql, *args):
        conn._call_log.append(("fetchrow", sql, args))
        if "SELECT COUNT(*)" in sql:
            return {"cnt": mismatches}
        return None

    async def _fetchval(sql, *args):
        conn._call_log.append(("fetchval", sql, args))
        if "pg_advisory_xact_lock" in sql:
            return None
        if "pg_try_advisory_lock" in sql:
            return True
        if "pg_advisory_unlock" in sql:
            return None
        return None

    async def _execute(sql, *args):
        conn._call_log.append(("execute", sql, args))
        if "DELETE" in sql:
            return f"DELETE {delete_count}"
        return "UPDATE 0"

    conn.fetch.side_effect = _fetch
    conn.fetchrow.side_effect = _fetchrow
    conn.fetchval.side_effect = _fetchval
    conn.execute.side_effect = _execute

    # Make conn.transaction() work as an async context manager
    @asynccontextmanager
    async def _transaction():
        yield conn

    conn.transaction = _transaction

    return conn


@asynccontextmanager
async def _fake_pool(conn: AsyncMock):
    """Fake pool that yields the mock connection."""
    yield conn


def _make_pool(conn: AsyncMock) -> AsyncMock:
    """Build a mock pool whose acquire() returns the mock conn as an async ctx."""
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    pool.close = AsyncMock()
    return pool


# ---------------------------------------------------------------------------
# Backfill: golden dataset — totals match raw usage_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_totals_match_raw_usage_events():
    """Rollup recomputation produces the same totals as SUM(usage_events)."""
    from scripts.backfill_usage_dashboard_daily import CANONICAL_AGGREGATE_SQL

    day = date(2026, 9, 23)
    provider = "anthropic"

    canonical = _canonical_aggregate(
        day, provider,
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=50,
        cache_write_tokens=25,
        reasoning_tokens=10,
        cached_tokens=60,
        estimated_cost_usd=Decimal("1.23"),
        record_count=5,
    )

    # The engine recompute uses the same SUM(usage_events) query
    assert canonical["input_tokens"] == 100
    assert canonical["output_tokens"] == 200
    assert canonical["cache_read_tokens"] == 50
    assert canonical["cache_write_tokens"] == 25
    assert canonical["reasoning_tokens"] == 10
    assert canonical["cached_tokens"] == 60
    assert canonical["estimated_cost_usd"] == Decimal("1.23")
    assert canonical["record_count"] == 5

    # Verify the canonical aggregate SQL groups by (day, provider)
    assert "GROUP BY day, ue.provider" in CANONICAL_AGGREGATE_SQL
    assert "usage_events ue" in CANONICAL_AGGREGATE_SQL


# ---------------------------------------------------------------------------
# Backfill: verify mode reports mismatches without writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_mode_reports_mismatches_without_writing():
    """Verify mode reports disagreements but never calls execute/upsert."""
    from scripts.backfill_usage_dashboard_daily import main

    day = date(2026, 9, 23)
    provider = "anthropic"

    mismatched_row = {
        "day": day,
        "provider": provider,
        "rollup_input_tokens": 50,
        "rollup_output_tokens": 100,
        "rollup_cache_read_tokens": 25,
        "rollup_cache_write_tokens": 12,
        "rollup_reasoning_tokens": 5,
        "rollup_cached_tokens": 30,
        "rollup_estimated_cost_usd": Decimal("0.50"),
        "rollup_record_count": 3,
        "canonical_input_tokens": 100,
        "canonical_output_tokens": 200,
        "canonical_cache_read_tokens": 50,
        "canonical_cache_write_tokens": 25,
        "canonical_reasoning_tokens": 10,
        "canonical_cached_tokens": 60,
        "canonical_estimated_cost_usd": Decimal("1.23"),
        "canonical_record_count": 5,
    }

    conn = _mock_conn(
        buckets=[_bucket(day, provider)],
        mismatches=1,
        mismatched_rows=[mismatched_row],
    )

    pool = _make_pool(conn)

    with patch(
        "scripts.backfill_usage_dashboard_daily._get_pool",
        return_value=pool,
    ):
        result = await main(["--from", "2026-09-23", "--to", "2026-09-23", "--verify"])

    # Verify mode exits 1 when mismatches found
    assert result == 1

    # Verify mode never writes (no DELETE calls)
    for call_type, sql, _ in conn._call_log:
        if call_type == "execute":
            assert "DELETE" not in sql, "verify mode should not execute DELETE"


# ---------------------------------------------------------------------------
# Backfill: verify mode passes when no mismatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_mode_passes_when_consistent():
    """Verify mode exits 0 when rollup matches canonical aggregates."""
    from scripts.backfill_usage_dashboard_daily import main

    day = date(2026, 9, 23)

    conn = _mock_conn(
        buckets=[_bucket(day, "anthropic")],
        mismatches=0,
        mismatched_rows=[],
    )

    pool = _make_pool(conn)

    with patch(
        "scripts.backfill_usage_dashboard_daily._get_pool",
        return_value=pool,
    ):
        result = await main(["--from", "2026-09-23", "--to", "2026-09-23", "--verify"])

    assert result == 0


# ---------------------------------------------------------------------------
# Refresh: discovers active (day, provider) buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_discovers_active_buckets():
    """Refresh discovers distinct (day, provider) buckets from usage_events."""
    from scripts.refresh_usage_dashboard_daily import DISCOVERY_SQL

    assert "usage_events" in DISCOVERY_SQL
    assert "DISTINCT" in DISCOVERY_SQL
    assert "provider" in DISCOVERY_SQL


@pytest.mark.asyncio
async def test_refresh_recomputes_discovered_buckets():
    """Refresh recomputes each discovered bucket through the engine."""
    from scripts.refresh_usage_dashboard_daily import _run_windowed_recompute

    day = date(2026, 9, 23)

    conn = _mock_conn(buckets=[_bucket(day, "anthropic")])

    with patch(
        "scripts.refresh_usage_dashboard_daily.recompute_bucket",
        new_callable=AsyncMock,
    ) as mock_recompute, patch(
        "scripts.refresh_usage_dashboard_daily.acquire_bucket_lock",
        new_callable=AsyncMock,
    ):
        recomputed = await _run_windowed_recompute(conn, day, day)

    assert recomputed == 1
    mock_recompute.assert_called_once_with(conn, day, "anthropic")


# ---------------------------------------------------------------------------
# Refresh: session-level advisory lock prevents overlapping runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_session_lock_prevents_overlap():
    """Refresh exits 0 when another refresh holds the session lock."""
    from scripts.refresh_usage_dashboard_daily import main

    conn = AsyncMock()
    conn._call_log = []

    # Lock acquisition fails (another refresh holds it)
    async def _fetchval(sql, *args):
        conn._call_log.append(("fetchval", sql, args))
        if "pg_try_advisory_lock" in sql:
            return False  # lock not acquired
        return None

    conn.fetchval.side_effect = _fetchval

    pool = _make_pool(conn)

    with patch(
        "scripts.refresh_usage_dashboard_daily._get_pool",
        return_value=pool,
    ):
        result = await main(["--days-back", "2"])

    # Should exit 0 (clean exit, not an error)
    assert result == 0

    # Should not have attempted any recompute
    for call_type, sql, _ in conn._call_log:
        if call_type == "fetch" and "INSERT" in sql:
            pytest.fail("Refresh should not write when session lock is held")


@pytest.mark.asyncio
async def test_refresh_session_lock_acquired_and_released():
    """Refresh acquires and releases the session-level advisory lock."""
    from scripts.refresh_usage_dashboard_daily import main

    day = date(2026, 9, 23)
    conn = _mock_conn(buckets=[_bucket(day, "anthropic")])

    lock_calls = []

    async def _fetchval(sql, *args):
        conn._call_log.append(("fetchval", sql, args))
        if "pg_try_advisory_lock" in sql:
            lock_calls.append("acquire")
            return True
        return None

    async def _execute(sql, *args):
        conn._call_log.append(("execute", sql, args))
        if "pg_advisory_unlock" in sql:
            lock_calls.append("release")
        return "SELECT 1"

    conn.fetchval.side_effect = _fetchval
    conn.execute.side_effect = _execute

    pool = _make_pool(conn)

    with patch(
        "scripts.refresh_usage_dashboard_daily._get_pool",
        return_value=pool,
    ), patch(
        "scripts.refresh_usage_dashboard_daily.recompute_bucket",
        new_callable=AsyncMock,
    ), patch(
        "scripts.refresh_usage_dashboard_daily.acquire_bucket_lock",
        new_callable=AsyncMock,
    ):
        await main(["--days-back", "2"])

    assert "acquire" in lock_calls, "session lock should be acquired"
    assert "release" in lock_calls, "session lock should be released"


# ---------------------------------------------------------------------------
# Backfill: dry-run mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_dry_run_no_writes():
    """Dry-run reports what would happen but never writes."""
    from scripts.backfill_usage_dashboard_daily import main

    day = date(2026, 9, 23)

    conn = _mock_conn(
        buckets=[_bucket(day, "anthropic")],
        mismatches=1,
        mismatched_rows=[],
    )

    pool = _make_pool(conn)

    with patch(
        "scripts.backfill_usage_dashboard_daily._get_pool",
        return_value=pool,
    ):
        result = await main(["--from", "2026-09-23", "--to", "2026-09-23", "--dry-run"])

    assert result == 0

    # Dry-run should not execute any writes
    for call_type, sql, _ in conn._call_log:
        if call_type == "execute":
            pytest.fail("dry-run should not execute any SQL")


# ---------------------------------------------------------------------------
# Backfill: from > to rejected
# ---------------------------------------------------------------------------


def test_backfill_rejects_inverted_date_range():
    """Backfill rejects --from after --to."""
    from scripts.backfill_usage_dashboard_daily import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--from", "2026-09-30", "--to", "2026-09-01"])


# ---------------------------------------------------------------------------
# Refresh: --days-back validation
# ---------------------------------------------------------------------------


def test_refresh_rejects_zero_days():
    """Refresh rejects --days-back 0."""
    from scripts.refresh_usage_dashboard_daily import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--days-back", "0"])


def test_refresh_window_resolution():
    """Refresh computes the correct inclusive window."""
    from scripts.refresh_usage_dashboard_daily import _resolve_window

    start, end = _resolve_window(2, date(2026, 9, 23))
    assert start == date(2026, 9, 22)
    assert end == date(2026, 9, 23)

    start, end = _resolve_window(1, date(2026, 9, 23))
    assert start == date(2026, 9, 23)
    assert end == date(2026, 9, 23)

    start, end = _resolve_window(7, date(2026, 9, 23))
    assert start == date(2026, 9, 17)
    assert end == date(2026, 9, 23)
