"""Unit tests for the current-aggregate layer (issue #480).

Verifies the forward-only current-aggregate merge over ``reporting_*``
deliveries using a mock asyncpg connection (no real database):

* stable resource identity extraction + normalization (lowercase URL,
  strip trailing slash) and None-safe malformed-resource handling;
* the advisory lock key is signed-int32 (asyncpg int4 bind safety) and
  deterministic across calls;
* the per-resource ``pg_advisory_xact_lock`` is emitted before the read;
* a first event inserts the aggregate; a later event UPDATEs it;
* forward-only merge: a newer event overwrites; a stale event fills only
  absent keys — never regresses state already set by a newer event
  (explicit late-event non-regression);
* equal-``occurred_at`` tie-break: lowest ``delivery_id`` wins;
* null/omitted incoming values never erase populated state.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import reporting_aggregates as agg
from app.core.reporting_aggregates import (
    AGGREGATE_LOCK_CLASS,
    ResourceIdentity,
    _aggregate_lock_key,
    advance_last,
    enrich_aggregate,
    forward_merge,
    is_newer,
    resource_identity_from_payload,
)
from app.core.repository import normalize_repository_url
from tests.conftest import mock_row

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

_T1 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
_T1_5 = datetime(2026, 8, 14, 12, 30, 0, tzinfo=UTC)
_T2 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _delivery(
    *,
    delivery_id: str,
    occurred_at: datetime,
    payload: dict,
) -> SimpleNamespace:
    """Return a duck-typed delivery carrying the fields enrich_aggregate reads."""
    return SimpleNamespace(
        provider="github",
        delivery_id=delivery_id,
        occurred_at=occurred_at,
        payload=payload,
    )


def _identity(resource_number: str = "42") -> ResourceIdentity:
    return ResourceIdentity(
        provider="github",
        repository_url="github.com/Acme/Backend",
        resource_type="issue",
        resource_number=resource_number,
    )


def _stored_row(
    *,
    occurred_at: datetime,
    delivery_id: str,
    payload: dict,
    key_provenance: dict | None = None,
):
    return mock_row(
        {
            "last_occurred_at": occurred_at,
            "last_delivery_id": delivery_id,
            "payload": payload,
            "key_provenance": key_provenance if key_provenance is not None else {},
        }
    )


# ── Identity extraction + normalization ──────────────────────────────────────


def test_identity_extracts_and_normalizes_resource() -> None:
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/Acme/Backend/",
                "type": "issue",
                "number": 42,
            }
        },
        provider="github",
    )
    assert identity is not None
    assert identity.provider == "github"
    assert identity.repository_url == "github.com/Acme/Backend"
    assert identity.resource_type == "issue"
    assert identity.resource_number == "42"
    assert (
        identity.composite_key
        == "github:github.com/Acme/Backend:issue:42"
    )


def test_identity_maps_pull_request_to_change_request() -> None:
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/acme/backend",
                "type": "pull_request",
                "number": "42",
            }
        },
        provider="github",
    )
    assert identity is not None
    assert identity.resource_type == "change_request"


def test_identity_maps_merge_request_to_change_request() -> None:
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://gitlab.com/acme/frontend",
                "type": "merge_request",
                "number": "7",
            }
        },
        provider="gitlab",
    )
    assert identity is not None
    assert identity.resource_type == "change_request"


def test_identity_keeps_issue_unchanged() -> None:
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/acme/backend",
                "type": "issue",
                "number": "1",
            }
        },
        provider="github",
    )
    assert identity is not None
    assert identity.resource_type == "issue"


def test_identity_returns_none_for_unknown_resource_type() -> None:
    assert (
        resource_identity_from_payload(
            {
                "resource": {
                    "repository_url": "https://github.com/acme/backend",
                    "type": "unknown_type",
                    "number": "42",
                }
            },
            provider="github",
        )
        is None
    )


def test_identity_returns_none_when_resource_missing() -> None:
    assert resource_identity_from_payload({"repository": "acme/backend"}, provider="github") is None
    assert resource_identity_from_payload({}, provider="github") is None


def test_identity_returns_none_when_resource_malformed() -> None:
    # resource present but not a mapping
    assert resource_identity_from_payload({"resource": "nope"}, provider="github") is None
    # missing repository_url
    assert (
        resource_identity_from_payload(
            {"resource": {"type": "issue", "number": "42"}},
            provider="github",
        )
        is None
    )
    # empty repository_url after normalization
    assert (
        resource_identity_from_payload(
            {"resource": {"repository_url": "///", "type": "issue", "number": "42"}},
            provider="github",
        )
        is None
    )


def test_normalize_repository_url_lowercases_and_strips_trailing_slash() -> None:
    # Hostname is lowercased; path preserves original case (mirrors afk_consumer).
    assert normalize_repository_url("https://github.com/Acme/Backend/") == (
        "github.com/Acme/Backend"
    )
    assert normalize_repository_url("HTTPS://EXAMPLE.COM/Repo") == "example.com/Repo"
    assert normalize_repository_url("https://github.com/acme/backend.git") == (
        "github.com/acme/backend"
    )
    assert normalize_repository_url("") is None
    assert normalize_repository_url("not-a-url") is None


# ── Advisory lock key ────────────────────────────────────────────────────────


def test_aggregate_lock_class_is_47006() -> None:
    assert AGGREGATE_LOCK_CLASS == 47_006


def test_aggregate_lock_key_is_signed_int32_and_deterministic() -> None:
    identity = _identity()
    key1 = _aggregate_lock_key(identity)
    key2 = _aggregate_lock_key(_identity())
    assert key1[0] == AGGREGATE_LOCK_CLASS
    assert -0x80000000 <= key1[1] <= 0x7FFFFFFF  # signed int32 range
    assert key1 == key2  # deterministic for the same composite key


def test_aggregate_lock_key_differs_by_resource_number() -> None:
    assert _aggregate_lock_key(_identity("1")) != _aggregate_lock_key(_identity("2"))


# ── Forward-only merge (pure) ────────────────────────────────────────────────


def test_forward_merge_newer_overwrites() -> None:
    stored = {"status": "open", "title": "old"}
    incoming = {"status": "closed", "title": "new"}
    merged, provenance = forward_merge(
        stored,
        incoming,
        stored_provenance={},
        incoming_occurred_at=_T2,
        incoming_delivery_id="d2",
        fallback_writer=(_T1, "d1"),
    )
    assert merged == {"status": "closed", "title": "new"}
    assert provenance == {"status": (_T2, "d2"), "title": (_T2, "d2")}


def test_forward_merge_stale_fills_absent_only() -> None:
    """Late-event non-regression: a stale event never regresses populated state."""
    stored = {"status": "closed", "title": "done"}
    incoming = {"status": "open", "title": "stale", "labels": ["bug"]}
    merged, _ = forward_merge(
        stored,
        incoming,
        stored_provenance={},
        incoming_occurred_at=_T1,
        incoming_delivery_id="d1",
        fallback_writer=(_T2, "d2"),  # aggregate's global last event is newer
    )
    assert merged["status"] == "closed"  # NOT regressed to "open"
    assert merged["title"] == "done"  # NOT regressed to "stale"
    assert merged["labels"] == ["bug"]  # absent key filled forward


def test_forward_merge_never_erases_on_null() -> None:
    stored = {"status": "closed", "title": "done"}
    incoming = {"status": None, "title": "done", "labels": None}
    merged, provenance = forward_merge(
        stored,
        incoming,
        stored_provenance={},
        incoming_occurred_at=_T2,
        incoming_delivery_id="d2",
        fallback_writer=(_T1, "d1"),
    )
    assert merged == {"status": "closed", "title": "done"}
    # null keys ("status", "labels") are skipped — no provenance written for them
    assert "status" not in provenance
    assert "labels" not in provenance


def test_forward_merge_does_not_mutate_stored() -> None:
    stored = {"status": "closed"}
    stored_provenance = {"status": (_T2, "d2")}
    forward_merge(
        stored,
        {"labels": ["bug"]},
        stored_provenance=stored_provenance,
        incoming_occurred_at=_T1,
        incoming_delivery_id="d1",
        fallback_writer=None,
    )
    assert stored == {"status": "closed"}
    assert stored_provenance == {"status": (_T2, "d2")}


# ── Forward-only merge — per-key provenance (review finding #1) ──────────────


def _apply_arrival_order(events: list[dict]) -> tuple[dict, dict]:
    """Drive ``forward_merge`` over events in order, tracking the global last event."""
    payload: dict = {}
    provenance: dict = {}
    last_occurred_at: datetime | None = None
    last_delivery_id: str | None = None
    for event in events:
        fallback = (
            (last_occurred_at, last_delivery_id)
            if last_occurred_at is not None
            else None
        )
        payload, provenance = forward_merge(
            payload,
            event["payload"],
            stored_provenance=provenance,
            incoming_occurred_at=event["occurred_at"],
            incoming_delivery_id=event["delivery_id"],
            fallback_writer=fallback,
        )
        if last_occurred_at is None:
            last_occurred_at, last_delivery_id = (
                event["occurred_at"],
                event["delivery_id"],
            )
        else:
            last_occurred_at, last_delivery_id = advance_last(
                last_occurred_at,
                last_delivery_id,
                event["occurred_at"],
                event["delivery_id"],
            )
    return payload, provenance


_THREE_EVENTS = [
    {"occurred_at": _T2, "delivery_id": "e1", "payload": {"x": 1}},
    {"occurred_at": _T1, "delivery_id": "e2", "payload": {"y": 2}},
    {"occurred_at": _T1_5, "delivery_id": "e3", "payload": {"y": 3}},
]


def test_forward_merge_three_event_converges_across_all_arrival_orders() -> None:
    """Review counterexample: per-key provenance converges across all 6 orders.

    Without per-key provenance, ``e1, e2, e3`` yielded ``{x:1, y:2}`` while
    ``e1, e3, e2`` yielded ``{x:1, y:3}`` — arrival-order dependent.  With
    per-key provenance every order must converge to ``{x:1, y:3}`` (``y``
    last written by ``e3``, newer than its original writer ``e2``).
    """
    expected_payload = {"x": 1, "y": 3}
    expected_provenance = {"x": (_T2, "e1"), "y": (_T1_5, "e3")}
    for order in itertools.permutations(_THREE_EVENTS):
        payload, provenance = _apply_arrival_order(list(order))
        assert payload == expected_payload, f"order {[e['delivery_id'] for e in order]}"
        assert provenance == expected_provenance, (
            f"order {[e['delivery_id'] for e in order]}"
        )


def test_forward_merge_stale_event_upgrades_key_newer_than_its_writer() -> None:
    """A globally-stale event still upgrades a key it is newer than its writer of.

    The aggregate's global last event is ``e1`` (``_T2``); ``y`` was written
    by ``e2`` (``_T1``).  ``e3`` (``_T1_5``) is older than ``e1`` but newer
    than ``e2``, so it must upgrade ``y``.
    """
    stored = {"x": 1, "y": 2}
    stored_provenance = {"x": (_T2, "e1"), "y": (_T1, "e2")}
    merged, provenance = forward_merge(
        stored,
        {"y": 3},
        stored_provenance=stored_provenance,
        incoming_occurred_at=_T1_5,
        incoming_delivery_id="e3",
        fallback_writer=(_T2, "e1"),
    )
    assert merged == {"x": 1, "y": 3}
    assert provenance["y"] == (_T1_5, "e3")
    assert provenance["x"] == (_T2, "e1")


def test_forward_merge_legacy_no_provenance_falls_back_to_global_last() -> None:
    """Legacy rows (no per-key provenance) preserve the old single-flag behavior."""
    stored = {"x": 1, "y": 2}
    incoming = {"y": 3}
    merged, _ = forward_merge(
        stored,
        incoming,
        stored_provenance=None,
        incoming_occurred_at=_T1,
        incoming_delivery_id="d-stale",
        fallback_writer=(_T2, "d-newer"),  # global last event is newer
    )
    # y is present and the stale event is older than the global last event,
    # so the old single-flag rule applies: no overwrite.
    assert merged == {"x": 1, "y": 2}


def test_forward_merge_fills_stored_none_key_regardless_of_fallback() -> None:
    """A stored ``None`` value is treated as absent (fill-forward).

    F1-residual: a legacy row may hold ``{"a": None}`` (written before the
    INSERT path filtered ``None`` keys).  An incoming real value must fill
    the key even when the aggregate's global last event (``fallback_writer``)
    is newer than the incoming event — the stored value is ``None``, not a
    real observation, so there is no state to regress.
    """
    stored = {"a": None}
    incoming = {"a": 7}
    merged, provenance = forward_merge(
        stored,
        incoming,
        stored_provenance={},  # no per-key writer for "a"
        incoming_occurred_at=_T1,
        incoming_delivery_id="d1",
        fallback_writer=(_T2, "d-newer"),  # newer than the incoming event
    )
    assert merged == {"a": 7}
    assert provenance["a"] == (_T1, "d1")


def test_provenance_serialization_round_trips() -> None:
    provenance = {"x": (_T2, "e1"), "y": (_T1_5, "e3")}
    raw = agg._serialize_provenance(provenance)
    assert raw == {
        "x": {"occurred_at": _T2.isoformat(), "delivery_id": "e1"},
        "y": {"occurred_at": _T1_5.isoformat(), "delivery_id": "e3"},
    }
    assert agg._deserialize_provenance(raw) == provenance


def test_deserialize_provenance_handles_missing_and_malformed() -> None:
    assert agg._deserialize_provenance(None) == {}
    assert agg._deserialize_provenance([]) == {}
    assert agg._deserialize_provenance("not-json") == {}
    assert agg._deserialize_provenance({"x": {"delivery_id": "e1"}}) == {}
    assert agg._deserialize_provenance(
        {"x": {"occurred_at": "not-a-date", "delivery_id": "e1"}}
    ) == {}


# ── Tie-break helpers ────────────────────────────────────────────────────────


def test_is_newer_equal_occurred_at_lower_delivery_id_wins() -> None:
    assert is_newer(_T2, "d1", _T1, "d1") is True  # strictly newer
    assert is_newer(_T1, "d1", _T2, "d1") is False  # strictly older
    # equal occurred_at → lower delivery_id wins
    assert is_newer(_T1, "delivery-a", _T1, "delivery-b") is True
    assert is_newer(_T1, "delivery-b", _T1, "delivery-a") is False


def test_advance_last_keeps_lowest_delivery_id_on_equal_occurred_at() -> None:
    new_occ, new_del = advance_last(_T1, "delivery-b", _T1, "delivery-a")
    assert new_occ == _T1
    assert new_del == "delivery-a"


# ── enrich_aggregate SQL shape (mock conn) ───────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_emits_lock_before_read(mock_conn: AsyncMock) -> None:
    order: list[str] = []

    async def _recorded_fetchval(sql, *args):
        order.append("lock")
        return True

    async def _recorded_fetchrow(sql, *args):
        order.append("read")
        return None

    async def _recorded_execute(sql, *args):
        order.append("write")
        return "OK"

    mock_conn.fetchval = AsyncMock(side_effect=_recorded_fetchval)
    mock_conn.fetchrow = AsyncMock(side_effect=_recorded_fetchrow)
    mock_conn.execute = AsyncMock(side_effect=_recorded_execute)

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(delivery_id="d1", occurred_at=_T1, payload={"a": 1}),
    )

    lock_sql = mock_conn.fetchval.call_args.args[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert order == ["lock", "read", "write"]


@pytest.mark.asyncio
async def test_enrich_inserts_when_aggregate_absent(mock_conn: AsyncMock) -> None:
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(delivery_id="d1", occurred_at=_T1, payload={"status": "open"}),
    )

    insert_sql = mock_conn.execute.call_args.args[0]
    assert "INSERT INTO reporting_resource_aggregates" in insert_sql
    args = mock_conn.execute.call_args.args
    # positional: sql, provider, repository_url, resource_type, resource_number,
    #            last_occurred_at, last_delivery_id, payload, key_provenance
    assert args[1] == "github"
    assert args[2] == "github.com/Acme/Backend"
    assert args[3] == "issue"
    assert args[4] == "42"
    assert args[5] == _T1
    assert args[6] == "d1"
    assert json.loads(args[7]) == {"status": "open"}
    assert json.loads(args[8]) == {
        "status": {"occurred_at": _T1.isoformat(), "delivery_id": "d1"}
    }


@pytest.mark.asyncio
async def test_enrich_insert_persists_only_non_null_keys(mock_conn: AsyncMock) -> None:
    """The INSERT path persists only non-``None`` keys (F1-residual).

    A first event carrying ``{"a": None, "b": 1}`` must store ``a`` as
    absent (fill-forward on the next real value), keeping payload and
    per-key provenance symmetric — ``a`` has no writer recorded.
    """
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(delivery_id="d1", occurred_at=_T1, payload={"a": None, "b": 1}),
    )

    insert_sql = mock_conn.execute.call_args.args[0]
    assert "INSERT INTO reporting_resource_aggregates" in insert_sql
    args = mock_conn.execute.call_args.args
    assert json.loads(args[7]) == {"b": 1}  # key "a" excluded from the payload
    assert json.loads(args[8]) == {
        "b": {"occurred_at": _T1.isoformat(), "delivery_id": "d1"}
    }


@pytest.mark.asyncio
async def test_enrich_update_forward_only_late_event_non_regression(
    mock_conn: AsyncMock,
) -> None:
    """A stale event updates forward but never regresses a newer event's state."""
    mock_conn.fetchval = AsyncMock(return_value=True)
    # stored aggregate already advanced by a newer event (T2, delivery d2)
    mock_conn.fetchrow = AsyncMock(
        return_value=_stored_row(
            occurred_at=_T2,
            delivery_id="d2",
            payload={"status": "closed", "title": "done"},
        )
    )
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(
            delivery_id="d1",
            occurred_at=_T1,  # stale: older than T2
            payload={"status": "open", "title": "stale", "labels": ["bug"]},
        ),
    )

    update_sql = mock_conn.execute.call_args.args[0]
    assert "UPDATE reporting_resource_aggregates" in update_sql
    args = mock_conn.execute.call_args.args
    # Positions: (sql, provider, url, type, number, payload, key_provenance,
    # last_occurred_at, last_delivery_id).
    merged = json.loads(args[5])
    assert merged["status"] == "closed"  # not regressed
    assert merged["title"] == "done"  # not regressed
    assert merged["labels"] == ["bug"]  # absent key filled forward
    assert args[7] == _T2  # last_occurred_at stays at the newer event
    assert args[8] == "d2"


@pytest.mark.asyncio
async def test_enrich_equal_occurred_at_lower_delivery_id_wins(
    mock_conn: AsyncMock,
) -> None:
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value=_stored_row(
            occurred_at=_T1,
            delivery_id="delivery-b",
            payload={"status": "open"},
        )
    )
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(
            delivery_id="delivery-a",
            occurred_at=_T1,  # equal occurred_at, lower delivery_id
            payload={"status": "closed"},
        ),
    )

    args = mock_conn.execute.call_args.args
    assert json.loads(args[5]) == {"status": "closed"}  # lower delivery_id wins
    assert args[7] == _T1
    assert args[8] == "delivery-a"


@pytest.mark.asyncio
async def test_update_sql_refreshes_updated_at(mock_conn: AsyncMock) -> None:
    """Review finding #2: the enrich UPDATE must refresh ``updated_at``."""
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value=_stored_row(
            occurred_at=_T2,
            delivery_id="d2",
            payload={"status": "closed"},
        )
    )
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    await enrich_aggregate(
        mock_conn,
        _identity(),
        _delivery(
            delivery_id="d1",
            occurred_at=_T1,
            payload={"status": "closed", "labels": ["bug"]},
        ),
    )

    update_sql = mock_conn.execute.call_args.args[0]
    assert "updated_at = now()" in update_sql
    assert "last_ingested_at = now()" in update_sql


class _AggregateStore:
    """Stateful fake asyncpg conn modelling ``enrich_aggregate``'s round-trip.

    Maintains a single in-memory aggregate row, parsing the INSERT/UPDATE
    ``execute`` args the same way ``enrich_aggregate`` emits them, so a
    sequence of deliveries drives the real merge logic end-to-end.
    """

    def __init__(self) -> None:
        self.row: dict | None = None

    async def fetchval(self, sql, *args):  # advisory lock — always acquired
        return True

    async def fetchrow(self, sql, *args):
        return None if self.row is None else mock_row(self.row)

    async def execute(self, sql, *args):
        if "INSERT INTO reporting_resource_aggregates" in sql:
            # $1 provider, $2 url, $3 type, $4 number, $5 occurred_at,
            # $6 delivery_id, $7 payload, $8 key_provenance
            self.row = {
                "last_occurred_at": args[4],
                "last_delivery_id": args[5],
                "payload": json.loads(args[6]),
                "key_provenance": json.loads(args[7]),
            }
        elif "UPDATE reporting_resource_aggregates" in sql:
            # $1 provider, $2 url, $3 type, $4 number, $5 payload,
            # $6 key_provenance, $7 occurred_at, $8 delivery_id
            self.row = {
                "last_occurred_at": args[6],
                "last_delivery_id": args[7],
                "payload": json.loads(args[4]),
                "key_provenance": json.loads(args[5]),
            }
        return "OK"


_NONE_KEY_EVENTS = [
    {"occurred_at": _T1, "delivery_id": "e1", "payload": {"a": None}},
    {"occurred_at": _T2, "delivery_id": "e2", "payload": {"b": 1}},
    {"occurred_at": _T1_5, "delivery_id": "e3", "payload": {"a": 7}},
]


@pytest.mark.asyncio
async def test_none_key_counterexample_converges_across_all_arrival_orders() -> None:
    """F1-residual counterexample: a ``None``-valued key must not depend on arrival order.

    ``e1`` (oldest) carries ``{a: None}``, ``e2`` (newest) carries
    ``{b: 1}``, ``e3`` (mid) carries ``{a: 7}``.  Before the fix, the order
    ``e1,e2,e3`` persisted ``a`` as ``None`` and then rejected ``e3``'s real
    ``a=7`` (the global last event had advanced to ``e2``), yielding
    ``{a: None, b: 1}`` while other orders yielded ``{a: 7, b: 1}``.  After
    the fix every order must converge to ``{a: 7, b: 1}`` with ``a`` written
    by ``e3`` and ``b`` written by ``e2``.
    """
    expected_payload = {"a": 7, "b": 1}
    expected_provenance = {"a": (_T1_5, "e3"), "b": (_T2, "e2")}
    for order in itertools.permutations(_NONE_KEY_EVENTS):
        store = _AggregateStore()
        for event in order:
            await enrich_aggregate(
                store,  # type: ignore[arg-type]
                _identity(),
                _delivery(
                    delivery_id=event["delivery_id"],
                    occurred_at=event["occurred_at"],
                    payload=event["payload"],
                ),
            )
        assert store.row is not None, f"order {[e['delivery_id'] for e in order]}"
        assert store.row["payload"] == expected_payload, (
            f"order {[e['delivery_id'] for e in order]}"
        )
        assert (
            agg._deserialize_provenance(store.row["key_provenance"])
            == expected_provenance
        ), f"order {[e['delivery_id'] for e in order]}"


# ── Duplicate delivery skips enrichment (ingest path) ────────────────────────


@pytest.mark.asyncio
async def test_duplicate_delivery_skips_enrichment(mock_conn: AsyncMock) -> None:
    """A redelivery (no fresh reporting_deliveries row) never enriches."""
    from tests.conftest import create_client

    def _auth_row():
        return mock_row(
            {
                "credential_id": uuid.uuid4(),
                "revoked_at": None,
                "last_used_at": None,
                "client_id": uuid.uuid4(),
                "client_name": "test-client",
                "client_is_active": True,
            }
        )

    # auth lookup returns a row; the delivery insert returns None → duplicate
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    payload = {
        "schema_version": "1.0",
        "deliveries": [
            {
                "provider": "github",
                "delivery_id": "dup-1",
                "event_type": "normalized",
                "occurred_at": _T1.isoformat(),
                "payload": {
                    "resource": {
                        "repository_url": "https://github.com/acme/backend",
                        "type": "issue",
                        "number": "42",
                    },
                    "status": "open",
                },
            }
        ],
    }

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["status"] == "duplicate"

    aggregate_sqls = [
        call.args[0]
        for call in mock_conn.execute.call_args_list
        if "reporting_resource_aggregates" in call.args[0]
    ]
    assert aggregate_sqls == [], "duplicate delivery must not enrich the aggregate"
