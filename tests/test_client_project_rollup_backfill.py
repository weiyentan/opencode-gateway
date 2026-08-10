# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the Client Project Rollup backfill script (issue #404).

Covers the acceptance criteria from the task contract:

1. The script recomputes rollup rows from ``usage_events`` with additive
   math identical to ingest-time maintenance (``app.core.reconciliation``
   :data:`ROLLUP_FIELDS` — input, output, cache read, cache write tokens
   plus estimated cost; ``cached_tokens``/``reasoning_tokens`` have no
   rollup column and must NOT be summed).
2. Verification mode flags disagreements between rollup and
   ``SUM(usage_events)`` per ``(client_id, project_id, day)`` — including
   rollup rows with no matching events and event groups with no rollup row.
3. Backfill↔live equivalence: backfilled totals equal live-maintained
   totals for the same window (replay-merge deltas telescope to the same
   sum as recomputing from final event values).
4. ``--dry-run`` and safe defaults, following the sibling script pattern
   (``scripts/backfill_cache_write_tokens.py``).

``usage_events`` remain the accounting truth (ADR 0015): the backfill
corrects the ROLLUP toward the event sums, never the reverse.

Tests follow the mock pattern from ``TestBackfillCacheWriteTokens`` in
``tests/test_usage.py`` (SQL-content assertions + AsyncMock connection).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.reconciliation import ROLLUP_FIELDS, compute_delta

# ══════════════════════════════════════════════════════════════════════════════
#  AC 1 + 2: Verification query — recompute source and disagreement flags
# ══════════════════════════════════════════════════════════════════════════════


class TestVerificationQuery:
    """Acceptance criterion 2: verification flags rollup vs SUM(usage_events)
    disagreements per (client_id, project_id, day)."""

    def _verification_query(self) -> str:
        from scripts.backfill_client_project_rollup import VERIFICATION_QUERY
        return VERIFICATION_QUERY

    def test_verification_query_is_defined(self):
        """The VERIFICATION_QUERY constant is a non-empty string joining
        client_project_rollup against usage_events."""
        sql = self._verification_query()
        assert sql
        assert "client_project_rollup" in sql
        assert "usage_events" in sql
        assert "FULL OUTER JOIN" in sql.upper()

    def test_verification_query_keys_on_client_project_day(self):
        """Disagreements are keyed per (client_id, project_id, day)."""
        sql = self._verification_query()
        assert "ON r.client_id = g.client_id" in sql
        assert "AND r.project_id = g.project_id" in sql
        assert "AND r.day = g.day" in sql

    def test_verification_query_compares_all_five_rollup_fields(self):
        """All five additive rollup columns must be compared on both sides."""
        sql = self._verification_query()
        for field in ROLLUP_FIELDS:
            assert f"rollup_{field}" in sql, (
                f"verification must expose the rollup side of {field}"
            )
            assert f"event_{field}" in sql, (
                f"verification must expose the SUM(usage_events) side of {field}"
            )

    def test_verification_query_flags_missing_rows_on_either_side(self):
        """A rollup row with no matching events, or events with no rollup
        row, must count as a disagreement."""
        sql = self._verification_query()
        assert "r.client_id IS NULL" in sql
        assert "g.client_id IS NULL" in sql

    def test_verification_query_excludes_null_project_events(self):
        """Events with NULL project_id cannot be keyed in the rollup (PK all
        NOT NULL) and must be excluded from the event sums."""
        sql = self._verification_query()
        assert "project_id IS NOT NULL" in sql

    def test_verification_query_uses_utc_day_bucketing(self):
        """The day bucket is the UTC date of reported_at, matching the
        ingest-time ``_rollup_day`` derivation."""
        sql = self._verification_query()
        assert "AT TIME ZONE 'UTC'" in sql
        assert "::date" in sql

    def test_verification_query_sums_only_additive_fields(self):
        """The event-sum side must sum exactly the ROLLUP_FIELDS — never
        cached_tokens or reasoning_tokens (they have no rollup column)."""
        sql = self._verification_query()
        summed = {
            m
            for m in __import__("re").findall(r"SUM\(ue\.(\w+)\)", sql)
        }
        assert sorted(summed) == sorted(ROLLUP_FIELDS), (
            f"summed fields {sorted(summed)} != ROLLUP_FIELDS {sorted(ROLLUP_FIELDS)}"
        )

    def test_verification_query_orders_by_key(self):
        """Mismatch rows are ordered by (client_id, project_id, day)."""
        sql = self._verification_query()
        assert "ORDER BY client_id, project_id, day" in sql


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: Backfill UPSERT — recompute with the same additive math
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillUpdateSql:
    """Acceptance criterion 1: the backfill recomputes rollup rows from
    usage_events with additive math identical to ingest-time maintenance."""

    def _backfill_sql(self, limit: int | None = None) -> str:
        from scripts.backfill_client_project_rollup import _backfill_sql
        return _backfill_sql(limit)

    def test_backfill_update_sql_is_defined(self):
        """The BACKFILL_UPDATE_SQL constant is a non-empty upsert."""
        from scripts.backfill_client_project_rollup import BACKFILL_UPDATE_SQL
        assert BACKFILL_UPDATE_SQL
        assert "INSERT INTO client_project_rollup" in BACKFILL_UPDATE_SQL

    def test_backfill_upserts_on_conflict(self):
        """Missing rollup rows are inserted; existing rows are overwritten
        with the recomputed values (INSERT ... ON CONFLICT DO UPDATE)."""
        sql = self._backfill_sql()
        assert "ON CONFLICT (client_id, project_id, day)" in sql
        assert "DO UPDATE SET" in sql

    def test_backfill_only_corrects_disagreeing_groups(self):
        """The upsert is restricted to groups whose rollup row disagrees
        with SUM(usage_events) (LEFT JOIN + mismatch predicates) — no-op
        rewrites of already-correct rows never happen, and a --limit run
        spends its budget on genuine corrections only."""
        sql = self._backfill_sql()
        assert "LEFT JOIN client_project_rollup r" in sql
        assert "r.client_id IS NULL" in sql
        for field in ROLLUP_FIELDS:
            assert f"r.{field} != g.{field}" in sql

    def test_backfill_corrects_rollup_toward_events(self):
        """Each rollup column is overwritten with EXCLUDED (the recomputed
        SUM) — the rollup is corrected toward usage_events, never the
        reverse (ADR 0015)."""
        sql = self._backfill_sql()
        for field in ROLLUP_FIELDS:
            assert f"{field} = EXCLUDED.{field}" in sql, (
                f"backfill must overwrite rollup {field} with the recomputed sum"
            )

    def test_backfill_sums_only_additive_fields(self):
        """The grouped subquery sums exactly the ROLLUP_FIELDS — cached_tokens
        and reasoning_tokens must never appear."""
        sql = self._backfill_sql()
        summed = {
            m
            for m in __import__("re").findall(r"SUM\(ue\.(\w+)\)", sql)
        }
        assert sorted(summed) == sorted(ROLLUP_FIELDS), (
            f"summed fields {sorted(summed)} != ROLLUP_FIELDS {sorted(ROLLUP_FIELDS)}"
        )
        assert "cached_tokens" not in summed
        assert "reasoning_tokens" not in summed

    def test_backfill_uses_utc_day_bucketing(self):
        """The backfill's day bucket must be the UTC date of reported_at."""
        sql = self._backfill_sql()
        assert "AT TIME ZONE 'UTC'" in sql
        assert "::date" in sql

    def test_backfill_excludes_null_project_events(self):
        """Events with NULL project_id cannot be keyed in the rollup."""
        sql = self._backfill_sql()
        assert "project_id IS NOT NULL" in sql

    def test_backfill_default_no_limit(self):
        """Without --limit the executed backfill SQL is the plain upsert
        (safe default: correct every disagreeing group)."""
        sql = self._backfill_sql()
        assert "INSERT INTO client_project_rollup" in sql
        assert "LIMIT" not in sql.upper()

    def test_backfill_with_limit_orders_and_bounds(self):
        """--limit N bounds the upsert to the first N disagreeing groups
        ordered by (client_id, project_id, day) for phased backfill."""
        sql = self._backfill_sql(limit=10)
        assert "ORDER BY g.client_id, g.project_id, g.day" in sql
        assert "LIMIT $1" in sql

    def test_backfill_sql_rejects_negative_limit(self):
        """A limit below 1 is rejected rather than producing a nonsensical
        partial backfill."""
        from scripts.backfill_client_project_rollup import _backfill_sql
        with pytest.raises(ValueError):
            _backfill_sql(limit=0)
        with pytest.raises(ValueError):
            _backfill_sql(limit=-3)


# ══════════════════════════════════════════════════════════════════════════════
#  Stale rollup rows — no backing events
# ══════════════════════════════════════════════════════════════════════════════


class TestStaleRowDeletionSql:
    """A rollup row whose (client_id, project_id, day) has NO backing
    usage_events group cannot be recomputed — it is corrected by deletion
    (the rollup is derived from usage_events; no events, no row)."""

    def test_stale_row_delete_sql_is_defined(self):
        from scripts.backfill_client_project_rollup import STALE_ROLLUP_DELETE_SQL
        assert STALE_ROLLUP_DELETE_SQL
        assert "DELETE FROM client_project_rollup" in STALE_ROLLUP_DELETE_SQL
        assert "NOT EXISTS" in STALE_ROLLUP_DELETE_SQL
        assert "usage_events" in STALE_ROLLUP_DELETE_SQL

    def test_stale_row_delete_matches_utc_day_and_null_project(self):
        """The delete must match the same day bucketing and NULL-project
        exclusion as the recompute."""
        from scripts.backfill_client_project_rollup import STALE_ROLLUP_DELETE_SQL
        assert "AT TIME ZONE 'UTC'" in STALE_ROLLUP_DELETE_SQL
        assert "project_id IS NOT NULL" in STALE_ROLLUP_DELETE_SQL

    @pytest.mark.asyncio
    async def test_delete_stale_rows_returns_count(self):
        from scripts.backfill_client_project_rollup import _delete_stale_rows
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 2")
        result = await _delete_stale_rows(mock_conn)
        assert result == 2


# ══════════════════════════════════════════════════════════════════════════════
#  Mismatch count query
# ══════════════════════════════════════════════════════════════════════════════


class TestMismatchCountSql:
    """The count query must wrap the same disagreement join as verification."""

    def test_mismatch_count_sql_is_defined(self):
        from scripts.backfill_client_project_rollup import MISMATCH_COUNT_SQL
        assert MISMATCH_COUNT_SQL
        assert "COUNT" in MISMATCH_COUNT_SQL.upper()
        assert "client_project_rollup" in MISMATCH_COUNT_SQL
        assert "usage_events" in MISMATCH_COUNT_SQL


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: --dry-run and safe defaults (arg parsing)
# ══════════════════════════════════════════════════════════════════════════════


class TestArgParsing:
    """The script follows the sibling pattern: --dry-run previews, --limit
    bounds a phased run, and the default is the safe verification-first flow."""

    def test_dry_run_default_false(self):
        from scripts.backfill_client_project_rollup import _parse_args
        args = _parse_args([])
        assert args.dry_run is False

    def test_dry_run_flag_true(self):
        from scripts.backfill_client_project_rollup import _parse_args
        args = _parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_limit_default_none(self):
        from scripts.backfill_client_project_rollup import _parse_args
        args = _parse_args([])
        assert args.limit is None

    def test_limit_flag_value(self):
        from scripts.backfill_client_project_rollup import _parse_args
        args = _parse_args(["--limit", "500"])
        assert args.limit == 500


# ══════════════════════════════════════════════════════════════════════════════
#  Helper behaviour against a mock connection
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillHelpers:
    """Mock-connection behaviour of the script's helper functions."""

    @pytest.mark.asyncio
    async def test_count_mismatches_returns_int(self):
        from scripts.backfill_client_project_rollup import _count_mismatches
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"cnt": 5})
        result = await _count_mismatches(mock_conn)
        assert result == 5
        call_sql = mock_conn.fetchrow.call_args[0][0]
        assert "COUNT" in call_sql

    @pytest.mark.asyncio
    async def test_count_mismatches_none_returns_zero(self):
        from scripts.backfill_client_project_rollup import _count_mismatches
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        result = await _count_mismatches(mock_conn)
        assert result == 0

    @pytest.mark.asyncio
    async def test_run_verification_returns_list(self):
        from scripts.backfill_client_project_rollup import _run_verification
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {
                "client_id": uuid.uuid4(),
                "project_id": "proj-a",
                "day": date(2025, 7, 16),
                "rollup_input_tokens": 0,
                "event_input_tokens": 5,
            },
        ])
        result = await _run_verification(mock_conn)
        assert len(result) == 1
        assert result[0]["rollup_input_tokens"] == 0
        assert result[0]["event_input_tokens"] == 5

    @pytest.mark.asyncio
    async def test_run_backfill_returns_upsert_count(self):
        """An INSERT ... ON CONFLICT tag ("INSERT 0 N") yields N."""
        from scripts.backfill_client_project_rollup import _run_backfill
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 7")
        result = await _run_backfill(mock_conn)
        assert result == 7

    @pytest.mark.asyncio
    async def test_run_backfill_zero_updated(self):
        from scripts.backfill_client_project_rollup import _run_backfill
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 0")
        result = await _run_backfill(mock_conn)
        assert result == 0

    @pytest.mark.asyncio
    async def test_run_backfill_passes_limit_param(self):
        """A --limit run binds the limit as the $1 parameter."""
        from scripts.backfill_client_project_rollup import _run_backfill
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 3")
        result = await _run_backfill(mock_conn, limit=10)
        assert result == 3
        sql, param = mock_conn.execute.call_args[0]
        assert "LIMIT $1" in sql
        assert param == 10


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Backfill↔live equivalence
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillLiveEquivalence:
    """Acceptance criterion 3: backfilled totals equal live-maintained totals
    for the same window.

    Simulates the two accumulation paths over the same canonical events:

    - **Live**: ingest-time maintenance — a full increment for each first
      insert, then per-field deltas for replay-merge corrections
      (``compute_delta`` + :data:`ROLLUP_FIELDS`, the exact math
      ``apply_replay_merge`` applies to the rollup).
    - **Backfill**: recompute from the final event values by summing the
      same :data:`ROLLUP_FIELDS` (the script's grouped ``SUM``).

    The replay deltas telescope: live total = sum of final event values,
    so the two paths must agree.  The day bucketing must also match the
    ingest-time ``_rollup_day`` derivation (UTC date of ``reported_at``).
    """

    # ── helpers ─────────────────────────────────────────────────────────

    def _final_event_value(self, initial: dict, updates: list[dict]) -> dict:
        """Apply replay-merge semantics to one event: non-null incoming
        values are authoritative, null/omitted incoming keeps the stored
        value (no erasure)."""
        final = dict(initial)
        for incoming in updates:
            for field, value in incoming.items():
                if value is not None:
                    final[field] = value
        return final

    def _live_rollup_totals(
        self,
        events: list[dict],
        updates: list[list[dict]],
    ) -> dict:
        """Simulate ingest-time live maintenance: full increments for first
        inserts, then per-field ROLLUP_FIELDS deltas for replay merges."""
        from app.core.reconciliation import _rollup_day  # ingest day derivation

        totals: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": Decimal("0"),
        }
        for event in events:
            for field in ROLLUP_FIELDS:
                value = event.get(field)
                if field == "estimated_cost_usd":
                    totals[field] += Decimal(str(value or 0))
                else:
                    totals[field] += int(value or 0)
        for event, event_updates in zip(events, updates):
            for incoming in event_updates:
                delta = compute_delta(event, incoming).deltas
                for field in ROLLUP_FIELDS:
                    if field == "estimated_cost_usd":
                        totals[field] += Decimal(str(delta[field]))
                    else:
                        totals[field] += int(delta[field])
        # Day bucketing: every event must be UTC-bucketable by the same
        # derivation the ingest-time maintenance applies (_rollup_day).
        for event in events:
            _rollup_day(event["reported_at"])
        return totals

    def _backfill_totals(self, events: list[dict]) -> dict:
        """Simulate the script's recompute: SUM of the final event values
        over the ROLLUP_FIELDS, grouped by the UTC day of reported_at."""
        totals: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": Decimal("0"),
        }
        for event in events:
            final = self._final_event_value(event, [])
            for field in ROLLUP_FIELDS:
                value = final.get(field)
                if field == "estimated_cost_usd":
                    totals[field] += Decimal(str(value or 0))
                else:
                    totals[field] += int(value or 0)
        return totals

    # ── tests ───────────────────────────────────────────────────────────

    def test_backfill_totals_equal_live_totals_after_replays(self):
        """Two events for the same (client, project, day), one corrected by
        a replay merge with mixed-sign deltas: the backfilled SUM must equal
        the live-maintained total."""
        events = [
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_tokens": 10,
                "estimated_cost_usd": Decimal("0.0035"),
                "reported_at": datetime(2025, 7, 16, 12, 0, tzinfo=timezone.utc),
            },
            {
                "input_tokens": 30,
                "output_tokens": 20,
                "cache_read_tokens": None,  # never observed → treated as 0
                "cache_write_tokens": 5,
                "estimated_cost_usd": None,  # never observed → 0 cost
                "reported_at": datetime(2025, 7, 16, 18, 0, tzinfo=timezone.utc),
            },
        ]
        updates = [
            [  # replay correction on event 0: input +100, output -20, cost +0.0015
                {
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "cached_tokens": 0,
                    "cache_read_tokens": 25,
                    "cache_write_tokens": 10,
                    "reasoning_tokens": 0,
                    "estimated_cost_usd": Decimal("0.0050"),
                },
            ],
            [],  # event 1 never replayed
        ]

        live = self._live_rollup_totals(events, updates)
        final_events = [
            self._final_event_value(ev, ups) for ev, ups in zip(events, updates)
        ]
        backfilled = self._backfill_totals(final_events)

        for field in ROLLUP_FIELDS:
            assert backfilled[field] == live[field], (
                f"backfill {field}={backfilled[field]} != live {field}={live[field]}"
            )

    def test_utc_day_bucketing_matches_ingest_time(self):
        """Events near a UTC day boundary bucket into the same UTC day by
        the ingest-time ``_rollup_day`` derivation, and the backfill SQL
        expresses the same UTC day bucketing."""
        from app.core.reconciliation import _rollup_day
        from scripts.backfill_client_project_rollup import (
            BACKFILL_UPDATE_SQL,
            VERIFICATION_QUERY,
        )

        tz_plus_two = timezone(timedelta(hours=2))
        # 23:30 +02:00 → 21:30 UTC on the same day
        late = datetime(2025, 7, 16, 23, 30, tzinfo=tz_plus_two)
        # 01:00 +02:00 → 23:00 UTC on the PREVIOUS day
        early = datetime(2025, 7, 17, 1, 0, tzinfo=tz_plus_two)

        assert _rollup_day(late) == date(2025, 7, 16)
        assert _rollup_day(early) == date(2025, 7, 16)
        assert "AT TIME ZONE 'UTC'" in VERIFICATION_QUERY
        assert "AT TIME ZONE 'UTC'" in BACKFILL_UPDATE_SQL
