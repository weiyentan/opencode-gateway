"""Tests for the AFK daily rollup refresh CLI (issue #717).

Covers the task contract acceptance criteria:

1. A CLI entry point (``scripts/refresh_afk_dashboard_daily.py``) invocable
   by a Kubernetes CronJob.
2. It recomputes ``afk_dashboard_daily`` buckets for a configurable window
   (default: today and yesterday) by delegating to the shared recomputation
   engine in ``app.core.afk_dashboard_daily`` (issue #715).
3. It acquires the ``AGGREGATE_LOCK_CLASS`` (47_006) session-level advisory
   lock before recomputing and exits cleanly (0) on lock contention.
4. It discovers active buckets, takes the engine's per-bucket advisory lock,
   and recomputes each bucket in its own transaction.
5. It connects via application settings (the same ``_get_pool`` pattern) and
   logs progress and results.

Tests use SQL-content assertions plus in-memory recording connections so
they exercise the real entry point without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

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
#  AC 2 + 4: Bucket discovery SQL + engine delegation
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverySql:
    """Discovery spans every canonical source table and bounds the scan to
    the configured day window."""

    def test_uses_aggregate_lock_class(self):
        assert refresh.AGGREGATE_LOCK_CLASS == 47_006

    def test_uses_fixed_daily_refresh_lock_key(self):
        assert refresh.DAILY_REFRESH_LOCK_KEY == 0

    def test_engine_lock_class_is_exposed(self):
        assert refresh.AFK_DASHBOARD_LOCK_CLASS == 47_007

    def test_discovers_distinct_buckets(self):
        sql = refresh.DISCOVERY_SQL
        assert "SELECT DISTINCT ON (day, provider, repository)" in sql
        assert "ORDER BY day, provider, repository" in sql

    def test_windows_every_canonical_source(self):
        sql = refresh.DISCOVERY_SQL
        for table in (
            "afk_runs",
            "engineering_events",
            "execution_bindings",
            "afk_run_sessions",
            "usage_events",
        ):
            assert table in sql
        # Each union branch constrains to the $1..$2 day window.
        assert sql.count("BETWEEN $1 AND $2") == 5

    def test_skips_rows_without_repository_identity(self):
        sql = refresh.DISCOVERY_SQL
        assert "r.repository IS NOT NULL" in sql
        assert "e.repository IS NOT NULL" in sql
        assert "b.repository_url IS NOT NULL" in sql
        assert "b.repository_url AS repository" in sql

    def test_change_requests_scoped_to_change_request_events(self):
        assert "e.entity_type = 'change_request'" in refresh.DISCOVERY_SQL


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Advisory lock — AGGREGATE_LOCK_CLASS, clean contention exit
# ══════════════════════════════════════════════════════════════════════════════


class TestRefreshLock:
    @pytest.mark.asyncio
    async def test_try_acquire_uses_aggregate_class_and_fixed_key(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        assert await refresh._try_acquire_refresh_lock(conn) is True
        sql, lock_class, key = conn.fetchval.call_args[0]
        assert "pg_try_advisory_lock" in sql
        assert lock_class == refresh.AGGREGATE_LOCK_CLASS
        assert key == refresh.DAILY_REFRESH_LOCK_KEY

    @pytest.mark.asyncio
    async def test_try_acquire_returns_false_when_contended(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)
        assert await refresh._try_acquire_refresh_lock(conn) is False


# ══════════════════════════════════════════════════════════════════════════════
#  Recording connection + pool doubles
# ══════════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """Records fetch/fetchval/execute calls; returns scripted results."""

    def __init__(self, *, lock_available: bool, buckets: list[dict] | None = None):
        self.lock_available = lock_available
        self.buckets = list(buckets or [])
        self.fetchvals: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self.transactions = 0

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        return self.lock_available

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return self.buckets

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "SELECT 1"

    def transaction(self):
        return _Transaction(self)


class _Transaction:
    def __init__(self, conn: _RecordingConn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.transactions += 1
        return self._conn

    async def __aexit__(self, *exc):
        return False


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


def _patch_engine(monkeypatch, calls: list[tuple]):
    """Replace the engine calls with recorders so orchestration is observable."""

    async def fake_acquire(conn, day, provider, repository):
        calls.append(("lock", day, provider, repository))

    async def fake_recompute(conn, day, provider, repository):
        calls.append(("recompute", day, provider, repository))
        return {}

    monkeypatch.setattr(refresh, "acquire_bucket_lock", fake_acquire)
    monkeypatch.setattr(refresh, "recompute_bucket", fake_recompute)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2 + 4: _run_windowed_recompute discovers and recomputes each bucket
# ══════════════════════════════════════════════════════════════════════════════


class TestRunWindowedRecompute:
    @pytest.mark.asyncio
    async def test_discovers_window_and_recomputes_each_bucket_in_own_txn(
        self, monkeypatch
    ):
        buckets = [
            {"day": date(2026, 3, 14), "provider": "gitlab", "repository": "a"},
            {"day": date(2026, 3, 15), "provider": "gitlab", "repository": "b"},
        ]
        conn = _RecordingConn(lock_available=True, buckets=buckets)
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        result = await refresh._run_windowed_recompute(
            conn, date(2026, 3, 14), date(2026, 3, 15)
        )

        assert result == 2
        # Discovery bound to the window.
        assert conn.fetches == [
            (refresh.DISCOVERY_SQL, (date(2026, 3, 14), date(2026, 3, 15)))
        ]
        # One transaction, lock, and recompute per discovered bucket.
        assert conn.transactions == 2
        assert calls == [
            ("lock", date(2026, 3, 14), "gitlab", "a"),
            ("recompute", date(2026, 3, 14), "gitlab", "a"),
            ("lock", date(2026, 3, 15), "gitlab", "b"),
            ("recompute", date(2026, 3, 15), "gitlab", "b"),
        ]

    @pytest.mark.asyncio
    async def test_empty_window_recomputes_nothing(self, monkeypatch):
        conn = _RecordingConn(lock_available=True, buckets=[])
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        result = await refresh._run_windowed_recompute(
            conn, date(2026, 3, 14), date(2026, 3, 15)
        )

        assert result == 0
        assert conn.transactions == 0
        assert calls == []


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3 + 5: main() — lock, recompute, release, exit codes
# ══════════════════════════════════════════════════════════════════════════════


class TestMain:
    @pytest.mark.asyncio
    async def test_lock_contention_exits_zero_without_recomputing(
        self, monkeypatch, caplog
    ):
        import logging

        conn = _RecordingConn(lock_available=False)
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        with caplog.at_level(logging.WARNING, logger="refresh_afk_dashboard_daily"):
            rc = await refresh.main([])

        assert rc == 0
        # No discovery, no recompute, lock never released.
        assert conn.fetches == []
        assert calls == []
        assert not any("pg_advisory_unlock" in sql for sql, _ in conn.executes)
        assert "lock" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_successful_run_recomputes_window_and_releases_lock(
        self, monkeypatch
    ):
        buckets = [
            {"day": date(2026, 3, 14), "provider": "gitlab", "repository": "a"},
            {"day": date(2026, 3, 15), "provider": "gitlab", "repository": "b"},
        ]
        conn = _RecordingConn(lock_available=True, buckets=buckets)
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        rc = await refresh.main(["--days-back", "2", "--as-of", "2026-03-15"])

        assert rc == 0
        # Session advisory lock attempted via AGGREGATE_LOCK_CLASS + fixed key.
        assert len(conn.fetchvals) == 1
        assert conn.fetchvals[0][1] == (
            refresh.AGGREGATE_LOCK_CLASS,
            refresh.DAILY_REFRESH_LOCK_KEY,
        )

        # Discovery bound to the resolved window; both buckets recomputed.
        assert conn.fetches[0][1] == (date(2026, 3, 14), date(2026, 3, 15))
        assert [c[0] for c in calls] == ["lock", "recompute", "lock", "recompute"]

        # Session lock released on the way out.
        assert any("pg_advisory_unlock" in sql for sql, _ in conn.executes)

    @pytest.mark.asyncio
    async def test_lock_released_even_when_recompute_fails(self, monkeypatch):
        conn = _RecordingConn(lock_available=True, buckets=[])

        async def fake_get_pool():
            return _fake_pool(conn)

        monkeypatch.setattr(refresh, "_get_pool", fake_get_pool)

        async def boom(conn_arg, day, provider, repository):
            raise RuntimeError("db exploded")

        # Fails on discovery (before any recompute) to exercise the finally.
        conn.fetch = AsyncMock(side_effect=RuntimeError("db exploded"))  # type: ignore[assignment]
        monkeypatch.setattr(refresh, "recompute_bucket", boom)

        with pytest.raises(RuntimeError):
            await refresh.main([])

        assert any("pg_advisory_unlock" in sql for sql, _ in conn.executes)
