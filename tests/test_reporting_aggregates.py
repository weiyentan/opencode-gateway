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

import json
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
    normalize_repository_url,
    resource_identity_from_payload,
)
from tests.conftest import mock_row

UTC = timezone.utc

_T1 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
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
        repository_url="https://github.com/acme/backend",
        resource_type="issue",
        resource_number=resource_number,
    )


def _stored_row(
    *,
    occurred_at: datetime,
    delivery_id: str,
    payload: dict,
):
    return mock_row(
        {
            "last_occurred_at": occurred_at,
            "last_delivery_id": delivery_id,
            "payload": payload,
        }
    )


# ── Identity extraction + normalization ──────────────────────────────────────


def test_identity_extracts_and_normalizes_resource() -> None:
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/Acme/Backend/",
                "resource_type": "issue",
                "resource_number": 42,
            }
        },
        provider="github",
    )
    assert identity is not None
    assert identity.provider == "github"
    assert identity.repository_url == "https://github.com/acme/backend"
    assert identity.resource_type == "issue"
    assert identity.resource_number == "42"
    assert (
        identity.composite_key
        == "github:https://github.com/acme/backend:issue:42"
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
            {"resource": {"resource_type": "issue", "resource_number": "42"}},
            provider="github",
        )
        is None
    )
    # empty repository_url after normalization
    assert (
        resource_identity_from_payload(
            {"resource": {"repository_url": "///", "resource_type": "issue", "resource_number": "42"}},
            provider="github",
        )
        is None
    )


def test_normalize_repository_url_lowercases_and_strips_trailing_slash() -> None:
    assert normalize_repository_url("https://github.com/Acme/Backend/") == (
        "https://github.com/acme/backend"
    )
    assert normalize_repository_url("HTTPS://EXAMPLE.COM/") == "https://example.com"


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
    assert forward_merge(stored, incoming, is_newer=True) == {
        "status": "closed",
        "title": "new",
    }


def test_forward_merge_stale_fills_absent_only() -> None:
    """Late-event non-regression: a stale event never regresses populated state."""
    stored = {"status": "closed", "title": "done"}
    incoming = {"status": "open", "title": "stale", "labels": ["bug"]}
    merged = forward_merge(stored, incoming, is_newer=False)
    assert merged["status"] == "closed"  # NOT regressed to "open"
    assert merged["title"] == "done"  # NOT regressed to "stale"
    assert merged["labels"] == ["bug"]  # absent key filled forward


def test_forward_merge_never_erases_on_null() -> None:
    stored = {"status": "closed", "title": "done"}
    incoming = {"status": None, "title": "done", "labels": None}
    merged = forward_merge(stored, incoming, is_newer=True)
    assert merged == {"status": "closed", "title": "done"}


def test_forward_merge_does_not_mutate_stored() -> None:
    stored = {"status": "closed"}
    forward_merge(stored, {"labels": ["bug"]}, is_newer=False)
    assert stored == {"status": "closed"}


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

    await enrich_aggregate(mock_conn, _identity(), _delivery(delivery_id="d1", occurred_at=_T1, payload={"a": 1}))

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
    #            last_occurred_at, last_delivery_id, payload
    assert args[1] == "github"
    assert args[2] == "https://github.com/acme/backend"
    assert args[3] == "issue"
    assert args[4] == "42"
    assert args[5] == _T1
    assert args[6] == "d1"
    assert json.loads(args[7]) == {"status": "open"}


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
    # last payload arg is the merged jsonb; last_occurred_at/last_delivery_id
    # follow it.  Positions: (sql, provider, url, type, number, payload,
    # last_occurred_at, last_delivery_id).
    merged = json.loads(args[5])
    assert merged["status"] == "closed"  # not regressed
    assert merged["title"] == "done"  # not regressed
    assert merged["labels"] == ["bug"]  # absent key filled forward
    assert args[6] == _T2  # last_occurred_at stays at the newer event
    assert args[7] == "d2"


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
    assert args[6] == _T1
    assert args[7] == "delivery-a"


# ── Duplicate delivery skips enrichment (ingest path) ────────────────────────


@pytest.mark.asyncio
async def test_duplicate_delivery_skips_enrichment(mock_conn: AsyncMock) -> None:
    """A redelivery (no fresh reporting_deliveries row) never enriches."""
    from tests.conftest import create_client
    import uuid

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
                        "resource_type": "issue",
                        "resource_number": "42",
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
