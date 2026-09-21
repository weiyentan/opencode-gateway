"""Tests for the AFK daily rollup refresh CLI (issue #717).

Covers the task contract acceptance criteria:

1. A CLI entry point (``scripts/refresh_afk_dashboard_daily.py``) invocable
   by a Kubernetes CronJob.
2. It recomputes client_project_rollup buckets for a configurable window
   (default: today and yesterday) reusing the recomputation logic in
   ``scripts/backfill_client_project_rollup.py``.
3. It acquires the ``AGGREGATE_LOCK_CLASS`` (47_006) advisory lock before
   recomputing and exits cleanly (0) on lock contention.
4. It connects via application settings (the same ``_get_pool`` pattern).
5. It logs progress and results.

Tests use SQL-content assertions plus an in-memory recording connection so
they exercise the real entry point without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.reconciliation import ROLLUP_FIELDS
from scripts.backfill_client_project_rollup import EVENT_AGGREGATE_SQL

import scripts.refresh_afk_dashboard_daily as refresh

# ══════════════════════════════════════════════════════════════════════════════
#  AC 2: Window resolution — default today + yesterday, configurable
# ══════════════════════════════════════════════════════════════════════════════


class TestWindow:
    """The recompute window defaults to today and yesterday (UTC) and is
    configurable via the day count."""

    def test_default_window_is_today_and_yesterday(self):
        start, end = refresh._resolve_window(2, date(2026, 3, 15))
        assert end == date(2026, 3, 15)
        assert start == date(2026, 3, 14)

    def test_multi_day_window_spans_requested_days(self):
        start, end = refresh._resolve_window(3, date(2026, 3, 15))
        assert start == date(2026, 3, 13)
        assert end == date(2026, 3, 15)

    def test_single_day_window(self):
        start, end = refresh._resolve_window(1, date(2026, 3, 15))
        assert start == end == date(2026, 3, 15)

    def test_invalid_days_raises(self):
        with pytest.raises(ValueError):
            refresh._resolve_window(0, date(2026, 3, 15))

    def test_default_as_of_is_today_utc(self):
        start, end = refresh._resolve_window(2)
        today = datetime.now(timezone.utc).date()
        assert end == today
        assert start == today - timedelta(days=1)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: CLI argument parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestArgParsing:
    """``--days-back`` and ``--as-of`` drive the window; invalid input fails
    fast."""

    def test_days_defaults_to_two(self):
        assert refresh._parse_args([]).days == 2

    def test_as_of_defaults_to_none(self):
        assert refresh._parse_args([]).as_of is None

    def test_days_override(self):
        assert refresh._parse_args(["--days-back", "5"]).days == 5

    def test_as_of_parses_iso_date(self):
        args = refresh._parse_args(["--as-of", "2026-03-15"])
        assert args.as_of == date(2026, 3, 15)

    def test_invalid_days_rejected(self):
        with pytest.raises(SystemExit):
            refresh._parse_args(["--days-back", "0"])

    def test_invalid_as_of_rejected(self):
        with pytest.raises(SystemExit):
            refresh._parse_args(["--as-of", "not-a-date"])


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2 + 3: Recompute SQL — windowed, reuses the engine, corrects rollup
# ══════════════════════════════════════════════════════════════════════════════


class TestRecomputeSql:
    """The daily recompute is the engine's additive grouped SUM, restricted
    to the configured day window, upserting only disagreeing groups."""

    def test_uses_aggregate_lock_class(self):
        assert refresh.AGGREGATE_LOCK_CLASS == 47_006

    def test_sql_reuses_engine_additive_sum(self):
        """The windowed CTE keeps the engine's additive SUM lines verbatim."""
        engine_sum_lines = [
            line.strip()
            for line in EVENT_AGGREGATE_SQL.splitlines()
            if "SUM(ue." in line
        ]
        assert len(engine_sum_lines) == len(ROLLUP_FIELDS)
        for line in engine_sum_lines:
            assert line in refresh.DAILY_BACKFILL_SQL

    def test_sql_windows_by_day_inside_cte(self):
        """The day-window predicate is pushed INTO the grouped CTE so the
        aggregate scans only the configured window, not all history."""
        sql = refresh.DAILY_BACKFILL_SQL
        cte = sql.split(")\nINSERT INTO")[0]
        assert "WITH grouped AS (" in cte
        assert (
            "(ue.reported_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2"
            in cte
        )
        # The outer SELECT no longer carries the day filter.
        assert "g.day >= $1" not in sql
        assert "g.day <= $2" not in sql

    def test_sql_upserts_and_corrects_toward_events(self):
        sql = refresh.DAILY_BACKFILL_SQL
        assert "INSERT INTO client_project_rollup" in sql
        assert "ON CONFLICT (client_id, project_id, day)" in sql
        for field in ROLLUP_FIELDS:
            assert f"{field} = EXCLUDED.{field}" in sql

    def test_sql_only_touches_disagreeing_groups(self):
        sql = refresh.DAILY_BACKFILL_SQL
        assert "LEFT JOIN client_project_rollup r" in sql
        assert "r.client_id IS NULL" in sql
        for field in ROLLUP_FIELDS:
            assert f"r.{field} != g.{field}" in sql

    @pytest.mark.asyncio
    async def test_run_windowed_recompute_binds_window_and_parses_count(self):
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 0 4")
        result = await refresh._run_windowed_recompute(
            conn, date(2026, 3, 14), date(2026, 3, 15)
        )
        assert result == 4
        sql, start, end = conn.execute.call_args[0]
        assert "BETWEEN $1 AND $2" in sql
        assert start == date(2026, 3, 14)
        assert end == date(2026, 3, 15)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Advisory lock — AGGREGATE_LOCK_CLASS, clean contention exit
# ══════════════════════════════════════════════════════════════════════════════


class TestRefreshLock:
    @pytest.mark.asyncio
    async def test_try_acquire_uses_aggregate_class_and_fixed_key(self):
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        assert await refresh._try_acquire_refresh_lock(conn) is True
        sql, lock_class, key = conn.fetchval.call_args[0]
        assert "pg_try_advisory_lock" in sql
        assert lock_class == refresh.AGGREGATE_LOCK_CLASS
        assert key == refresh.DAILY_REFRESH_LOCK_KEY

    @pytest.mark.asyncio
    async def test_try_acquire_returns_false_when_contended(self):
        from unittest.mock import AsyncMock

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)
        assert await refresh._try_acquire_refresh_lock(conn) is False


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3 + 4 + 5: main() — lock, recompute, release, exit codes
# ══════════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """Records fetchval/execute calls; returns scripted results."""

    def __init__(self, *, lock_available: bool, execute_result: str = "INSERT 0 3"):
        self.lock_available = lock_available
        self.execute_result = execute_result
        self.fetchvals: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        return self.lock_available

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        if "pg_advisory_unlock" in sql:
            return "SELECT 1"
        return self.execute_result


def _fake_pool(conn: _RecordingConn):
    class _Ctx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Ctx()

        async def close(self):
            pass

    return _Pool()


class TestMain:
    @pytest.mark.asyncio
    async def test_lock_contention_exits_zero_without_recomputing(
        self, monkeypatch, caplog
    ):
        import logging

        conn = _RecordingConn(lock_available=False)

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        with caplog.at_level(logging.WARNING, logger="refresh_afk_dashboard_daily"):
            rc = await refresh.main([])

        assert rc == 0
        assert not any(
            "INSERT INTO client_project_rollup" in sql for sql, _ in conn.executes
        )
        assert not any("pg_advisory_unlock" in sql for sql, _ in conn.executes)
        assert "lock" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_successful_run_recomputes_window_and_releases_lock(
        self, monkeypatch
    ):
        conn = _RecordingConn(lock_available=True, execute_result="INSERT 0 7")

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        rc = await refresh.main(["--days-back", "2", "--as-of", "2026-03-15"])

        assert rc == 0
        # Advisory lock attempted via AGGREGATE_LOCK_CLASS with the fixed key.
        assert len(conn.fetchvals) == 1
        assert conn.fetchvals[0][1] == (refresh.AGGREGATE_LOCK_CLASS, refresh.DAILY_REFRESH_LOCK_KEY)

        # Exactly one recompute, bound to the resolved window.
        recompute = [
            (sql, params)
            for sql, params in conn.executes
            if "INSERT INTO client_project_rollup" in sql
        ]
        assert len(recompute) == 1
        assert recompute[0][1] == (date(2026, 3, 14), date(2026, 3, 15))

        # Lock released on the way out.
        assert any("pg_advisory_unlock" in sql for sql, _ in conn.executes)

    @pytest.mark.asyncio
    async def test_lock_released_even_when_recompute_fails(self, monkeypatch):
        conn = _RecordingConn(lock_available=True)

        async def boom(sql, *args):
            conn.executes.append((sql, args))
            if "pg_advisory_unlock" in sql:
                return "SELECT 1"
            raise RuntimeError("db exploded")

        conn.execute = boom  # type: ignore[assignment]

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        with pytest.raises(RuntimeError):
            await refresh.main([])

        assert any("pg_advisory_unlock" in sql for sql, _ in conn.executes)
