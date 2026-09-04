"""End-to-end regression tests for replay cost correction behavior.

Issue #647 — Exercises the public ingest behavior and accounting effects
across new records, duplicate deliveries, cost corrections, immutable
conflicts, quarantine, audit records, aggregate updates, idempotency,
and concurrency.

These tests drive ``POST /ingest`` end-to-end through the same mock
connection harness as ``tests/test_ingest.py``, following the patterns of
``TestCanonicalDuplicateDetection`` (canonical-layer duplicate/update
classification) and ``tests/test_client_project_rollup_ingest.py``
(rollup delta assertions).  They verify the *observable* behavior of the
cost-correction feature:

- A replay whose ONLY changed field is a non-NULL ``estimated_cost_usd``
  is no longer a bare legacy ``conflict`` — the ingest handler routes it
  through canonical accounting (issue #646), returning ``updated`` after
  the cost Replay Merge applies ``incoming − stored`` to the canonical
  event, the owning session aggregate, and the client project rollup.
- Equivalent Decimal representations (``0.0035`` vs ``0.00350``) are
  idempotent duplicates under exact normalized monetary equality.
- A NULL incoming cost never erases a stored cost and never fires the
  cost-only marker (a NULL can only erase, never authoritatively update).
- Token, model, or session changes stay immutable ``conflict`` outcomes —
  the narrow cost-only authority must never silently broaden.
- Quarantined records never touch accounting, regardless of cost.
- Repeated identical deliveries apply the correction only once.

All tests use the shared helpers imported from ``tests.test_ingest``
(``_build_ingest_app``, ``_auth_row``, ``_add_transaction_support``,
``_valid_ingest_payload``, ``_canonical_event_side_effect_items``,
``_handler_routing_side_effect_items``) and the ``mock_row`` helper from
``tests.conftest``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import mock_row
from tests.test_ingest import (
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

# ── Shared constants (mirror tests/test_ingest.py) ─────────────────────────

_MERGED_SESSION_ID = uuid.uuid4()      # internal session for the merged event
_MERGED_MODEL_ID = uuid.uuid4()        # internal observed model id for the event


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _record_payload(**overrides) -> dict:
    """Build a single usage-record dict (defaults match the stored event)."""
    record: dict = {
        "source_record_id": "rec-cost-001",
        "session_id": str(_SESSION_ID),
        "model": "gpt-4",
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "estimated_cost_usd": "0.0035",
        "reported_at": _mk_ts().isoformat(),
    }
    record.update(overrides)
    return record


def _full_event_mock(*, cost: Decimal | None = Decimal("0.0035")) -> MagicMock:
    """Return a canonical-event fetchrow mock whose values match
    ``_record_payload()`` defaults except for ``estimated_cost_usd``."""
    values: dict = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "estimated_cost_usd": cost,
        "provider": None,
        "mode": None,
        "finish_reason": None,
        "session_id": _MERGED_SESSION_ID,
        "model_id": _MERGED_MODEL_ID,
    }
    row = MagicMock()
    row.__getitem__.side_effect = values.__getitem__
    return row


def _merge_event_row(
    *,
    cost: Decimal | None = Decimal("0.0035"),
    client_id: uuid.UUID = _CLIENT_ID,
    project_id: str | None = None,
) -> MagicMock:
    """Return a ``usage_events`` row for ``apply_replay_merge``'s FOR UPDATE
    read.  ``compute_delta`` calls ``.get()`` on this row, so it must use
    ``mock_row``.  The row's stored cost defaults to the canonical event's
    first-delivery cost (the *old* value the delta is computed against)."""
    row: dict = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "estimated_cost_usd": cost,
        "session_id": _MERGED_SESSION_ID,
        "client_id": client_id,
        "project_id": project_id,
        "reported_at": _mk_ts(),
    }
    return mock_row(row)


def _session_aggregate_row(
    *,
    total_cost: Decimal = Decimal("0.0035"),
) -> MagicMock:
    """Return the ``sessions`` aggregate row read by ``apply_replay_merge``."""
    return mock_row({
        "total_input_tokens": 100,
        "total_output_tokens": 50,
        "total_cached_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_estimated_cost_usd": total_cost,
    })


def _enrichment_read_mock() -> MagicMock:
    """Return the enrichment FOR UPDATE row (all NULL — no COALESCE fill)."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "provider": None,
        "mode": None,
        "finish_reason": None,
    }.__getitem__
    return row


def _cost_only_fetchrow_items(
    *,
    stored_cost: Decimal | None = Decimal("0.0035"),
    legacy_cost: Decimal | None = None,
    model_id: uuid.UUID = _MERGED_MODEL_ID,
    session_id: uuid.UUID = _MERGED_SESSION_ID,
    with_rollup: bool = False,
    total_cost: Decimal = Decimal("0.0035"),
) -> list:
    """Build the full ``fetchrow.side_effect`` list for ONE legacy-loser
    replay that is routed through canonical cost correction (issue #646).

    Sequence consumed by the ingest handler for a single record:

    auth, sd-check,
    handler cross-identity check,
    model upsert, atomic INSERT loser (None), legacy dedup row (exists),
    _record_canonical_event: model lookup, session lookup,
      FOR UPDATE event lookup (exists) → full event field read,
    apply_replay_merge: FOR UPDATE usage_events row, FOR UPDATE sessions row,
    _fill_canonical_text_enrichment: FOR UPDATE enrichment read.

    The stored canonical event and the merge event row share ``stored_cost``
    (the first-delivery cost); the incoming record carries the corrected
    cost, so the merge delta is ``incoming − stored_cost``.

    ``legacy_cost`` defaults to ``stored_cost`` and feeds the LEGACY dedup
    row — it can diverge from the canonical ``stored_cost`` to model a
    legacy row whose first-write-wins cost differs from an already-corrected
    canonical event (the idempotent re-delivery scenario).
    """
    if legacy_cost is None:
        legacy_cost = stored_cost
    items: list = [_auth_row(), None]                       # auth + sd_check
    items.extend(_handler_routing_side_effect_items())      # cross-identity

    # _process_one_record: model upsert, atomic INSERT (loser → None),
    # legacy dedup query returning a row that exists.
    model_upsert_row = MagicMock()
    model_upsert_row.__getitem__.side_effect = {"id": model_id}.__getitem__
    legacy_dedup_row = MagicMock()
    legacy_dedup_row.__getitem__.side_effect = {
        "id": uuid.uuid4(),
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 0,
        "estimated_cost_usd": legacy_cost,
    }.__getitem__
    items.extend([model_upsert_row, None, legacy_dedup_row])

    # _record_canonical_event: model lookup, session lookup
    model_row = MagicMock()
    model_row.__getitem__.side_effect = {"id": model_id}.__getitem__
    session_lookup_row = MagicMock()
    session_lookup_row.__getitem__.side_effect = {"id": session_id}.__getitem__
    items.extend([model_row, session_lookup_row])

    # FOR UPDATE event lookup → event exists; full field read for the
    # cost-only comparison gate.
    event_exists_row = MagicMock()
    event_exists_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    items.extend([event_exists_row, _full_event_mock(cost=stored_cost)])

    # apply_replay_merge internals
    items.append(_merge_event_row(cost=stored_cost,
                                  project_id="proj-test-abc" if with_rollup else None))
    items.append(_session_aggregate_row(total_cost=total_cost))

    # _fill_canonical_text_enrichment FOR UPDATE read
    items.append(_enrichment_read_mock())

    return items


def _winner_existing_canonical_fetchrow_items(
    *,
    cost: Decimal | None = Decimal("0.0035"),
) -> list:
    """Build the full ``fetchrow.side_effect`` list for a record that WINS
    the legacy insert but whose canonical event already exists (the
    ``TestCanonicalDuplicateDetection`` pattern).

    The canonical event's full field read drives the comparison: identical
    → ``duplicate``; cost-only difference → the canonical cost merge.

    Sequence: auth, sd-check, handler cross-identity check,
    ``_process_one_record`` winner (model upsert, atomic INSERT winner,
    session resolution), then ``_record_canonical_event``: model lookup,
    session lookup, FOR UPDATE event lookup (exists), full-event read.
    """
    items: list = [_auth_row(), None]                       # auth + sd_check
    items.extend(_handler_routing_side_effect_items())      # cross-identity

    model_upsert_row = MagicMock()
    model_upsert_row.__getitem__.side_effect = {"id": _MERGED_MODEL_ID}.__getitem__
    insert_row = MagicMock()
    insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    session_row = MagicMock()
    session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    items.extend([model_upsert_row, insert_row, session_row])

    # _record_canonical_event: model lookup, session lookup
    model_row = MagicMock()
    model_row.__getitem__.side_effect = {"id": _MERGED_MODEL_ID}.__getitem__
    session_lookup_row = MagicMock()
    session_lookup_row.__getitem__.side_effect = {"id": _MERGED_SESSION_ID}.__getitem__
    items.extend([model_row, session_lookup_row])

    # Canonical event exists (FOR UPDATE), then full field read.
    event_exists_row = MagicMock()
    event_exists_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    items.extend([event_exists_row, _full_event_mock(cost=cost)])

    return items


def _find_execute_calls(mock_conn: AsyncMock, sql_fragment: str) -> list:
    """Return every ``conn.execute`` call whose SQL contains ``sql_fragment``."""
    return [
        c for c in mock_conn.execute.call_args_list
        if isinstance(c.args[0], str) and sql_fragment in c.args[0]
    ]


def _attempt_outcomes(mock_conn: AsyncMock) -> list[str]:
    """Return the outcome strings recorded in ``usage_ingest_attempts``."""
    outcomes: list[str] = []
    for call in _find_execute_calls(mock_conn, "INSERT INTO usage_ingest_attempts"):
        # call.args[0] = SQL; params are args[1..]:
        # (id, usage_event_id, source_identity_id, record_id, jsonb,
        #  ingest_batch_id, outcome, replay_id, delivered_at)
        outcomes.append(str(call.args[7]))
    return outcomes


# ══════════════════════════════════════════════════════════════════════════
#  AC 1 — New record creation
# ══════════════════════════════════════════════════════════════════════════


class TestNewRecordAccounting:
    """A new Usage Record creates one canonical event and one accounting
    increment."""

    @pytest.mark.asyncio
    async def test_new_record_creates_one_event_and_full_increment(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        items: list = [_auth_row(), None]                   # auth + sd_check
        items.extend(_handler_routing_side_effect_items())  # cross-identity
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])
        items.extend(_canonical_event_side_effect_items())

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory lock
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(
                    records=[_record_payload(project_id="proj-test-abc")],
                ),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "accepted"

        # Exactly one canonical event INSERT.
        event_inserts = _find_execute_calls(mock_conn, "INSERT INTO usage_events")
        assert len(event_inserts) == 1
        # The event INSERT carries the incoming full cost.
        assert event_inserts[0].args[13] == Decimal("0.0035")

        # One rollup UPSERT with the FULL first-insert values (not a delta).
        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        rollup_args = rollup_calls[0].args
        assert rollup_args[1] == _CLIENT_ID
        assert rollup_args[2] == "proj-test-abc"
        assert rollup_args[8] == Decimal("0.0035")

        # Session aggregate increment happened during _resolve_session:
        # one INSERT INTO sessions ... ON CONFLICT (winner side effect).
        session_upserts = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(c.args[0])
        ]
        assert len(session_upserts) == 1

        # One audit attempt with outcome "accepted".
        assert _attempt_outcomes(mock_conn) == ["accepted"]


# ══════════════════════════════════════════════════════════════════════════
#  AC 2–3 — Identical replay & cost-increase correction
# ══════════════════════════════════════════════════════════════════════════


class TestIdenticalReplayNoAccountingChange:
    """An identical replay returns ``duplicate`` and makes no accounting
    change."""

    @pytest.mark.asyncio
    async def test_identical_replay_returns_duplicate_with_no_event_update(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_winner_existing_canonical_fetchrow_items(),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload()]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "duplicate"

        # No UPDATE on usage_events, no rollup write, no session write.
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0

        # The duplicate attempt is still audited.
        assert _attempt_outcomes(mock_conn) == ["duplicate"]


class TestCostIncreaseCorrection:
    """A replay raising the cost returns ``updated`` and applies a positive
    delta to the event, the session aggregate, and the rollup."""

    @pytest.mark.asyncio
    async def test_cost_increase_applies_positive_delta(self, monkeypatch) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.0035"),
                with_rollup=True,
                total_cost=Decimal("0.0035"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory locks
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Stored 0.0035, incoming 0.0100 → delta +0.0065
        payload = _valid_ingest_payload(
            records=[_record_payload(
                source_record_id="rec-cost-up",
                estimated_cost_usd="0.0100",
                project_id="proj-test-abc",
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated", (
            f"Expected 'updated' for a cost-only increase replay, "
            f"got '{result['status']}' (reason={result['reason']})"
        )

        # The event UPDATE carries only the corrected cost.
        event_updates = _find_execute_calls(mock_conn, "UPDATE usage_events")
        cost_set_updates = [
            c for c in event_updates
            if "estimated_cost_usd" in c.args[0] and "SET last_ingested_at" not in c.args[0]
        ]
        assert len(cost_set_updates) == 1
        assert "estimated_cost_usd = $1" in cost_set_updates[0].args[0]
        assert cost_set_updates[0].args[1] == Decimal("0.0100")

        # Session aggregate moved by the positive delta: 0.0035 + 0.0065.
        session_updates = _find_execute_calls(mock_conn, "UPDATE sessions")
        assert len(session_updates) == 1
        assert "total_estimated_cost_usd = $1" in session_updates[0].args[0]
        assert session_updates[0].args[1] == Decimal("0.0100")

        # Rollup moved by the SAME positive delta (not a full re-increment).
        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        rollup_args = rollup_calls[0].args
        assert rollup_args[8] == Decimal("0.0065")

        # Audit: one attempt recorded with "updated".
        assert _attempt_outcomes(mock_conn) == ["updated"]

    @pytest.mark.asyncio
    async def test_cost_increase_without_project_skips_rollup(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(stored_cost=Decimal("0.0035")),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[
                    _record_payload(estimated_cost_usd="0.0100"),
                ]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "updated"
        # No project_id → no rollup key → no rollup write.
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0


# ══════════════════════════════════════════════════════════════════════════
#  AC 4 — Cost decrease
# ══════════════════════════════════════════════════════════════════════════


class TestCostDecreaseCorrection:
    """A replay lowering the cost returns ``updated`` and applies a negative
    delta."""

    @pytest.mark.asyncio
    async def test_cost_decrease_applies_negative_delta(self, monkeypatch) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.0100"),
                with_rollup=True,
                total_cost=Decimal("0.0100"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Stored 0.0100, incoming 0.0035 → delta −0.0065
        payload = _valid_ingest_payload(
            records=[_record_payload(
                source_record_id="rec-cost-down",
                estimated_cost_usd="0.0035",
                project_id="proj-test-abc",
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated", (
            f"Expected 'updated' for a cost-only decrease replay, "
            f"got '{result['status']}'"
        )

        # Event corrected down to 0.0035.
        cost_set_updates = [
            c for c in _find_execute_calls(mock_conn, "UPDATE usage_events")
            if "estimated_cost_usd" in c.args[0] and "SET last_ingested_at" not in c.args[0]
        ]
        assert len(cost_set_updates) == 1
        assert cost_set_updates[0].args[1] == Decimal("0.0035")

        # Session aggregate: 0.0100 − 0.0065 = 0.0035.
        session_updates = _find_execute_calls(mock_conn, "UPDATE sessions")
        assert len(session_updates) == 1
        assert session_updates[0].args[1] == Decimal("0.0035")

        # Rollup receives the NEGATIVE delta −0.0065.
        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        assert rollup_calls[0].args[8] == Decimal("-0.0065")

        assert _attempt_outcomes(mock_conn) == ["updated"]


# ══════════════════════════════════════════════════════════════════════════
#  AC 5 — NULL-to-nonzero cost correction
# ══════════════════════════════════════════════════════════════════════════


class TestNullStoredCostCorrection:
    """A stored NULL cost corrected by a non-NULL incoming cost applies the
    full incoming value as the delta (old NULL treated as zero)."""

    @pytest.mark.asyncio
    async def test_null_stored_cost_backfilled_by_non_null_incoming(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=None,
                with_rollup=True,
                total_cost=Decimal("0"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Stored NULL, incoming 0.0075 → delta +0.0075 (NULL treated as zero).
        payload = _valid_ingest_payload(
            records=[_record_payload(
                source_record_id="rec-null-to-cost",
                estimated_cost_usd="0.0075",
                project_id="proj-test-abc",
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated"

        # Event corrected to the full incoming value.
        cost_set_updates = [
            c for c in _find_execute_calls(mock_conn, "UPDATE usage_events")
            if "estimated_cost_usd" in c.args[0] and "SET last_ingested_at" not in c.args[0]
        ]
        assert len(cost_set_updates) == 1
        assert cost_set_updates[0].args[1] == Decimal("0.0075")

        # Session aggregate delta = 0.0075 − 0 = 0.0075.
        session_updates = _find_execute_calls(mock_conn, "UPDATE sessions")
        assert len(session_updates) == 1
        assert session_updates[0].args[1] == Decimal("0.0075")

        # Rollup delta = full incoming value.
        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        assert rollup_calls[0].args[8] == Decimal("0.0075")


# ══════════════════════════════════════════════════════════════════════════
#  AC 6 — Nonzero-to-NULL cost (no erasure)
# ══════════════════════════════════════════════════════════════════════════


class TestNonNullStoredCostNullIncomingNoErasure:
    """An incoming NULL cost does not erase a known stored cost and never
    fires the cost-only marker — it stays a legacy conflict (a NULL can
    only erase, never authoritatively update)."""

    @pytest.mark.asyncio
    async def test_null_incoming_cost_does_not_erase_and_stays_conflict(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        items: list = [_auth_row(), None]                   # auth + sd_check
        items.extend(_handler_routing_side_effect_items())  # cross-identity

        model_upsert_row = MagicMock()
        model_upsert_row.__getitem__.side_effect = {"id": _MERGED_MODEL_ID}.__getitem__
        legacy_dedup_row = MagicMock()
        legacy_dedup_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        items.extend([model_upsert_row, None, legacy_dedup_row])

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Incoming: cost omitted entirely (None).  Tokens identical to stored.
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    source_record_id="rec-null-incoming",
                    estimated_cost_usd=None,
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "conflict"
        # NOT the cost-only marker — a NULL incoming cost can only erase and
        # therefore never fires cost-only routing; stays a legacy conflict.
        assert "cost-only" not in (result["reason"] or "").lower()

        # No canonical correction, no event/session/rollup write.
        assert len(_find_execute_calls(mock_conn, "INSERT INTO usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0

        # The legacy conflict is recorded in the per-record audit table
        # (a plain legacy conflict never reaches the canonical attempt layer).
        audit_inserts = _find_execute_calls(mock_conn, "INSERT INTO ingest_audit")
        assert len(audit_inserts) == 1
        assert audit_inserts[0].args[3] == "conflict"
        assert "Divergent duplicate" in str(audit_inserts[0].args[4])


# ══════════════════════════════════════════════════════════════════════════
#  AC 7 — Sub-$0.0001 cost delta
# ══════════════════════════════════════════════════════════════════════════


class TestSubTenthMilliCostDelta:
    """A cost delta smaller than $0.0001 is a real monetary difference and
    is covered as ``updated`` under exact monetary comparison."""

    @pytest.mark.asyncio
    async def test_tiny_cost_delta_is_updated_not_duplicate(self, monkeypatch) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.00350"),
                with_rollup=True,
                total_cost=Decimal("0.00350"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Stored 0.00350, incoming 0.00351 → delta +0.00001 (< $0.0001).
        payload = _valid_ingest_payload(
            records=[_record_payload(
                source_record_id="rec-tiny-delta",
                estimated_cost_usd="0.00351",
                project_id="proj-test-abc",
            )],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated", (
            f"Expected 'updated' for a sub-$0.0001 cost delta, "
            f"got '{result['status']}'"
        )

        cost_set_updates = [
            c for c in _find_execute_calls(mock_conn, "UPDATE usage_events")
            if "estimated_cost_usd" in c.args[0] and "SET last_ingested_at" not in c.args[0]
        ]
        assert len(cost_set_updates) == 1
        assert cost_set_updates[0].args[1] == Decimal("0.00351")

        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        assert rollup_calls[0].args[8] == Decimal("0.00001")

        assert _attempt_outcomes(mock_conn) == ["updated"]


# ══════════════════════════════════════════════════════════════════════════
#  AC 8 — Equivalent Decimal representations
# ══════════════════════════════════════════════════════════════════════════


class TestEquivalentDecimalRepresentation:
    """``0.0035`` vs ``0.00350`` are the same normalized monetary value — a
    replay carrying the equivalent representation is an idempotent
    duplicate."""

    @pytest.mark.asyncio
    async def test_equivalent_decimal_representation_is_duplicate(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        # Winner-exists canonical pattern: the record wins the legacy insert
        # (or is an idempotent duplicate) and the canonical event already
        # exists with the equivalent stored cost.
        mock_conn.fetchrow = AsyncMock(
            side_effect=_winner_existing_canonical_fetchrow_items(
                cost=Decimal("0.00350"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Incoming carries "0.0035" (string), stored is Decimal("0.00350").
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    source_record_id="rec-equiv-decimal",
                    estimated_cost_usd="0.0035",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "duplicate", (
            f"Expected 'duplicate' for equivalent Decimal representation, "
            f"got '{result['status']}'"
        )

        # No accounting change at all.
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0


# ══════════════════════════════════════════════════════════════════════════
#  AC 9–10 — Token / model changes stay immutable conflicts
# ══════════════════════════════════════════════════════════════════════════


class TestTokenModelChangeStaysConflict:
    """A token or model change on a replay is NOT a cost correction — it
    stays an immutable ``conflict``.  The narrow cost-only authority must
    never silently broaden into a token/model/session correction."""

    @pytest.mark.asyncio
    async def test_token_change_returns_plain_conflict(self, monkeypatch) -> None:
        mock_conn = AsyncMock()
        items: list = [_auth_row(), None]                   # auth + sd_check
        items.extend(_handler_routing_side_effect_items())  # cross-identity

        model_upsert_row = MagicMock()
        model_upsert_row.__getitem__.side_effect = {"id": _MERGED_MODEL_ID}.__getitem__
        # Legacy dedup: stored input_tokens=100, cost 0.0035. Incoming
        # changes input_tokens → divergent duplicate (token change).
        legacy_dedup_row = MagicMock()
        legacy_dedup_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        items.extend([model_upsert_row, None, legacy_dedup_row])

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Tokens differ (and cost also differs, but the token difference
        # dominates → plain legacy conflict, no canonical routing).
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    source_record_id="rec-token-change",
                    input_tokens=200,
                    estimated_cost_usd="0.0100",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "conflict"
        # Not the cost-only marker — a plain divergent conflict reason.
        assert "cost-only" not in (result["reason"] or "").lower()

        # No canonical event write/update, no accounting.
        assert len(_find_execute_calls(mock_conn, "INSERT INTO usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0

    @pytest.mark.asyncio
    async def test_model_change_returns_plain_conflict(self, monkeypatch) -> None:
        mock_conn = AsyncMock()
        items: list = [_auth_row(), None]                   # auth + sd_check
        items.extend(_handler_routing_side_effect_items())  # cross-identity

        model_upsert_row = MagicMock()
        model_upsert_row.__getitem__.side_effect = {"id": _MERGED_MODEL_ID}.__getitem__
        legacy_dedup_row = MagicMock()
        legacy_dedup_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        items.extend([model_upsert_row, None, legacy_dedup_row])

        mock_conn.fetchrow = AsyncMock(side_effect=items)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    source_record_id="rec-model-change",
                    model="claude-3",          # different model name
                    estimated_cost_usd="0.0100",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "conflict"
        # No canonical event write/update, no accounting.
        assert len(_find_execute_calls(mock_conn, "INSERT INTO usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0


# ══════════════════════════════════════════════════════════════════════════
#  AC 11 — Quarantined records make no accounting mutation
# ══════════════════════════════════════════════════════════════════════════


class TestQuarantinedCostRecordNoMutation:
    """A record whose source identity has an active quarantine resolves to
    ``quarantined`` regardless of cost — no canonical event, no session
    aggregate, no rollup write."""

    @pytest.mark.asyncio
    async def test_quarantined_replay_with_cost_change_makes_no_accounting_change(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            _auth_row(),   # auth
            None,          # source_database check (new)
        ])
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)
        # Quarantine gate returns True → batch routes to quarantined before
        # legacy/canonical accounting.
        mock_conn.fetchval = AsyncMock(return_value=True)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    estimated_cost_usd="0.0100",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "quarantined"
        assert "quarantine" in (result["reason"] or "").lower()
        assert result["event_id"] is None

        # No session resolution, no canonical event, no rollup write.
        session_upserts = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(c.args[0])
        ]
        assert len(session_upserts) == 0
        assert len(_find_execute_calls(mock_conn, "INSERT INTO usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0

        # Quarantine attempt is audited.
        assert _attempt_outcomes(mock_conn) == ["quarantined"]


# ══════════════════════════════════════════════════════════════════════════
#  AC 12 — Ingest-attempt outcomes for each scenario
# ══════════════════════════════════════════════════════════════════════════


class TestIngestAttemptOutcomes:
    """Every scenario records the correct per-record ingest-attempt outcome
    (the audit trail of replay-safe ingest)."""

    @pytest.mark.asyncio
    async def test_new_record_records_accepted_attempt(self, monkeypatch) -> None:
        """A brand-new record records an ``accepted`` attempt."""
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
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload()]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "accepted"
        assert _attempt_outcomes(mock_conn) == ["accepted"]

    @pytest.mark.asyncio
    async def test_cost_increase_records_updated_attempt(self, monkeypatch) -> None:
        """A cost-only increase records an ``updated`` attempt."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(stored_cost=Decimal("0.0035")),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    estimated_cost_usd="0.0100",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "updated"
        assert _attempt_outcomes(mock_conn) == ["updated"]


# ══════════════════════════════════════════════════════════════════════════
#  AC 13 — Session aggregate updates for each scenario
# ══════════════════════════════════════════════════════════════════════════


class TestSessionAggregateUpdates:
    """The owning session's ``total_estimated_cost_usd`` moves by the delta
    for cost corrections and is untouched for duplicates/conflicts."""

    @pytest.mark.asyncio
    async def test_session_aggregate_adjusted_by_delta_on_cost_increase(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.0035"),
                total_cost=Decimal("1.00"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Session aggregate currently 1.00; stored event cost 0.0035;
        # incoming 0.0100 → session moves +0.0065 → 1.0065.
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    estimated_cost_usd="0.0100",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "updated"

        session_updates = _find_execute_calls(mock_conn, "UPDATE sessions")
        assert len(session_updates) == 1
        assert session_updates[0].args[1] == Decimal("1.0065")

    @pytest.mark.asyncio
    async def test_session_aggregate_untouched_on_duplicate(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_winner_existing_canonical_fetchrow_items(),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload()]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["status"] == "duplicate"
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0


# ══════════════════════════════════════════════════════════════════════════
#  AC 14 — Client project rollup cost deltas
# ══════════════════════════════════════════════════════════════════════════


class TestClientProjectRollupCostDeltas:
    """The rollup row moves by the same cost delta as the event/session."""

    @pytest.mark.asyncio
    async def test_rollup_moves_by_cost_delta_not_full_reincrement(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.0100"),
                with_rollup=True,
                total_cost=Decimal("0.0100"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Stored cost 0.0100 → incoming 0.0060 → delta −0.0040.
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    estimated_cost_usd="0.0060",
                    project_id="proj-test-abc",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated"

        rollup_calls = _find_execute_calls(mock_conn, "client_project_rollup")
        assert len(rollup_calls) == 1
        rollup_args = rollup_calls[0].args
        assert rollup_args[1] == _CLIENT_ID
        assert rollup_args[2] == "proj-test-abc"
        assert rollup_args[3] == _mk_ts().date()
        # Delta only — never the full incoming cost.
        assert rollup_args[8] == Decimal("-0.0040"), (
            f"Expected rollup delta -0.0040, got {rollup_args[8]} "
            f"(raw incoming 0.0060 would be a full re-increment!)"
        )

        # Token deltas are zero (only cost changed) → only cost is passed.
        assert rollup_args[4] == 0
        assert rollup_args[5] == 0
        assert rollup_args[6] == 0
        assert rollup_args[7] == 0


# ══════════════════════════════════════════════════════════════════════════
#  AC 15 — Idempotency: repeated deliveries apply the delta only once
# ══════════════════════════════════════════════════════════════════════════


class TestIdempotentCostCorrection:
    """After a cost correction is applied, re-delivering the corrected value
    is an idempotent duplicate — the delta is never applied twice."""

    @pytest.mark.asyncio
    async def test_repeated_corrected_delivery_applies_delta_only_once(
        self, monkeypatch,
    ) -> None:
        mock_conn = AsyncMock()
        # The legacy row keeps the ORIGINAL first-write cost (0.0035) while the
        # canonical event was already corrected to 0.0100 by a prior delivery.
        # Re-delivering 0.0100 diverges from the legacy row (marker fires) but
        # exactly matches the stored canonical event → idempotent duplicate.
        mock_conn.fetchrow = AsyncMock(
            side_effect=_cost_only_fetchrow_items(
                stored_cost=Decimal("0.0100"),      # canonical (already corrected)
                legacy_cost=Decimal("0.0035"),      # legacy first-write-wins
                with_rollup=True,
                total_cost=Decimal("0.0100"),
            ),
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Incoming cost equals the (already corrected) stored cost → the
        # exact-Decimal gate classifies it as duplicate; no second delta.
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[_record_payload(
                    estimated_cost_usd="0.0100",
                    project_id="proj-test-abc",
                )]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "duplicate"

        # No cost UPDATE, no session adjustment, no rollup write.
        assert len(_find_execute_calls(mock_conn, "UPDATE usage_events")) == 0
        assert len(_find_execute_calls(mock_conn, "UPDATE sessions")) == 0
        assert len(_find_execute_calls(mock_conn, "client_project_rollup")) == 0

        # Duplicate attempt audited — not another "updated".
        assert _attempt_outcomes(mock_conn) == ["duplicate"]


# ══════════════════════════════════════════════════════════════════════════
#  Concurrency — cost correction applies the delta exactly once
# ══════════════════════════════════════════════════════════════════════════


class TestConcurrentCostCorrection:
    """Two genuinely concurrent cost-correction deliveries for the same
    canonical event serialise on the per-event advisory lock: the second
    blocks until the first commits, re-reads the corrected event, and
    resolves to an idempotent ``duplicate`` — the delta is applied exactly
    once (the cost-only routing gate of issue #646 runs inside the same
    canonical lock as the normal replay-merge path)."""

    @pytest.mark.asyncio
    async def test_concurrent_cost_corrections_apply_delta_once(
        self,
    ) -> None:
        import asyncio

        from app.api.ingest import IngestRecord, _record_canonical_event

        client_id = uuid.uuid4()
        source_db_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        identity_id = uuid.uuid4()
        model_id = uuid.uuid4()
        session_id = uuid.uuid4()
        now = _mk_ts()

        # ── Stateful fake DB for usage_events ─────────────────────
        # Mirrors the real pg_advisory_xact_lock serialisation with an
        # asyncio.Lock that is acquired at the lock SELECT and released
        # when the (fake) transaction exits.  The 0-sleep yields guarantee
        # the two coroutines interleave on the event loop.
        event_key = (str(identity_id), "rec-concurrent-cost")
        fake_events: dict[tuple, dict] = {}
        # Pre-existing canonical event (first delivery already committed).
        fake_events[event_key] = {
            "id": uuid.uuid4(),
            "estimated_cost_usd": Decimal("0.0035"),  # stored first-delivery cost
            "session_cost": Decimal("0.0035"),
            "rollup_cost": Decimal("0.0035"),
        }
        # Per-key advisory locks.  The cost-only correction path acquires TWO
        # distinct xact locks inside one transaction — the canonical-event
        # insertion lock (in ``_record_canonical_event``) and the per-event
        # replay lock (inside ``apply_replay_merge``) — so each key needs its
        # own asyncio.Lock or a single delivery would deadlock against itself.
        locks: dict[tuple[int, int], asyncio.Lock] = {}
        blocked: dict[tuple[int, int], int] = {}

        async def _fetchval_for(*args):
            await asyncio.sleep(0)
            sql = str(args[0]) if args else ""
            if "SELECT pg_advisory_xact_lock" in sql:
                key = (int(args[1]), int(args[2]))
                lock = locks.setdefault(key, asyncio.Lock())
                was_locked = lock.locked()
                await lock.acquire()
                if was_locked:
                    blocked[key] = blocked.get(key, 0) + 1
            return None

        async def _fetchrow_for(*args, **kwargs):
            await asyncio.sleep(0)
            sql = str(args[0]) if args else ""
            if "SELECT id FROM observed_models" in sql:
                return mock_row({"id": model_id})
            if "SELECT total_input_tokens, total_output_tokens" in sql:
                # apply_replay_merge: FOR UPDATE read of the sessions aggregate.
                event = fake_events[event_key]
                return mock_row({
                    "total_input_tokens": 100,
                    "total_output_tokens": 50,
                    "total_cached_tokens": 0,
                    "total_cache_read_tokens": 0,
                    "total_cache_write_tokens": 0,
                    "total_estimated_cost_usd": event["session_cost"],
                })
            if "SELECT id FROM sessions" in sql:
                return mock_row({"id": session_id})
            if "SELECT id FROM usage_events" in sql and "FOR UPDATE" in sql:
                # FOR UPDATE re-read after the advisory lock.
                if event_key in fake_events:
                    return mock_row({"id": fake_events[event_key]["id"]})
                return None
            if sql.startswith("SELECT input_tokens, output_tokens") and "FOR UPDATE" in sql:
                # apply_replay_merge: FOR UPDATE read of the usage_events row.
                event = fake_events[event_key]
                return mock_row({
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "estimated_cost_usd": event["estimated_cost_usd"],
                    "session_id": session_id,
                    "client_id": client_id,
                    "project_id": "proj-test-abc",
                    "reported_at": now,
                })
            if sql.startswith("SELECT input_tokens, output_tokens"):
                # Full canonical event field read for the comparison gate.
                event = fake_events[event_key]
                return mock_row({
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "estimated_cost_usd": event["estimated_cost_usd"],
                    "provider": None,
                    "mode": None,
                    "finish_reason": None,
                    "session_id": session_id,
                    "model_id": model_id,
                })
            return None

        async def _execute_for(*args):
            await asyncio.sleep(0)
            sql = str(args[0])
            if "UPDATE usage_events SET" in sql:
                # apply_replay_merge event update: corrected cost is the first
                # bound parameter (the only SET clause in the cost-only path).
                if "estimated_cost_usd = $1" in sql:
                    event = fake_events[event_key]
                    event["estimated_cost_usd"] = args[1]
            elif "UPDATE sessions SET" in sql:
                if "total_estimated_cost_usd = $1" in sql:
                    fake_events[event_key]["session_cost"] = args[1]
            elif "INSERT INTO client_project_rollup" in sql:
                if "DO UPDATE SET" in sql and "estimated_cost_usd" in sql:
                    fake_events[event_key]["rollup_cost"] += args[8]
            return None

        class _SerialisedTx:
            """Fake transaction — releases ALL advisory locks held by this
            delivery on exit (xact-scoped locks release at commit/rollback)."""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                for lock in locks.values():
                    if lock.locked():
                        lock.release()
                return None

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_for)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval_for)
        mock_conn.execute = AsyncMock(side_effect=_execute_for)
        mock_conn.transaction = MagicMock(return_value=_SerialisedTx())

        # Both deliveries carry the same corrected cost 0.0100.
        def _make_delivery():
            record = IngestRecord(
                source_record_id="rec-concurrent-cost",
                session_id=str(session_id),
                model="gpt-4",
                input_tokens=100,
                output_tokens=50,
                reported_at=now,
                estimated_cost_usd=Decimal("0.0100"),
                project_id="proj-test-abc",
            )
            return _record_canonical_event(
                mock_conn, record, client_id, source_db_id,
                batch_id, None, now,
                canonical_identity_id=identity_id,
                cost_only=True,
            )

        results = await asyncio.gather(*[_make_delivery() for _ in range(2)])

        # One delivery applied the correction; the other idempotently
        # resolved to duplicate after re-reading the corrected event.
        statuses = sorted(r["status"] for r in results)
        assert statuses == ["duplicate", "updated"], statuses

        # The stored canonical cost was corrected exactly once (to 0.0100),
        # and the second delivery genuinely blocked on the advisory lock.
        assert fake_events[event_key]["estimated_cost_usd"] == Decimal("0.0100")
        assert fake_events[event_key]["session_cost"] == Decimal("0.0100")
        assert fake_events[event_key]["rollup_cost"] == Decimal("0.0100")
        # The second delivery genuinely blocked on the canonical-event lock
        # (the first lock key) while the first delivery's transaction ran.
        first_lock_key = next(iter(locks))
        assert blocked.get(first_lock_key, 0) == 1, (
            "expected one delivery to block on the advisory lock; "
            f"blocked={blocked}"
        )

        # Exactly one cost-only UPDATE on usage_events and one attempt per
        # delivery (each delivery records its own audit attempt).
        cost_updates = [
            c for c in mock_conn.execute.call_args_list
            if isinstance(c.args[0], str)
            and "UPDATE usage_events" in c.args[0]
            and "estimated_cost_usd" in c.args[0]
        ]
        assert len(cost_updates) == 1, (
            f"expected exactly 1 cost correction UPDATE, got {len(cost_updates)}"
        )
        attempt_inserts = [
            c for c in mock_conn.execute.call_args_list
            if isinstance(c.args[0], str)
            and "INSERT INTO usage_ingest_attempts" in c.args[0]
        ]
        assert len(attempt_inserts) == 2
