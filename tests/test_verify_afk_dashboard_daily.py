# ruff: noqa: UP017 — timezone.utc for consistent tz handling in tests
"""Tests for the nightly rollup-parity verification script (issue #718).

Covers the acceptance criteria from the task contract:

1. The script accepts a configurable recent window (``--window-days N`` or an
   explicit ``--from-date``/``--to-date`` range).
2. It compares ``client_project_rollup`` rows against ``SUM(usage_events)``
   per ``(client_id, project_id, day)`` (token totals + cost).
3. It compares ``reporting_resource_aggregates`` against
   ``reporting_deliveries`` source data per stable resource identity.
4. Mismatches are grouped by ``(day, provider, repository)`` with a detailed
   breakdown of the differing fields.
5. The process exits non-zero when mismatches are found, zero when the
   rollups match canonical data.
6. It never modifies, deletes, or updates any rollup or canonical row —
   read-only reporting (the SQL is SELECT-only).

Tests follow the mock pattern of ``tests/test_client_project_rollup_backfill.py``
(SQL-content assertions + ``AsyncMock`` connections + pure comparison helpers).
"""

from __future__ import annotations

import re
import uuid
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
    """CLI argument parsing for the verification entry point."""

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
#  AC 2: client_project_rollup vs SUM(usage_events)
# ══════════════════════════════════════════════════════════════════════════════


class TestUsageRollupSql:
    """Acceptance criterion 2: the usage-side query joins the rollup against
    ``SUM(usage_events)`` per ``(client_id, project_id, day)``."""

    def _sql(self) -> str:
        from scripts.verify_afk_dashboard_daily import USAGE_ROLLUP_MISMATCH_SQL
        return USAGE_ROLLUP_MISMATCH_SQL

    def test_joins_both_tables_full_outer(self):
        sql = self._sql()
        assert "client_project_rollup" in sql
        assert "usage_events" in sql
        assert "FULL OUTER JOIN" in sql.upper()

    def test_keys_on_client_project_day(self):
        sql = self._sql()
        assert "ON r.client_id = g.client_id" in sql
        assert "AND r.project_id = g.project_id" in sql
        assert "AND r.day = g.day" in sql

    def test_compares_all_rollup_fields(self):
        from app.core.reconciliation import ROLLUP_FIELDS

        sql = self._sql()
        for field in ROLLUP_FIELDS:
            assert f"rollup_{field}" in sql, f"missing rollup side of {field}"
            assert f"canonical_{field}" in sql, f"missing canonical side of {field}"

    def test_sums_only_additive_fields(self):
        from app.core.reconciliation import ROLLUP_FIELDS

        sql = self._sql()
        summed = set(re.findall(r"SUM\(ue\.(\w+)\)", sql))
        assert sorted(summed) == sorted(ROLLUP_FIELDS)
        assert "cached_tokens" not in summed
        assert "reasoning_tokens" not in summed

    def test_flags_missing_rows_on_either_side(self):
        sql = self._sql()
        assert "r.client_id IS NULL" in sql
        assert "g.client_id IS NULL" in sql

    def test_excludes_null_project_events(self):
        sql = self._sql()
        assert "project_id IS NOT NULL" in sql

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
        assert re.search(r"ORDER BY\s+day,\s+client_id,\s+project_id", sql)


class TestCompareUsageRows:
    """Pure mapping of SQL mismatch rows into grouped mismatch records."""

    def _row(self, **overrides):
        row = {
            "client_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
            "project_id": "proj-a",
            "day": date(2026, 9, 20),
            "rollup_input_tokens": 100,
            "rollup_output_tokens": 50,
            "rollup_cache_read_tokens": 10,
            "rollup_cache_write_tokens": 5,
            "rollup_estimated_cost_usd": Decimal("0.0035"),
            "canonical_input_tokens": 120,
            "canonical_output_tokens": 50,
            "canonical_cache_read_tokens": 10,
            "canonical_cache_write_tokens": 5,
            "canonical_estimated_cost_usd": Decimal("0.0040"),
            "canonical_event_count": 2,
        }
        row.update(overrides)
        return row

    def test_maps_differing_fields_and_delta(self):
        from scripts.verify_afk_dashboard_daily import compare_usage_rollup_rows

        mismatches = compare_usage_rollup_rows([self._row()])
        assert len(mismatches) == 1
        mismatch = mismatches[0]
        assert mismatch.source == "client_project_rollup"
        assert mismatch.day == date(2026, 9, 20)
        assert mismatch.provider == "11111111-1111-1111-1111-111111111111"
        assert mismatch.repository == "proj-a"
        assert mismatch.group_key == (date(2026, 9, 20), mismatch.provider, "proj-a")

        assert mismatch.fields["input_tokens"].rollup == 100
        assert mismatch.fields["input_tokens"].canonical == 120
        assert mismatch.fields["input_tokens"].delta == 20
        assert mismatch.fields["estimated_cost_usd"].delta == Decimal("0.0005")
        # A matching field still appears in the breakdown with a zero delta.
        assert mismatch.fields["output_tokens"].delta == 0

    def test_missing_rollup_row_has_none_rollup_side(self):
        from scripts.verify_afk_dashboard_daily import compare_usage_rollup_rows

        row = self._row(
            rollup_input_tokens=None,
            rollup_output_tokens=None,
            rollup_cache_read_tokens=None,
            rollup_cache_write_tokens=None,
            rollup_estimated_cost_usd=None,
        )
        mismatch = compare_usage_rollup_rows([row])[0]
        assert mismatch.fields["input_tokens"].rollup is None
        assert mismatch.fields["input_tokens"].canonical == 120
        assert mismatch.fields["input_tokens"].delta is None

    def test_stale_rollup_row_has_none_canonical_side(self):
        from scripts.verify_afk_dashboard_daily import compare_usage_rollup_rows

        row = self._row(
            canonical_input_tokens=None,
            canonical_output_tokens=None,
            canonical_cache_read_tokens=None,
            canonical_cache_write_tokens=None,
            canonical_estimated_cost_usd=None,
            canonical_event_count=0,
        )
        mismatch = compare_usage_rollup_rows([row])[0]
        assert mismatch.fields["input_tokens"].rollup == 100
        assert mismatch.fields["input_tokens"].canonical is None

    def test_event_count_is_reported_as_context(self):
        from scripts.verify_afk_dashboard_daily import compare_usage_rollup_rows

        mismatch = compare_usage_rollup_rows([self._row()])[0]
        assert mismatch.context["event_count"] == 2


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: reporting_resource_aggregates vs reporting_deliveries
# ══════════════════════════════════════════════════════════════════════════════


class TestReportingSql:
    """Acceptance criterion 3: the reporting-side queries read the current
    aggregate table and the canonical delivery table."""

    def test_aggregate_select_reads_current_table(self):
        from scripts.verify_afk_dashboard_daily import REPORTING_AGGREGATES_SQL

        assert "reporting_resource_aggregates" in REPORTING_AGGREGATES_SQL
        assert "provider" in REPORTING_AGGREGATES_SQL
        assert "repository_url" in REPORTING_AGGREGATES_SQL
        assert "last_delivery_id" in REPORTING_AGGREGATES_SQL

    def test_deliveries_select_is_windowed_by_utc_day(self):
        from scripts.verify_afk_dashboard_daily import REPORTING_DELIVERIES_SQL

        assert "reporting_deliveries" in REPORTING_DELIVERIES_SQL
        assert "AT TIME ZONE 'UTC'" in REPORTING_DELIVERIES_SQL
        assert "BETWEEN $1 AND $2" in REPORTING_DELIVERIES_SQL


class TestCanonicalLatestByResource:
    """The canonical latest delivery per stable resource identity mirrors the
    ingest-time forward-only advance (max occurred_at, lowest delivery_id)."""

    def _delivery(self, *, provider="gitlab", delivery_id, occurred_at, number="1"):
        return {
            "provider": provider,
            "delivery_id": delivery_id,
            "occurred_at": occurred_at,
            "payload": {
                "resource": {
                    "repository_url": "https://GitLab.example.com/group/proj.git",
                    "type": "merge_request",
                    "number": number,
                }
            },
        }

    def test_picks_max_occurred_at(self):
        from scripts.verify_afk_dashboard_daily import canonical_latest_by_resource

        latest, counts = canonical_latest_by_resource([
            self._delivery(
                delivery_id="d1", occurred_at=datetime(2026, 9, 20, 1, tzinfo=timezone.utc),
            ),
            self._delivery(
                delivery_id="d2", occurred_at=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
            ),
        ])
        key = ("gitlab", "gitlab.example.com/group/proj", "change_request", "1")
        assert latest[key] == (datetime(2026, 9, 20, 2, tzinfo=timezone.utc), "d2")
        assert counts[key] == 2

    def test_tie_break_lowest_delivery_id(self):
        from scripts.verify_afk_dashboard_daily import canonical_latest_by_resource

        same_time = datetime(2026, 9, 20, 2, tzinfo=timezone.utc)
        latest, _ = canonical_latest_by_resource([
            self._delivery(delivery_id="d9", occurred_at=same_time),
            self._delivery(delivery_id="d3", occurred_at=same_time),
        ])
        key = ("gitlab", "gitlab.example.com/group/proj", "change_request", "1")
        assert latest[key][1] == "d3"

    def test_maps_provider_resource_type_to_canonical(self):
        """A GitHub pull_request and a GitLab merge_request both canonicalise
        to the reporting layer's ``change_request`` type."""
        from scripts.verify_afk_dashboard_daily import canonical_latest_by_resource

        row = self._delivery(
            delivery_id="d1", occurred_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )
        row["payload"]["resource"]["type"] = "pull_request"
        latest, _ = canonical_latest_by_resource([row])
        assert ("gitlab", "gitlab.example.com/group/proj", "change_request", "1") in latest

    def test_skips_malformed_payload(self):
        from scripts.verify_afk_dashboard_daily import canonical_latest_by_resource

        latest, counts = canonical_latest_by_resource([
            {"provider": "gitlab", "delivery_id": "d1",
             "occurred_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
             "payload": {"no_resource": True}},
            {"provider": "gitlab", "delivery_id": "d2",
             "occurred_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
             "payload": None},
        ])
        assert latest == {}
        assert counts == {}


class TestCompareReportingAggregates:
    """Pure comparison of aggregate rows against the canonical latest delivery
    derived from reporting_deliveries."""

    def _window(self):
        from scripts.verify_afk_dashboard_daily import parse_window
        return parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))

    def _delivery(self, *, delivery_id, occurred_at, provider="gitlab", number="6"):
        return {
            "provider": provider,
            "delivery_id": delivery_id,
            "occurred_at": occurred_at,
            "payload": {
                "resource": {
                    "repository_url": "https://gitlab.example.com/cloudnative-pg.git",
                    "type": "merge_request",
                    "number": number,
                }
            },
        }

    def _aggregate(self, *, delivery_id, occurred_at, provider="gitlab", number="6"):
        return {
            "provider": provider,
            "repository_url": "gitlab.example.com/cloudnative-pg",
            "resource_type": "change_request",
            "resource_number": number,
            "last_occurred_at": occurred_at,
            "last_delivery_id": delivery_id,
        }

    def test_clean_when_pointer_matches(self):
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        t = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=t)],
            [self._delivery(delivery_id="d1", occurred_at=t)],
            self._window(),
        )
        assert mismatches == []

    def test_flags_last_delivery_id_mismatch(self):
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        t1 = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=t1)],
            [self._delivery(delivery_id="d2", occurred_at=t2)],
            self._window(),
        )
        assert len(mismatches) == 1
        mismatch = mismatches[0]
        assert mismatch.source == "reporting_resource_aggregates"
        assert mismatch.provider == "gitlab"
        assert mismatch.repository == "gitlab.example.com/cloudnative-pg"
        assert mismatch.resource_type == "change_request"
        assert mismatch.resource_number == "6"
        assert mismatch.fields["last_delivery_id"].rollup == "d1"
        assert mismatch.fields["last_delivery_id"].canonical == "d2"
        assert "last_occurred_at" in mismatch.fields

    def test_flags_missing_aggregate_for_observed_identity(self):
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        t = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [],
            [self._delivery(delivery_id="d1", occurred_at=t)],
            self._window(),
        )
        assert len(mismatches) == 1
        assert mismatches[0].fields["last_delivery_id"].rollup is None
        assert mismatches[0].fields["last_delivery_id"].canonical == "d1"
        assert mismatches[0].context["reason"] == "aggregate_missing"

    def test_flags_aggregate_without_backing_delivery_in_window(self):
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        t = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=t)],
            [],
            self._window(),
        )
        assert len(mismatches) == 1
        assert mismatches[0].fields["last_delivery_id"].rollup == "d1"
        assert mismatches[0].fields["last_delivery_id"].canonical is None
        assert mismatches[0].context["reason"] == "no_backing_delivery"

    def test_ignores_aggregates_newer_than_window(self):
        """A resource whose aggregate advanced past the window end is not a
        window mismatch — the historical window simply does not cover it."""
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        later = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=later)],
            [],
            self._window(),
        )
        assert mismatches == []

    def test_ignores_aggregate_outside_window_when_no_deliveries(self):
        from scripts.verify_afk_dashboard_daily import compare_reporting_aggregates

        old = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=old)],
            [],
            self._window(),
        )
        assert mismatches == []


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

    def _sql_constants(self) -> dict[str, str]:
        from scripts import verify_afk_dashboard_daily as mod

        return {
            "USAGE_ROLLUP_MISMATCH_SQL": mod.USAGE_ROLLUP_MISMATCH_SQL,
            "REPORTING_AGGREGATES_SQL": mod.REPORTING_AGGREGATES_SQL,
            "REPORTING_DELIVERIES_SQL": mod.REPORTING_DELIVERIES_SQL,
        }

    def test_no_write_statements(self):
        for name, sql in self._sql_constants().items():
            match = self._WRITE_RE.search(sql)
            assert match is None, f"{name} contains write keyword {match.group(0)!r}"

    def test_all_sql_starts_with_select(self):
        for name, sql in self._sql_constants().items():
            assert sql.lstrip().upper().startswith("SELECT"), name


# ══════════════════════════════════════════════════════════════════════════════
#  AC 5: exit codes
# ══════════════════════════════════════════════════════════════════════════════


class TestGroupMismatches:
    """Acceptance criterion 4: mismatches are grouped by
    ``(day, provider, repository)`` in a stable order."""

    def _mismatch(self, day, provider, repository):
        from scripts.verify_afk_dashboard_daily import Mismatch
        return Mismatch(
            source="client_project_rollup",
            day=day,
            provider=provider,
            repository=repository,
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )

    def test_groups_by_day_provider_repository(self):
        from scripts.verify_afk_dashboard_daily import group_mismatches

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
        from scripts.verify_afk_dashboard_daily import group_mismatches
        assert group_mismatches([]) == {}


class TestExitCode:
    """Acceptance criterion 5: non-zero when mismatches exist, zero otherwise."""

    def test_zero_when_no_mismatches(self):
        from scripts.verify_afk_dashboard_daily import _exit_code
        assert _exit_code([]) == 0

    def test_one_when_mismatches(self):
        from scripts.verify_afk_dashboard_daily import Mismatch, _exit_code
        mismatch = Mismatch(
            source="client_project_rollup",
            day=date(2026, 9, 20),
            provider="p",
            repository="r",
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )
        assert _exit_code([mismatch]) == 1


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
        from scripts.verify_afk_dashboard_daily import Mismatch

        mismatch = Mismatch(
            source="client_project_rollup",
            day=date(2026, 9, 20),
            provider="p",
            repository="r",
            resource_type=None,
            resource_number=None,
            fields={},
            context={},
        )
        assert await self._run_main(monkeypatch, [mismatch]) == 1


class TestFetchReportingMismatches:
    """The reporting fetch path issues the aggregate + delivery queries and
    delegates to the pure comparison."""

    async def test_uses_both_queries(self):
        from scripts import verify_afk_dashboard_daily as mod

        t = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[
            # aggregates
            [
                {
                    "provider": "gitlab",
                    "repository_url": "gitlab.example.com/cloudnative-pg",
                    "resource_type": "change_request",
                    "resource_number": "6",
                    "last_occurred_at": t,
                    "last_delivery_id": "d1",
                }
            ],
            # deliveries
            [
                {
                    "provider": "gitlab",
                    "delivery_id": "d1",
                    "occurred_at": t,
                    "payload": {
                        "resource": {
                            "repository_url": "https://gitlab.example.com/cloudnative-pg",
                            "type": "merge_request",
                            "number": "6",
                        }
                    },
                }
            ],
        ])
        window = mod.parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        result = await mod._fetch_reporting_mismatches(conn, window)
        assert result == []
        assert conn.fetch.await_count == 2
        # First call is window-parameterised on the deliveries query only.
        first_args = conn.fetch.call_args_list[0][0]
        second_args = conn.fetch.call_args_list[1][0]
        assert first_args[0] == mod.REPORTING_AGGREGATES_SQL
        assert second_args[0] == mod.REPORTING_DELIVERIES_SQL
        assert second_args[1:] == (date(2026, 9, 20), date(2026, 9, 21))


class TestFetchUsageMismatches:
    """The usage fetch path binds the window bounds to the mismatch query."""

    async def test_binds_window_bounds(self):
        from scripts import verify_afk_dashboard_daily as mod

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        window = mod.parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        result = await mod._fetch_usage_mismatches(conn, window)
        assert result == []
        args = conn.fetch.call_args[0]
        assert args[0] == mod.USAGE_ROLLUP_MISMATCH_SQL
        assert args[1:] == (date(2026, 9, 20), date(2026, 9, 21))


class TestWindowDayCount:
    """Sanity: the inclusive day count is used for the report summary."""

    def test_day_count(self):
        from scripts.verify_afk_dashboard_daily import parse_window
        window = parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        assert window.day_count == 2
        assert window.contains(date(2026, 9, 20))
        assert window.contains(date(2026, 9, 21))
        assert not window.contains(date(2026, 9, 22))


def test_today_utc_default_uses_utc_calendar_day():
    """The default 'today' uses UTC so a nightly run near midnight buckets
    consistently with the UTC rollup day."""
    from scripts.verify_afk_dashboard_daily import _today_utc

    before = datetime.now(timezone.utc).date()
    today = _today_utc()
    after = datetime.now(timezone.utc).date()
    assert before <= today <= after
