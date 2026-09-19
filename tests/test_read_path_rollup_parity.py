"""Rollup / ETL read-model parity helpers and tests (issue #708).

A read model (the Client Project Rollup today; any future ETL projection
tomorrow) is a *derived convenience*, never the source of truth: canonical
``usage_events`` remain the accounting truth (ADR 0015).  Optimizing the
read path onto a read model is only safe if the read model can be proven
equal to the canonical events, and if drift/replay is detected and corrected.

This module provides reusable parity helpers plus focused tests for the four
invariants the acceptance criteria name:

- **freshness** — a read model carries a derivation timestamp that the
  consumer can judge fresh or stale.
- **replay correction** — the canonical-event Replay Merge (ADR 0012) uses
  non-null authoritative values and delta-adjusts, never re-increments and
  never erases on a null/omitted collector value.
- **rebuild equality** — rebuilding the read model from canonical events
  yields totals equal to the incrementally maintained model.
- **canonical-event equality** — the read model's additive totals equal the
  canonical-event sums per ``(client_id, project_id, day)``.

The helpers deliberately use the *production* merge policy
(``app.core.merge_policy`` via ``app.core.reconciliation``) so the parity
check cannot drift from the shipped replay semantics.

Protocol reference: ``docs/adr/0017-migration-0019-index-measurement.md``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

import pytest

from app.core.reconciliation import ROLLUP_FIELDS, compute_delta

_T0 = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

_CLIENT_A = "client-a"
_PROJECT_1 = "proj-1"


# ══════════════════════════════════════════════════════════════════════════
#  Parity helpers (reusable across future read models)
# ══════════════════════════════════════════════════════════════════════════


def _day(reported_at: datetime) -> date:
    """UTC day bucket, matching the rollup ``day`` key derivation."""
    return reported_at.astimezone(timezone.utc).date()


def _key(row: Mapping[str, Any]) -> tuple[str, str, date]:
    return (str(row["client_id"]), str(row["project_id"]), _day(row["reported_at"]))


def _zero_totals() -> dict[str, Any]:
    return {field: 0 for field in ROLLUP_FIELDS if field != "estimated_cost_usd"} | {
        "estimated_cost_usd": Decimal("0")
    }


def canonical_event_totals(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, date], dict[str, Any]]:
    """Additive totals over canonical events per ``(client, project, day)``.

    Events with a NULL ``project_id`` are excluded: the rollup primary key is
    all-NOT-NULL, so such events cannot be keyed (ADR 0015).
    """
    totals: dict[tuple[str, str, date], dict[str, Any]] = {}
    for event in events:
        if event.get("project_id") is None:
            continue
        bucket = totals.setdefault(_key(event), _zero_totals())
        for field in ROLLUP_FIELDS:
            bucket[field] += event.get(field) or (
                Decimal("0") if field == "estimated_cost_usd" else 0
            )
    return totals


def read_model_totals(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, date], dict[str, Any]]:
    """Additive totals from read-model rows of the same shape as the rollup."""
    totals: dict[tuple[str, str, date], dict[str, Any]] = {}
    for row in rows:
        bucket = totals.setdefault(_key(row), _zero_totals())
        for field in ROLLUP_FIELDS:
            bucket[field] += row.get(field) or (
                Decimal("0") if field == "estimated_cost_usd" else 0
            )
    return totals


def assert_canonical_event_equality(
    events: Iterable[Mapping[str, Any]],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    """Assert the read model equals the canonical-event sums.

    Raises ``AssertionError`` naming the differing key(s); a read-model key
    absent from the canonical sums (or vice versa) is drift.
    """
    canonical = canonical_event_totals(events)
    model = read_model_totals(rows)
    all_keys = set(canonical) | set(model)
    mismatches = {
        key: (canonical.get(key, _zero_totals()), model.get(key, _zero_totals()))
        for key in all_keys
        if canonical.get(key, _zero_totals()) != model.get(key, _zero_totals())
    }
    assert not mismatches, f"read model drifted from canonical events: {mismatches}"


def apply_replay(
    base_event: Mapping[str, Any],
    replayed_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a replay into a canonical event using the production policy.

    Non-null replayed values are authoritative; null/omitted values never
    erase the stored value (delta zero).
    """
    delta = compute_delta(base_event, replayed_values)
    return dict(base_event) | dict(delta.new_values)


def replay_delta(base_event: Mapping[str, Any], replayed_values: Mapping[str, Any]):
    """Expose the production :func:`compute_delta` result for assertions."""
    return compute_delta(base_event, replayed_values)


def rebuild_read_model(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, date], dict[str, Any]]:
    """Rebuild the read model purely from canonical events."""
    return canonical_event_totals(events)


def is_fresh(
    derived_at: datetime | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> bool:
    """Freshness judgement for a derived read-model timestamp.

    Unknown-age (``None``) is treated as **not fresh** — staleness must never
    be silently assumed away.  Boundaries are inclusive: a timestamp exactly
    ``max_age`` old is still fresh.
    """
    if derived_at is None:
        return False
    return now - derived_at <= max_age


# ══════════════════════════════════════════════════════════════════════════
#  Tests
# ══════════════════════════════════════════════════════════════════════════


def _event(**overrides) -> dict[str, Any]:
    event = {
        "client_id": _CLIENT_A,
        "project_id": _PROJECT_1,
        "reported_at": _T0,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "estimated_cost_usd": Decimal("0.01"),
    }
    event.update(overrides)
    return event


def _rollup(**overrides) -> dict[str, Any]:
    row = {
        "client_id": _CLIENT_A,
        "project_id": _PROJECT_1,
        "reported_at": _T0,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "estimated_cost_usd": Decimal("0.01"),
    }
    row.update(overrides)
    return row


class TestCanonicalEventEquality:
    def test_equal_read_model_passes(self):
        assert_canonical_event_equality([_event()], [_rollup()])

    def test_drift_is_detected(self):
        with pytest.raises(AssertionError, match="drifted"):
            assert_canonical_event_equality(
                [_event()],
                [_rollup(input_tokens=999)],
            )

    def test_missing_read_model_key_is_drift(self):
        with pytest.raises(AssertionError, match="drifted"):
            assert_canonical_event_equality([_event()], [])

    def test_null_project_events_are_excluded(self):
        """NULL-project events cannot be keyed in the rollup, so they must be
        absent from both sides of the comparison."""
        events = [_event(), _event(project_id=None, input_tokens=12345)]
        canonical = canonical_event_totals(events)
        assert len(canonical) == 1
        assert canonical[(_CLIENT_A, _PROJECT_1, _T0.date())]["input_tokens"] == 100

    def test_multiple_days_and_keys_aggregate_independently(self):
        events = [
            _event(),
            _event(project_id="proj-2"),
            _event(reported_at=_T0 + timedelta(days=1)),
        ]
        canonical = canonical_event_totals(events)
        assert len(canonical) == 3


class TestReplayCorrection:
    def test_non_null_replay_is_authoritative_and_delta_adjusts(self):
        base = _event()
        replayed = _event(input_tokens=250, estimated_cost_usd=Decimal("0.05"))
        merged = apply_replay(base, replayed)

        assert merged["input_tokens"] == 250
        assert merged["estimated_cost_usd"] == Decimal("0.05")
        # Untouched fields keep their stored values.
        assert merged["output_tokens"] == 50

        # The delta-adjustment telescopes to the same totals as recomputing
        # from the merged canonical event — never a double-increment.
        base_total = canonical_event_totals([base])[(_CLIENT_A, _PROJECT_1, _T0.date())]
        delta = replay_delta(base, replayed)
        base_total["input_tokens"] += delta.deltas["input_tokens"]
        base_total["estimated_cost_usd"] += delta.deltas["estimated_cost_usd"]
        merged_total = canonical_event_totals([merged])[(_CLIENT_A, _PROJECT_1, _T0.date())]
        assert base_total == merged_total

    def test_null_replay_never_erases(self):
        base = _event()
        replayed = {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "estimated_cost_usd": None,
        }
        delta = replay_delta(base, replayed)
        assert all(v == 0 for v in delta.deltas.values())
        merged = apply_replay(base, replayed)
        # No stored (non-null) value is erased — including fields absent from
        # the base event, which stay absent-or-null rather than becoming 0.
        for field, stored in base.items():
            assert merged[field] == stored

    def test_zero_is_a_valid_observed_value(self):
        base = _event(input_tokens=100)
        delta = replay_delta(base, {"input_tokens": 0})
        assert delta.new_values["input_tokens"] == 0
        assert delta.deltas["input_tokens"] == -100


class TestRebuildEquality:
    def test_rebuild_from_canonical_equals_read_model(self):
        events = [_event(), _event(project_id="proj-2", output_tokens=7)]
        rebuilt = rebuild_read_model(events)
        assert rebuilt == read_model_totals([_rollup(), _rollup(project_id="proj-2", output_tokens=7)])

    def test_rebuild_after_replay_equals_corrected_read_model(self):
        base = _event()
        replayed = _event(input_tokens=250)
        merged = apply_replay(base, replayed)

        incremental = read_model_totals([_rollup(input_tokens=250)])
        assert rebuild_read_model([merged]) == incremental

    def test_stale_read_model_is_flagged_until_rebuilt(self):
        base = _event()
        replayed = _event(input_tokens=250)
        merged = apply_replay(base, replayed)
        stale_model = read_model_totals([_rollup()])  # pre-replay totals
        assert rebuild_read_model([merged]) != stale_model
        # Rebuilding toward the canonical event restores equality.
        assert rebuild_read_model([merged]) == read_model_totals([_rollup(input_tokens=250)])


class TestFreshness:
    def test_fresh_within_window(self):
        now = _T0 + timedelta(minutes=5)
        assert is_fresh(_T0, now=now, max_age=timedelta(minutes=10))

    def test_boundary_age_is_still_fresh(self):
        now = _T0 + timedelta(minutes=10)
        assert is_fresh(_T0, now=now, max_age=timedelta(minutes=10))

    def test_stale_beyond_window(self):
        now = _T0 + timedelta(minutes=11)
        assert not is_fresh(_T0, now=now, max_age=timedelta(minutes=10))

    def test_unknown_age_is_never_fresh(self):
        assert not is_fresh(None, now=_T0, max_age=timedelta(hours=1))
