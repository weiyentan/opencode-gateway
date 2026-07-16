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
"""

from __future__ import annotations

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

    Order per record: source_db check | dedup | model | session.
    """
    per_record = [None, None, None, None]  # sd, dedup, model, session
    return per_record * record_count


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
                    "input_tokens": -10,   # Pydantic will reject (ge=0)
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

        # Pydantic catches negative values (ge=0 constraint)
        assert response.status_code == 422


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
        mock_conn.fetchrow.side_effect = [
            auth,          # 1. auth
            existing_sd,   # 2. source_database check → exists (UPDATE)
            None,          # 3. dedup check → not found
            None,          # 4. model check → not found
            None,          # 5. session check → not found
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

        mock_conn.fetchrow = AsyncMock()
        # Order: auth | sd_check | dedup | model_check | session_check
        mock_conn.fetchrow.side_effect = [
            auth,            # 1. auth
            None,            # 2. source_database check → not found
            None,            # 3. dedup check → not found (proceed)
            existing_model,  # 4. model check → found (UPDATE)
            None,            # 5. session check → not found
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
