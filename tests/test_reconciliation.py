"""Tests for ``app.core.reconciliation`` — canonical replay-merge delta computation.

Covers the three public entry points:

- ``compute_delta`` — per-field delta computation between an existing
  canonical ``usage_events`` row and incoming collector values
  (non-null collector values are authoritative; null/omitted values
  produce a zero delta so existing values are never erased).
- ``validate_no_negative_totals`` — clamps adjusted session totals so no
  negative token total is ever written.
- ``apply_replay_merge`` — applies the merge inside the caller's
  transaction, serialised by an advisory lock keyed on the event id,
  and adjusts session aggregates by the computed delta.

Uses the shared ``mock_row`` / ``mock_conn`` fixtures from
``tests/conftest.py`` — no real database required.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.reconciliation import (
    REPLAY_LOCK_CLASS,
    DeltaResult,
    IngestOutcome,
    apply_replay_merge,
    compute_delta,
    validate_no_negative_totals,
)
from tests.conftest import mock_row

# ── Helpers ────────────────────────────────────────────────────────────────


def _event(**overrides: object) -> dict:
    """Build a canonical ``usage_events`` row dict (old values)."""
    row: dict[str, object] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": 10,
        "reasoning_tokens": 5,
        "estimated_cost_usd": Decimal("1.25"),
        "session_id": _event_uuid(),
    }
    row.update(overrides)
    return row


def _values(**overrides: object) -> dict:
    """Build an incoming collector ``new_values`` dict."""
    values: dict[str, object] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": 10,
        "reasoning_tokens": 5,
        "estimated_cost_usd": Decimal("1.25"),
    }
    values.update(overrides)
    return values


def _session_row(**overrides: object) -> dict:
    """Build a ``sessions`` aggregate row dict."""
    row: dict[str, object] = {
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_cached_tokens": 200,
        "total_cache_read_tokens": 100,
        "total_cache_write_tokens": 100,
        "total_estimated_cost_usd": Decimal("12.50"),
    }
    row.update(overrides)
    return row


def _event_uuid() -> uuid.UUID:
    return uuid.UUID("12345678-1234-5678-1234-567812345678")


# ══════════════════════════════════════════════════════════════════════════
#  compute_delta
# ══════════════════════════════════════════════════════════════════════════


class TestComputeDelta:
    """Per-field delta computation between a canonical event and incoming values."""

    def test_identical_values_produce_all_zero_deltas(self):
        result = compute_delta(_event(), _values())

        assert isinstance(result, DeltaResult)
        for field in result.deltas:
            assert result.deltas[field] == 0, f"expected zero delta for {field}"
        assert result.token_adjustment == 0
        assert result.cost_adjustment == Decimal("0")

    def test_single_field_change(self):
        result = compute_delta(_event(), _values(input_tokens=120))

        assert result.deltas["input_tokens"] == 20
        assert result.deltas["output_tokens"] == 0
        assert result.deltas["cached_tokens"] == 0
        assert result.deltas["cache_read_tokens"] == 0
        assert result.deltas["cache_write_tokens"] == 0
        assert result.deltas["reasoning_tokens"] == 0
        assert result.deltas["estimated_cost_usd"] == 0
        assert result.old_values["input_tokens"] == 100
        assert result.new_values["input_tokens"] == 120
        assert result.token_adjustment == 20
        assert result.cost_adjustment == Decimal("0")

    def test_multi_field_change(self):
        result = compute_delta(
            _event(),
            _values(input_tokens=110, output_tokens=45, cache_read_tokens=15),
        )

        assert result.deltas["input_tokens"] == 10
        assert result.deltas["output_tokens"] == -5
        assert result.deltas["cache_read_tokens"] == 5
        assert result.token_adjustment == 10 - 5 + 5

    def test_null_collector_value_produces_zero_delta_and_never_erases(self):
        result = compute_delta(_event(), _values(input_tokens=None))

        assert result.deltas["input_tokens"] == 0
        assert result.new_values["input_tokens"] == 100
        assert result.old_values["input_tokens"] == 100

    def test_omitted_collector_field_is_treated_as_null(self):
        incoming = _values()
        del incoming["cache_read_tokens"]
        result = compute_delta(_event(), incoming)

        assert result.deltas["cache_read_tokens"] == 0
        assert result.new_values["cache_read_tokens"] == 10

    def test_non_null_incoming_value_is_authoritative(self):
        result = compute_delta(_event(), _values(cache_read_tokens=15))

        assert result.deltas["cache_read_tokens"] == 5
        assert result.new_values["cache_read_tokens"] == 15
        assert result.old_values["cache_read_tokens"] == 10

    def test_fills_absent_nullable_numeric_field(self):
        result = compute_delta(
            _event(cache_read_tokens=None, reasoning_tokens=None),
            _values(cache_read_tokens=7, reasoning_tokens=3),
        )

        assert result.deltas["cache_read_tokens"] == 7
        assert result.deltas["reasoning_tokens"] == 3
        assert result.new_values["cache_read_tokens"] == 7
        assert result.new_values["reasoning_tokens"] == 3

    def test_cost_delta(self):
        result = compute_delta(_event(), _values(estimated_cost_usd=Decimal("1.50")))

        assert result.deltas["estimated_cost_usd"] == Decimal("0.25")
        assert result.cost_adjustment == Decimal("0.25")
        assert result.old_values["estimated_cost_usd"] == Decimal("1.25")
        assert result.new_values["estimated_cost_usd"] == Decimal("1.50")

    def test_cost_fill_from_null(self):
        result = compute_delta(
            _event(estimated_cost_usd=None),
            _values(estimated_cost_usd=Decimal("0.50")),
        )

        assert result.deltas["estimated_cost_usd"] == Decimal("0.50")
        assert result.cost_adjustment == Decimal("0.50")

    def test_null_cost_produces_zero_cost_adjustment(self):
        result = compute_delta(_event(), _values(estimated_cost_usd=None))

        assert result.deltas["estimated_cost_usd"] == 0
        assert result.cost_adjustment == Decimal("0")
        assert result.new_values["estimated_cost_usd"] == Decimal("1.25")

    def test_token_adjustment_covers_session_aggregated_token_fields(self):
        # reasoning_tokens has no session aggregate column — it appears in
        # the per-field deltas but not in the session-facing token adjustment.
        result = compute_delta(_event(), _values(reasoning_tokens=9))

        assert result.deltas["reasoning_tokens"] == 4
        assert result.token_adjustment == 0


# ══════════════════════════════════════════════════════════════════════════
#  validate_no_negative_totals
# ══════════════════════════════════════════════════════════════════════════


class TestValidateNoNegativeTotals:
    """Clamping of adjusted session totals so no negative total is written."""

    def test_returns_true_when_all_totals_non_negative(self):
        adjusted = {"total_input_tokens": 100, "total_cached_tokens": 0}

        assert validate_no_negative_totals(_event_uuid(), adjusted) is True
        assert adjusted == {"total_input_tokens": 100, "total_cached_tokens": 0}

    def test_clamps_negative_token_total_to_zero(self):
        adjusted = {"total_input_tokens": -5, "total_output_tokens": 10}

        assert validate_no_negative_totals(_event_uuid(), adjusted) is True
        assert adjusted["total_input_tokens"] == 0
        assert adjusted["total_output_tokens"] == 10

    def test_clamps_all_negative_token_totals(self):
        adjusted = {
            "total_input_tokens": -1,
            "total_output_tokens": -2,
            "total_cached_tokens": -3,
            "total_cache_read_tokens": -4,
            "total_cache_write_tokens": -5,
        }

        assert validate_no_negative_totals(_event_uuid(), adjusted) is True
        assert adjusted == {key: 0 for key in adjusted}

    def test_clamps_negative_cost_to_zero(self):
        adjusted = {"total_estimated_cost_usd": Decimal("-0.10")}

        assert validate_no_negative_totals(_event_uuid(), adjusted) is True
        assert adjusted["total_estimated_cost_usd"] == Decimal("0")

    def test_returns_false_for_none_session(self):
        assert (
            validate_no_negative_totals(None, {"total_input_tokens": 5}) is False
        )


# ══════════════════════════════════════════════════════════════════════════
#  apply_replay_merge
# ══════════════════════════════════════════════════════════════════════════


class TestApplyReplayMerge:
    """Applying the canonical replay merge inside the caller's transaction."""

    async def test_acquires_advisory_lock_keyed_on_event_id(
        self, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(input_tokens=120)
        )

        assert outcome == IngestOutcome.UPDATED
        lock_call = mock_conn.fetchval.call_args
        assert lock_call.args[0] == "SELECT pg_advisory_xact_lock($1, $2)"
        assert lock_call.args[1] == REPLAY_LOCK_CLASS
        assert lock_call.args[2] == _event_uuid().int & 0xFFFFFFFF

    async def test_runs_within_callers_transaction(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        await apply_replay_merge(mock_conn, _event_uuid(), _values(input_tokens=120))

        mock_conn.transaction.assert_not_called()

    async def test_identical_replay_returns_duplicate_without_updates(
        self, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_event()))

        outcome = await apply_replay_merge(mock_conn, _event_uuid(), _values())

        assert outcome == IngestOutcome.DUPLICATE
        mock_conn.execute.assert_not_called()

    async def test_null_incoming_values_do_not_erase(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_event()))

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(input_tokens=None, cache_read_tokens=None)
        )

        assert outcome == IngestOutcome.DUPLICATE
        mock_conn.execute.assert_not_called()

    async def test_updates_event_field_and_adds_delta_to_session(
        self, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(input_tokens=120)
        )

        assert outcome == IngestOutcome.UPDATED

        event_sql, event_params = self._find_execute(mock_conn, "UPDATE usage_events")
        assert "input_tokens = $1" in event_sql
        assert event_params[0] == 120
        assert event_params[1] == _event_uuid()

        session_sql, session_params = self._find_execute(mock_conn, "UPDATE sessions")
        assert "total_input_tokens = $1" in session_sql
        assert session_params[0] == 1020  # 1000 current + 20 delta
        assert session_params[1] == _event_uuid()

    async def test_negative_delta_subtracts_from_session(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(output_tokens=40)
        )

        assert outcome == IngestOutcome.UPDATED
        _, session_params = self._find_execute(mock_conn, "UPDATE sessions")
        assert session_params[0] == 490  # 500 current - 10 delta

    async def test_clamps_session_total_to_zero(self, mock_conn: AsyncMock):
        # Current session total (5) is smaller than the negative delta (-10),
        # so the adjusted total (-5) must be clamped to zero.
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_event()),
                mock_row(_session_row(total_input_tokens=5)),
            ]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(input_tokens=90)
        )

        assert outcome == IngestOutcome.UPDATED
        _, session_params = self._find_execute(mock_conn, "UPDATE sessions")
        assert session_params[0] == 0

    async def test_multi_field_change_adjusts_each_session_column(
        self, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn,
            _event_uuid(),
            _values(input_tokens=110, output_tokens=45, cache_read_tokens=15),
        )

        assert outcome == IngestOutcome.UPDATED
        session_sql, session_params = self._find_execute(mock_conn, "UPDATE sessions")
        assert "total_input_tokens = $1" in session_sql
        assert "total_output_tokens = $2" in session_sql
        assert "total_cache_read_tokens = $3" in session_sql
        assert session_params[:3] == [1010, 495, 105]

    async def test_cost_delta_adjusts_session_cost(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(estimated_cost_usd=Decimal("1.50"))
        )

        assert outcome == IngestOutcome.UPDATED
        event_sql, event_params = self._find_execute(mock_conn, "UPDATE usage_events")
        assert "estimated_cost_usd = $1" in event_sql
        assert event_params[0] == Decimal("1.50")
        session_sql, session_params = self._find_execute(mock_conn, "UPDATE sessions")
        assert "total_estimated_cost_usd = $1" in session_sql
        assert session_params[0] == Decimal("12.75")

    async def test_reasoning_delta_updates_event_not_session(
        self, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), mock_row(_session_row())]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(reasoning_tokens=9)
        )

        assert outcome == IngestOutcome.UPDATED
        event_sql, event_params = self._find_execute(mock_conn, "UPDATE usage_events")
        assert "reasoning_tokens = $1" in event_sql
        assert event_params[0] == 9
        assert not any("UPDATE sessions" in c.args[0] for c in mock_conn.execute.call_args_list)

    async def test_missing_event_returns_rejected(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        outcome = await apply_replay_merge(mock_conn, _event_uuid(), _values())

        assert outcome == IngestOutcome.REJECTED
        mock_conn.execute.assert_not_called()

    async def test_missing_session_row_updates_event_only(self, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(
            side_effect=[mock_row(_event()), None]
        )

        outcome = await apply_replay_merge(
            mock_conn, _event_uuid(), _values(input_tokens=120)
        )

        assert outcome == IngestOutcome.UPDATED
        self._find_execute(mock_conn, "UPDATE usage_events")
        assert not any("UPDATE sessions" in c.args[0] for c in mock_conn.execute.call_args_list)

    def _find_execute(self, mock_conn: AsyncMock, sql_fragment: str) -> tuple[str, list]:
        for call in mock_conn.execute.call_args_list:
            sql = call.args[0]
            if sql_fragment in sql:
                return sql, list(call.args[1:])
        pytest.fail(f"no conn.execute call containing {sql_fragment!r}")
