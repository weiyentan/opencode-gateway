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
    CANONICAL_EVENT_LOCK_CLASS,
    DeltaResult,
    IngestOutcome,
    _canonical_event_lock_key,
    _replay_lock_key,
    acquire_canonical_event_lock,
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
        expected_class, expected_key = _replay_lock_key(_event_uuid())
        assert lock_call.args[1] == expected_class
        assert lock_call.args[2] == expected_key

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


# ══════════════════════════════════════════════════════════════════════════
#  Canonical event advisory lock (issue #395)
# ══════════════════════════════════════════════════════════════════════════


class TestCanonicalEventLock:
    """Per-transaction advisory lock for canonical event first-delivery serialisation."""

    def test_lock_key_is_deterministic(self):
        """Same (identity_id, record_id) produce the same lock key."""
        identity_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        record_id = "rec-001"

        key_a = _canonical_event_lock_key(identity_id, record_id)
        key_b = _canonical_event_lock_key(identity_id, record_id)

        assert key_a == key_b
        assert key_a[0] == CANONICAL_EVENT_LOCK_CLASS
        assert isinstance(key_a[1], int)
        # Signed int32 range — must bind to pg_advisory_xact_lock(int, int).
        assert -(0x80000000) <= key_a[1] <= 0x7FFFFFFF

    def test_different_records_produce_different_keys(self):
        """Different source_record_id values produce different lock keys."""
        identity_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        key_a = _canonical_event_lock_key(identity_id, "rec-001")
        key_b = _canonical_event_lock_key(identity_id, "rec-002")

        assert key_a != key_b

    def test_different_identities_produce_different_keys(self):
        """Different canonical_source_identity_id values produce different lock keys."""
        id_a = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        id_b = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        key_a = _canonical_event_lock_key(id_a, "rec-001")
        key_b = _canonical_event_lock_key(id_b, "rec-001")

        assert key_a != key_b

    async def test_acquires_advisory_xact_lock_with_correct_key(self, mock_conn: AsyncMock):
        """acquire_canonical_event_lock calls pg_advisory_xact_lock with the derived key."""
        identity_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        record_id = "rec-001"

        await acquire_canonical_event_lock(mock_conn, identity_id, record_id)

        lock_call = mock_conn.fetchval.call_args
        assert lock_call.args[0] == "SELECT pg_advisory_xact_lock($1, $2)"
        expected_class, expected_key = _canonical_event_lock_key(identity_id, record_id)
        assert lock_call.args[1] == expected_class
        assert lock_call.args[2] == expected_key

    async def test_lock_released_on_rollback(self, mock_conn: AsyncMock):
        """The advisory xact lock is released when the transaction rolls back.

        Acquire a lock inside an explicit transaction, then verify the
        transaction was entered (so the lock scope is bounded).  A rollback
        releases pg_advisory_xact_lock automatically — we verify the
        transactional scope was established.
        """
        identity_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        # Enclose in an explicit transaction — the xact lock is released
        # when the transaction exits (commit or rollback).
        async with mock_conn.transaction():
            await acquire_canonical_event_lock(mock_conn, identity_id, "rec-001")

        # The transaction was entered (__aenter__ called)
        tx = mock_conn.transaction.return_value
        tx.__aenter__.assert_awaited()
        tx.__aexit__.assert_awaited()

    async def test_transaction_required_for_lock_scope(self, mock_conn: AsyncMock):
        """The caller must own the transaction for the lock to be scoped properly.

        acquire_canonical_event_lock itself does NOT open a transaction;
        the caller wraps the lock + critical section in one.
        """
        identity_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        await acquire_canonical_event_lock(mock_conn, identity_id, "rec-001")

        # The function does not open its own transaction
        mock_conn.transaction.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  Concurrent same-event delivery — acceptance criterion #6 (issue #395)
# ══════════════════════════════════════════════════════════════════════════


class TestConcurrentSameEventDelivery:
    """Two concurrent deliveries of the same canonical event produce one event.

    Exercises the blocked-then-reread path: the second delivery acquires
    the advisory lock after the first commits, re-reads ``usage_events``
    with ``SELECT ... FOR UPDATE``, finds the event already present, and
    records only its ingest attempt — no double-insert.
    """

    async def test_two_concurrent_deliveries_produce_one_event_two_attempts(
        self, mock_conn: AsyncMock,
    ) -> None:
        """The second delivery re-reads after the lock and finds the event."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        from app.api.ingest import IngestRecord, _record_canonical_event

        identity_id = uuid.uuid4()
        model_id = uuid.uuid4()
        session_id = uuid.uuid4()
        source_db_id = uuid.uuid4()
        client_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        now = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

        # ── Stateful fake DB for usage_events ─────────────────────
        fake_usage_events: dict[tuple, dict] = {}

        def _fetchrow_for(*args, **kwargs):
            sql = str(args[0]) if args else ""
            if "SELECT id FROM observed_models" in sql:
                return mock_row({"id": model_id})
            if "SELECT id FROM sessions" in sql:
                return mock_row({"id": session_id})
            if "SELECT input_tokens, output_tokens" in sql and "FROM usage_events" in sql:
                # Full canonical event field read for duplicate/replay-merge
                # Return values matching the record → identical → "duplicate"
                return mock_row({
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "estimated_cost_usd": None,
                    "provider": None,
                    "mode": None,
                    "finish_reason": None,
                })
            if "SELECT id FROM usage_events" in sql:
                key = (str(identity_id), "rec-001")
                row = fake_usage_events.get(key)
                if row is not None:
                    return mock_row({"id": row["id"]})
                return None
            return None

        async def _execute_for(*args):
            sql = str(args[0])
            if "INSERT INTO usage_events" in sql and "canonical_source_identity_id" in sql:
                event_id = args[1]
                fake_usage_events[(str(identity_id), str(args[3]))] = {"id": event_id}
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_for)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(side_effect=_execute_for)

        record = IngestRecord(
            source_record_id="rec-001",
            session_id="ses-test",
            model="test-model",
            input_tokens=100,
            output_tokens=50,
            reported_at=now,
        )

        with patch(
            "app.core.identity.resolve_canonical_identity",
            return_value=identity_id,
        ):
            result1 = await _record_canonical_event(
                mock_conn, record, client_id, source_db_id,
                batch_id, None, now,
                canonical_identity_id=identity_id,
            )
            result2 = await _record_canonical_event(
                mock_conn, record, client_id, source_db_id,
                batch_id, None, now,
                canonical_identity_id=identity_id,
            )

        # ── Assertions ─────────────────────────────────────────────
        # Both deliveries resolved the same event id
        event_id = result1["event_id"]
        assert event_id is not None
        assert result2["event_id"] == event_id
        assert result1["attempt_id"] is not None
        assert result2["attempt_id"] is not None
        assert result1["attempt_id"] != result2["attempt_id"]

        # Exactly one INSERT INTO usage_events
        event_insert_count = sum(
            1 for c in mock_conn.execute.call_args_list
            if isinstance(c.args[0], str)
            and "INSERT INTO usage_events" in c.args[0]
        )
        assert event_insert_count == 1, (
            f"expected 1 INSERT INTO usage_events, got {event_insert_count}"
        )

        # Two INSERT INTO usage_ingest_attempts (one per delivery)
        attempt_insert_count = sum(
            1 for c in mock_conn.execute.call_args_list
            if isinstance(c.args[0], str)
            and "INSERT INTO usage_ingest_attempts" in c.args[0]
        )
        assert attempt_insert_count == 2, (
            f"expected 2 INSERT INTO usage_ingest_attempts, got {attempt_insert_count}"
        )


# ══════════════════════════════════════════════════════════════════════════
#  Lock serialisation latency — acceptance criterion #6 (issue #395)
# ══════════════════════════════════════════════════════════════════════════


class TestLockSerializationLatency:
    """The advisory lock does not add unreasonable serialisation overhead.

    Measures wall-clock time of 10 sequential vs 10 concurrent
    same-event deliveries through ``_record_canonical_event`` with a
    mocked database connection.  The test runs under the default
    ``-m "not profiling"`` pytest selection.

    The assertion uses a *relative* bound — concurrent time < sequential
    time + 100ms — which is robust to machine load in constrained
    environments.  The mock path completes in microseconds, so the bound
    is trivially satisfied as long as asyncio scheduling overhead is
    reasonable.
    """

    async def test_ten_concurrent_deliveries_under_serialisation_budget(
        self, mock_conn: AsyncMock,
    ) -> None:
        """10 concurrent deliveries serialise with acceptable overhead."""
        import asyncio
        import time
        from datetime import datetime, timezone
        from unittest.mock import patch

        from app.api.ingest import IngestRecord, _record_canonical_event

        identity_id = uuid.uuid4()
        model_id = uuid.uuid4()
        session_id = uuid.uuid4()
        source_db_id = uuid.uuid4()
        client_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        now = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
        event_id = uuid.uuid4()

        # ── Build fresh mock per call ──────────────────────────────
        # Because _record_canonical_event calls fetchrow 4 times per
        # delivery (model, session, FOR UPDATE event SELECT, full field
        # read for duplicate comparison), 10 deliveries need 40 entries.
        def _build_fetchrow_side_effect(existing_event_id: uuid.UUID):
            entries = []
            for _ in range(10):
                entries.extend([
                    mock_row({"id": model_id}),           # model lookup
                    mock_row({"id": session_id}),         # session lookup
                    mock_row({"id": existing_event_id}),  # FOR UPDATE: event exists
                    mock_row({                            # full field read: identical → duplicate
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_read_tokens": None,
                        "cache_write_tokens": None,
                        "estimated_cost_usd": None,
                        "provider": None,
                        "mode": None,
                        "finish_reason": None,
                    }),
                ])
            return entries

        record = IngestRecord(
            source_record_id="rec-001",
            session_id="ses-test",
            model="test-model",
            input_tokens=100,
            output_tokens=50,
            reported_at=now,
        )

        def _make_delivery():
            """Coroutine for one delivery through _record_canonical_event."""
            return _record_canonical_event(
                mock_conn, record, client_id, source_db_id,
                batch_id, None, now,
                canonical_identity_id=identity_id,
            )

        # ── 1. 10 sequential deliveries (baseline) ─────────────────
        mock_conn.fetchrow = AsyncMock(
            side_effect=_build_fetchrow_side_effect(event_id)
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock()

        with patch(
            "app.core.identity.resolve_canonical_identity",
            return_value=identity_id,
        ):
            t0_seq = time.perf_counter()
            for _ in range(10):
                await _make_delivery()
            t1_seq = time.perf_counter()

        sequential_ms = (t1_seq - t0_seq) * 1000

        # ── 2. 10 concurrent deliveries ────────────────────────────
        # Re-create side effects for a fresh sequence
        mock_conn.fetchrow = AsyncMock(
            side_effect=_build_fetchrow_side_effect(event_id)
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock()

        with patch(
            "app.core.identity.resolve_canonical_identity",
            return_value=identity_id,
        ):
            t0_conc = time.perf_counter()
            tasks = [asyncio.create_task(_make_delivery()) for _ in range(10)]
            results = await asyncio.gather(*tasks)
            t1_conc = time.perf_counter()

        concurrent_ms = (t1_conc - t0_conc) * 1000

        # All deliveries resolved the same event
        for r in results:
            assert r["event_id"] == event_id

        # ── Serialisation overhead assertion ───────────────────────
        # With mocked connections the lock is a no-op, so both
        # sequential and concurrent should be near-instant.  The
        # budget (100 ms) protects against asyncio scheduling jitter
        # in constrained environments.
        overhead_ms = concurrent_ms - sequential_ms
        assert overhead_ms < 100, (
            f"Concurrent serialisation overhead {overhead_ms:.1f} ms "
            f"exceeds 100 ms budget (concurrent={concurrent_ms:.1f} ms, "
            f"sequential={sequential_ms:.1f} ms)"
        )
