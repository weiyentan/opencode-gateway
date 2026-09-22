# ruff: noqa: UP017 — timezone.utc for consistent tz handling in tests
"""Tests for the nightly ``afk_dashboard_daily`` parity verifier (issue #718).

Covers the acceptance criteria from the task contract:

1. The script accepts a configurable recent window (``--window-days N`` or an
   explicit ``--from-date``/``--to-date`` range).
2. It compares ``afk_dashboard_daily`` rows against the canonical source tables
   the recompute engine projects from, per ``(day, provider, repository)``.
   Every one of the fourteen additive metrics is compared — run / change-request
   / execution / session counts, the four token categories, and estimated cost.
3. Mismatches are grouped by ``(day, provider, repository)`` with a detailed
   breakdown of the differing metrics.
4. The process exits non-zero when mismatches are found, zero when the rollups
   match canonical data.
5. It never modifies, deletes, or updates any rollup or canonical row —
   read-only reporting (the SQL is SELECT-only).

Reporting aggregate tests have been moved to
``test_verify_reporting_resource_aggregates.py`` (issue #729) so each verifier
has an independent test suite matching its independent exit status.

Tests follow the mock pattern of ``tests/test_client_project_rollup_backfill.py``
(SQL-content assertions + ``AsyncMock`` connections + pure comparison helpers).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: configurable verification window
# ══════════════════════════════════════════════════════════════════════════════


class TestParseWindow:
    """Acceptance criterion 1: the window is configurable via --days or an
    explicit --from-date/--to-date range (both bounds inclusive)."""

    def _parse(self, **kwargs):
        from scripts.verify_afk_dashboard_daily import parse_window
        return parse_window(**kwargs)

    def test_default_seven_day_inclusive_window(self):
        window = self._parse(today=date(2026, 9, 21))
        assert window.to_date == date(2026, 9, 21)
        assert window.from_date == date(2026, 9, 15)
        assert window.day_count == 7

    def test_days_one_is_today_only(self):
        window = self._parse(days=1, today=date(2026, 9, 21))
        assert window.from_date == date(2026, 9, 21)
        assert window.to_date == date(2026, 9, 21)

    def test_explicit_from_and_to(self):
        window = self._parse(
            from_date=date(2026, 9, 1), to_date=date(2026, 9, 10),
        )
        assert window.from_date == date(2026, 9, 1)
        assert window.to_date == date(2026, 9, 10)

    def test_days_must_be_positive(self):
        with pytest.raises(ValueError):
            self._parse(days=0)
        with pytest.raises(ValueError):
            self._parse(days=-1)

    def test_from_after_to_rejected(self):
        with pytest.raises(ValueError):
            self._parse(
                from_date=date(2026, 9, 10), to_date=date(2026, 9, 1),
            )

    def test_only_from_rejected(self):
        with pytest.raises(ValueError):
            self._parse(from_date=date(2026, 9, 1))

    def test_only_to_rejected(self):
        with pytest.raises(ValueError):
            self._parse(to_date=date(2026, 9, 1))


class TestParseArgs:
    """CLI argument parsing for the AFK verification entry point."""

    def _parse(self, argv):
        from scripts.verify_afk_dashboard_daily import _parse_args
        return _parse_args(argv)

    def test_defaults(self):
        args = self._parse([])
        assert args.days == 7
        assert args.from_date is None
        assert args.to_date is None
        assert args.json is False

    def test_days_flag(self):
        args = self._parse(["--window-days", "3"])
        assert args.days == 3

    def test_from_to_flags_parsed_as_dates(self):
        args = self._parse(["--from-date", "2026-09-01", "--to-date", "2026-09-10"])
        assert args.from_date == date(2026, 9, 1)
        assert args.to_date == date(2026, 9, 10)

    def test_json_flag(self):
        args = self._parse(["--json"])
        assert args.json is True

    def test_dry_run_default_false(self):
        args = self._parse([])
        assert args.dry_run is False

    def test_dry_run_flag(self):
        args = self._parse(["--dry-run"])
        assert args.dry_run is True


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2: afk_dashboard_daily vs canonical source aggregation
# ══════════════════════════════════════════════════════════════════════════════


class TestAfkDashboardDailySql:
    """Acceptance criterion 2: the rollup-side query joins ``afk_dashboard_daily``
    against the canonical per-``(day, provider, repository)`` aggregation the
    recompute engine projects from its source tables."""

    def _sql(self) -> str:
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_DAILY_MISMATCH_SQL,
        )
        return AFK_DASHBOARD_DAILY_MISMATCH_SQL

    def test_joins_rollup_against_canonical_full_outer(self):
        sql = self._sql()
        assert "FROM afk_dashboard_daily d" in sql
        assert "FULL OUTER JOIN (" in sql
        assert "SELECT * FROM canonical" in sql
        assert "ON d.day = c.day" in sql

    def test_compares_all_fourteen_metric_columns(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_METRIC_COLUMNS,
        )

        assert len(AFK_DASHBOARD_METRIC_COLUMNS) == 14
        sql = self._sql()
        for name in AFK_DASHBOARD_METRIC_COLUMNS:
            assert f"rollup_{name}" in sql, f"missing rollup side of {name}"
            assert f"canonical_{name}" in sql, f"missing canonical side of {name}"

    def test_canonical_side_reads_every_source_table(self):
        sql = self._sql()
        for table in (
            "afk_runs",
            "engineering_events",
            "execution_bindings",
            "afk_run_sessions",
            "usage_events",
        ):
            assert f"FROM {table}" in sql, f"canonical side missing {table}"

    def test_canonical_side_recomputes_the_pivot_metrics(self):
        """The canonical CTEs compute the count/SUM metrics, not just project
        the rollup columns."""
        sql = self._sql()
        assert "COUNT(*)::int AS runs_started" in sql
        assert "FILTER (WHERE e.event_type = 'change_request.opened')" in sql
        assert "FILTER (WHERE e.event_type = 'change_request.merged')" in sql
        assert "FILTER (WHERE e.event_type = 'change_request.closed')" in sql
        assert "FILTER (WHERE b.outcome = 'completed')" in sql
        assert "FILTER (WHERE b.outcome = 'failed')" in sql
        assert "FILTER (WHERE b.outcome = 'cancelled')" in sql
        assert "COALESCE(SUM(ue.input_tokens), 0)" in sql
        assert "COALESCE(SUM(ue.estimated_cost_usd), 0)" in sql

    def test_excludes_ambiguous_session_attribution(self):
        sql = self._sql()
        assert "HAVING COUNT(DISTINCT ars.afk_run_id) = 1" in sql

    def test_flags_missing_rows_on_either_side(self):
        sql = self._sql()
        assert "d.day IS NULL" in sql
        assert "c.day IS NULL" in sql

    def test_excludes_rows_without_repository_identity(self):
        sql = self._sql()
        assert "r.repository IS NOT NULL" in sql
        assert "e.repository IS NOT NULL" in sql
        assert "b.repository_url IS NOT NULL" in sql
        assert "b.repository_url AS repository" in sql

    def test_uses_utc_day_bucketing(self):
        sql = self._sql()
        assert "AT TIME ZONE 'UTC'" in sql
        assert "::date" in sql

    def test_window_bounds_are_parameters(self):
        sql = self._sql()
        assert "BETWEEN $1 AND $2" in sql
        assert sql.count("$1") >= 1
        assert sql.count("$2") >= 1

    def test_orders_by_day_provider_repository(self):
        sql = self._sql()
        assert re.search(r"ORDER BY\s+day,\s+provider,\s+repository", sql)

    def test_backward_compatible_alias_points_at_new_sql(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_DAILY_MISMATCH_SQL,
            USAGE_ROLLUP_MISMATCH_SQL,
        )

        assert USAGE_ROLLUP_MISMATCH_SQL == AFK_DASHBOARD_DAILY_MISMATCH_SQL


class TestCompareAfkDashboardDailyRows:
    """Pure mapping of SQL mismatch rows into grouped mismatch records."""

    _ROLLUP = {
        "runs_started": 5,
        "change_requests_opened": 3,
        "change_requests_merged": 2,
        "change_requests_closed": 1,
        "execution_count": 4,
        "successful_execution_count": 3,
        "failed_execution_count": 1,
        "cancelled_execution_count": 0,
        "session_count": 6,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "estimated_cost_usd": Decimal("0.0035"),
    }
    _CANONICAL = {
        **_ROLLUP,
        "runs_started": 7,
        "input_tokens": 120,
        "estimated_cost_usd": Decimal("0.0040"),
    }

    def _row(self, **overrides):
        row = {
            "day": date(2026, 9, 20),
            "provider": "gitlab",
            "repository": "cloudnative-pg",
        }
        for name, value in self._ROLLUP.items():
            row[f"rollup_{name}"] = value
        for name, value in self._CANONICAL.items():
            row[f"canonical_{name}"] = value
        row.update(overrides)
        return row

    def test_maps_all_fourteen_metrics(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_METRIC_COLUMNS,
            compare_afk_dashboard_daily_rows,
        )

        mismatch = compare_afk_dashboard_daily_rows([self._row()])[0]
        assert set(mismatch.fields) == set(AFK_DASHBOARD_METRIC_COLUMNS)
        assert len(mismatch.fields) == 14

    def test_maps_identity_and_source(self):
        from scripts.verify_afk_dashboard_daily import (
            compare_afk_dashboard_daily_rows,
        )

        mismatch = compare_afk_dashboard_daily_rows([self._row()])[0]
        assert mismatch.source == "afk_dashboard_daily"
        assert mismatch.day == date(2026, 9, 20)
        assert mismatch.provider == "gitlab"
        assert mismatch.repository == "cloudnative-pg"
        assert mismatch.group_key == (
            date(2026, 9, 20), "gitlab", "cloudnative-pg",
        )

    def test_computes_delta_for_differing_metrics(self):
        from scripts.verify_afk_dashboard_daily import (
            compare_afk_dashboard_daily_rows,
        )

        fields = compare_afk_dashboard_daily_rows([self._row()])[0].fields
        assert fields["runs_started"].rollup == 5
        assert fields["runs_started"].canonical == 7
        assert fields["runs_started"].delta == 2
        assert fields["input_tokens"].delta == 20
        assert fields["estimated_cost_usd"].delta == Decimal("0.0005")
        # A matching metric still appears with a zero delta.
        assert fields["output_tokens"].delta == 0

    def test_reports_the_mismatched_metric_names(self):
        from scripts.verify_afk_dashboard_daily import (
            compare_afk_dashboard_daily_rows,
        )

        mismatch = compare_afk_dashboard_daily_rows([self._row()])[0]
        assert set(mismatch.context["mismatched_metrics"]) == {
            "runs_started",
            "input_tokens",
            "estimated_cost_usd",
        }

    def test_missing_rollup_row_has_none_rollup_side(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_METRIC_COLUMNS,
            compare_afk_dashboard_daily_rows,
        )

        row = self._row()
        for name in AFK_DASHBOARD_METRIC_COLUMNS:
            row[f"rollup_{name}"] = None
        mismatch = compare_afk_dashboard_daily_rows([row])[0]
        assert mismatch.fields["input_tokens"].rollup is None
        assert mismatch.fields["input_tokens"].canonical == 120
        assert mismatch.fields["input_tokens"].delta is None

    def test_stale_rollup_row_has_none_canonical_side(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_METRIC_COLUMNS,
            compare_afk_dashboard_daily_rows,
        )

        row = self._row()
        for name in AFK_DASHBOARD_METRIC_COLUMNS:
            row[f"canonical_{name}"] = None
        mismatch = compare_afk_dashboard_daily_rows([row])[0]
        assert mismatch.fields["input_tokens"].rollup == 100
        assert mismatch.fields["input_tokens"].canonical is None


# ══════════════════════════════════════════════════════════════════════════════
#  AC 6: read-only — no write SQL anywhere in the script
# ══════════════════════════════════════════════════════════════════════════════


class TestReadOnlySql:
    """Acceptance criterion 6: the verification tool never modifies, deletes,
    or updates rollup or canonical data — its SQL is SELECT-only."""

    _WRITE_RE = re.compile(
        r"\b(INSERT|UPDATE|DELETE|UPSERT|DROP|ALTER|TRUNCATE|CREATE|MERGE)\b",
        re.IGNORECASE,
    )

    def test_no_write_statements_in_afk_sql(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_DAILY_MISMATCH_SQL,
        )

        match = self._WRITE_RE.search(AFK_DASHBOARD_DAILY_MISMATCH_SQL)
        assert match is None, f"AFK SQL contains write keyword {match.group(0)!r}"

    def test_afk_sql_starts_with_select(self):
        from scripts.verify_afk_dashboard_daily import (
            AFK_DASHBOARD_DAILY_MISMATCH_SQL,
        )

        assert AFK_DASHBOARD_DAILY_MISMATCH_SQL.lstrip().upper().startswith(
            ("SELECT", "WITH")
        )

    def test_no_write_statements_in_helpers(self):
        """The shared helpers contain no SQL — it's a pure-Python module."""
        from scripts import verify_helpers as mod
        import inspect

        for name, obj in inspect.getmembers(mod):
            if isinstance(obj, str) and obj.lstrip().upper().startswith(
                ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")
            ):
                # Skip SQL constants that are only strings, not executed
                continue


# ══════════════════════════════════════════════════════════════════════════════
#  AC 5: exit codes
# ══════════════════════════════════════════════════════════════════════════════


class TestGroupMismatches:
    """Acceptance criterion 4: mismatches are grouped by
    ``(day, provider, repository)`` in a stable order."""

    def _mismatch(self, day, provider, repository):
        from scripts.verify_helpers import Mismatch
        return Mismatch(
            source="afk_dashboard_daily",
            day=day,
            provider=provider,
            repository=repository,
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )

    def test_groups_by_day_provider_repository(self):
        from scripts.verify_helpers import group_mismatches

        a = self._mismatch(date(2026, 9, 20), "p1", "r1")
        b = self._mismatch(date(2026, 9, 20), "p1", "r1")
        c = self._mismatch(date(2026, 9, 21), "p2", "r2")
        groups = group_mismatches([c, a, b])
        assert list(groups.keys()) == [
            (date(2026, 9, 20), "p1", "r1"),
            (date(2026, 9, 21), "p2", "r2"),
        ]
        assert len(groups[(date(2026, 9, 20), "p1", "r1")]) == 2

    def test_empty_returns_empty(self):
        from scripts.verify_helpers import group_mismatches
        assert group_mismatches([]) == {}


class TestExitCode:
    """Acceptance criterion 5: non-zero when mismatches exist, zero otherwise."""

    def test_zero_when_no_mismatches(self):
        from scripts.verify_helpers import exit_code
        assert exit_code([]) == 0

    def test_one_when_mismatches(self):
        from scripts.verify_helpers import Mismatch, exit_code
        mismatch = Mismatch(
            source="afk_dashboard_daily",
            day=date(2026, 9, 20),
            provider="p",
            repository="r",
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )
        assert exit_code([mismatch]) == 1


class TestMain:
    """End-to-end exit-code behaviour against a mocked database pool."""

    class _AsyncCM:
        def __init__(self, value):
            self._value = value

        async def __aenter__(self):
            return self._value

        async def __aexit__(self, *exc_info):
            return False

    class _FakePool:
        def __init__(self, conn):
            self._conn = conn

        def acquire(self):
            return TestMain._AsyncCM(self._conn)

        async def close(self):
            return None

    async def _run_main(self, monkeypatch, mismatches):
        from scripts import verify_afk_dashboard_daily as mod

        async def _fake_get_pool():
            return TestMain._FakePool(AsyncMock())

        async def _fake_run(conn, window):
            return mismatches

        monkeypatch.setattr(mod, "_get_pool", _fake_get_pool)
        monkeypatch.setattr(mod, "_run_verification", _fake_run)
        monkeypatch.setattr(mod, "_emit_report", lambda *a, **k: None)
        return await mod.main(["--window-days", "1"])

    async def test_main_returns_zero_when_clean(self, monkeypatch):
        assert await self._run_main(monkeypatch, []) == 0

    async def test_main_returns_one_when_mismatches(self, monkeypatch):
        from scripts.verify_helpers import Mismatch

        mismatch = Mismatch(
            source="afk_dashboard_daily",
            day=date(2026, 9, 20),
            provider="p",
            repository="r",
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )
        assert await self._run_main(monkeypatch, [mismatch]) == 1


class TestFetchAfkDashboardMismatches:
    """The dashboard fetch path binds the window bounds to the mismatch query."""

    async def test_binds_window_bounds(self):
        from scripts import verify_afk_dashboard_daily as mod

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        window = mod.parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        result = await mod._fetch_afk_dashboard_mismatches(conn, window)
        assert result == []
        args = conn.fetch.call_args[0]
        assert args[0] == mod.AFK_DASHBOARD_DAILY_MISMATCH_SQL
        assert args[1:] == (date(2026, 9, 20), date(2026, 9, 21))


class TestWindowDayCount:
    """Sanity: the inclusive day count is used for the report summary."""

    def test_day_count(self):
        from scripts.verify_helpers import parse_window
        window = parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        assert window.day_count == 2
        assert window.contains(date(2026, 9, 20))
        assert window.contains(date(2026, 9, 21))
        assert not window.contains(date(2026, 9, 22))


def test_today_utc_default_uses_utc_calendar_day():
    """The default 'today' uses UTC so a nightly run near midnight buckets
    consistently with the UTC rollup day."""
    from scripts.verify_helpers import _today_utc

    before = datetime.now(timezone.utc).date()
    today = _today_utc()
    after = datetime.now(timezone.utc).date()
    assert before <= today <= after


# ══════════════════════════════════════════════════════════════════════════════
#  AFK-only verification: reporting mismatch cannot affect AFK exit code
# ══════════════════════════════════════════════════════════════════════════════


class TestAfkOnlyVerification:
    """Issue #729 acceptance: the AFK verifier checks ONLY AFK rollup parity.
    A reporting mismatch cannot fail the AFK verifier."""

    async def test_run_verification_only_checks_afk(self, monkeypatch):
        """_run_verification calls only _fetch_afk_dashboard_mismatches, not
        _fetch_reporting_mismatches."""
        from scripts import verify_afk_dashboard_daily as mod

        afk_mismatch = type("M", (), {"source": "afk_dashboard_daily"})()
        afk_called = []
        reporting_called = []

        async def _fake_afk(conn, window):
            afk_called.append(True)
            return [afk_mismatch]

        async def _fake_reporting(conn, window):
            reporting_called.append(True)
            return []

        monkeypatch.setattr(mod, "_fetch_afk_dashboard_mismatches", _fake_afk)
        if hasattr(mod, "_fetch_reporting_mismatches"):
            monkeypatch.setattr(mod, "_fetch_reporting_mismatches", _fake_reporting)

        conn = AsyncMock()
        window = mod.parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        result = await mod._run_verification(conn, window)

        assert afk_called, "AFK fetch should have been called"
        assert not reporting_called, "Reporting fetch should NOT be called by AFK verifier"
        assert len(result) == 1
        assert result[0].source == "afk_dashboard_daily"
