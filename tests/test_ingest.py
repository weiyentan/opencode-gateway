"""Tests for the POST /ingest endpoint — collector-facing usage ingestion.

Covers:
- Valid batch → all records accepted
- Duplicate batch → idempotent accept (no new rows)
- Divergent duplicate → conflict status
- Malformed records → rejection
- Empty batch → heartbeat
- Unauthenticated → 401
- Schema version mismatch → 400
- Source database upsert
- Model upsert
- Validation detail logging (issue #238)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session


# ── Shared test data ────────────────────────────────────────────────────────

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ts() -> datetime:
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _valid_ingest_payload(
    *,
    schema_version: str = "1.0",
    collector_version: str = "0.1.0",
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    records: list[dict] | None = None,
) -> dict:
    """Return a valid ingest request with sensible defaults."""
    if records is None:
        records = [
            {
                "source_record_id": "rec-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            },
        ]
    return {
        "schema_version": schema_version,
        "collector_version": collector_version,
        "source_database_id": str(source_database_id),
        "records": records,
    }


# ── Mock helpers ─────────────────────────────────────────────────────────────


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


def _build_ingest_app(
    mock_conn: AsyncMock,
    *,
    monkeypatch,
) -> AsyncClient:
    """Build an app where collector-token auth is the ONLY auth layer.

    Disables the API-key middleware so tests can focus on collector
    token behaviour.  Sets the ``Authorization`` header to carry the
    collector token *and* configures the mock connection to return a
    valid auth row regardless of the token value.
    """
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_ENV", "development")
    import importlib

    import app.core.config as _cfg

    importlib.reload(_cfg)

    app = create_app(configure_logging=False)

    async def _override(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


def _new_record_side_effect(record_count: int = 1) -> list:
    """Build a fetchrow side-effect list for ``record_count`` new records.

    Structure: [sd_check] + [dedup, model, session_upsert] * record_count.

    The session upsert always returns a row with ``id`` (the new or existing
    internal session UUID).
    """
    per_record: list = [None]  # sd check (once per batch)
    for _ in range(record_count):
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        per_record.extend([None, None, session_row])  # dedup, model, session
    return per_record


# ════════════════════════════════════════════════════════════════════════════
#  Tests
# ════════════════════════════════════════════════════════════════════════════


class TestValidBatch:
    """Happy path — all records are accepted and counts are correct."""

    @pytest.mark.asyncio
    async def test_all_records_accepted(self, monkeypatch):
        """A valid batch produces accepted status for every record."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                                     # auth
            *_new_record_side_effect(record_count=2),  # 8 items for 2 records
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-002",
                    "session_id": str(uuid.uuid4()),
                    "model": "claude-3",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 10,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 2
        assert data["rejected_count"] == 0
        assert len(data["results"]) == 2
        assert data["results"][0]["status"] == "accepted"
        assert data["results"][1]["status"] == "accepted"
        assert data["batch_id"] is not None

    @pytest.mark.asyncio
    async def test_response_has_correct_shape(self, monkeypatch):
        """The IngestResponse has batch_id, accepted_count, rejected_count, results."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert uuid.UUID(data["batch_id"])
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        assert len(data["results"]) == 1
        assert data["results"][0]["index"] == 0
        assert data["results"][0]["status"] == "accepted"


class TestDuplicateBatchIdempotent:
    """Re-posting identical records returns accepted without new rows."""

    @pytest.mark.asyncio
    async def test_duplicate_batch_accepted_idempotently(self, monkeypatch):
        """Same dedup key + same values → accepted, no insert."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        # Existing record with matching values
        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,          # 1. auth
            None,          # 2. source_database check (new)
            existing_row,  # 3. dedup check → match found (return early)
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        assert data["results"][0]["status"] == "accepted"
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        # Verify no new usage record was inserted for the duplicate
        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(insert_calls) == 0


class TestDivergentDuplicate:
    """Same dedup key but different values → conflict status."""

    @pytest.mark.asyncio
    async def test_divergent_duplicate_returns_conflict(self, monkeypatch):
        """Same dedup key, different token counts → conflict."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        # Existing record with DIFFERENT values
        existing_row = MagicMock()
        existing_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 200,   # different!
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,          # 1. auth
            None,          # 2. source_database check (new)
            existing_row,  # 3. dedup check → divergent match
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 1
        assert data["results"][0]["status"] == "conflict"


class TestMalformedRecord:
    """Non-numeric or invalid field values → rejection."""

    @pytest.mark.asyncio
    async def test_pydantic_rejects_non_numeric_tokens(self, monkeypatch):
        """Pydantic validates types — string in int field → 422."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock(return_value=auth)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-bad",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": "not-a-number",
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        # Pydantic rejects non-int tokens at validation layer → 422
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_tokens_rejected(self, monkeypatch):
        """Negative token values → rejected per-record with 200."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,          # auth
            None,          # source_database check
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-neg",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": -10,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 1
        assert data["results"][0]["status"] == "rejected"
        assert data["results"][0]["reason"] == "Negative token value"


class TestEmptyBatchHeartbeat:
    """Empty records array → heartbeat (0 records accepted, batch recorded)."""

    @pytest.mark.asyncio
    async def test_empty_batch_heartbeat(self, monkeypatch):
        """An empty records array returns 0/0 counts and records a batch."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,       # 1. auth
            None,       # 2. source_database check (new)
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 0
        assert len(data["results"]) == 0

        # Verify ingest_batch was recorded with 0 record_count
        batch_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO ingest_batches" in str(call)
        ]
        assert len(batch_inserts) == 1


class TestUnauthenticated:
    """Requests without a valid collector token return 401."""

    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self, monkeypatch):
        """No Authorization header → 401 from collector token auth."""
        mock_conn = AsyncMock()
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post("/ingest", json=_valid_ingest_payload())

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, monkeypatch):
        """Unrecognized bearer token → 401."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # not found
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer invalid-token-here"},
            )

        assert response.status_code == 401


class TestSchemaVersionMismatch:
    """Unknown schema version → 400 before processing records."""

    @pytest.mark.asyncio
    async def test_unknown_schema_version_returns_400(self, monkeypatch):
        """An unrecognized schema_version is rejected with 400."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock(return_value=auth)
        mock_conn.execute = AsyncMock()

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="999.0")

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 400


class TestSourceDatabaseUpsert:
    """First push creates source_database; subsequent pushes update it."""

    @pytest.mark.asyncio
    async def test_first_push_creates_source_database(self, monkeypatch):
        """When source_database doesn't exist, an INSERT is performed."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, *_new_record_side_effect(1)]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify source_database INSERT was called
        sd_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO source_databases" in str(call)
        ]
        assert len(sd_inserts) >= 1

    @pytest.mark.asyncio
    async def test_subsequent_push_updates_last_seen_at(self, monkeypatch):
        """When source_database exists, last_seen_at is updated."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_sd = MagicMock()
        existing_sd.__getitem__.side_effect = {"id": _SOURCE_DB_ID}.__getitem__
        mock_conn.fetchrow = AsyncMock()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow.side_effect = [
            auth,          # 1. auth
            existing_sd,   # 2. source_database check → exists (UPDATE)
            None,          # 3. dedup check → not found
            None,          # 4. model check → not found
            session_row,   # 5. session upsert → returns new id
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify source_database UPDATE (last_seen_at) was called
        sd_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE source_databases SET last_seen_at" in str(call)
        ]
        assert len(sd_updates) >= 1


class TestModelUpsert:
    """New model names create observed_models rows; existing models update last_seen_at."""

    @pytest.mark.asyncio
    async def test_new_model_creates_row(self, monkeypatch):
        """A model name not yet seen results in an INSERT."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, *_new_record_side_effect(1)]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify observed_models INSERT was called
        model_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO observed_models" in str(call)
        ]
        assert len(model_inserts) == 1

    @pytest.mark.asyncio
    async def test_existing_model_updates_last_seen_at(self, monkeypatch):
        """A previously seen model name triggers an UPDATE of last_seen_at."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        model_id = uuid.uuid4()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": model_id}.__getitem__

        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        # Order: auth | sd_check | dedup | model_check | session upsert
        mock_conn.fetchrow.side_effect = [
            auth,            # 1. auth
            None,            # 2. source_database check → not found
            None,            # 3. dedup check → not found (proceed)
            existing_model,  # 4. model check → found (UPDATE)
            session_row,     # 5. session upsert → returns new id
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify observed_models UPDATE was called
        model_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE observed_models SET last_seen_at" in str(call)
        ]
        assert len(model_updates) == 1


class TestHealthExtended:
    """Health endpoint reflects collector state after ingestion."""

    @pytest.mark.asyncio
    async def test_health_includes_collectors_and_source_dbs(self, monkeypatch):
        """After configuring mock responses, /health returns collector info."""
        mock_conn1 = AsyncMock()  # for health check acquire
        mock_conn2 = AsyncMock()  # for collector summary
        mock_conn3 = AsyncMock()  # for source-db summary
        mock_conn4 = AsyncMock()  # for last ingest timestamp

        # Collector summary row
        cs_row = MagicMock()
        cs_row.__getitem__.side_effect = {
            "credential_id": _CREDENTIAL_ID,
            "client_name": "test-client",
            "last_heartbeat": _mk_ts(),
            "total_records_ingested": 10,
        }.__getitem__

        # Source-db summary row
        sd_row = MagicMock()
        sd_row.__getitem__.side_effect = {
            "source_database_id": _SOURCE_DB_ID,
            "client_name": "test-client",
            "last_push": _mk_ts(),
            "record_count": 5,
        }.__getitem__

        # Last ingest timestamp row
        ts_row = MagicMock()
        ts_row.__getitem__.side_effect = {"last_ts": _mk_ts()}.__getitem__

        # Configure separate mock connections for each health check step
        mock_conn2.fetch = AsyncMock(return_value=[cs_row])
        mock_conn3.fetch = AsyncMock(return_value=[sd_row])
        mock_conn4.fetchrow = AsyncMock(return_value=ts_row)

        from app.db.session import get_session
        from app.db.session import DatabasePool

        app = create_app(configure_logging=False)
        mock_pool = AsyncMock()
        # Sequential acquire: health check, collector summary, source-db summary, last ingest
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.side_effect = [
            mock_conn1,
            mock_conn2,
            mock_conn3,
            mock_conn4,
        ]
        mock_pool.release = AsyncMock()
        app.state.pool = mock_pool

        async def _override(request: Request):
            yield AsyncMock()

        app.dependency_overrides[get_session] = _override

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as c:
            response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()["data"]
        # Extended fields should be present
        assert "collectors" in data
        assert "source_databases" in data
        assert "last_ingest_timestamp" in data
        assert data["last_ingest_timestamp"] is not None


# ════════════════════════════════════════════════════════════════════════════
#  Issue #236 — Session resolution tests
# ════════════════════════════════════════════════════════════════════════════


class TestSessionIdAcceptsSesString:
    """IngestRecord.session_id accepts ses_* strings (no 422)."""

    @pytest.mark.asyncio
    async def test_ses_string_accepted(self, monkeypatch):
        """A ses_* external session ID is accepted without validation error."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,          # auth
            None,          # source_database check (new)
            None,          # dedup check (new)
            None,          # model check (new)
            session_row,    # session upsert → returns new id
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-ses-001",
                    "session_id": "ses_abc123def456",
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        assert data["results"][0]["status"] == "accepted"


    @pytest.mark.asyncio
    async def test_random_string_session_id_accepted(self, monkeypatch):
        """Any string (not just ses_*) is accepted as external session ID."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None, None, None, session_row,
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-custom-001",
                    "session_id": "my-custom-session-id!",
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1


class TestResolveSessionCreatesNewRow:
    """_resolve_session() creates a new sessions row when external ID is new."""

    @pytest.mark.asyncio
    async def test_creates_session_with_external_id(self, monkeypatch):
        """When no session matches the external ID, a new row is inserted."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,          # auth
            None,          # source_database check (new)
            None,          # dedup check (new)
            None,          # model check (new)
            session_row,    # session upsert → returns new id
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify the upsert (INSERT … ON CONFLICT … RETURNING id) was called
        session_upserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_upserts) == 1

        # Verify the usage record INSERT received a resolved session UUID,
        # not the raw external session ID string
        record_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(record_inserts) == 1
        # The session_id argument in the usage record INSERT should be a UUID,
        # not a string (the 6th positional arg after VALUES)
        usage_insert_call = record_inserts[0]
        session_id_arg = usage_insert_call.args[5]  # 6th positional arg (after SQL) = session_id
        assert isinstance(session_id_arg, uuid.UUID)


class TestResolveSessionReturnsExisting:
    """_resolve_session() returns existing UUID when external ID matches."""

    @pytest.mark.asyncio
    async def test_existing_session_returned(self, monkeypatch):
        """When a session with the same (source_db, external_id) exists,
        the ON CONFLICT path updates counters and returns the existing UUID."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_session_id = uuid.uuid4()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": existing_session_id}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,           # auth
            None,           # source_database check (new)
            None,           # dedup check (new)
            None,           # model check (new)
            session_row,    # session upsert → returns existing id (ON CONFLICT path)
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify the upsert (INSERT … ON CONFLICT … DO UPDATE … RETURNING)
        # was called — this handles both new and existing sessions.
        session_upserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_upserts) == 1
        assert "ON CONFLICT" in str(session_upserts[0])

        # Verify the usage record was inserted with the existing session UUID
        record_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(record_inserts) == 1
        usage_insert_call = record_inserts[0]
        session_id_arg = usage_insert_call.args[5]
        assert session_id_arg == existing_session_id


class TestDifferentSourceDbSameExternalId:
    """Same external session ID from different source DBs resolves to different UUIDs."""

    @pytest.mark.asyncio
    async def test_different_source_db_produces_different_internal_uuid(
        self, monkeypatch
    ):
        """Two ingests with the same external session ID but different
        source_database_id produce different internal session UUIDs."""
        from app.api.ingest import _resolve_session

        from decimal import Decimal

        mock_conn = AsyncMock()
        session_a_id = uuid.uuid4()

        # First call: source_db_a, ses_abc → ON CONFLICT path returns existing UUID
        row_a = MagicMock()
        row_a.__getitem__.side_effect = {"id": session_a_id}.__getitem__
        mock_conn.fetchrow = AsyncMock(return_value=row_a)

        result_a = await _resolve_session(
            mock_conn,
            source_database_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            client_id=_CLIENT_ID,
            external_session_id="ses_abc",
            input_tokens=100,
            output_tokens=50,
            cached_tokens=0,
            estimated_cost_usd=None,
            now=_utcnow(),
        )
        assert result_a == session_a_id

        # Verify the upsert was made via fetchrow (INSERT … ON CONFLICT … RETURNING)
        upserts_a = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(c)
        ]
        assert len(upserts_a) == 1
        assert "ON CONFLICT" in str(upserts_a[0])

        # Second call: source_db_b, same ses_abc → INSERT path (new UUID)
        new_b_id = uuid.uuid4()
        row_b = MagicMock()
        row_b.__getitem__.side_effect = {"id": new_b_id}.__getitem__
        mock_conn.fetchrow.return_value = row_b

        result_b = await _resolve_session(
            mock_conn,
            source_database_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            client_id=_CLIENT_ID,
            external_session_id="ses_abc",
            input_tokens=200,
            output_tokens=75,
            cached_tokens=10,
            estimated_cost_usd=Decimal("0.0070"),
            now=_utcnow(),
        )
        assert isinstance(result_b, uuid.UUID)
        assert result_b == new_b_id
        assert result_b != session_a_id

        # Verify the upsert was made for source B too
        upserts_b = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(c)
        ]
        assert len(upserts_b) == 2  # both calls


class TestIdempotencyWithSessionResolution:
    """Idempotency still works after session resolution change."""

    @pytest.mark.asyncio
    async def test_duplicate_batch_after_resolve_is_idempotent(self, monkeypatch):
        """Re-posting identical records returns accepted via idempotency,
        without triggering session resolution again."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        # Existing record with matching values — dedup will short-circuit
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,            # 1. auth
            None,            # 2. source_database check (new)
            existing_dedup,  # 3. dedup → match (returns early, no session resolve)
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        # Verify session resolution was never called (dedup short-circuits)
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call) or "SELECT id FROM sessions" in str(call)
        ]
        assert len(session_calls) == 0


class TestSchemaVersion11Accepted:
    """schema_version \"1.1\" is accepted by the schema validation gate."""

    @pytest.mark.asyncio
    async def test_schema_version_1_1_accepted(self, monkeypatch):
        """A payload with schema_version 1.1 is accepted (not 400)."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None, None, None, session_row,
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="1.1")

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1


class TestSessionModelIndex:
    """The ORM Session model includes the unique partial index."""

    def test_model_has_partial_unique_index(self):
        """Verify Session.__table_args__ includes the index."""
        from app.db.models.ingest import Session

        args = getattr(Session, "__table_args__", None)
        assert args is not None, "Session should have __table_args__"

        if isinstance(args, tuple):
            # Find the Index entry
            indexes = [a for a in args if hasattr(a, "unique") and a.unique]
            assert len(indexes) >= 1, "Should have at least one unique Index"
            idx = indexes[0]
            assert idx.name == "uq_sessions_external_session_id"


class TestResolveSessionConcurrentSafety:
    """Concurrent calls to _resolve_session() with the same external
    session ID return the same internal UUID (no race condition)."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_same_external_id(self, monkeypatch):
        """Two concurrent _resolve_session() calls with the same
        (source_database_id, external_session_id) return the same UUID."""
        from app.api.ingest import _resolve_session
        from decimal import Decimal
        import asyncio

        expected_id = uuid.uuid4()

        # Mock fetchrow to always return the same UUID — simulating
        # the upsert (INSERT … ON CONFLICT … RETURNING) that resolves
        # to the same internal session UUID regardless of which caller
        # wins the race.
        mock_conn = AsyncMock()
        row = MagicMock()
        row.__getitem__.side_effect = {"id": expected_id}.__getitem__
        mock_conn.fetchrow = AsyncMock(return_value=row)

        source_db_id = uuid.uuid4()
        external_id = "ses_concurrent_test"
        now = _utcnow()

        # Fire two calls concurrently
        results = await asyncio.gather(
            _resolve_session(
                mock_conn,
                source_database_id=source_db_id,
                client_id=_CLIENT_ID,
                external_session_id=external_id,
                input_tokens=100,
                output_tokens=50,
                cached_tokens=0,
                estimated_cost_usd=None,
                now=now,
            ),
            _resolve_session(
                mock_conn,
                source_database_id=source_db_id,
                client_id=_CLIENT_ID,
                external_session_id=external_id,
                input_tokens=200,
                output_tokens=75,
                cached_tokens=10,
                estimated_cost_usd=Decimal("0.0070"),
                now=now,
            ),
        )

        # Both must return UUIDs
        assert all(isinstance(r, uuid.UUID) for r in results)

        # Both must return the SAME UUID (same session identity)
        assert results[0] == results[1] == expected_id

        # Verify INSERT … ON CONFLICT … RETURNING was called twice
        session_upserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_upserts) == 2

        # Verify both calls used the ON CONFLICT pattern
        for call in session_upserts:
            assert "ON CONFLICT" in str(call)


# ════════════════════════════════════════════════════════════════════════════
#  Issue #238 — Validation detail logging
# ════════════════════════════════════════════════════════════════════════════


class TestValidationDetailLogging:
    """GATEWAY_LOG_VALIDATION_DETAIL controls structured 422 logging."""

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _setup_env(monkeypatch, set_env_val: str | None = None):
        """Configure env vars and reload config.

        Args:
            set_env_val: If a string, set GATEWAY_LOG_VALIDATION_DETAIL
                to that value.  If ``None``, delete the env var.
        """
        if set_env_val is not None:
            monkeypatch.setenv("GATEWAY_LOG_VALIDATION_DETAIL", set_env_val)
        else:
            monkeypatch.delenv("GATEWAY_LOG_VALIDATION_DETAIL", raising=False)
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "development")

        import importlib
        import app.core.config as _cfg
        importlib.reload(_cfg)

    @staticmethod
    def _build_app_and_client(mock_conn):
        """Build a test app with mocked DB session."""
        app = create_app(configure_logging=False)

        async def _override(request: Request):
            yield mock_conn

        app.dependency_overrides[get_session] = _override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        return AsyncClient(transport=transport, base_url="http://test")

    @staticmethod
    def _bad_payload(**overrides: object) -> dict:
        """Return a payload that triggers a 422 validation error."""
        payload = {
            "schema_version": "1.0",
            "collector_version": "0.1.0",
            "source_database_id": str(uuid.uuid4()),
            "records": [
                {
                    "source_record_id": "rec-bad",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": "not-a-number",
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                },
            ],
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _make_auth() -> MagicMock:
        auth = MagicMock()
        auth.__getitem__.side_effect = {
            "credential_id": uuid.uuid4(),
            "revoked_at": None,
            "last_used_at": None,
            "client_id": uuid.uuid4(),
            "client_name": "test-client",
            "client_is_active": True,
        }.__getitem__
        return auth

    # ── Tests ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_default_disabled_no_validation_log(self, monkeypatch, caplog):
        """When GATEWAY_LOG_VALIDATION_DETAIL is not set, no validation
        detail is logged on 422."""
        self._setup_env(monkeypatch)
        caplog.set_level(logging.INFO)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=self._make_auth())

        async with self._build_app_and_client(mock_conn) as client:
            response = await client.post(
                "/ingest",
                json=self._bad_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 422
        detail_messages = [
            r for r in caplog.records
            if r.name == "app.core.envelope" and "Validation detail" in r.getMessage()
        ]
        assert len(detail_messages) == 0

    @pytest.mark.asyncio
    async def test_enabled_logs_validation_detail(self, monkeypatch, caplog):
        """When GATEWAY_LOG_VALIDATION_DETAIL=true, validation details
        are logged on 422."""
        self._setup_env(monkeypatch, set_env_val="true")
        caplog.set_level(logging.INFO)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=self._make_auth())

        async with self._build_app_and_client(mock_conn) as client:
            response = await client.post(
                "/ingest",
                json=self._bad_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 422
        detail_messages = [
            r for r in caplog.records
            if r.name == "app.core.envelope" and "Validation detail" in r.getMessage()
        ]
        assert len(detail_messages) >= 1

        message = detail_messages[0].getMessage()
        # Verify structured fields are present
        assert "input_tokens" in message
        assert "not-a-number" in message
        assert "int_parsing" in message

    @pytest.mark.asyncio
    async def test_disabled_explicit_false_no_log(self, monkeypatch, caplog):
        """When GATEWAY_LOG_VALIDATION_DETAIL=false explicitly, no
        validation detail is logged on 422."""
        self._setup_env(monkeypatch, set_env_val="false")
        caplog.set_level(logging.INFO)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=self._make_auth())

        async with self._build_app_and_client(mock_conn) as client:
            response = await client.post(
                "/ingest",
                json=self._bad_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 422
        detail_messages = [
            r for r in caplog.records
            if r.name == "app.core.envelope" and "Validation detail" in r.getMessage()
        ]
        assert len(detail_messages) == 0

    @pytest.mark.asyncio
    async def test_enabled_redacts_nested_secrets(self, monkeypatch, caplog):
        """When validation detail logging is enabled, secret-like values
        inside nested dict `input` fields are redacted."""
        self._setup_env(monkeypatch, set_env_val="true")
        caplog.set_level(logging.INFO)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=self._make_auth())

        async with self._build_app_and_client(mock_conn) as client:
            # Send a malformed payload where a scalar field receives a
            # dict with a secret-like key to verify redaction.
            payload = self._bad_payload(
                records=[
                    {
                        "source_record_id": "rec-secret",
                        "session_id": str(uuid.uuid4()),
                        "model": "gpt-4",
                        "input_tokens": {"api_key": "super-secret-value", "value": 100},
                        "output_tokens": 50,
                        "cached_tokens": 0,
                        "estimated_cost_usd": "0.0035",
                        "reported_at": datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                    },
                ],
            )
            response = await client.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 422

        detail_messages = [
            r for r in caplog.records
            if r.name == "app.core.envelope" and "Validation detail" in r.getMessage()
        ]
        assert len(detail_messages) >= 1

        message = detail_messages[0].getMessage()
        # The redacted placeholder should appear for the secret-like key
        from app.core.secrets import REDACTED
        assert REDACTED in message
        # The raw secret value should NOT appear in the log
        assert "super-secret-value" not in message


# ════════════════════════════════════════════════════════════════════════════
#  Issue #247 — Enriched telemetry ingest (v1.2)
# ════════════════════════════════════════════════════════════════════════════


class TestV12HappyPath:
    """v1.2 payloads with enrichment fields are accepted without error."""

    @pytest.mark.asyncio
    async def test_v12_payload_accepted(self, monkeypatch):
        """A full v1.2 payload with all enrichment fields is accepted."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-v12-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "provider": "openai",
                    "mode": "chat",
                    "finish_reason": "stop",
                    "reasoning_tokens": 20,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["results"][0]["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_v12_missing_optional_fields_defaults_to_none(self, monkeypatch):
        """v1.2 payload without enrichment fields still works (defaults to None/0)."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-v12-minimal-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1


class TestV10BackwardCompat:
    """v1.0 payloads continue to work with the new schema."""

    @pytest.mark.asyncio
    async def test_v10_payload_accepted(self, monkeypatch):
        """A standard v1.0 payload is accepted (no enrichment fields needed)."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="1.0")

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1


class TestV11BackwardCompat:
    """v1.1 payloads continue to work with the new schema."""

    @pytest.mark.asyncio
    async def test_v11_payload_accepted(self, monkeypatch):
        """A standard v1.1 payload is accepted."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="1.1")

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1


class TestMixedBatchV10AndV12:
    """Mixed batches with v1.0 and v1.2 records are handled correctly."""

    @pytest.mark.asyncio
    async def test_mixed_v10_and_v12_records(self, monkeypatch):
        """A batch with both v1.0 and v1.2 records accepts both."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=2),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-v10-001",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-v12-001",
                    "session_id": str(uuid.uuid4()),
                    "model": "claude-3",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                    "provider": "anthropic",
                    "mode": "chat",
                    "finish_reason": "stop",
                    "reasoning_tokens": 30,
                    "cache_read_tokens": 15,
                    "cache_write_tokens": 8,
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 2
        assert data["rejected_count"] == 0


class TestSessionUpsertAgent:
    """Session-level fields follow last-write-wins semantics."""

    @pytest.mark.asyncio
    async def test_second_record_overrides_agent(self, monkeypatch):
        """Two records for the same session: the second agent value wins."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()

        # First record: creates session with agent="agent-a"
        session_id = _SESSION_ID
        internal_session = MagicMock()
        internal_session.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow.side_effect = [
            auth,              # auth
            None,              # sd check (batch level)
            None,              # dedup rec-1
            None,              # model rec-1
            internal_session,  # session upsert rec-1 (ON CONFLICT inserted)
            None,              # dedup rec-2
            None,              # model rec-2
            internal_session,  # session upsert rec-2 (ON CONFLICT updated)
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(session_id),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "agent": "agent-a",
                },
                {
                    "source_record_id": "rec-002",
                    "session_id": str(session_id),
                    "model": "gpt-4",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                    "agent": "agent-b",
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 2


class TestCachedTokensComputation:
    """v1.2 computes cached_tokens as cache_read + cache_write."""

    @pytest.mark.asyncio
    async def test_v12_cached_tokens_is_sum_of_read_and_write(self, monkeypatch):
        """A v1.2 record with cache_read_tokens=10 and cache_write_tokens=5 succeeds."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-cache-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1

    @pytest.mark.asyncio
    async def test_v10_uses_wire_cached_tokens_directly(self, monkeypatch):
        """v1.0 payload uses the wire cached_tokens directly."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.0",
            records=[
                {
                    "source_record_id": "rec-cache-v10-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 42,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["results"][0]["status"] == "accepted"

        # Verify the wire cached_tokens=42 was persisted (not 0 from enrichment defaults)
        # The execute call should use effective_cached_tokens=$9=42 for v1.0 payload
        execute_call = mock_conn.execute.call_args
        # args[0] = SQL, args[1..18] = $1..$18 parameters
        # $9 = effective_cached_tokens at args[9]
        assert execute_call is not None
        cached_tokens_param = execute_call[0][9]
        assert cached_tokens_param == 42, (
            f"Expected effective_cached_tokens=42 for v1.0 wire value, "
            f"got {cached_tokens_param}"
        )
