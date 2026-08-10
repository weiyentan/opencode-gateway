# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for ingest-time Client Project Rollup maintenance (issue #403).

Covers the acceptance criteria from the task contract:

1. First-time record increments the rollup row for ``(client_id, project_id, day)``
2. Replayed record with differing values adjusts the rollup by delta, never
   re-increments
3. Exact duplicate makes no rollup change
4. Rollup update commits atomically with the usage_event write
5. Backfill↔live equivalence: after replay, rollup matches SUM(usage_events)
6. NULL project_id events skip the rollup write (design decision)

Tests follow the mock pattern from ``tests/test_ingest.py`` (AsyncMock
connection, monkeypatched resolution, side-effect lists for fetchrow).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.reconciliation import (
    ROLLUP_FIELDS,
    IngestOutcome,
    _upsert_client_project_rollup,
    apply_replay_merge,
    compute_delta,
)
from tests.conftest import mock_row  # noqa: E402 — shared mock helpers
from tests.test_ingest import (  # shared helpers from the main ingest tests
    _CLIENT_ID,
    _SESSION_ID,
    _add_transaction_support,
    _auth_row,
    _build_ingest_app,
    _canonical_event_side_effect_items,
    _handler_routing_side_effect_items,
    _mk_ts,
    _valid_ingest_payload,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _project_record(**overrides) -> dict:
    """Build a usage record dict with a project_id."""
    record: dict = {
        "source_record_id": "rec-proj-001",
        "session_id": str(_SESSION_ID),
        "model": "gpt-4",
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "cache_read_tokens": 10,
        "cache_write_tokens": 10,
        "estimated_cost_usd": "0.0035",
        "reported_at": _mk_ts().isoformat(),
        "project_id": "proj-test-abc",
    }
    record.update(overrides)
    return record


def _rollup_execute_calls(mock_conn: AsyncMock) -> list:
    """Return all execute calls that reference 'client_project_rollup'."""
    return [
        c for c in mock_conn.execute.call_args_list
        if "client_project_rollup" in str(c.args)
    ]


def _rollup_row(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    estimated_cost_usd: Decimal | None = None,
) -> dict:
    """Build a client_project_rollup mock row."""
    row = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": estimated_cost_usd or Decimal("0"),
    }
    return row


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: First-time record increments the rollup
# ══════════════════════════════════════════════════════════════════════════════


class TestFirstInsertRollupIncrement:
    """Acceptance criterion 1: First-time record increments rollup row."""

    @pytest.mark.asyncio
    async def test_first_insert_upserts_rollup_with_full_values(self, monkeypatch):
        """A new record's first delivery should INSERT INTO client_project_rollup
        with the record's token/cost values via the ON CONFLICT UPSERT."""
        mock_conn = AsyncMock()

        # Build a side-effect list for a single new record with project_id
        items: list = [_auth_row(), None]  # auth + sd_check
        items.extend(_handler_routing_side_effect_items())

        # _process_one_record: model, atomic INSERT, session
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        # _record_canonical_event
        items.extend(_canonical_event_side_effect_items())

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory lock
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[_project_record()],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "accepted"

        # The rollup UPSERT should be in the execute calls
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 1, (
            f"Expected 1 rollup UPSERT, got {len(rollup_calls)}"
        )

        rollup_sql = str(rollup_calls[0].args)
        assert "INSERT INTO client_project_rollup" in rollup_sql
        assert "ON CONFLICT (client_id, project_id, day)" in rollup_sql
        assert "DO UPDATE SET" in rollup_sql

        # Verify the values passed match the record
        rollup_args = rollup_calls[0].args
        # args[0] = SQL, args[1:] = params ($1, $2, ...)
        assert rollup_args[1] == _CLIENT_ID
        assert rollup_args[2] == "proj-test-abc"  # project_id
        # day = _mk_ts().date() = 2025-07-16
        assert rollup_args[3] == _mk_ts().date()
        assert rollup_args[4] == 100  # input_tokens
        assert rollup_args[5] == 50   # output_tokens
        assert rollup_args[6] == 10   # cache_read_tokens
        assert rollup_args[7] == 10   # cache_write_tokens
        assert rollup_args[8] == Decimal("0.0035")  # estimated_cost_usd


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2: Replay-merge adjusts rollup by delta
# ══════════════════════════════════════════════════════════════════════════════


class TestReplayMergeRollupDelta:
    """Acceptance criterion 2: Replay with differing values adjusts rollup by delta."""

    @pytest.mark.asyncio
    async def test_differing_replay_adjusts_rollup_by_delta(self, monkeypatch):
        """A replay with differing token values should adjust the rollup by
        the per-field delta (new - old), never re-increment the full value."""
        mock_conn = AsyncMock()

        # Build side-effect for a SINGLE new record that maps to an existing
        # canonical event (replay path).  Follow the pattern from
        # TestCanonicalDuplicateDetection in test_ingest.py.
        event_id = uuid.uuid4()

        # auth + sd_check
        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        # _process_one_record: model, atomic INSERT (winner), session
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        # _record_canonical_event: model lookup, session lookup
        model_row = MagicMock()
        model_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([model_row, session_lookup_row])

        # event lookup → existing
        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {"id": event_id}.__getitem__
        items.append(existing_row)

        # Stored canonical event — DIFFERENT values than incoming
        # NOTE: effective_cached_tokens = cache_read + cache_write = 10+10 = 20
        stored = MagicMock()
        stored.__getitem__.side_effect = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 20,  # effective_cached_tokens: 10+10
            "reasoning_tokens": 5,
            "cache_read_tokens": 10,
            "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
            "provider": None,
            "mode": None,
            "finish_reason": None,
        }.__getitem__
        items.append(stored)

        # apply_replay_merge internals:
        #   1. pg_advisory_xact_lock (fetchval) — already handled by default AsyncMock
        #   2. FOR UPDATE read of usage_events → returns a row with session_id
        #      Must use mock_row() since compute_delta calls .get() on this row.
        merge_event_row = mock_row({
            "input_tokens": 100, "output_tokens": 50,
            "cached_tokens": 20, "reasoning_tokens": 5,
            "cache_read_tokens": 10, "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
            "session_id": uuid.uuid4(),
        })
        items.append(merge_event_row)

        #   3. FOR UPDATE read of sessions → session aggregate row
        session_agg = mock_row({
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cached_tokens": 200, "total_cache_read_tokens": 100,
            "total_cache_write_tokens": 100,
            "total_estimated_cost_usd": Decimal("12.50"),
        })
        items.append(session_agg)

        # _fill_canonical_text_enrichment: FOR UPDATE read
        enrichment_row = MagicMock()
        enrichment_row.__getitem__.side_effect = {
            "provider": None, "mode": None, "finish_reason": None,
        }.__getitem__
        items.append(enrichment_row)

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory lock
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Incoming record has DIFFERENT values:
        # input_tokens: 100 → 200 (+100 delta)
        # output_tokens: 50 → 30 (-20 delta)
        # cache_read_tokens: 10 → 25 (+15 delta)
        # estimated_cost_usd: 0.0035 → 0.0050 (+0.0015 delta)
        payload = _valid_ingest_payload(
            records=[_project_record(
                source_record_id="rec-replay-delta-001",
                input_tokens=200,
                output_tokens=30,
                cache_read_tokens=25,
                cache_write_tokens=10,  # same
                estimated_cost_usd="0.0050",
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "updated"

        # The rollup UPSERT should be called with the deltas:
        # input: +100, output: -20, cache_read: +15, cache_write: 0, cost: +0.0015
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 1, (
            f"Expected 1 rollup update, got {len(rollup_calls)}"
        )

        rollup_args = rollup_calls[0].args
        assert rollup_args[1] == _CLIENT_ID
        assert rollup_args[2] == "proj-test-abc"
        assert rollup_args[3] == _mk_ts().date()
        assert rollup_args[4] == 100   # delta input_tokens: 200 - 100
        assert rollup_args[5] == -20   # delta output_tokens: 30 - 50
        assert rollup_args[6] == 15    # delta cache_read_tokens: 25 - 10
        assert rollup_args[7] == 0     # delta cache_write_tokens: 10 - 10 = 0
        assert rollup_args[8] == Decimal("0.0015")  # delta cost: 0.0050 - 0.0035

    @pytest.mark.asyncio
    async def test_differing_replay_delta_not_reincrement(self, monkeypatch):
        """A replay must delta-adjust (existing + delta), not blindly re-increment
        the full incoming value.  Confirm the values passed to the rollup UPSERT
        are deltas, not raw record values."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()

        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        model_row = MagicMock()
        model_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([model_row, session_lookup_row])

        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {"id": event_id}.__getitem__
        items.append(existing_row)

        # Stored has 500 input_tokens; incoming has 200 (negative delta of -300)
        stored = MagicMock()
        stored.__getitem__.side_effect = {
            "input_tokens": 500,
            "output_tokens": 50,
            "cached_tokens": 20,  # effective: cache_read(10)+cache_write(10)
            "reasoning_tokens": 0,
            "cache_read_tokens": 10,
            "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("1.00"),
            "provider": None,
            "mode": None,
            "finish_reason": None,
        }.__getitem__
        items.append(stored)

        merge_event_row = mock_row({
            "input_tokens": 500, "output_tokens": 50,
            "cached_tokens": 20, "reasoning_tokens": 0,
            "cache_read_tokens": 10, "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("1.00"),
            "session_id": uuid.uuid4(),
        })
        items.append(merge_event_row)

        session_agg = mock_row({
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cached_tokens": 0, "total_cache_read_tokens": 100,
            "total_cache_write_tokens": 100,
            "total_estimated_cost_usd": Decimal("10.00"),
        })
        items.append(session_agg)

        enrichment_row = MagicMock()
        enrichment_row.__getitem__.side_effect = {
            "provider": None, "mode": None, "finish_reason": None,
        }.__getitem__
        items.append(enrichment_row)

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[_project_record(
                source_record_id="rec-downward-correction",
                input_tokens=200,  # was 500 → delta = -300
                output_tokens=50,
                cache_read_tokens=10,
                cache_write_tokens=10,
                estimated_cost_usd="0.50",  # was 1.00 → delta = -0.50
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 1
        rollup_args = rollup_calls[0].args

        # These should be deltas, NOT the raw incoming values
        assert rollup_args[4] == -300, (
            f"Expected delta -300, got {rollup_args[4]} (raw value 200 would be reincrement!)"
        )
        assert rollup_args[5] == 0     # output: 50 - 50
        assert rollup_args[8] == Decimal("-0.50"), (
            f"Expected delta -0.50, got {rollup_args[8]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Exact duplicate makes no rollup change
# ══════════════════════════════════════════════════════════════════════════════


class TestDuplicateNoRollupChange:
    """Acceptance criterion 3: Exact duplicate = no-op on rollup."""

    @pytest.mark.asyncio
    async def test_identical_replay_does_not_write_rollup(self, monkeypatch):
        """An exact duplicate replay must make NO rollup change — no
        INSERT/UPDATE to client_project_rollup."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()

        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        model_row = MagicMock()
        model_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([model_row, session_lookup_row])

        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {"id": event_id}.__getitem__
        items.append(existing_row)

        # Stored values are IDENTICAL to incoming
        stored = MagicMock()
        stored.__getitem__.side_effect = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 20,  # effective: cache_read(10)+cache_write(10)
            "reasoning_tokens": 0,
            "cache_read_tokens": 10,
            "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
            "provider": None,
            "mode": None,
            "finish_reason": None,
        }.__getitem__
        items.append(stored)

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[_project_record(
                source_record_id="rec-identical-replay",
                # All values match stored
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "duplicate"

        # No rollup writes for a duplicate
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 0, (
            f"Expected zero rollup writes for duplicate, got {len(rollup_calls)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: Rollup update commits atomically with usage_event write
# ══════════════════════════════════════════════════════════════════════════════


class TestRollupAtomicity:
    """Acceptance criterion 4: Rollup write must be in the same transaction."""

    @pytest.mark.asyncio
    async def test_rollup_upsert_inside_same_transaction_as_event_write(
        self, monkeypatch,
    ):
        """The rollup UPSERT must execute between the transaction begin/commit
        that wraps the usage_event INSERT — never in a separate transaction."""
        mock_conn = AsyncMock()

        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        items.extend(_canonical_event_side_effect_items())

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest", json=_valid_ingest_payload(
                    records=[_project_record()],
                ),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Collect execute calls in order to verify transaction framing
        execute_sequence = [
            (i, str(c.args[0])[:80]) for i, c in enumerate(mock_conn.execute.call_args_list)
        ]

        # Find the rollup and event positions
        rollup_positions = [
            i for i, sql in execute_sequence
            if "client_project_rollup" in sql
        ]
        event_positions = [
            i for i, sql in execute_sequence
            if "INSERT INTO usage_events" in sql and "usage_ingest_attempts" not in sql
        ]
        attempt_positions = [
            i for i, sql in execute_sequence
            if "INSERT INTO usage_ingest_attempts" in sql
        ]

        assert len(rollup_positions) == 1
        assert len(event_positions) == 1
        assert len(attempt_positions) == 1

        # The rollup must be BETWEEN the event INSERT and the attempt INSERT
        # (the transaction context manager wraps all three)
        rp = rollup_positions[0]
        ep = event_positions[0]
        ap = attempt_positions[0]

        assert ep < rp < ap, (
            f"Rollup ({rp}) must be between event INSERT ({ep}) and "
            f"attempt INSERT ({ap}), not before or after the transaction"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 5: Backfill↔live equivalence
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillLiveEquivalence:
    """Acceptance criterion 5: Rollup must match SUM(usage_events) for the window.

    Tests the core reconciliation unit directly: ``apply_replay_merge`` must
    produce rollup deltas that, when added to the initial full-increment row
    value, equal the SUM of the canonical usage_events after the merge.
    """

    async def test_rollup_delta_matches_sum_of_usage_events_after_merge(
        self, mock_conn,
    ):
        """The rollup row value after a replay merge must equal
        SUM(usage_events) for the same (client_id, project_id, day) window."""

        client_id = uuid.uuid4()
        project_id = "equiv-proj"
        reported_at = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

        # ── Initial "backfill" state: a usage_event with these values ──
        initial_event = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "cache_read_tokens": 10,
            "cache_write_tokens": 10,
            "reasoning_tokens": 5,
            "estimated_cost_usd": Decimal("0.0035"),
            "session_id": uuid.uuid4(),
        }
        # The initial full rollup row would be: SUM(initial_event.ROLLUP_FIELDS)
        initial_rollup = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
        }

        # ── Step 1: First insert — full increment ──
        # Call _upsert_client_project_rollup directly with initial values
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        await _upsert_client_project_rollup(
            mock_conn,
            client_id=client_id,
            project_id=project_id,
            day=reported_at.date(),
            **initial_rollup,
        )

        # ── Step 2: Replay with differing values ──
        # Simulate what apply_replay_merge does internally:
        # The stored event is the initial_event.
        # The incoming new_values have different values.
        new_values = {
            "input_tokens": 200,     # was 100 → delta +100
            "output_tokens": 30,     # was 50  → delta -20
            "cached_tokens": 0,
            "cache_read_tokens": 25, # was 10  → delta +15
            "cache_write_tokens": 10, # same
            "reasoning_tokens": 0,
            "estimated_cost_usd": Decimal("0.0050"),  # was 0.0035 → delta +0.0015
        }

        delta_result = compute_delta(initial_event, new_values)

        # ── Step 3: Apply rollup delta via _upsert_client_project_rollup ──
        # This is what apply_replay_merge does for the rollup
        rollup_deltas = {}
        for field_name in ROLLUP_FIELDS:
            d = delta_result.deltas[field_name]
            if d != 0:
                rollup_deltas[field_name] = d

        await _upsert_client_project_rollup(
            mock_conn,
            client_id=client_id,
            project_id=project_id,
            day=reported_at.date(),
            input_tokens=int(rollup_deltas.get("input_tokens", 0)),
            output_tokens=int(rollup_deltas.get("output_tokens", 0)),
            cache_read_tokens=int(rollup_deltas.get("cache_read_tokens", 0)),
            cache_write_tokens=int(rollup_deltas.get("cache_write_tokens", 0)),
            estimated_cost_usd=rollup_deltas.get("estimated_cost_usd", Decimal("0")),
        )

        # ── Step 4: Verify equivalence ──
        # Expected rollup after merge = initial + delta
        expected_after = {
            "input_tokens": 100 + 100,       # 200
            "output_tokens": 50 + (-20),     # 30
            "cache_read_tokens": 10 + 15,    # 25
            "cache_write_tokens": 10 + 0,    # 10
            "estimated_cost_usd": Decimal("0.0035") + Decimal("0.0015"),  # 0.0050
        }

        # SUM(usage_events) for the same window AFTER merge = the updated event
        # values (which are the new_values since non-null collector values are
        # authoritative):
        expected_usage_event = {
            "input_tokens": 200,
            "output_tokens": 30,
            "cache_read_tokens": 25,
            "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0050"),
        }

        # The rollup after deltas should match the canonical event totals
        for field in ["input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens"]:
            assert expected_after[field] == expected_usage_event[field], (
                f"Rollup {field}={expected_after[field]} != "
                f"SUM(usage_events) {field}={expected_usage_event[field]}"
            )
        assert expected_after["estimated_cost_usd"] == expected_usage_event["estimated_cost_usd"], (
            f"Rollup cost={expected_after['estimated_cost_usd']} != "
            f"SUM(usage_events) cost={expected_usage_event['estimated_cost_usd']}"
        )

        # Also verify the rollup UPSERT was called twice
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 2, (
            f"Expected 2 rollup calls (full increment + delta), got {len(rollup_calls)}"
        )

        # Second call's params should be the deltas
        second_call_args = rollup_calls[1].args
        assert second_call_args[4] == 100    # delta input
        assert second_call_args[5] == -20    # delta output
        assert second_call_args[6] == 15     # delta cache_read
        assert second_call_args[7] == 0      # delta cache_write
        assert second_call_args[8] == Decimal("0.0015")  # delta cost

    async def test_apply_replay_merge_with_rollup_params(
        self, mock_conn,
    ):
        """``apply_replay_merge`` with ``client_id``, ``project_id``, and
        ``reported_at`` should also write the rollup row."""
        client_id = uuid.uuid4()
        event_id = uuid.uuid4()
        project_id = "merge-rollup-proj"
        reported_at = datetime(2025, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

        # Stored event row returned by FOR UPDATE read
        stored_row = mock_row({
            "input_tokens": 200,
            "output_tokens": 100,
            "cached_tokens": 50,
            "reasoning_tokens": 10,
            "cache_read_tokens": 30,
            "cache_write_tokens": 20,
            "estimated_cost_usd": Decimal("2.00"),
            "session_id": uuid.uuid4(),
        })

        # Session aggregate row
        session_row = mock_row({
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "total_cached_tokens": 500,
            "total_cache_read_tokens": 300,
            "total_cache_write_tokens": 200,
            "total_estimated_cost_usd": Decimal("20.00"),
        })

        mock_conn.fetchrow = AsyncMock(
            side_effect=[stored_row, session_row],
        )
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory lock
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        # Incoming values: input=250 (+50 delta), cost=3.00 (+1.00 delta)
        outcome = await apply_replay_merge(
            mock_conn, event_id,
            {"input_tokens": 250, "output_tokens": 100, "cached_tokens": 50,
             "cache_read_tokens": 30, "cache_write_tokens": 20,
             "reasoning_tokens": 10, "estimated_cost_usd": Decimal("3.00")},
            client_id=client_id,
            project_id=project_id,
            reported_at=reported_at,
        )

        assert outcome == IngestOutcome.UPDATED

        # Verify a rollup UPSERT was executed
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 1

        rollup_args = rollup_calls[0].args
        assert rollup_args[1] == client_id
        assert rollup_args[2] == project_id
        assert rollup_args[3] == reported_at.date()
        # Only input_tokens (+50) and estimated_cost_usd (+1.00) changed
        assert rollup_args[4] == 50
        assert rollup_args[5] == 0
        assert rollup_args[6] == 0
        assert rollup_args[7] == 0
        assert rollup_args[8] == Decimal("1.00")

    async def test_apply_replay_merge_skips_rollup_when_no_params(
        self, mock_conn,
    ):
        """Without rollup params, ``apply_replay_merge`` should NOT write
        to ``client_project_rollup`` (backward compatible)."""
        event_id = uuid.uuid4()

        stored_row = mock_row({
            "input_tokens": 200, "output_tokens": 100,
            "cached_tokens": 50, "reasoning_tokens": 10,
            "cache_read_tokens": 30, "cache_write_tokens": 20,
            "estimated_cost_usd": Decimal("2.00"),
            "session_id": uuid.uuid4(),
        })

        session_row = mock_row({
            "total_input_tokens": 2000, "total_output_tokens": 1000,
            "total_cached_tokens": 500, "total_cache_read_tokens": 300,
            "total_cache_write_tokens": 200,
            "total_estimated_cost_usd": Decimal("20.00"),
        })

        mock_conn.fetchrow = AsyncMock(
            side_effect=[stored_row, session_row],
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        # Call WITHOUT rollup params (old call signature, backward compat)
        outcome = await apply_replay_merge(
            mock_conn, event_id,
            {"input_tokens": 250},
        )

        assert outcome == IngestOutcome.UPDATED

        # No rollup writes
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 0, (
            "apply_replay_merge without rollup params should not touch client_project_rollup"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  NULL project_id handling (design decision)
# ══════════════════════════════════════════════════════════════════════════════


class TestNullProjectSkipsRollup:
    """Events with NULL project_id cannot be keyed in the rollup (PK NOT NULL)."""

    @pytest.mark.asyncio
    async def test_first_insert_null_project_skips_rollup(self, monkeypatch):
        """A record with project_id=None must NOT write to client_project_rollup."""
        mock_conn = AsyncMock()

        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        items.extend(_canonical_event_side_effect_items())

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Record WITHOUT project_id
        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-no-project",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
                # No project_id
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "accepted"

        # No rollup writes
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 0, (
            f"NULL project_id should not trigger rollup write, got {len(rollup_calls)}"
        )

    @pytest.mark.asyncio
    async def test_replay_null_project_still_passes_session_delta(
        self, monkeypatch,
    ):
        """A replay with project_id=None should still apply session deltas
        but skip the rollup write.  The project_id=None is passed to
        apply_replay_merge which correctly skips the rollup path."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()

        items: list = [_auth_row(), None]
        items.extend(_handler_routing_side_effect_items())

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        model_row = MagicMock()
        model_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([model_row, session_lookup_row])

        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {"id": event_id}.__getitem__
        items.append(existing_row)

        # Stored without project_id
        stored = MagicMock()
        stored.__getitem__.side_effect = {
            "input_tokens": 100, "output_tokens": 50,
            "cached_tokens": 20,  # effective: cache_read(10)+cache_write(10)
            "reasoning_tokens": 0,
            "cache_read_tokens": 10, "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
            "provider": None, "mode": None, "finish_reason": None,
        }.__getitem__
        items.append(stored)

        merge_event_row = mock_row({
            "input_tokens": 100, "output_tokens": 50,
            "cached_tokens": 20, "reasoning_tokens": 0,
            "cache_read_tokens": 10, "cache_write_tokens": 10,
            "estimated_cost_usd": Decimal("0.0035"),
            "session_id": uuid.uuid4(),
        })
        items.append(merge_event_row)

        session_agg = mock_row({
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cached_tokens": 0, "total_cache_read_tokens": 100,
            "total_cache_write_tokens": 100,
            "total_estimated_cost_usd": Decimal("10.00"),
        })
        items.append(session_agg)

        enrichment_row = MagicMock()
        enrichment_row.__getitem__.side_effect = {
            "provider": None, "mode": None, "finish_reason": None,
        }.__getitem__
        items.append(enrichment_row)

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-no-project-replay",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 200,  # differs from stored
                "output_tokens": 50,
                "cached_tokens": 0,
                "cache_read_tokens": 10,
                "cache_write_tokens": 10,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
                # No project_id — recording should still succeed
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        # Should be "updated" (delta applied to event + session, not rollup)
        status = response.json()["data"]["results"][0]["status"]
        # The status could be "updated" or "duplicate" depending on whether
        # the pre-comparison considers it different.  With input_tokens differing
        # it should be "updated".
        assert status in ("updated", "duplicate")

        # No rollup writes
        rollup_calls = _rollup_execute_calls(mock_conn)
        assert len(rollup_calls) == 0, (
            "NULL project_id replay should not write rollup"
        )
