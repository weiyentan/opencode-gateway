"""Unit tests for the reporting-ingestion endpoint (issue #479).

Verifies the SQL shape and outcome mapping of the idempotent, transactional
write path behind ``POST /api/v1/reporting/ingest/deliveries`` using a mock
asyncpg connection (no real database):

* the delivery insert uses ``ON CONFLICT (provider, delivery_id) DO NOTHING``
  and ``RETURNING id`` to discriminate accepted vs duplicate;
* the state-trail insert uses
  ``ON CONFLICT (provider, delivery_id, state, occurred_at) DO NOTHING``;
* the delivery insert precedes the trail insert (the trail is gated on a
  fresh delivery row);
* a fresh row → ``accepted``; no row → ``duplicate``; a DB error → ``rejected``;
* an unknown ``schema_version`` → 400; a missing/invalid collector token → 401.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import create_client

# ── Shared test data ────────────────────────────────────────────────────────

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _auth_row(
    client_id: uuid.UUID = _CLIENT_ID,
    credential_id: uuid.UUID = _CREDENTIAL_ID,
    client_name: str = "test-client",
) -> MagicMock:
    """Return a mock row that passes require_collector_token."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "credential_id": credential_id,
        "revoked_at": None,
        "last_used_at": None,
        "client_id": client_id,
        "client_name": client_name,
        "client_is_active": True,
    }.__getitem__
    return row


def _delivery_row(delivery_record_id: uuid.UUID | None = None) -> MagicMock:
    """Return a mock ``reporting_deliveries`` insert row (RETURNING id)."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "id": delivery_record_id or uuid.uuid4(),
    }.__getitem__
    return row


def _payload(
    *,
    provider: str = "github",
    delivery_id: str = "delivery-uuid-001",
    event_type: str = "normalized",
    schema_version: str = "1.0",
) -> dict:
    return {
        "schema_version": schema_version,
        "deliveries": [
            {
                "provider": provider,
                "delivery_id": delivery_id,
                "event_type": event_type,
                "occurred_at": _utcnow().isoformat(),
                "payload": {"repository": "acme/backend"},
            }
        ],
    }


# ── SQL shape + ordering ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delivery_insert_uses_conflict_ignore(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), _delivery_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())
    assert response.status_code == 200

    delivery_sql = mock_conn.fetchrow.call_args_list[1].args[0]
    assert "ON CONFLICT (provider, delivery_id) DO NOTHING" in delivery_sql
    assert "RETURNING id" in delivery_sql


@pytest.mark.asyncio
async def test_trail_insert_uses_conflict_ignore(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), _delivery_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())
    assert response.status_code == 200

    trail_sqls = [
        call.args[0]
        for call in mock_conn.execute.call_args_list
        if "delivery_state_trails" in call.args[0]
    ]
    assert trail_sqls, "expected a delivery_state_trails insert"
    assert "ON CONFLICT (provider, delivery_id, state, occurred_at) DO NOTHING" in trail_sqls[0]


@pytest.mark.asyncio
async def test_delivery_insert_precedes_trail_insert(mock_conn: AsyncMock) -> None:
    calls: list[str] = []

    async def _recorded_fetchrow(*args, **kwargs):
        sql = args[0]
        if "collector_credentials" in sql:
            return _auth_row()
        if "reporting_deliveries" in sql:
            calls.append("delivery_insert")
            return _delivery_row()
        return None

    async def _recorded_execute(*args, **kwargs):
        sql = args[0]
        if "delivery_state_trails" in sql:
            calls.append("trail_insert")
        return "INSERT 0 1"

    mock_conn.fetchrow = AsyncMock(side_effect=_recorded_fetchrow)
    mock_conn.execute = AsyncMock(side_effect=_recorded_execute)

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())
    assert response.status_code == 200

    assert calls == ["delivery_insert", "trail_insert"], (
        f"expected delivery insert before trail insert, got {calls}"
    )


@pytest.mark.asyncio
async def test_one_explicit_transaction_per_delivery(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), _delivery_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())
    assert response.status_code == 200

    assert mock_conn.transaction.called, "expected an explicit transaction"


# ── Outcome mapping ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_delivery_returns_accepted(mock_conn: AsyncMock) -> None:
    delivery_record_id = uuid.uuid4()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[_auth_row(), _delivery_row(delivery_record_id)]
    )
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["accepted_count"] == 1
    assert data["duplicate_count"] == 0
    assert data["rejected_count"] == 0
    assert data["results"][0]["status"] == "accepted"
    assert data["results"][0]["delivery_record_id"] == str(delivery_record_id)


@pytest.mark.asyncio
async def test_redelivery_returns_duplicate(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["accepted_count"] == 0
    assert data["duplicate_count"] == 1
    assert data["rejected_count"] == 0
    assert data["results"][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_db_error_returns_rejected(mock_conn: AsyncMock) -> None:
    async def _raise_on_delivery(*args, **kwargs):
        sql = args[0]
        if "reporting_deliveries" in sql:
            raise RuntimeError("boom")
        return _auth_row()

    mock_conn.fetchrow = AsyncMock(side_effect=_raise_on_delivery)
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["accepted_count"] == 0
    assert data["rejected_count"] == 1
    assert data["results"][0]["status"] == "rejected"
    assert "boom" in data["results"][0]["reason"]


# ── Payload redaction + tz-aware occurred_at ──────────────────────────────────


@pytest.mark.asyncio
async def test_payload_is_redacted_before_persist(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), _delivery_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    payload = _payload()
    payload["deliveries"][0]["payload"] = {
        "repository": "acme/backend",
        "api_key": "ghp_abc123",
        "nested": {"token": "supersecret"},
    }

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=payload)
    assert response.status_code == 200

    # fetchrow call[0] is the auth lookup; call[1] is the delivery insert.
    # Positional args: 0=SQL, 1=provider, 2=delivery_id, 3=event_type,
    # 4=client_id, 5=redacted payload JSON.
    delivery_call = mock_conn.fetchrow.call_args_list[1]
    assert json.loads(delivery_call.args[5]) == {
        "repository": "acme/backend",
        "api_key": "***",
        "nested": {"token": "***"},
    }


@pytest.mark.asyncio
async def test_occurred_at_requires_timezone_offset(mock_conn: AsyncMock) -> None:
    # A timezone-naive occurred_at fails validation before any write.
    naive = _payload()
    naive["deliveries"][0]["occurred_at"] = "2026-08-15T10:00:00"
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
    mock_conn.execute = AsyncMock()

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=naive)
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # only the auth lookup consumed a fetchrow slot; no delivery insert ran
    assert mock_conn.fetchrow.await_count == 1

    # A tz-aware occurred_at is accepted.
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row(), _delivery_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())
    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["status"] == "accepted"


# ── Error paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_collector_token_returns_401(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(return_value=None)

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post("/api/v1/reporting/ingest/deliveries", json=_payload())

    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_unknown_schema_version_returns_400(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post(
            "/api/v1/reporting/ingest/deliveries", json=_payload(schema_version="9.9")
        )

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_unknown_schema_version_does_not_write(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
    mock_conn.execute = AsyncMock()

    client = create_client(mock_conn)
    async with client as c:
        response = await c.post(
            "/api/v1/reporting/ingest/deliveries", json=_payload(schema_version="9.9")
        )

    assert response.status_code == 400
    # only the auth lookup consumed a fetchrow slot; no delivery insert ran
    assert mock_conn.fetchrow.await_count == 1
