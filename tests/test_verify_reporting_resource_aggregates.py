# ruff: noqa: UP017 — timezone.utc for consistent tz handling in tests
"""Tests for the reporting aggregate consistency verifier (issue #729).

Covers the acceptance criteria for ``verify_reporting_resource_aggregates.py``:

1. The script accepts a configurable recent window (``--window-days N`` or an
   explicit ``--from-date``/``--to-date`` range).
2. It compares ``reporting_resource_aggregates`` against the canonical
   ``reporting_deliveries`` source data per stable resource identity.
3. Mismatches are grouped by ``(day, provider, repository)`` with a detailed
   breakdown.
4. The process exits non-zero when mismatches are found, zero when the
   aggregates match.
5. It never modifies, deletes, or updates any aggregate or delivery row —
   read-only reporting (the SQL is SELECT-only).
6. An AFK rollup drift cannot fail this verifier.

Tests follow the mock pattern of ``tests/test_client_project_rollup_backfill.py``
(SQL-content assertions + ``AsyncMock`` connections + pure comparison helpers).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: configurable verification window
# ══════════════════════════════════════════════════════════════════════════════


class TestParseWindow:
    """Acceptance criterion 1: the window is configurable via --days or an
    explicit --from-date/--to-date range (both bounds inclusive)."""

    def _parse(self, **kwargs):
        from scripts.verify_reporting_resource_aggregates import parse_window
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
    """CLI argument parsing for the reporting verification entry point."""

    def _parse(self, argv):
        from scripts.verify_reporting_resource_aggregates import _parse_args
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
#  AC 2: reporting_resource_aggregates vs reporting_deliveries
# ══════════════════════════════════════════════════════════════════════════════


class TestReportingSql:
    """Acceptance criterion 2: the reporting-side queries read the current
    aggregate table and the canonical delivery table."""

    def test_aggregate_select_reads_current_table(self):
        from scripts.verify_reporting_resource_aggregates import REPORTING_AGGREGATES_SQL

        assert "reporting_resource_aggregates" in REPORTING_AGGREGATES_SQL
        assert "provider" in REPORTING_AGGREGATES_SQL
        assert "repository_url" in REPORTING_AGGREGATES_SQL
        assert "last_delivery_id" in REPORTING_AGGREGATES_SQL

    def test_deliveries_select_is_windowed_by_utc_day(self):
        from scripts.verify_reporting_resource_aggregates import REPORTING_DELIVERIES_SQL

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
        from scripts.verify_helpers import canonical_latest_by_resource

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
        from scripts.verify_helpers import canonical_latest_by_resource

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
        from scripts.verify_helpers import canonical_latest_by_resource

        row = self._delivery(
            delivery_id="d1", occurred_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )
        row["payload"]["resource"]["type"] = "pull_request"
        latest, _ = canonical_latest_by_resource([row])
        assert ("gitlab", "gitlab.example.com/group/proj", "change_request", "1") in latest

    def test_skips_malformed_payload(self):
        from scripts.verify_helpers import canonical_latest_by_resource

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
        from scripts.verify_helpers import parse_window
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
        from scripts.verify_helpers import compare_reporting_aggregates

        t = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=t)],
            [self._delivery(delivery_id="d1", occurred_at=t)],
            self._window(),
        )
        assert mismatches == []

    def test_flags_last_delivery_id_mismatch(self):
        from scripts.verify_helpers import compare_reporting_aggregates

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
        from scripts.verify_helpers import compare_reporting_aggregates

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
        from scripts.verify_helpers import compare_reporting_aggregates

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
        from scripts.verify_helpers import compare_reporting_aggregates

        later = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=later)],
            [],
            self._window(),
        )
        assert mismatches == []

    def test_ignores_aggregate_outside_window_when_no_deliveries(self):
        from scripts.verify_helpers import compare_reporting_aggregates

        old = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        mismatches = compare_reporting_aggregates(
            [self._aggregate(delivery_id="d1", occurred_at=old)],
            [],
            self._window(),
        )
        assert mismatches == []


# ══════════════════════════════════════════════════════════════════════════════
#  AC 5: read-only — no write SQL anywhere in the script
# ══════════════════════════════════════════════════════════════════════════════


class TestReadOnlySql:
    """Acceptance criterion 5: the verification tool never modifies, deletes,
    or updates aggregate or delivery data — its SQL is SELECT-only."""

    _WRITE_RE = re.compile(
        r"\b(INSERT|UPDATE|DELETE|UPSERT|DROP|ALTER|TRUNCATE|CREATE|MERGE)\b",
        re.IGNORECASE,
    )

    def test_no_write_statements_in_reporting_sql(self):
        from scripts.verify_reporting_resource_aggregates import (
            REPORTING_AGGREGATES_SQL,
            REPORTING_DELIVERIES_SQL,
        )

        for name, sql in [
            ("REPORTING_AGGREGATES_SQL", REPORTING_AGGREGATES_SQL),
            ("REPORTING_DELIVERIES_SQL", REPORTING_DELIVERIES_SQL),
        ]:
            match = self._WRITE_RE.search(sql)
            assert match is None, f"{name} contains write keyword {match.group(0)!r}"

    def test_all_sql_starts_with_select(self):
        from scripts.verify_reporting_resource_aggregates import (
            REPORTING_AGGREGATES_SQL,
            REPORTING_DELIVERIES_SQL,
        )

        for name, sql in [
            ("REPORTING_AGGREGATES_SQL", REPORTING_AGGREGATES_SQL),
            ("REPORTING_DELIVERIES_SQL", REPORTING_DELIVERIES_SQL),
        ]:
            assert sql.lstrip().upper().startswith(("SELECT", "WITH")), name


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: exit codes
# ══════════════════════════════════════════════════════════════════════════════


class TestGroupMismatches:
    """Acceptance criterion 3: mismatches are grouped by
    ``(day, provider, repository)`` in a stable order."""

    def _mismatch(self, day, provider, repository):
        from scripts.verify_helpers import Mismatch
        return Mismatch(
            source="reporting_resource_aggregates",
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
    """Acceptance criterion 4: non-zero when mismatches exist, zero otherwise."""

    def test_zero_when_no_mismatches(self):
        from scripts.verify_helpers import exit_code
        assert exit_code([]) == 0

    def test_one_when_mismatches(self):
        from scripts.verify_helpers import Mismatch, exit_code
        mismatch = Mismatch(
            source="reporting_resource_aggregates",
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
        from scripts import verify_reporting_resource_aggregates as mod

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
            source="reporting_resource_aggregates",
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
        from scripts import verify_reporting_resource_aggregates as mod

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
#  Reporting-only verification: AFK mismatch cannot affect reporting exit code
# ══════════════════════════════════════════════════════════════════════════════


class TestReportingOnlyVerification:
    """Issue #729 acceptance: the reporting verifier checks ONLY reporting
    aggregate consistency.  An AFK rollup mismatch cannot fail this verifier."""

    async def test_run_verification_only_checks_reporting(self, monkeypatch):
        """_run_verification calls only _fetch_reporting_mismatches, not
        _fetch_afk_dashboard_mismatches."""
        from scripts import verify_reporting_resource_aggregates as mod

        reporting_mismatch = type("M", (), {"source": "reporting_resource_aggregates"})()
        reporting_called = []
        afk_called = []

        async def _fake_reporting(conn, window):
            reporting_called.append(True)
            return [reporting_mismatch]

        async def _fake_afk(conn, window):
            afk_called.append(True)
            return []

        monkeypatch.setattr(mod, "_fetch_reporting_mismatches", _fake_reporting)
        if hasattr(mod, "_fetch_afk_dashboard_mismatches"):
            monkeypatch.setattr(mod, "_fetch_afk_dashboard_mismatches", _fake_afk)

        conn = AsyncMock()
        window = mod.parse_window(from_date=date(2026, 9, 20), to_date=date(2026, 9, 21))
        result = await mod._run_verification(conn, window)

        assert reporting_called, "Reporting fetch should have been called"
        assert not afk_called, "AFK fetch should NOT be called by reporting verifier"
        assert len(result) == 1
        assert result[0].source == "reporting_resource_aggregates"
