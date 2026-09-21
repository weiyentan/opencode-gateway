# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the AFK dashboard daily backfill CLI (issue #716).

Covers the task contract acceptance criteria:

1. An operator CLI ``scripts/backfill_afk_dashboard_daily.py`` invocable with
   ``--from`` / ``--to`` ISO date flags (UTC calendar days, inclusive).
2. ``--dry-run`` reports the buckets that would be recomputed / stale rows
   that would be deleted without writing.
3. ``--verify`` compares the stored ``afk_dashboard_daily`` rollups against
   canonical SUM/COUNT recomputation and reports mismatches.
4. ``--provider`` / ``--repository`` narrow the backfill scope.
5. Recompute delegates to the shared recomputation engine in
   ``app.core.afk_dashboard_daily`` (issue #715) — the engine's full-row
   upsert makes the backfill idempotent.
6. Stale rollup rows (no backing canonical bucket) are deleted.

Tests use SQL-content assertions plus recording connections (the pattern from
``tests/test_refresh_afk_dashboard_daily.py``), so they exercise the real
entry point without a database.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

import scripts.backfill_afk_dashboard_daily as backfill

_DAY = date(2026, 1, 15)
_DAY2 = date(2026, 1, 16)
_PROVIDER = "gitlab"
_REPOSITORY = "cloudnative-pg"


# ══════════════════════════════════════════════════════════════════════════════
#  CLI argument parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestArgParsing:
    """``--from`` / ``--to`` are required ISO dates; the mode and scope flags
    default to safe values."""

    def test_from_and_to_parse_iso_dates(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31"]
        )
        assert args.from_date == date(2026, 1, 15)
        assert args.to_date == date(2026, 1, 31)

    def test_from_is_required(self):
        with pytest.raises(SystemExit):
            backfill._parse_args(["--to", "2026-01-31"])

    def test_to_is_required(self):
        with pytest.raises(SystemExit):
            backfill._parse_args(["--from", "2026-01-15"])

    def test_invalid_date_rejected(self):
        with pytest.raises(SystemExit):
            backfill._parse_args(
                ["--from", "not-a-date", "--to", "2026-01-31"]
            )

    def test_from_after_to_rejected(self):
        with pytest.raises(SystemExit):
            backfill._parse_args(
                ["--from", "2026-02-01", "--to", "2026-01-31"]
            )

    def test_dry_run_default_false(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31"]
        )
        assert args.dry_run is False

    def test_dry_run_flag_true(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31", "--dry-run"]
        )
        assert args.dry_run is True

    def test_verify_default_false(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31"]
        )
        assert args.verify is False

    def test_verify_flag_true(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31", "--verify"]
        )
        assert args.verify is True

    def test_provider_and_repository_default_none(self):
        args = backfill._parse_args(
            ["--from", "2026-01-15", "--to", "2026-01-31"]
        )
        assert args.provider is None
        assert args.repository is None

    def test_provider_and_repository_flags(self):
        args = backfill._parse_args(
            [
                "--from", "2026-01-15",
                "--to", "2026-01-31",
                "--provider", "gitlab",
                "--repository", "cloudnative-pg",
            ]
        )
        assert args.provider == "gitlab"
        assert args.repository == "cloudnative-pg"

    def test_dry_run_and_verify_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            backfill._parse_args(
                [
                    "--from", "2026-01-15",
                    "--to", "2026-01-31",
                    "--dry-run",
                    "--verify",
                ]
            )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: Bucket discovery SQL (window + provider/repository scope)
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverySql:
    """Discovery spans every canonical source table, bounds the scan to the
    ``--from``/``--to`` window, and honours the scope filters."""

    def test_discovers_distinct_buckets_ordered(self):
        sql = backfill.DISCOVERY_SQL
        assert "SELECT DISTINCT" in sql
        assert "ORDER BY b.day, b.provider, b.repository" in sql

    def test_windows_every_canonical_source(self):
        sql = backfill.DISCOVERY_SQL
        for table in (
            "afk_runs",
            "engineering_events",
            "execution_bindings",
            "afk_run_sessions",
            "usage_events",
        ):
            assert table in sql, f"expected canonical source {table}"

    def test_skips_rows_without_repository_identity(self):
        sql = backfill.DISCOVERY_SQL
        assert "r.repository IS NOT NULL" in sql
        assert "e.repository IS NOT NULL" in sql
        assert "b.repository_url IS NOT NULL" in sql

    def test_change_requests_scoped_to_change_request_events(self):
        assert "e.entity_type = 'change_request'" in backfill.DISCOVERY_SQL

    def test_window_is_bound_by_parameters(self):
        assert "b.day BETWEEN $1 AND $2" in backfill.DISCOVERY_SQL

    def test_provider_and_repository_filters_bound_by_parameters(self):
        sql = backfill.DISCOVERY_SQL
        assert "($3::text IS NULL OR b.provider = $3)" in sql
        assert "($4::text IS NULL OR b.repository = $4)" in sql

    def test_discovery_excludes_ambiguous_sessions(self):
        """A session mapped to more than one AFK run must not contribute its
        own usage rows or afk_run_sessions rows to bucket discovery."""
        sql = backfill.DISCOVERY_SQL
        assert "unambiguous AS" in sql
        assert "HAVING COUNT(DISTINCT ars.afk_run_id) = 1" in sql
        assert "JOIN unambiguous u ON u.session_id = ars.session_id" in sql
        assert "JOIN unambiguous u ON u.session_id = ue.session_id" in sql

    @pytest.mark.asyncio
    async def test_discover_buckets_passes_window_and_filters(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        result = await backfill._discover_buckets(
            conn, _DAY, _DAY2, provider=_PROVIDER, repository=_REPOSITORY
        )
        assert result == []
        sql, *params = conn.fetch.call_args[0]
        assert sql == backfill.DISCOVERY_SQL
        assert params == [_DAY, _DAY2, _PROVIDER, _REPOSITORY]

    @pytest.mark.asyncio
    async def test_discover_buckets_unfiltered_passes_none(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await backfill._discover_buckets(conn, _DAY, _DAY2)
        assert conn.fetch.call_args[0][1:] == (_DAY, _DAY2, None, None)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Verification query — rollup vs canonical SUM/COUNT
# ══════════════════════════════════════════════════════════════════════════════


class TestVerificationSql:
    """``--verify`` compares every rollup metric against canonical
    SUM/COUNT recomputation per ``(day, provider, repository)``."""

    def test_verification_query_joins_rollup_to_canonical(self):
        sql = backfill.VERIFICATION_QUERY
        assert "afk_dashboard_daily" in sql
        assert "FULL OUTER JOIN" in sql.upper()

    def test_verification_query_uses_canonical_sources(self):
        sql = backfill.VERIFICATION_QUERY + backfill.CANONICAL_AGGREGATE_SQL
        for table in (
            "afk_runs",
            "engineering_events",
            "execution_bindings",
            "afk_run_sessions",
            "usage_events",
        ):
            assert table in sql, f"expected canonical source {table}"

    def test_verification_query_exposes_both_sides_of_every_metric(self):
        sql = backfill.VERIFICATION_QUERY
        for column in backfill.METRIC_COLUMNS:
            assert f"rollup_{column}" in sql, f"missing rollup side of {column}"
            assert f"canonical_{column}" in sql, (
                f"missing canonical side of {column}"
            )

    def test_verification_query_flags_missing_rows_on_either_side(self):
        sql = backfill.VERIFICATION_QUERY
        assert "d.day IS NULL" in sql
        assert "c.day IS NULL" in sql

    def test_verification_query_uses_utc_day_bucketing(self):
        sql = backfill.CANONICAL_AGGREGATE_SQL
        assert "AT TIME ZONE 'UTC'" in sql
        assert "::date" in sql

    def test_verification_query_windows_and_filters_by_parameters(self):
        sql = backfill.VERIFICATION_QUERY
        assert "COALESCE(d.day, c.day) BETWEEN $1 AND $2" in sql
        assert "($3::text IS NULL OR COALESCE(d.provider, c.provider) = $3)" in sql
        assert (
            "($4::text IS NULL OR COALESCE(d.repository, c.repository) = $4)"
            in sql
        )

    def test_verification_query_orders_by_bucket(self):
        assert (
            "ORDER BY day, provider, repository"
            in backfill.VERIFICATION_QUERY
        )

    def test_mismatch_count_sql_wraps_the_join(self):
        sql = backfill.MISMATCH_COUNT_SQL
        assert "COUNT" in sql.upper()
        assert "afk_dashboard_daily" in sql
        assert "usage_events" in sql

    @pytest.mark.asyncio
    async def test_count_mismatches_returns_int_and_forwards_filters(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 4})
        result = await backfill._count_mismatches(
            conn, _DAY, _DAY2, provider=_PROVIDER, repository=_REPOSITORY
        )
        assert result == 4
        sql, *params = conn.fetchrow.call_args[0]
        assert sql == backfill.MISMATCH_COUNT_SQL
        assert params == [_DAY, _DAY2, _PROVIDER, _REPOSITORY]

    @pytest.mark.asyncio
    async def test_count_mismatches_none_returns_zero(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        assert await backfill._count_mismatches(conn, _DAY, _DAY2) == 0

    @pytest.mark.asyncio
    async def test_run_verification_returns_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"day": _DAY}])
        rows = await backfill._run_verification(conn, _DAY, _DAY2)
        assert rows == [{"day": _DAY}]
        assert conn.fetch.call_args[0][0] == backfill.VERIFICATION_QUERY


# ══════════════════════════════════════════════════════════════════════════════
#  AC 6: Stale rollup rows — no backing canonical bucket
# ══════════════════════════════════════════════════════════════════════════════


class TestStaleDeleteSql:
    """A rollup row whose bucket has no canonical activity is deleted."""

    def test_delete_targets_the_rollup(self):
        sql = backfill.STALE_ROLLUP_DELETE_SQL
        assert "DELETE FROM afk_dashboard_daily" in sql
        assert "NOT EXISTS" in sql

    def test_delete_checks_every_canonical_source(self):
        sql = backfill.STALE_ROLLUP_DELETE_SQL
        for table in (
            "afk_runs",
            "engineering_events",
            "execution_bindings",
            "afk_run_sessions",
            "usage_events",
        ):
            assert table in sql, f"expected canonical source {table}"

    def test_delete_matches_bucket_identity(self):
        sql = backfill.STALE_ROLLUP_DELETE_SQL
        assert "b.day = d.day" in sql
        assert "b.provider = d.provider" in sql
        assert "b.repository = d.repository" in sql

    def test_delete_windows_and_filters_by_parameters(self):
        sql = backfill.STALE_ROLLUP_DELETE_SQL
        assert "d.day BETWEEN $1 AND $2" in sql
        assert "($3::text IS NULL OR d.provider = $3)" in sql
        assert "($4::text IS NULL OR d.repository = $4)" in sql

    @pytest.mark.asyncio
    async def test_delete_stale_rows_returns_count_and_forwards_filters(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 3")
        result = await backfill._delete_stale_rows(
            conn, _DAY, _DAY2, provider=_PROVIDER, repository=_REPOSITORY
        )
        assert result == 3
        sql, *params = conn.execute.call_args[0]
        assert sql == backfill.STALE_ROLLUP_DELETE_SQL
        assert params == [_DAY, _DAY2, _PROVIDER, _REPOSITORY]


# ══════════════════════════════════════════════════════════════════════════════
#  Recording connection + engine doubles
# ══════════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """Records calls; returns scripted results."""

    def __init__(self, *, buckets=None):
        self.buckets = list(buckets or [])
        self.fetches = []
        self.fetchrows = []
        self.executes = []
        self.transactions = 0

    async def fetch(self, sql, *args):
        self.fetches.append((sql, args))
        return self.buckets

    async def fetchrow(self, sql, *args):
        self.fetchrows.append((sql, args))
        return {"cnt": 0}

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "DELETE 0"

    def transaction(self):
        return _Transaction(self)


class _Transaction:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.transactions += 1
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _fake_pool(conn):
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


def _patch_engine(monkeypatch, calls):
    """Replace the engine calls with recorders so orchestration is observable."""

    async def fake_acquire(conn, day, provider, repository):
        calls.append(("lock", day, provider, repository))

    async def fake_recompute(conn, day, provider, repository):
        calls.append(("recompute", day, provider, repository))
        return {}

    monkeypatch.setattr(backfill, "acquire_bucket_lock", fake_acquire)
    monkeypatch.setattr(backfill, "recompute_bucket", fake_recompute)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 5: Backfill delegates to the shared engine per bucket
# ══════════════════════════════════════════════════════════════════════════════


class TestRunBackfill:
    """Each discovered bucket is recomputed in its own transaction, under the
    engine's per-bucket advisory lock."""

    @pytest.mark.asyncio
    async def test_recomputes_every_bucket_under_lock_and_txn(self, monkeypatch):
        buckets = [
            {"day": _DAY, "provider": _PROVIDER, "repository": "a"},
            {"day": _DAY2, "provider": _PROVIDER, "repository": "b"},
        ]
        conn = _RecordingConn()
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        result = await backfill._run_backfill(conn, buckets)

        assert result == 2
        assert conn.transactions == 2
        assert calls == [
            ("lock", _DAY, _PROVIDER, "a"),
            ("recompute", _DAY, _PROVIDER, "a"),
            ("lock", _DAY2, _PROVIDER, "b"),
            ("recompute", _DAY2, _PROVIDER, "b"),
        ]

    @pytest.mark.asyncio
    async def test_empty_bucket_list_recomputes_nothing(self, monkeypatch):
        conn = _RecordingConn()
        calls: list[tuple] = []
        _patch_engine(monkeypatch, calls)

        assert await backfill._run_backfill(conn, []) == 0
        assert conn.transactions == 0
        assert calls == []

    @pytest.mark.asyncio
    async def test_rerun_produces_identical_recompute_calls(self, monkeypatch):
        """Idempotency: rerunning over the same buckets issues the same
        (lock, recompute) sequence — the engine's full-row upsert replaces
        rather than increments, so results are identical."""
        buckets = [
            {"day": _DAY, "provider": _PROVIDER, "repository": "a"},
            {"day": _DAY2, "provider": _PROVIDER, "repository": "b"},
        ]
        first_calls: list[tuple] = []
        _patch_engine(monkeypatch, first_calls)
        first = await backfill._run_backfill(_RecordingConn(), buckets)

        second_calls: list[tuple] = []
        _patch_engine(monkeypatch, second_calls)
        second = await backfill._run_backfill(_RecordingConn(), buckets)

        assert first == second == 2
        assert first_calls == second_calls

    @pytest.mark.asyncio
    async def test_backfill_fails_closed_when_engine_absent(self, monkeypatch):
        monkeypatch.setattr(backfill, "recompute_bucket", None)
        monkeypatch.setattr(backfill, "acquire_bucket_lock", None)
        conn = _RecordingConn()
        with pytest.raises(RuntimeError):
            await backfill._run_backfill(
                conn,
                [{"day": _DAY, "provider": _PROVIDER, "repository": "a"}],
            )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2 + 3: main() — dry-run / verify / default flows, scope forwarding
# ══════════════════════════════════════════════════════════════════════════════


def _patch_main_helpers(monkeypatch, conn, **overrides):
    """Patch main()'s collaborators and return the AsyncMocks."""
    mocks = {
        "_discover_buckets": AsyncMock(return_value=[]),
        "_count_mismatches": AsyncMock(return_value=0),
        "_run_verification": AsyncMock(return_value=[]),
        "_run_backfill": AsyncMock(return_value=0),
        "_delete_stale_rows": AsyncMock(return_value=0),
    }
    mocks.update(overrides)
    monkeypatch.setattr(
        backfill, "_get_pool", AsyncMock(return_value=_fake_pool(conn))
    )
    for name, mock in mocks.items():
        monkeypatch.setattr(backfill, name, mock)
    return mocks


class TestMain:
    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writing(self, monkeypatch):
        conn = AsyncMock()
        buckets = [{"day": _DAY, "provider": _PROVIDER, "repository": "a"}]
        mocks = _patch_main_helpers(
            monkeypatch, conn, _discover_buckets=AsyncMock(return_value=buckets)
        )

        rc = await backfill.main(
            ["--from", "2026-01-15", "--to", "2026-01-16", "--dry-run"]
        )

        assert rc == 0
        mocks["_discover_buckets"].assert_awaited_once()
        mocks["_run_backfill"].assert_not_awaited()
        mocks["_delete_stale_rows"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verify_mode_reports_mismatches_exit_one(self, monkeypatch):
        conn = AsyncMock()
        mocks = _patch_main_helpers(
            monkeypatch, conn, _count_mismatches=AsyncMock(return_value=2)
        )

        rc = await backfill.main(
            ["--from", "2026-01-15", "--to", "2026-01-16", "--verify"]
        )

        assert rc == 1
        mocks["_run_backfill"].assert_not_awaited()
        mocks["_delete_stale_rows"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verify_mode_passes_exit_zero(self, monkeypatch):
        conn = AsyncMock()
        _patch_main_helpers(monkeypatch, conn)

        rc = await backfill.main(
            ["--from", "2026-01-15", "--to", "2026-01-16", "--verify"]
        )

        assert rc == 0

    @pytest.mark.asyncio
    async def test_default_flow_recomputes_and_deletes_stale(self, monkeypatch):
        conn = AsyncMock()
        buckets = [{"day": _DAY, "provider": _PROVIDER, "repository": "a"}]
        mocks = _patch_main_helpers(
            monkeypatch,
            conn,
            _discover_buckets=AsyncMock(return_value=buckets),
            _count_mismatches=AsyncMock(side_effect=[1, 0]),
            _run_backfill=AsyncMock(return_value=1),
            _delete_stale_rows=AsyncMock(return_value=2),
        )

        rc = await backfill.main(["--from", "2026-01-15", "--to", "2026-01-16"])

        assert rc == 0
        mocks["_run_backfill"].assert_awaited_once_with(conn, buckets)
        mocks["_delete_stale_rows"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_flow_returns_one_when_still_disagreeing(
        self, monkeypatch
    ):
        conn = AsyncMock()
        _patch_main_helpers(
            monkeypatch,
            conn,
            _count_mismatches=AsyncMock(return_value=1),
            _run_backfill=AsyncMock(return_value=1),
        )

        rc = await backfill.main(["--from", "2026-01-15", "--to", "2026-01-16"])

        assert rc == 1

    @pytest.mark.asyncio
    async def test_scope_filters_forwarded_to_every_query(self, monkeypatch):
        conn = AsyncMock()
        mocks = _patch_main_helpers(monkeypatch, conn)

        await backfill.main(
            [
                "--from", "2026-01-15",
                "--to", "2026-01-16",
                "--provider", _PROVIDER,
                "--repository", _REPOSITORY,
            ]
        )

        args = (_DAY, _DAY2, _PROVIDER, _REPOSITORY)
        mocks["_discover_buckets"].assert_awaited_once_with(conn, *args)
        mocks["_count_mismatches"].assert_awaited_with(conn, *args)
        mocks["_run_verification"].assert_awaited_with(conn, *args)
        mocks["_delete_stale_rows"].assert_awaited_once_with(conn, *args)


# ══════════════════════════════════════════════════════════════════════════════
#  Metric vocabulary mirrors the engine (#715)
# ══════════════════════════════════════════════════════════════════════════════


class TestMetricVocabulary:
    """The script's comparison metrics mirror the recomputation engine's
    additive columns."""

    def test_metric_columns_match_expected_vocabulary(self):
        assert tuple(backfill.METRIC_COLUMNS) == (
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

    def test_non_additive_columns_absent(self):
        assert "cached_tokens" not in backfill.METRIC_COLUMNS
        assert "reasoning_tokens" not in backfill.METRIC_COLUMNS
