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


def _add_transaction_support(mock_conn: AsyncMock) -> None:
    """Set up ``mock_conn.transaction()`` to return an async context manager
    matching the shape of ``asyncpg.Connection.transaction()``.

    Needed by tests that exercise ``_apply_replay_merge``, which wraps
    its SELECT FOR UPDATE + COALESCE UPDATE + aggregate repair inside
    ``async with conn.transaction():``."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=ctx)


def _add_quarantine_defaults(mock_conn: AsyncMock) -> None:
    """Set default return values for quarantine-related mock methods.

    ``is_quarantined()`` uses ``fetchval`` → defaults to ``False``.
    ``check_quarantine_overlap()`` uses ``fetch`` → defaults to ``[]``.
    Tests that exercise quarantine routing can override these.
    """
    mock_conn.fetchval = AsyncMock(return_value=False)
    mock_conn.fetch = AsyncMock(return_value=[])


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

    Adds transaction support to ``mock_conn`` so that the winner-path and
    replay-merge ``async with conn.transaction()`` blocks work correctly
    in tests.
    """
    _add_transaction_support(mock_conn)
    _add_quarantine_defaults(mock_conn)

    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_ENV", "development")
    import importlib

    import app.core.config as _cfg

    importlib.reload(_cfg)

    # Issue #389 restructure: patch resolve_canonical_identity to return a fixed
    # UUID without consuming fetchrow slots. Tests not exercising the quarantine
    # path don't need identity resolution items in their fetchrow sequences.
    import app.core.identity as _identity_mod
    _fixed_canonical_id = uuid.uuid4()
    monkeypatch.setattr(
        _identity_mod,
        "resolve_canonical_identity",
        AsyncMock(return_value=_fixed_canonical_id),
    )

    app = create_app(configure_logging=False)

    async def _override(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


def _new_record_side_effect(record_count: int = 1) -> list:
    """Build a fetchrow side-effect list for ``record_count`` new records.

    Structure: [sd_check] + [
        cross_identity_check,          # handler: cross-identity conflict check
        model, atomic_insert, session, # _process_one_record
        model_lookup, session_lookup, event_lookup, # _record_canonical_event
    ] * record_count.

    ``resolve_canonical_identity`` is monkeypatched in ``_build_ingest_app``
    to return a fixed UUID without consuming fetchrow slots.
    ``is_quarantined`` → fetchval (False by default) and
    ``check_quarantine_overlap`` → fetch (empty by default) are handled by
    ``_add_quarantine_defaults``.

    The atomic INSERT ON CONFLICT must return a row (winner path); the
    session upsert always returns a row with ``id`` (the new or existing
    internal session UUID).
    """
    per_record: list = [None]  # sd check (once per batch)
    # handler: cross-identity conflict check
    *_handler_routing_side_effect_items(),
    for _ in range(record_count):
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        model_lookup_row = MagicMock()
        model_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        per_record.extend([
            None,                                     # handler: cross-identity conflict check
            None, insert_row, session_row,            # _process_one_record
            model_lookup_row,                         # _record_canonical_event: model lookup
            session_lookup_row,                       # _record_canonical_event: session lookup
            None,                                     # _record_canonical_event: event lookup
        ])
    return per_record


def _canonical_event_side_effect_items() -> list:
    """Build the 3 fetchrow items needed for ``_record_canonical_event``.

    Returns: [model_lookup_row, session_lookup_row, event_lookup_row]

    After the #389 restructure, identity resolution runs in the handler
    (monkeypatched), so only model/session lookup and event creation remain.
    """
    model_lookup_row = MagicMock()
    model_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    session_lookup_row = MagicMock()
    session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    return [
        model_lookup_row,        # model lookup
        session_lookup_row,      # session lookup
        None,                    # event lookup (no existing canonical event)
    ]


def _handler_routing_side_effect_items() -> list:
    """Build the 1 fetchrow item for the handler's cross-identity conflict check
    (issue #389 restructure).

    ``resolve_canonical_identity`` is monkeypatched by ``_build_ingest_app``
    to return a fixed UUID without consuming fetchrow slots.  This helper
    provides the single slot consumed by the cross-identity conflict
    query in the handler loop (``None`` = no conflict).
    """
    return [None]


def _canonical_exists_row() -> MagicMock:
    """Return a mock row signalling that a canonical event already exists
    (used as a fetchrow response for the backfill existence check)."""
    row = MagicMock()
    # Any non-None row means "event exists"
    return row


def _projection_payload(
    *,
    schema_version: str = "1.0",
    collector_version: str = "0.1.0",
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    records: list[dict] | None = None,
    session_contexts: list[dict] | None = None,
    projects: list[dict] | None = None,
    project_directories: list[dict] | None = None,
    session_todos: list[dict] | None = None,
) -> dict:
    """Return a valid ingest request with projection arrays — defaults to empty."""
    payload = _valid_ingest_payload(
        schema_version=schema_version,
        collector_version=collector_version,
        source_database_id=source_database_id,
        records=records or [],
    )
    payload["session_contexts"] = session_contexts or []
    payload["projects"] = projects or []
    payload["project_directories"] = project_directories or []
    payload["session_todos"] = session_todos or []
    return payload


def _mk_session_context_payload(
    *,
    external_session_id: str = "ses_ctx_test",
    title: str = "Test Session",
    session_model: str | None = "gpt-4",
    external_project_id: str | None = None,
    parent_external_session_id: str | None = None,
    **kwargs,
) -> dict:
    defaults: dict = {
        "external_session_id": external_session_id,
        "title": title,
        "source_input_tokens": 1000,
        "source_output_tokens": 500,
        "source_payload": {"summary": "test session context"},
    }
    if session_model is not None:
        defaults["session_model"] = session_model
    if external_project_id is not None:
        defaults["external_project_id"] = external_project_id
    if parent_external_session_id is not None:
        defaults["parent_external_session_id"] = parent_external_session_id
    defaults.update(kwargs)
    return defaults


def _mk_project_payload(
    *,
    external_project_id: str = "proj_test",
    name: str = "Test Project",
    **kwargs,
) -> dict:
    defaults: dict = {
        "external_project_id": external_project_id,
        "name": name,
        "source_payload": {"summary": "test project"},
    }
    defaults.update(kwargs)
    return defaults


def _mk_directory_payload(
    *,
    directory: str = "/tmp/test",
    directory_type: str = "workspace",
    **kwargs,
) -> dict:
    defaults: dict = {
        "directory": directory,
        "directory_type": directory_type,
    }
    defaults.update(kwargs)
    return defaults


def _mk_todo_payload(
    *,
    external_session_id: str = "ses_todo_test",
    content: str = "Test todo item",
    position: int = 1,
    **kwargs,
) -> dict:
    defaults: dict = {
        "external_session_id": external_session_id,
        "content": content,
        "position": position,
    }
    defaults.update(kwargs)
    return defaults


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
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        # Existing record with matching values
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        # Existing model (idempotent — harmless)
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        # lock_row for _apply_replay_merge FOR UPDATE (all-None — no enrichment)
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert → existing model
            None,             # 4. atomic INSERT ON CONFLICT → conflict (loser)
            existing_dedup,   # 5. dedup query → identical match
            lock_row,         # 6. _apply_replay_merge: SELECT FOR UPDATE
            None,             # 7. existence check → no canonical event (backfill)
            *_canonical_event_side_effect_items(),  # 8-10. _record_canonical_event
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

        # Event was backfilled — event_id and attempt_id are populated
        assert data["results"][0]["event_id"] is not None
        assert data["results"][0]["attempt_id"] is not None

        # Verify no new legacy usage record was inserted for the duplicate
        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(insert_calls) == 0

    @pytest.mark.asyncio
    async def test_v12_duplicate_uses_effective_cached_tokens(self, monkeypatch):
        """v1.2 duplicates compare against stored cache_read + cache_write tokens."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            None,
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,  # model upsert
            None,            # atomic INSERT → conflict
            existing_dedup,  # dedup query → identical
            lock_row,        # _apply_replay_merge: SELECT FOR UPDATE
            None,            # existence check → no canonical event (backfill)
            *_canonical_event_side_effect_items(),  # model, session, event lookups
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
        assert data["rejected_count"] == 0
        assert data["results"][0]["status"] == "accepted"
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

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
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 200,   # different!
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert → existing model
            None,             # 4. atomic INSERT ON CONFLICT → conflict (loser)
            existing_dedup,   # 5. dedup query → divergent match
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
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
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
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
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
        # handler: cross-identity conflict check
        *_handler_routing_side_effect_items(),
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow.side_effect = [
            auth,          # 1. auth
            existing_sd,   # 2. source_database check → exists (UPDATE)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,          # 3. model check → not found
            insert_row,    # 4. atomic INSERT → winner
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
        # handler: cross-identity conflict check
        *_handler_routing_side_effect_items(),
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        # Order: auth | sd_check | model | atomic_insert | session
        mock_conn.fetchrow.side_effect = [
            auth,               # 1. auth
            None,               # 2. source_database check → not found
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,     # 3. model check → found (UPDATE)
            insert_row,         # 4. atomic INSERT → winner
            session_row,        # 5. session upsert → returns new id
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,          # auth
            None,          # source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,          # model check (new)
            insert_row,    # atomic INSERT → winner
            session_row,   # session upsert → returns new id
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            None, insert_row, session_row,
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,          # auth
            None,          # source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,          # model check (new)
            insert_row,    # atomic INSERT → winner
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

        # Verify the atomic usage-record INSERT was attempted via fetchrow
        # and succeeded (winner path), then session_id was backfilled via
        # an UPDATE execute call.
        record_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(record_inserts) == 1
        # The session_id argument in the atomic INSERT is NULL,
        # then backfilled via UPDATE after session resolution.
        session_id_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records SET session_id" in str(call)
        ]
        assert len(session_id_updates) == 1
        # The UPDATE's session_id argument must be a UUID, not a string
        update_call = session_id_updates[0]
        session_id_arg = update_call.args[1]  # 2nd positional arg = session_id
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,           # auth
            None,           # source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,           # model check (new)
            insert_row,     # atomic INSERT → winner
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

        # Verify the session_id was backfilled via UPDATE after session
        # resolution (atomic INSERT uses NULL initially).
        session_id_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records SET session_id" in str(call)
        ]
        assert len(session_id_updates) == 1
        update_call = session_id_updates[0]
        session_id_arg = update_call.args[1]
        assert session_id_arg == existing_session_id


class TestDifferentSourceDbSameExternalId:
    """Same external session ID from different source DBs resolves to different UUIDs."""

    @pytest.mark.asyncio
    async def test_different_source_db_produces_different_internal_uuid(
        self, monkeypatch
    ):
        """Two ingests with the same external session ID but different
        source_database_id produce different internal session UUIDs."""
        from decimal import Decimal

        from app.api.ingest import _resolve_session

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
        # Existing record with matching values — atomic INSERT will conflict
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert → existing
            None,             # 4. atomic INSERT ON CONFLICT → conflict (loser)
            existing_dedup,   # 5. dedup query → identical match
            lock_row,         # 6. _apply_replay_merge: SELECT FOR UPDATE
            _canonical_exists_row(),  # 7. existence check → canonical event exists
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
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            None, insert_row, session_row,
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
        import asyncio
        from decimal import Decimal

        from app.api.ingest import _resolve_session

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
        insert_row_1 = MagicMock()
        insert_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        insert_row_2 = MagicMock()
        insert_row_2.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow.side_effect = [
            auth,              # auth
            None,              # sd check (batch level)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,              # model rec-1
            insert_row_1,      # atomic INSERT rec-1 → winner
            internal_session,  # session upsert rec-1 (ON CONFLICT inserted)
            *_canonical_event_side_effect_items(),  # canonical event rec-1
            None,              # model rec-2
            insert_row_2,      # atomic INSERT rec-2 → winner
            internal_session,  # session upsert rec-2 (ON CONFLICT updated)
            *_canonical_event_side_effect_items(),  # canonical event rec-2
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
        # The atomic INSERT ON CONFLICT uses effective_cached_tokens at position
        # $8 in the fetchrow call (session_id is NULL inline, so all indices
        # shifted down by one).
        atomic_insert_call = next(
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        )
        assert atomic_insert_call is not None
        # $8 = effective_cached_tokens at args[0][8]
        cached_tokens_param = atomic_insert_call[0][8]
        assert cached_tokens_param == 42, (
            f"Expected effective_cached_tokens=42 for v1.0 wire value, "
            f"got {cached_tokens_param}"
        )


class TestSessionCacheTokensUpdate:
    """Ingest records with cache_read_tokens/cache_write_tokens update session totals."""

    @pytest.mark.asyncio
    async def test_v12_cache_tokens_flow_to_session(self, monkeypatch):
        """A v1.2 record with cache_read_tokens=10, cache_write_tokens=5
        propagates both to the session upsert alongside combined total_cached_tokens.

        The session should receive:
        - total_cached_tokens += 15 (combined)
        - total_cache_read_tokens += 10
        - total_cache_write_tokens += 5
        """
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
                    "source_record_id": "rec-cache-002",
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
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["results"][0]["status"] == "accepted"

        # Verify the session upsert SQL received the new columns
        session_call = next(
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        )
        # The positional args to fetchrow: args[0] is the SQL, args[1:] are $1..$N
        # $8 = cached_tokens       = 15 (combined: 10 + 5)
        # $9 = cache_read_tokens   = 10
        # $10 = cache_write_tokens = 5
        combined_cached = session_call[0][8]   # $8 → effective_cached_tokens
        cache_read = session_call[0][9]         # $9 → cache_read_tokens
        cache_write = session_call[0][10]        # $10 → cache_write_tokens
        assert combined_cached == 15, (
            f"Expected combined cached_tokens=15, got {combined_cached}"
        )
        assert cache_read == 10, (
            f"Expected cache_read_tokens=10, got {cache_read}"
        )
        assert cache_write == 5, (
            f"Expected cache_write_tokens=5, got {cache_write}"
        )

    @pytest.mark.asyncio
    async def test_v12_cache_tokens_default_to_zero_when_absent(self, monkeypatch):
        """When cache_read/cache_write are not present in the record,
        the session upsert receives 0 for both."""
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
                    "source_record_id": "rec-v10-no-cache-fields",
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
        assert response.json()["data"]["accepted_count"] == 1

        # The session upsert should still receive 0 for cache_read/write
        session_call = next(
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        )
        # For v1.0 payload with wire cached_tokens=42:
        # $8 = effective_cached_tokens = 42
        # $9 = cache_read_tokens       = 0
        # $10 = cache_write_tokens     = 0
        combined = session_call[0][8]
        cache_read = session_call[0][9]
        cache_write = session_call[0][10]
        assert combined == 42, f"Expected combined=42, got {combined}"
        assert cache_read == 0, f"Expected cache_read=0, got {cache_read}"
        assert cache_write == 0, f"Expected cache_write=0, got {cache_write}"

    @pytest.mark.asyncio
    async def test_multiple_records_accumulate_cache_tokens(self, monkeypatch):
        """Two v1.2 records for the same session accumulate cache tokens
        in the session upsert — each call increments by its own values."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        shared_session_id = uuid.uuid4()

        # Helper: create a session row mock returning the same UUID each time
        def _session_row(_id: uuid.UUID = shared_session_id):
            r = MagicMock()
            r.__getitem__.side_effect = {"id": _id}.__getitem__
            return r

        def _insert_row():
            r = MagicMock()
            r.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
            return r

        mock_conn.fetchrow.side_effect = [
            auth,                    # 0. auth
            None,                    # 1. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,                    # 2. model gpt-4 (new)
            _insert_row(),           # 3. atomic INSERT rec-001 → winner
            _session_row(),          # 4. session upsert rec-001 → returns shared id
            *_canonical_event_side_effect_items(),  # canonical event rec-001
            None,                    # 5. model claude-3 (new)
            _insert_row(),           # 6. atomic INSERT rec-002 → winner
            _session_row(),          # 7. session upsert rec-002 → returns shared id
            *_canonical_event_side_effect_items(),  # canonical event rec-002
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(shared_session_id),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                },
                {
                    "source_record_id": "rec-002",
                    "session_id": str(shared_session_id),
                    "model": "claude-3",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 20,
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

        # Collect all session upsert calls
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 2

        # First record: cache_read=10, cache_write=5, combined=15
        first_call = session_calls[0]
        assert first_call[0][8] == 15, (
            f"First record: expected combined=15, got {first_call[0][8]}"
        )
        assert first_call[0][9] == 10, (
            f"First record: expected cache_read=10, got {first_call[0][9]}"
        )
        assert first_call[0][10] == 5, (
            f"First record: expected cache_write=5, got {first_call[0][10]}"
        )

        # Second record: cache_read=20, cache_write=8, combined=28
        second_call = session_calls[1]
        assert second_call[0][8] == 28, (
            f"Second record: expected combined=28, got {second_call[0][8]}"
        )
        assert second_call[0][9] == 20, (
            f"Second record: expected cache_read=20, got {second_call[0][9]}"
        )
        assert second_call[0][10] == 8, (
            f"Second record: expected cache_write=8, got {second_call[0][10]}"
        )

    @pytest.mark.asyncio
    async def test_mixed_v10_and_v12_same_session(self, monkeypatch):
        """A v1.0 and v1.2 record for the same session both contribute
        to the session upsert.  The v1.0 record passes wire cached_tokens
        as $8 and 0 for $9/$10; the v1.2 record passes cache_read/write
        separately."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        shared_session_id = uuid.uuid4()

        def _session_row(_id: uuid.UUID = shared_session_id):
            r = MagicMock()
            r.__getitem__.side_effect = {"id": _id}.__getitem__
            return r

        def _insert_row():
            r = MagicMock()
            r.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
            return r

        mock_conn.fetchrow.side_effect = [
            auth,                    # 0. auth
            None,                    # 1. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,                    # 2. model gpt-4 (new)
            _insert_row(),           # 3. atomic INSERT rec-v10 → winner
            _session_row(),          # 4. session upsert rec-v10 → shared id
            *_canonical_event_side_effect_items(),  # canonical event rec-v10
            None,                    # 5. model claude-3 (new)
            _insert_row(),           # 6. atomic INSERT rec-v12 → winner
            _session_row(),          # 7. session upsert rec-v12 → shared id
            *_canonical_event_side_effect_items(),  # canonical event rec-v12
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Batch schema_version must match the first record's era — we use
        # "1.2" because the batch-level version only controls the schema
        # gate, individual records can be minimal v1.0 style.
        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                # v1.0-style record (no enrichment fields)
                {
                    "source_record_id": "rec-v10",
                    "session_id": str(shared_session_id),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 42,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                # v1.2-style record with cache_read/cache_write
                {
                    "source_record_id": "rec-v12",
                    "session_id": str(shared_session_id),
                    "model": "claude-3",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 15,
                    "cache_write_tokens": 7,
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

        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 2

        # v1.0 record: uses wire cached_tokens=42, no cache_read/write
        v10_call = session_calls[0]
        assert v10_call[0][8] == 42, (
            f"v1.0: expected combined=42, got {v10_call[0][8]}"
        )
        assert v10_call[0][9] == 0, (
            f"v1.0: expected cache_read=0, got {v10_call[0][9]}"
        )
        assert v10_call[0][10] == 0, (
            f"v1.0: expected cache_write=0, got {v10_call[0][10]}"
        )

        # v1.2 record: cache_read=15, cache_write=7, combined=22
        v12_call = session_calls[1]
        assert v12_call[0][8] == 22, (
            f"v1.2: expected combined=22, got {v12_call[0][8]}"
        )
        assert v12_call[0][9] == 15, (
            f"v1.2: expected cache_read=15, got {v12_call[0][9]}"
        )
        assert v12_call[0][10] == 7, (
            f"v1.2: expected cache_write=7, got {v12_call[0][10]}"
        )

    @pytest.mark.asyncio
    async def test_v12_zero_cache_write_tokens(self, monkeypatch):
        """A v1.2 record with explicitly zero cache_write_tokens passes 0."""
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
                    "source_record_id": "rec-zero-cw",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 0,
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

        session_call = next(
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        )
        # cache_read=10 + cache_write=0 → combined=10
        assert session_call[0][8] == 10, (
            f"Expected combined=10, got {session_call[0][8]}"
        )
        assert session_call[0][9] == 10, (
            f"Expected cache_read=10, got {session_call[0][9]}"
        )
        assert session_call[0][10] == 0, (
            f"Expected cache_write=0, got {session_call[0][10]}"
        )

    @pytest.mark.asyncio
    async def test_v12_missing_cache_write_tokens_defaults_zero(self, monkeypatch):
        """A v1.2 record with None cache_write_tokens passes 0 for cache_write.
        Since cache_read is also None, effective_cached_tokens = wire value."""
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
                    "source_record_id": "rec-no-cw",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 30,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    # intentionally omit cache_read_tokens and cache_write_tokens
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

        session_call = next(
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        )
        # Both cache_read and cache_write are None → use wire cached_tokens=30
        # cache_read/cache_write default to 0 in the upsert
        assert session_call[0][8] == 30, (
            f"Expected combined=30, got {session_call[0][8]}"
        )
        assert session_call[0][9] == 0, (
            f"Expected cache_read=0, got {session_call[0][9]}"
        )
        assert session_call[0][10] == 0, (
            f"Expected cache_write=0, got {session_call[0][10]}"
        )


# ════════════════════════════════════════════════════════════════════════════
#  Issue #316 — Backfill migration (0017) session cache-token totals
# ════════════════════════════════════════════════════════════════════════════


class TestSessionCacheTokenBackfill:
    """Backfill migration (0017) recomputes session cache-token totals from
    raw opencode_usage_records for sessions ingested before the per-category
    cache columns were added in migration 0016."""

    def test_backfill_aggregation_sums_correctly(self):
        """The SQL aggregation in the backfill migration should:
        - SUM raw record cache columns grouped by session_id
        - COALESCE to 0 for sessions with no records
        """
        # Simulate raw records in opencode_usage_records
        raw = [
            ("sid-a", 5, 10, 3),   # (session_id, cache_write, cache_read, cached)
            ("sid-a", 3, 7, 2),
            ("sid-b", 20, 15, 10),
            ("sid-b", 0, 0, 0),
        ]

        from collections import defaultdict
        totals = defaultdict(lambda: {"cw": 0, "cr": 0, "ct": 0})
        for sid, cw, cr, ct in raw:
            totals[sid]["cw"] += cw
            totals[sid]["cr"] += cr
            totals[sid]["ct"] += ct

        # Session A: 5+3=8 cache_write, 10+7=17 cache_read, 3+2=5 cached
        assert totals["sid-a"]["cw"] == 8
        assert totals["sid-a"]["cr"] == 17
        assert totals["sid-a"]["ct"] == 5

        # Session B: 20+0=20 cache_write, 15+0=15 cache_read, 10+0=10 cached
        assert totals["sid-b"]["cw"] == 20
        assert totals["sid-b"]["cr"] == 15
        assert totals["sid-b"]["ct"] == 10

    def test_backfill_handles_session_with_no_raw_records(self):
        """COALESCE(SUM(...), 0) ensures sessions with no matching raw
        records receive 0 for all cache-token totals."""
        from collections import defaultdict
        totals = defaultdict(lambda: {"cw": 0, "cr": 0, "ct": 0})

        # No records added for "sid-empty" — COALESCE gives 0
        assert totals["sid-empty"]["cw"] == 0
        assert totals["sid-empty"]["cr"] == 0
        assert totals["sid-empty"]["ct"] == 0

    @pytest.mark.asyncio
    async def test_backfill_matches_ingest_accumulation(self, monkeypatch):
        """When multiple records are ingested for the same session,
        the session-upsert parameter values per record match what the
        backfill would SUM across all records.

        On the second record, the per-record cache tokens are correctly
        passed to the upsert SQL (accumulation happens in the DB via
        ``ON CONFLICT ... DO UPDATE SET x = x + $param``).
        """
        mock_conn = AsyncMock()
        auth = _auth_row()

        # Two records for the same session.
        # Structure: [auth] + [sd_check] + [model, insert, session, *canonical] * 2
        fetchrow_side_effects = [auth, None]  # auth + source-database check
        for _ in range(2):
            insert_row = MagicMock()
            insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
            session_row = MagicMock()
            session_row.__getitem__.side_effect = {"id": _SESSION_ID}.__getitem__
            fetchrow_side_effects.extend(
                [None, insert_row, session_row]
            )  # model, atomic_insert, session
            fetchrow_side_effects.extend(
                _canonical_event_side_effect_items()
            )  # canonical event recording

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = fetchrow_side_effects
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-multi-001",
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
                {
                    "source_record_id": "rec-multi-002",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 200,
                    "output_tokens": 100,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0070",
                    "reported_at": _mk_ts().isoformat(),
                    "cache_read_tokens": 20,
                    "cache_write_tokens": 15,
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

        # There should be 2 session upsert calls (one per record)
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 2, (
            f"Expected 2 session upserts for 2 records, got {len(session_calls)}"
        )

        # First record per-record params: cache_read=10, cache_write=5, cached=15
        first = session_calls[0]
        assert first[0][8] == 15, f"First record cached_tokens: expected 15, got {first[0][8]}"
        assert first[0][9] == 10, f"First record cache_read_tokens: expected 10, got {first[0][9]}"
        assert first[0][10] == 5, f"First record cache_write_tokens: expected 5, got {first[0][10]}"

        # Second record per-record params: cache_read=20, cache_write=15, cached=35
        second = session_calls[1]
        assert second[0][8] == 35, f"Second record cached_tokens: expected 35, got {second[0][8]}"
        assert second[0][9] == 20, f"Second record cache_read_tokens: expected 20, got {second[0][9]}"
        assert second[0][10] == 15, f"Second record cache_write_tokens: expected 15, got {second[0][10]}"

        # The backfill would SUM these two records:
        # total_cache_read_tokens  = 10 + 20 = 30
        # total_cache_write_tokens = 5 + 15 = 20
        # total_cached_tokens      = 15 + 35 = 50
        # (Accumulation in the DB via ON CONFLICT DO UPDATE would produce the same totals)


class TestProjectionBackwardCompat:
    """Payloads without projection arrays return projection counts of 0."""

    @pytest.mark.asyncio
    async def test_omitted_projection_arrays_produce_zero_counts(self, monkeypatch):
        """A payload with no projection arrays returns projection_accepted_count=0."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Use the standard payload without projection arrays
        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 0
        # Ensure usage-record fields are unchanged
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0

    @pytest.mark.asyncio
    async def test_payload_with_empty_projection_arrays(self, monkeypatch):
        """Explicitly empty projection arrays also produce zero counts."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload()
        # Add empty projection arrays explicitly
        payload["session_contexts"] = []
        payload["projects"] = []
        payload["project_directories"] = []
        payload["session_todos"] = []

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 0

    @pytest.mark.asyncio
    async def test_empty_batch_with_no_projections_returns_200(self, monkeypatch):
        """Empty batch with no projection arrays returns 200 with zero counts."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,       # auth
            None,       # source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
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
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 0


class TestSessionContextUpsert:
    """Session context projections are upserted by (source_database_id, external_session_id)."""

    @pytest.mark.asyncio
    async def test_single_session_context_accepted(self, monkeypatch):
        """A single session context item is accepted via upsert."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        # Side-effect: auth, sd check (new), resolve session (not found),
        # handler: cross-identity conflict check
        *_handler_routing_side_effect_items(),
        # resolve parent (not present), resolve project (not present)
        mock_conn.fetchrow.side_effect = [
            auth,  # 1. auth
            None,  # 2. sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,  # 3. resolve session_id for ctx
            None,  # 4. resolve source_project_id for ctx
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[_mk_session_context_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        # Verify INSERT INTO opencode_session_contexts was called
        sc_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_contexts" in str(call)
        ]
        assert len(sc_inserts) == 1
        assert "ON CONFLICT" in str(sc_inserts[0])

    @pytest.mark.asyncio
    async def test_session_context_model_key_maps_to_session_model(self, monkeypatch):
        """The collector's `model` key (pre-#46) populates session_model on ingest."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,  # 1. auth
            None,  # 2. sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,  # 3. resolve session_id for ctx
            None,  # 4. resolve source_project_id for ctx
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[
                _mk_session_context_payload(
                    session_model=None,
                    model="claude-sonnet-4-20250514",
                ),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["projection_accepted_count"] == 1

        # The model value must reach the upsert as the session_model parameter
        sc_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_contexts" in str(call)
        ]
        assert len(sc_inserts) == 1
        assert "claude-sonnet-4-20250514" in str(sc_inserts[0])

    @pytest.mark.asyncio
    async def test_session_context_session_model_key_maps_to_session_model(self, monkeypatch):
        """The corrected collector's `session_model` key also maps to session_model."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,  # 1. auth
            None,  # 2. sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,  # 3. resolve session_id for ctx
            None,  # 4. resolve source_project_id for ctx
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[
                _mk_session_context_payload(session_model="claude-sonnet-4-20250514"),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["projection_accepted_count"] == 1

        # The session_model value must reach the upsert as the session_model parameter
        sc_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_contexts" in str(call)
        ]
        assert len(sc_inserts) == 1
        assert "claude-sonnet-4-20250514" in str(sc_inserts[0])

    @pytest.mark.asyncio
    async def test_session_context_preserves_first_seen_on_upsert(self, monkeypatch):
        """On conflict, first_seen_at is NOT in the DO UPDATE SET clause."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,
            None,  # sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,  # resolve session_id
            None,  # resolve source_project_id
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[_mk_session_context_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Verify that first_seen_at is NOT in the DO UPDATE SET clause
        sc_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_contexts" in str(call)
        ]
        sql = str(sc_inserts[0])
        assert "last_seen_at = EXCLUDED.last_seen_at" in sql
        assert "first_seen_at" not in (sql.split("DO UPDATE SET")[1] if "DO UPDATE SET" in sql else sql)

    @pytest.mark.asyncio
    async def test_session_context_resolves_parent_session(self, monkeypatch):
        """When parent_external_session_id is provided and a matching session
        exists, parent_session_id is resolved to the internal UUID."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        parent_session_id = uuid.uuid4()

        # We need: auth, sd check, resolve session_id (not found), resolve parent (found!)
        # handler: cross-identity conflict check
        *_handler_routing_side_effect_items(),
        mock_conn.fetchrow.side_effect = [
            auth,                      # 1. auth
            None,                      # 2. sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,                      # 3. resolve session_id for ctx → not found
            MagicMock(__getitem__=({"id": parent_session_id}).__getitem__),  # 4. resolve parent_session_id → found
            None,                      # 5. resolve source_project_id → not found
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[
                _mk_session_context_payload(
                    parent_external_session_id="ses_parent",
                ),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        # Check that parent session resolve was called
        resolve_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "SELECT id FROM sessions WHERE source_database_id" in str(call)
        ]
        assert len(resolve_calls) >= 2  # one for session, one for parent

    @pytest.mark.asyncio
    async def test_session_context_resolves_source_project(self, monkeypatch):
        """When external_project_id is provided and a matching project exists,
        source_project_id is resolved to the internal UUID."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        project_id = uuid.uuid4()

        mock_conn.fetchrow.side_effect = [
            auth,
            None,  # sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,  # resolve session_id → not found
            None,  # resolve parent → not applicable (no parent)
            MagicMock(__getitem__=({"id": project_id}).__getitem__),  # resolve source_project_id → found
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[
                _mk_session_context_payload(external_project_id="proj_abc"),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["projection_accepted_count"] == 1

    @pytest.mark.asyncio
    async def test_multiple_session_contexts_accepted(self, monkeypatch):
        """Two session contexts in the same batch are both accepted."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()

        # Side-effects for each context: resolve session, resolve source_project
        mock_conn.fetchrow.side_effect = [
            auth, None,  # auth, sd
            None,             # ctx1: resolve session_id → not found
            None,             # ctx1: resolve source_project → not found
            None,             # ctx2: resolve session_id → not found
            None,             # ctx2: resolve source_project → not found
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_contexts=[
                _mk_session_context_payload(external_session_id="ses_ctx_1"),
                _mk_session_context_payload(external_session_id="ses_ctx_2"),
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
        assert data["projection_accepted_count"] == 2
        assert data["projection_rejected_count"] == 0


class TestProjectUpsert:
    """Source projects are upserted by (source_database_id, external_project_id)."""

    @pytest.mark.asyncio
    async def test_single_project_accepted(self, monkeypatch):
        """A single project item is accepted via upsert."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,       # auth
            None,       # sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            projects=[_mk_project_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        # Verify INSERT INTO opencode_source_projects was called
        proj_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_source_projects" in str(call)
        ]
        assert len(proj_inserts) == 1
        assert "ON CONFLICT" in str(proj_inserts[0])

    @pytest.mark.asyncio
    async def test_multiple_projects_accepted(self, monkeypatch):
        """Two projects in the same batch are both upserted."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth, None,  # auth, sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            projects=[
                _mk_project_payload(external_project_id="proj_1"),
                _mk_project_payload(external_project_id="proj_2"),
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
        assert data["projection_accepted_count"] == 2

        proj_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_source_projects" in str(call)
        ]
        assert len(proj_inserts) == 2

    @pytest.mark.asyncio
    async def test_project_preserves_first_seen_on_upsert(self, monkeypatch):
        """On conflict, first_seen_at is not in the DO UPDATE SET clause."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            projects=[_mk_project_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200

        proj_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_source_projects" in str(call)
        ]
        sql = str(proj_inserts[0])
        assert "last_seen_at = EXCLUDED.last_seen_at" in sql
        do_update = sql.split("DO UPDATE SET")[1]
        assert "first_seen_at" not in do_update


class TestProjectDirectoryReplace:
    """Project directories are replaced per source_database scope."""

    @pytest.mark.asyncio
    async def test_single_directory_accepted(self, monkeypatch):
        """A single directory item triggers DELETE + INSERT."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[_mk_directory_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        # Verify DELETE was called before INSERT
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_project_directories" in str(call)
        ]
        assert len(delete_calls) == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_multiple_directories_replace(self, monkeypatch):
        """A batch of directories deletes old rows once then inserts all new ones."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory="/tmp/a"),
                _mk_directory_payload(directory="/tmp/b"),
                _mk_directory_payload(directory="/tmp/c"),
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
        assert data["projection_accepted_count"] == 3
        assert data["projection_rejected_count"] == 0

        # Only one DELETE, 3 INSERTs
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_project_directories" in str(call)
        ]
        assert len(delete_calls) == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 3

    @pytest.mark.asyncio
    async def test_duplicate_directories_collapsed(self, monkeypatch):
        """Duplicate directory paths in one snapshot produce one row each."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory="/tmp/a"),
                _mk_directory_payload(directory="/tmp/b"),
                _mk_directory_payload(directory="/tmp/a"),
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
        assert data["projection_accepted_count"] == 2
        assert data["projection_rejected_count"] == 0

        # One DELETE, then exactly one INSERT per distinct directory
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_project_directories" in str(call)
        ]
        assert len(delete_calls) == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 2

        inserted_dirs = sorted(call.args[4] for call in insert_calls)
        assert inserted_dirs == ["/tmp/a", "/tmp/b"]

    @pytest.mark.asyncio
    async def test_blank_directories_filtered(self, monkeypatch):
        """Blank and whitespace-only directory paths never reach INSERT.

        Issue #413: filtered entries are reported in
        ``projection_rejected_count`` so the response shows how many
        projection items were dropped.
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory="/tmp/a"),
                _mk_directory_payload(directory="   "),
                _mk_directory_payload(directory="\t\n"),
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
        assert data["projection_accepted_count"] == 1
        # 2 whitespace-only entries filtered per item and reported
        assert data["projection_rejected_count"] == 2

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 1
        assert insert_calls[0].args[4] == "/tmp/a"

    @pytest.mark.asyncio
    async def test_empty_directory_filtered_per_item(self, monkeypatch):
        """An empty directory string is filtered per item, not rejected at batch level.

        Issue #413: ``directory=""`` previously failed ``min_length=1`` schema
        validation and rejected the entire /ingest batch with 422.  Empty
        strings now pass validation and are dropped per projection item,
        matching the whitespace-only filtering behaviour.
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory=""),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        # Empty directory is filtered per item — the batch still succeeds
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 0

    @pytest.mark.asyncio
    async def test_all_blank_directories_preserve_existing_rows(self, monkeypatch):
        """An all-blank directory batch must NOT delete previously-stored rows.

        Issue #413 review finding: when the batch is non-empty but every
        entry filters out as blank/whitespace-only, the function returns
        early with no DELETE — existing directory rows for the scope are
        preserved.  An explicit empty array still clears (covered by the
        sibling test that asserts the DELETE runs).
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory=""),
                _mk_directory_payload(directory="   "),
                _mk_directory_payload(directory="\t\n"),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        # Batch still succeeds; all three entries reported as rejected
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 3

        # No DELETE and no INSERT issued — existing rows preserved
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_project_directories" in str(call)
        ]
        assert len(delete_calls) == 0
        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 0

    @pytest.mark.asyncio
    async def test_empty_directories_array_clears(self, monkeypatch):
        """An explicit empty ``project_directories`` array still runs the DELETE.

        The all-blank guard only applies when the batch contained entries
        that were filtered out.  ``[]`` is an authoritative "no directories"
        snapshot and must preserve the pre-existing replace/clear semantics.
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(records=[], project_directories=[])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 0

        # DELETE still runs (clear semantics); no INSERT
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_project_directories" in str(call)
        ]
        assert len(delete_calls) == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 0

    @pytest.mark.parametrize("blank_directory", ["", "   ", "\t\n"])
    @pytest.mark.asyncio
    async def test_valid_record_with_blank_directory_ingests(
        self, monkeypatch, blank_directory,
    ):
        """A valid usage record ingests even when a directory entry is blank.

        Issue #413 regression: an empty or whitespace-only directory entry
        must not 422 the batch or block the valid usage record — the record
        is written to both ``usage_events`` and ``opencode_usage_records``,
        the valid directory entry is stored, and the blank entry is reported
        as rejected.
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
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
            ],
            project_directories=[
                _mk_directory_payload(directory="/tmp/a"),
                _mk_directory_payload(directory=blank_directory),
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        # Batch succeeds — no whole-batch 422
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["results"][0]["status"] == "accepted"
        # Valid directory stored, blank entry reported as rejected
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 1

        # Valid usage record written to both tables
        usage_events_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(usage_events_inserts) == 1
        # The legacy opencode_usage_records write is the atomic dedup INSERT,
        # issued via fetchrow (winner path)
        usage_records_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(usage_records_inserts) == 1

        # Only the valid directory is stored
        directory_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(directory_inserts) == 1
        assert directory_inserts[0].args[4] == "/tmp/a"


class TestSessionTodoReplace:
    """Session todos are replaced per external session within the batch."""

    @pytest.mark.asyncio
    async def test_single_todo_accepted(self, monkeypatch):
        """A single todo item triggers DELETE per session + INSERT."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth, None,                     # auth, sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,                            # resolve session_id → not found
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_todos=[_mk_todo_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        # Verify DELETE per session + INSERT
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_session_todos" in str(call)
        ]
        assert len(delete_calls) == 1

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_todos" in str(call)
        ]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_multiple_todos_across_sessions(self, monkeypatch):
        """Todos across different sessions are independently replaced per session."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        # For each distinct session: one resolve call
        mock_conn.fetchrow.side_effect = [
            auth, None,   # auth, sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None,         # resolve session_id for ses_a → not found
            None,         # resolve session_id for ses_b → not found
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            session_todos=[
                _mk_todo_payload(external_session_id="ses_a", position=1, content="Task A1"),
                _mk_todo_payload(external_session_id="ses_a", position=2, content="Task A2"),
                _mk_todo_payload(external_session_id="ses_b", position=1, content="Task B1"),
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
        assert data["projection_accepted_count"] == 3
        assert data["projection_rejected_count"] == 0

        # Two DELETEs (one per session), 3 INSERTs
        delete_calls = [
            call for call in mock_conn.execute.call_args_list
            if "DELETE FROM opencode_session_todos" in str(call)
        ]
        assert len(delete_calls) == 2

        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_session_todos" in str(call)
        ]
        assert len(insert_calls) == 3


class TestProjectionPartialFailure:
    """Malformed or rejected projection data does not block usage records."""

    @pytest.mark.asyncio
    async def test_usage_records_accepted_when_projections_fail(self, monkeypatch):
        """When a projection processing call raises an exception, usage records
        are still accepted and the projection is counted as rejected."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        # execute works for usage records, then raises on first projection execute
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        # Override the project processing to fail at the DB level
        # We'll monkeypatch _process_project to raise
        import app.api.ingest as ingest_module
        original_process = ingest_module._process_project

        async def _failing_process(*args, **kwargs):
            raise ValueError("Simulated DB failure in project processing")

        monkeypatch.setattr(ingest_module, "_process_project", _failing_process)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
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
            ],
            projects=[_mk_project_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # Usage records must still be accepted
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        # Projection must be counted as rejected
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 1

        # Restore original
        monkeypatch.setattr(ingest_module, "_process_project", original_process)

    @pytest.mark.asyncio
    async def test_some_projections_fail_others_succeed(self, monkeypatch):
        """When some projection items fail and others succeed, counts reflect both."""
        import app.api.ingest as ingest_module

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth, None,  # auth, sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
        ]

        # Patch _process_project directly: first call succeeds, second fails
        proj_call_count = [0]

        async def _failing_process(conn, proj, client_id, source_db_id, now):
            proj_call_count[0] += 1
            if proj.external_project_id == "proj_fail":
                raise RuntimeError("Simulated DB failure for project 2")
            return True

        monkeypatch.setattr(ingest_module, "_process_project", _failing_process)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            projects=[
                _mk_project_payload(external_project_id="proj_ok"),
                _mk_project_payload(external_project_id="proj_fail"),
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
        # One accepted, one rejected
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 1

    @pytest.mark.asyncio
    async def test_directory_replace_nonexistent_scope_succeeds(self, monkeypatch):
        """Replacing directories when none exist is a no-op + insert."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory="/tmp/new-project"),
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
        assert data["projection_accepted_count"] == 1


class TestProjectionIndependentPaths:
    """Projection and usage-record paths are truly independent."""

    @pytest.mark.asyncio
    async def test_projection_errors_dont_block_usage(self, monkeypatch):
        """A full projection failure does not affect usage record success."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=2),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        import app.api.ingest as ingest_module

        # Make ALL projection paths fail
        async def _failing(*args, **kwargs):
            raise RuntimeError("All projections failed")

        monkeypatch.setattr(ingest_module, "_process_project", _failing)
        monkeypatch.setattr(ingest_module, "_process_session_context", _failing)
        monkeypatch.setattr(ingest_module, "_process_project_directories", _failing)
        monkeypatch.setattr(ingest_module, "_process_session_todos", _failing)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
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
            ],
            session_contexts=[_mk_session_context_payload()],
            projects=[_mk_project_payload()],
            project_directories=[_mk_directory_payload()],
            session_todos=[_mk_todo_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # Usage record MUST be accepted
        assert data["accepted_count"] == 1
        # All projections rejected
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] > 0

    @pytest.mark.asyncio
    async def test_usage_failure_doesnt_block_projections(self, monkeypatch):
        """When usage records fail, projections are still processed."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        # auth, sd check, resolve (not found), resolve (not found)
        # handler: cross-identity conflict check
        *_handler_routing_side_effect_items(),
        mock_conn.fetchrow.side_effect = [
            auth, None,  None, None,
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Empty records, but include projections
        payload = _projection_payload(
            records=[],
            session_contexts=[_mk_session_context_payload()],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        # No usage records
        assert data["accepted_count"] == 0
        # Projection still processed
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0


# ════════════════════════════════════════════════════════════════════════════
#  Issue #375 — Atomic usage record deduplication under concurrent replay
# ════════════════════════════════════════════════════════════════════════════


class TestConcurrentIdenticalRecords:
    """Two concurrent identical records produce one row and one aggregate increment."""

    @pytest.mark.asyncio
    async def test_concurrent_identical_records_single_insert_and_increment(self, monkeypatch):
        """Two identical concurrent records: both return accepted, but only one
        row is inserted into opencode_usage_records and session aggregates /
        source database record count are incremented exactly once."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        # ── Build fetchrow side-effect list ───────────────────────────
        fetchrow_responses = [auth]  # auth

        # Source database check
        fetchrow_responses.append(None)  # new source database

        # Two records, same dedup key
        # Record 1: handler routing checks
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        #  - model upsert → new model (None)
        #  - atomic INSERT ON CONFLICT → WINNER (returns row with id)
        #  - session resolve → returns session UUID
        fetchrow_responses.append(None)  # model check (new)
        insert_row_1 = MagicMock()
        insert_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.append(insert_row_1)  # atomic INSERT → winner
        session_row_1 = MagicMock()
        session_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.append(session_row_1)  # session resolve

        # _record_canonical_event for Record 1 (winner → new canonical event)
        fetchrow_responses.extend(_canonical_event_side_effect_items())

        # Record 2 (same source_record_id):
        # Record 2: handler routing checks
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        #  - model upsert → existing model (we return a row)
        #  - atomic INSERT ON CONFLICT → LOSER (returns None)
        #  - existing record query → returns matching values (identical)
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.append(existing_model)  # model check (existing)
        fetchrow_responses.append(None)  # atomic INSERT → conflict
        existing_dedup_row = MagicMock()
        existing_dedup_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        fetchrow_responses.append(existing_dedup_row)  # dedup query → identical
        # _apply_replay_merge SELECT FOR UPDATE
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        fetchrow_responses.append(lock_row)
        fetchrow_responses.append(_canonical_exists_row())

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = fetchrow_responses
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-concurrent-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-concurrent-001",  # SAME dedup key
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
        data = response.json()["data"]

        # Both records return accepted
        assert data["accepted_count"] == 2
        assert data["rejected_count"] == 0
        assert data["results"][0]["status"] == "accepted"
        assert data["results"][1]["status"] == "accepted"

        # ── Exactly ONE usage record inserted ─────────────────────────
        record_inserts_via_execute = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(record_inserts_via_execute) == 0, (
            "Usage record INSERT should happen via atomic INSERT ON CONFLICT "
            "(fetchrow call), not via execute"
        )

        atomic_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
            and "ON CONFLICT" in str(call)
        ]
        assert len(atomic_inserts) == 2, (
            "Two atomic INSERT attempts (one winner, one loser)"
        )

        # ── Session resolve called exactly ONCE ───────────────────────
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 1, (
            f"Session resolve should be called exactly once (winner only), "
            f"got {len(session_calls)}"
        )

        # ── Source database record count bumped exactly ONCE ──────────
        sd_bumps = [
            call for call in mock_conn.execute.call_args_list
            if "record_count = record_count + 1" in str(call)
        ]
        assert len(sd_bumps) == 1, (
            f"Source DB count should be bumped exactly once (winner only), "
            f"got {len(sd_bumps)}"
        )

        # ── Second record's result includes idempotent reason ──────────
        assert "idempotent" in (data["results"][1].get("reason") or "").lower()


class TestProjectionCombinedPayloads:
    """Payloads with both usage records and multiple projection types."""

    @pytest.mark.asyncio
    async def test_full_payload_all_accepted(self, monkeypatch):
        """A payload with usage records, session contexts, projects,
        directories, and todos all succeed."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()

        # Side effects: auth, sd, usage record (model, insert, session=3 items)
        # Then: 2 session contexts (resolve session, resolve proj each = 4)
        # Then: 1 todo resolve
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth,                   # auth
            None,                   # sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            None, insert_row, session_row,  # usage record: model, atomic_insert, session
            *_canonical_event_side_effect_items(),  # canonical event recording
            None, None,             # ctx1: resolve session_id, resolve source_project
            None, None,             # ctx2: resolve session_id, resolve source_project
            None,                   # todo: resolve session_id
        ]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
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
            ],
            session_contexts=[
                _mk_session_context_payload(
                    external_session_id="ses_ctx_1",
                    external_project_id="proj_1",
                ),
                _mk_session_context_payload(
                    external_session_id="ses_ctx_2",
                ),
            ],
            projects=[_mk_project_payload()],
            project_directories=[_mk_directory_payload()],
            session_todos=[_mk_todo_payload()],
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
        assert data["projection_accepted_count"] == 5  # 1 proj + 2 ctx + 1 dir + 1 todo
        assert data["projection_rejected_count"] == 0


# ════════════════════════════════════════════════════════════════════════════
#  Issue #379 — Replay Merge: enrich absent fields without erasing populated values
# ════════════════════════════════════════════════════════════════════════════


class TestReplayMergeFillAbsent:
    """Replay with identical required values + additional enrichment fills only NULL fields."""

    @pytest.mark.asyncio
    async def test_replay_fills_absent_provider(self, monkeypatch):
        """A replay with identical required values and a new provider fills
        provider on the stored record without touching other fields."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert
            None,             # 4. atomic INSERT → conflict (loser)
            existing_dedup,   # 5. dedup query → identical match
            lock_row,         # 6. _apply_replay_merge: SELECT FOR UPDATE
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": "openai",
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # Verify an UPDATE was issued for provider
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
            and "provider" in str(call)
        ]
        assert len(enrichment_updates) == 1

    @pytest.mark.asyncio
    async def test_replay_fills_absent_numeric_enrichment(self, monkeypatch):
        """A replay with identical required values fills absent numeric
        enrichment fields (reasoning_tokens, cache_read_tokens, etc.)."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                    "reasoning_tokens": 42,
                    "finish_reason": "stop",
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # Verify updates include all enrichment fields
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 1
        update_sql = str(enrichment_updates[0])
        assert "reasoning_tokens" in update_sql
        assert "cache_read_tokens" in update_sql
        assert "cache_write_tokens" in update_sql
        assert "finish_reason" in update_sql

    @pytest.mark.asyncio
    async def test_zero_numeric_is_valid_not_missing(self, monkeypatch):
        """Numeric zero is a valid observed value — a stored NULL is
        filled with 0 from the replay, and a stored 0 is NOT treated
        as missing."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "reasoning_tokens": 0,
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # Verify 0 was written for reasoning_tokens
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 1
        update_sql = str(enrichment_updates[0])
        assert "reasoning_tokens" in update_sql

    @pytest.mark.asyncio
    async def test_concurrent_replays_with_differing_enrichment_do_not_overwrite(self, monkeypatch):
        """Two concurrent replay losers with identical accounting values but
        differing enrichment payloads must not overwrite each other's writes.
        The COALESCE-based UPDATE is atomic — each fill lands in its own NULL
        column and Postgres row-level locking serialises the two UPDATEs, so
        no populated value is erased."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()

        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row_a = MagicMock()
        lock_row_a.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        lock_row_b = MagicMock()
        lock_row_b.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert (record A)
            None,             # 4. atomic INSERT → conflict (record A, loser)
            existing_dedup,   # 5. dedup query → identical match
            lock_row_a,       # 6. _apply_replay_merge FOR UPDATE (record A)
            _canonical_exists_row(),  # existence check → canonical event exists
            # Record B:
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 7. model upsert (record B)
            None,             # 8. atomic INSERT → conflict (record B, loser)
            existing_dedup,   # 9. dedup query → identical match
            lock_row_b,       # 10. _apply_replay_merge FOR UPDATE (record B)
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-concurrent-fill-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "provider": "openai",
                },
                {
                    "source_record_id": "rec-concurrent-fill-001",  # SAME dedup key
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                    "mode": "chat",
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
        assert data["results"][0]["status"] == "accepted"
        assert data["results"][1]["status"] == "accepted"

        # Both records should report enrichment applied since each fills a
        # different NULL column.
        assert "enrichment applied" in (data["results"][0]["reason"] or "")
        assert "enrichment applied" in (data["results"][1]["reason"] or "")

        # Verify enrichment UPDATEs used COALESCE (the atomic guard)
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 2, (
            f"Expected 2 enrichment UPDATEs (one per record), "
            f"got {len(enrichment_updates)}"
        )

        # First UPDATE should use COALESCE for provider
        update_1_sql = str(enrichment_updates[0])
        assert "COALESCE" in update_1_sql, (
            "UPDATE must use COALESCE for atomic non-overwriting fill"
        )
        assert "provider" in update_1_sql

        # Second UPDATE should use COALESCE for mode
        update_2_sql = str(enrichment_updates[1])
        assert "COALESCE" in update_2_sql, (
            "UPDATE must use COALESCE for atomic non-overwriting fill"
        )
        assert "mode" in update_2_sql

        # Neither UPDATE should contain a bare "provider = $" (without COALESCE)
        # or "mode = $" — the COALESCE wrapper is the invariant guard.
        import re
        bare_set = re.compile(
            r"\b(provider|mode|finish_reason|reasoning_tokens"
            r"|cache_read_tokens|cache_write_tokens)\s*=\s*\$"
        )
        assert not bare_set.search(update_1_sql), (
            f"UPDATE must not contain bare SET column = $n (COALESCE guard missing): {update_1_sql}"
        )
        assert not bare_set.search(update_2_sql), (
            f"UPDATE must not contain bare SET column = $n (COALESCE guard missing): {update_2_sql}"
        )


class TestReplayMergeNoOverwrite:
    """Replay with identical required values must NOT overwrite populated enrichment fields."""

    @pytest.mark.asyncio
    async def test_replay_does_not_overwrite_populated_provider(self, monkeypatch):
        """When the stored record already has a provider, a replay with a
        different provider does NOT overwrite it, and no enrichment UPDATE
        is issued because no column is fillable."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": "openai",    # stored value — NOT fillable
            "mode": None,            # stored NULL — fillable
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": "anthropic",  # DIFFERENT provider
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
        # Stored provider is already populated → no column is fillable
        # → no enrichment UPDATE issued.
        assert "enrichment applied" not in (data["results"][0]["reason"] or "")
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        # Verify NO enrichment UPDATE was issued
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 0, (
            "No enrichment UPDATE expected when no column is fillable"
        )

    @pytest.mark.asyncio
    async def test_replay_fills_absent_but_does_not_overwrite_others(self, monkeypatch):
        """A replay fills an absent 'mode' field while leaving an already-populated
        'provider' untouched — only the absent field is updated."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": "openai",    # stored value — NOT fillable
            "mode": None,            # stored NULL — fillable
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": "anthropic",  # should NOT overwrite "openai"
                    "mode": "chat",           # should be filled
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # Verify UPDATE includes mode (fillable) but NOT provider (already
        # populated — not fillable).  Each SET clause is guarded by COALESCE.
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 1
        update_sql = str(enrichment_updates[0])
        assert "mode" in update_sql
        assert "provider" not in update_sql, (
            "Provider must NOT be in the UPDATE — stored value is already populated"
        )
        assert "COALESCE" in update_sql, (
            "UPDATE must use COALESCE for atomic non-overwriting fill"
        )
        # Verify no bare SET column = $n (COALESCE guard is present)
        import re
        bare_set = re.compile(
            r'\b(provider|mode)\s*=\s*\$'
        )
        assert not bare_set.search(update_sql), (
            f"mode must be guarded by COALESCE, got: {update_sql}"
        )


class TestReplayMergeWhitespaceNormalization:
    """Whitespace-only optional text values are treated as missing under Replay Merge."""

    @pytest.mark.asyncio
    async def test_whitespace_only_provider_treated_as_missing(self, monkeypatch):
        """A replay with provider='   ' (whitespace-only) is normalised to None
        and does NOT overwrite a NULL stored provider."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": "   ",
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
        # Whitespace-only → treated as missing → no enrichment applied
        assert "enrichment applied" not in (data["results"][0]["reason"] or "")
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        # Verify NO enrichment UPDATE was issued
        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 0

    @pytest.mark.asyncio
    async def test_single_space_provider_treated_as_missing(self, monkeypatch):
        """provider=' ' (single space) is also normalised to None."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": " ",
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
        assert "enrichment applied" not in (data["results"][0]["reason"] or "")
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 0

    @pytest.mark.asyncio
    async def test_whitespace_finish_reason_treated_as_missing(self, monkeypatch):
        """finish_reason='  ' (whitespace-only) treated as missing."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "finish_reason": "  ",
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
        assert "enrichment applied" not in (data["results"][0]["reason"] or "")
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()

        enrichment_updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE opencode_usage_records" in str(call)
        ]
        assert len(enrichment_updates) == 0

    @pytest.mark.asyncio
    async def test_newline_only_provider_treated_as_missing(self, monkeypatch):
        """A provider='\n' (whitespace-only after strip) is treated as
        missing — no enrichment applied."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "provider": "\n",
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
        # newline-only → stripped to empty → treated as missing
        assert "enrichment applied" not in (data["results"][0]["reason"] or "")
        assert "idempotent" in (data["results"][0]["reason"] or "").lower()


# ════════════════════════════════════════════════════════════════════════════
#  Issue #380 — Replay enrichment: aggregate repair without double-counting
# ════════════════════════════════════════════════════════════════════════════


class TestReplayEnrichmentSessionAggregateRepair:
    """When replay merge fills cache_read_tokens / cache_write_tokens on a
    usage record, the session aggregate's derived totals are repaired
    without inflating base totals or message_count."""

    @pytest.mark.asyncio
    async def test_replay_enrichment_updates_session_cache_totals(self, monkeypatch):
        """A replay that fills cache_read_tokens=10 and cache_write_tokens=5
        on a previously-stored record updates the session's total_cache_read_tokens
        and total_cache_write_tokens via atomic aggregate repair UPDATEs."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,  # 10 + 5 — matches effective cached
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        # FOR UPDATE returns: cache_read_tokens=NULL, cache_write_tokens=NULL
        session_id_for_repair = uuid.uuid4()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check (new)
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert
            None,             # 4. atomic INSERT → conflict (loser)
            existing_dedup,   # 5. dedup query → identical match
            lock_row,         # 6. _apply_replay_merge: SELECT FOR UPDATE
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # ── Verify session aggregate repair UPDATEs were issued ─────────
        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]

        # cache_read_tokens repair
        cr_repairs = [
            call for call in all_execute_calls
            if "total_cache_read_tokens" in call
        ]
        assert len(cr_repairs) == 1, (
            f"Expected 1 session total_cache_read_tokens repair UPDATE, "
            f"got {len(cr_repairs)}"
        )
        assert "+ $1" in cr_repairs[0] or "total_cache_read_tokens +" in cr_repairs[0], (
            "Repair UPDATE must add (not overwrite) the delta"
        )

        # cache_write_tokens repair
        cw_repairs = [
            call for call in all_execute_calls
            if "total_cache_write_tokens" in call
        ]
        assert len(cw_repairs) == 1, (
            f"Expected 1 session total_cache_write_tokens repair UPDATE, "
            f"got {len(cw_repairs)}"
        )
        assert "+ $1" in cw_repairs[0] or "total_cache_write_tokens +" in cw_repairs[0], (
            "Repair UPDATE must add (not overwrite) the delta"
        )

    @pytest.mark.asyncio
    async def test_replay_enrichment_does_not_inflate_base_totals(self, monkeypatch):
        """A replay delivery that fills cache tokens must NOT increment
        total_input_tokens, total_output_tokens, total_cached_tokens,
        or message_count on the session aggregate.  The session resolution
        path (winner) is never reached for a dedup loser."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 28,  # 20 + 8 — matches effective cached
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        session_id_for_repair = uuid.uuid4()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            None,             # source_database check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # model upsert
            None,             # atomic INSERT → conflict
            existing_dedup,   # dedup query → identical
            lock_row,         # _apply_replay_merge: SELECT FOR UPDATE
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "cache_read_tokens": 20,
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
        assert data["accepted_count"] == 1
        assert data["results"][0]["status"] == "accepted"

        # ── Verify NO session resolution was triggered ──────────────────
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Replay delivery (loser path) must NOT call _resolve_session — "
            "base totals must not be incremented"
        )

        # ── Verify NO base-total or message_count UPDATEs ───────────────
        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]
        base_total_fields = [
            "total_input_tokens", "total_output_tokens",
            "total_cached_tokens", "message_count",
        ]
        for field in base_total_fields:
            bad_updates = [
                call for call in all_execute_calls if field in call
            ]
            assert len(bad_updates) == 0, (
                f"Replay delivery must NOT update {field} on session aggregate. "
                f"Found: {bad_updates}"
            )

    @pytest.mark.asyncio
    async def test_duplicate_delivery_does_not_double_count(self, monkeypatch):
        """A second identical replay delivery must NOT double-count any
        session aggregate.  The FOR UPDATE sees already-filled cache token
        columns and produces a zero delta, so no repair UPDATE is issued."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,  # 10 + 5 — matches effective cached
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        session_id_for_repair = uuid.uuid4()

        # First record: cache tokens are NULL → deltas will be applied
        lock_row_first = MagicMock()
        lock_row_first.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        # Second record (duplicate): cache tokens are already filled → deltas = 0
        lock_row_second = MagicMock()
        lock_row_second.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": 10,   # already filled by first replay
            "cache_write_tokens": 5,   # already filled by first replay
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                   # 1. auth
            None,                   # 2. source_database check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,         # 3. model upsert (record 1)
            None,                   # 4. atomic INSERT → conflict (record 1)
            existing_dedup,         # 5. dedup query → identical (record 1)
            lock_row_first,         # 6. _apply_replay_merge FOR UPDATE (record 1)
            _canonical_exists_row(),  # existence check → canonical event exists
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,         # 7. model upsert (record 2, duplicate)
            None,                   # 8. atomic INSERT → conflict (record 2)
            existing_dedup,         # 9. dedup query → identical (record 2)
            lock_row_second,        # 10. _apply_replay_merge FOR UPDATE (record 2)
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        base_record = {
            "source_record_id": "rec-001",
            "session_id": str(_SESSION_ID),
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": "0.0035",
            "reported_at": _mk_ts().isoformat(),
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
        }

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[dict(base_record), dict(base_record)],
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
        assert data["results"][0]["status"] == "accepted"
        assert data["results"][1]["status"] == "accepted"

        # ── Verify exactly ONE set of session repair UPDATEs ────────────
        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]
        cr_repairs = [
            call for call in all_execute_calls
            if "total_cache_read_tokens" in call
        ]
        cw_repairs = [
            call for call in all_execute_calls
            if "total_cache_write_tokens" in call
        ]
        assert len(cr_repairs) == 1, (
            f"Duplicate delivery must NOT double-count cache_read_tokens. "
            f"Found {len(cr_repairs)} repair UPDATEs: {cr_repairs}"
        )
        assert len(cw_repairs) == 1, (
            f"Duplicate delivery must NOT double-count cache_write_tokens. "
            f"Found {len(cw_repairs)} repair UPDATEs: {cw_repairs}"
        )

    @pytest.mark.asyncio
    async def test_replay_with_zero_cache_tokens_zero_delta(self, monkeypatch):
        """A replay with cache_read_tokens=0, cache_write_tokens=0 fills
        the stored fields but produces a delta of 0, so no session aggregate
        repair is issued (adding 0 is a no-op)."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        session_id_for_repair = uuid.uuid4()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
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

        # ── Verify NO session repair UPDATEs for zero deltas ────────────
        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]
        cr_repairs = [
            call for call in all_execute_calls
            if "total_cache_read_tokens" in call
        ]
        cw_repairs = [
            call for call in all_execute_calls
            if "total_cache_write_tokens" in call
        ]
        assert len(cr_repairs) == 0, (
            f"Zero delta must not issue cache_read repair UPDATE. "
            f"Found: {cr_repairs}"
        )
        assert len(cw_repairs) == 0, (
            f"Zero delta must not issue cache_write repair UPDATE. "
            f"Found: {cw_repairs}"
        )

    @pytest.mark.asyncio
    async def test_replay_only_cache_read_fills_only_that_total(self, monkeypatch):
        """A replay with only cache_read_tokens (cache_write_tokens omitted)
        updates only total_cache_read_tokens, not total_cache_write_tokens."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        session_id_for_repair = uuid.uuid4()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # cache_read_tokens provided, cache_write_tokens NOT in payload
        payload = _valid_ingest_payload(
            schema_version="1.2",
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
                    "cache_read_tokens": 10,
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

        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]
        cr_repairs = [
            call for call in all_execute_calls
            if "total_cache_read_tokens" in call
        ]
        cw_repairs = [
            call for call in all_execute_calls
            if "total_cache_write_tokens" in call
        ]
        assert len(cr_repairs) == 1, (
            f"Expected cache_read repair when cache_read_tokens=10. "
            f"Found: {cr_repairs}"
        )
        assert len(cw_repairs) == 0, (
            f"Expected NO cache_write repair when cache_write_tokens is absent. "
            f"Found: {cw_repairs}"
        )


# ════════════════════════════════════════════════════════════════════════════
#  PR #382 review fixes — new tests
# ════════════════════════════════════════════════════════════════════════════


class TestF2WinnerTransactionAtomicity:
    """F2: The winner path's atomic INSERT + side effects run inside a transaction."""

    @pytest.mark.asyncio
    async def test_winner_path_runs_in_transaction(self, monkeypatch):
        """When the atomic INSERT wins, all side effects are inside
        conn.transaction()."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        # _build_ingest_app already calls _add_transaction_support, which
        # replaces conn.transaction with a MagicMock we can count calls on.
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
        assert data["accepted_count"] == 1

        # _build_ingest_app calls _add_transaction_support, which replaces
        # conn.transaction with a MagicMock we can count calls on (never
        # called during setup). The single-record winner scenario enters
        # two transactions: one in _process_one_record (atomic dedup) and
        # one in _record_canonical_event (advisory lock + canonical insert).
        assert mock_conn.transaction.call_count == 2, (
            "Winner path must enter exactly two explicit transactions"
        )


class TestC6PerRecordExceptionHandling:
    """C6: A transient error in one record does not abort the whole batch."""

    @pytest.mark.asyncio
    async def test_single_record_failure_does_not_abort_batch(self, monkeypatch):
        """One record's _process_one_record raising → 200, that record
        rejected, others accepted."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()

        # Record 0 (index 0): _process_one_record raises an exception
        # Record 1 (index 1): normal winner path

        # We need the fetchrow side_effect to be complex:
        # For record 0: auth -> sd_check -> ... -> atomic INSERT raises
        # For record 1: normal winner path

        # Simpler approach: make the model upsert raise for record 0 but
        # work for record 1.  _upsert_model is a fetchrow call.

        # Side effect: auth, sd_check(None), then model upsert → Exception
        # But the exception actually raises from fetchrow. Let's make
        # the atomic INSERT for record 0 raise by making fetchrow raise
        # for that specific call.

        # Actually, let's just make the model upsert for record 0 raise.
        model_id_row = MagicMock()
        model_id_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        # Record 0: model upsert raises Exception
        mock_conn.fetchrow.side_effect = [
            auth,           # auth
            None,           # sd check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            Exception("DB connection lost"),  # model upsert for record 0 → boom
            # Record 1: handler routing + normal winner path
            *_handler_routing_side_effect_items(),
            None,           # model upsert for record 1
            insert_row,     # atomic INSERT winner
            session_row,    # session resolve
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-fail",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-ok",
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

        # Response is still 200, batch succeeds with partial results
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1, f"Expected 1 accepted, got {data}"
        assert data["rejected_count"] == 1, f"Expected 1 rejected, got {data}"

        results = data["results"]
        assert results[0]["status"] == "rejected"
        assert "Processing error" in (results[0]["reason"] or "")
        assert results[1]["status"] == "accepted"


class TestC3WhitespaceDirectoryDedup:
    """C3: directory dedup uses stripped canonical paths."""

    @pytest.mark.asyncio
    async def test_trailing_whitespace_directory_collapsed(self, monkeypatch):
        """Directories '/tmp/a' and '/tmp/a ' (trailing whitespace) in one
        snapshot collapse to ONE row stored as '/tmp/a'."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        auth = _auth_row()
        mock_conn.fetchrow.side_effect = [auth, None]

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _projection_payload(
            records=[],
            project_directories=[
                _mk_directory_payload(directory="/tmp/a"),
                _mk_directory_payload(directory="/tmp/a "),
                _mk_directory_payload(directory=" /tmp/b"),
                _mk_directory_payload(directory="/tmp/b"),
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
        # 4 entries collapse to 2 canonical: /tmp/a and /tmp/b
        assert data["projection_accepted_count"] == 2
        assert data["projection_rejected_count"] == 0

        # One DELETE, then exactly two INSERTs
        insert_calls = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO opencode_project_directories" in str(call)
        ]
        assert len(insert_calls) == 2
        inserted_dirs = sorted(call.args[4] for call in insert_calls)
        assert inserted_dirs == ["/tmp/a", "/tmp/b"]


class Test380SessionIdNullFallback:
    """#380: session_id=NULL fallback path and repair delta args in
    _apply_replay_merge."""

    @pytest.mark.asyncio
    async def test_session_id_null_fallback_resolves_session(self, monkeypatch):
        """When the FOR UPDATE lock row has session_id=NULL, the merge path
        calls _resolve_internal_session_id and repairs the right session."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        resolved_session_id = uuid.uuid4()

        # FOR UPDATE lock row: session_id=NULL
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": None,  # not backfilled yet
        }.__getitem__

        # _resolve_internal_session_id query result
        resolve_row = MagicMock()
        resolve_row.__getitem__.side_effect = {"id": resolved_session_id}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check
            # handler: cross-identity conflict check
            *_handler_routing_side_effect_items(),
            existing_model,   # 3. model upsert
            None,             # 4. atomic INSERT → conflict
            existing_dedup,   # 5. dedup query → identical
            lock_row,         # 6. _apply_replay_merge: SELECT FOR UPDATE
            resolve_row,      # 7. _resolve_internal_session_id SELECT
            _canonical_exists_row(),  # existence check → canonical event exists
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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
        assert "enrichment applied" in (data["results"][0]["reason"] or "")

        # Verify session aggregate repair uses the RESOLVED session id
        all_execute_calls = [
            str(call) for call in mock_conn.execute.call_args_list
        ]
        # Look for the repair UPDATE targeting resolved_session_id
        cr_repairs = [
            call for call in all_execute_calls
            if "total_cache_read_tokens" in call
            and str(resolved_session_id) in call
        ]
        cw_repairs = [
            call for call in all_execute_calls
            if "total_cache_write_tokens" in call
            and str(resolved_session_id) in call
        ]
        assert len(cr_repairs) == 1, (
            f"Expected cache_read repair targeting resolved session "
            f"{resolved_session_id}. Found: {cr_repairs}"
        )
        assert len(cw_repairs) == 1, (
            f"Expected cache_write repair targeting resolved session. "
            f"Found: {cw_repairs}"
        )

    @pytest.mark.asyncio
    async def test_repair_delta_args_are_correct_values(self, monkeypatch):
        """When stored cache_read_tokens=NULL, cache_write_tokens=NULL and
        incoming values are 10 and 5, the aggregate UPDATEs carry the exact
        incoming values as deltas."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        auth = _auth_row()
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 15,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__

        session_id_for_repair = uuid.uuid4()
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": session_id_for_repair,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth, None,
            *_handler_routing_side_effect_items(),
            existing_model, None, existing_dedup, lock_row,
        _canonical_exists_row(),  # existence check
        ]
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
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

        # Inspect execute call args for the cache token repair UPDATEs
        all_call_args = mock_conn.execute.call_args_list

        cr_call = None
        cw_call = None
        for call in all_call_args:
            sql = str(call)
            if "total_cache_read_tokens" in sql:
                cr_call = call
            elif "total_cache_write_tokens" in sql:
                cw_call = call

        assert cr_call is not None, "Missing total_cache_read_tokens repair UPDATE"
        assert cw_call is not None, "Missing total_cache_write_tokens repair UPDATE"

        # Verify delta arguments: total_cache_read_tokens + 10, total_cache_write_tokens + 5
        cr_args = cr_call.args
        assert cr_args[1] == 10, (
            f"cache_read repair delta expected 10, got {cr_args[1]}"
        )
        assert cr_args[2] == session_id_for_repair, (
            f"cache_read repair session_id expected {session_id_for_repair}, "
            f"got {cr_args[2]}"
        )

        cw_args = cw_call.args
        assert cw_args[1] == 5, (
            f"cache_write repair delta expected 5, got {cw_args[1]}"
        )
        assert cw_args[2] == session_id_for_repair, (
            f"cache_write repair session_id expected {session_id_for_repair}, "
            f"got {cw_args[2]}"
        )


# ════════════════════════════════════════════════════════════════════════════
#  Tests — Canonical event accept path (issue #387)
# ════════════════════════════════════════════════════════════════════════════


class TestCanonicalEventAccept:
    """New record first delivery → canonical event in ``usage_events``
    and Ingest Attempt in ``usage_ingest_attempts``."""

    @pytest.mark.asyncio
    async def test_canonical_event_inserted_on_first_delivery(self, monkeypatch):
        """A new record creates a row in ``usage_events`` with all fields."""
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
                    "source_record_id": "rec-canon-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 500,
                    "output_tokens": 250,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0100",
                    "reported_at": _mk_ts().isoformat(),
                    "provider": "openai",
                    "mode": "chat",
                    "finish_reason": "stop",
                    "cache_read_tokens": 50,
                    "cache_write_tokens": 25,
                    "project_id": "proj-test",
                    "agent": "test-agent",
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
        assert data["results"][0]["event_id"] is not None
        assert data["results"][0]["attempt_id"] is not None

        # Verify INSERT INTO usage_events was called
        usage_events_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(usage_events_inserts) == 1

        # Verify the INSERT includes all expected fields
        usage_event_call = usage_events_inserts[0]
        args = usage_event_call.args
        assert "canonical_source_identity_id" in str(args)
        assert "source_record_id" in str(args)
        assert "client_id" in str(args)
        assert "session_id" in str(args)
        assert "model_id" in str(args)
        assert "input_tokens" in str(args)
        assert "output_tokens" in str(args)
        assert "provider" in str(args)
        assert "mode" in str(args)
        assert "finish_reason" in str(args)
        assert "cache_read_tokens" in str(args)
        assert "cache_write_tokens" in str(args)
        assert "project_id" in str(args)
        assert "agent" in str(args)

    @pytest.mark.asyncio
    async def test_ingest_attempt_row_recorded_with_jsonb(self, monkeypatch):
        """The Ingest Attempt row stores the full redacted record as JSONB."""
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
            records=[
                {
                    "source_record_id": "rec-attempt-001",
                    "session_id": str(_SESSION_ID),
                    "model": "claude-3",
                    "input_tokens": 300,
                    "output_tokens": 150,
                    "cached_tokens": 10,
                    "estimated_cost_usd": "0.0050",
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

        # Verify INSERT INTO usage_ingest_attempts was called
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1

        # Verify the attempt stores record_jsonb, outcome, etc.
        attempt_call = attempt_inserts[0]
        assert "record_jsonb" in str(attempt_call.args)
        assert "accepted" in str(attempt_call.args)
        assert "ingest_batch_id" in str(attempt_call.args)

    @pytest.mark.asyncio
    async def test_session_aggregate_counters_updated(self, monkeypatch):
        """Session aggregate counters are updated on the canonical event path."""
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
            records=[
                {
                    "source_record_id": "rec-agg-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cached_tokens": 100,
                    "estimated_cost_usd": "0.0200",
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

        # Verify session aggregate was resolved (INSERT INTO sessions)
        session_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_inserts) == 1

    @pytest.mark.asyncio
    async def test_outcome_accepted_has_event_id_and_attempt_id(self, monkeypatch):
        """The ``IngestRecordResult`` for an accepted record includes
        ``event_id`` and ``attempt_id``."""
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
        result = data["results"][0]
        assert result["status"] == "accepted"
        assert result["event_id"] is not None, "event_id should be set for accepted path"
        assert result["attempt_id"] is not None, "attempt_id should be set for accepted path"
        # Validate UUID format
        uuid.UUID(result["event_id"])
        uuid.UUID(result["attempt_id"])

    @pytest.mark.asyncio
    async def test_duplicate_record_does_not_create_second_canonical_event(self, monkeypatch):
        """A duplicate record returns accepted but does NOT insert a second
        canonical event or ingest attempt for the new-record path."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        _add_transaction_support(mock_conn)

        # Record 1: new, accepted → canonical event path
        # Record 2: duplicate (same dedup key, same values) → no canonical event path
        fetchrow_responses = [auth, None]  # auth + sd_check

        # Record 1: handler routing + model, atomic_insert(winner), session
        insert_row_1 = MagicMock()
        insert_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row_1 = MagicMock()
        session_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        fetchrow_responses.extend([None, insert_row_1, session_row_1])
        fetchrow_responses.extend(_canonical_event_side_effect_items())

        # Record 2: handler routing + model, atomic_insert(loser), dedup query, lock_row
        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None, "mode": None, "finish_reason": None,
            "reasoning_tokens": None, "cache_read_tokens": None,
            "cache_write_tokens": None, "session_id": _SESSION_ID,
        }.__getitem__
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        fetchrow_responses.extend([existing_model, None, existing_dedup, lock_row])
        # Existence check: canonical event already inserted by record 1
        canonical_exists = MagicMock()
        fetchrow_responses.append(canonical_exists)

        mock_conn.fetchrow.side_effect = fetchrow_responses
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-dup-canon",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-dup-canon",  # SAME dedup key
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
        data = response.json()["data"]
        assert data["accepted_count"] == 2

        # Record 1 has event_id/attempt_id, Record 2 does not
        assert data["results"][0]["event_id"] is not None
        assert data["results"][0]["attempt_id"] is not None
        assert data["results"][1]["event_id"] is None
        assert data["results"][1]["attempt_id"] is None

        # Only one INSERT INTO usage_events (for record 1)
        usage_events_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(usage_events_inserts) == 1

        # Only one INSERT INTO usage_ingest_attempts (for record 1)
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1

    @pytest.mark.asyncio
    async def test_legacy_duplicate_without_canonical_event_backfills(self, monkeypatch):
        """A legacy duplicate (idempotent replay of a pre-existing record)
        with NO canonical event backfills one — the existence check returns
        None and a canonical event + ingest attempt are created.

        This is the self-healing replay path for PR #396, finding #8:
        pre-deploy records are backfilled at their first post-deploy replay.
        """
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        _add_transaction_support(mock_conn)

        # Single legacy record: pre-existing in opencode_usage_records,
        # no canonical event exists yet.
        fetchrow_responses = [auth, None]  # auth + sd_check

        # Handler routing: cross-identity conflict check
        fetchrow_responses.extend(_handler_routing_side_effect_items())

        # _process_one_record (loser path): model, atomic_insert(loser→None),
        # dedup query (identical → idempotent), lock row (no enrichment)
        model_lookup = MagicMock()
        model_lookup.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None, "mode": None, "finish_reason": None,
            "reasoning_tokens": None, "cache_read_tokens": None,
            "cache_write_tokens": None, "session_id": _SESSION_ID,
        }.__getitem__
        fetchrow_responses.extend([model_lookup, None, existing_dedup, lock_row])

        # Existence check → None (no canonical event yet → backfill)
        fetchrow_responses.append(None)

        # _record_canonical_event: model lookup, session lookup, event lookup
        fetchrow_responses.extend(_canonical_event_side_effect_items())

        mock_conn.fetchrow.side_effect = fetchrow_responses
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-legacy-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]

        # Legacy duplicate with no canonical event → backfilled
        result = data["results"][0]
        assert result["status"] == "accepted", (
            f"Expected 'accepted' for backfilled legacy record, got '{result['status']}'"
        )
        assert result["event_id"] is not None, (
            "Legacy record must get a canonical event_id after backfill"
        )
        assert result["attempt_id"] is not None, (
            "Legacy record must get an attempt_id after backfill"
        )

        # Verify one canonical event INSERT was issued
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 1, (
            f"Expected 1 INSERT INTO usage_events, got {len(event_inserts)}"
        )

        # Verify one ingest attempt INSERT was issued
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1, (
            f"Expected 1 INSERT INTO usage_ingest_attempts, got {len(attempt_inserts)}"
        )


class TestIngestRequestReplayMetadata:
    """``IngestRequest`` accepts optional replay metadata fields."""

    @pytest.mark.asyncio
    async def test_replay_metadata_fields_accepted(self, monkeypatch):
        """``replay_id``, ``replay_requested_start``, ``replay_delivery_mode``
        are optional and accepted in the request body."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        replay_id = str(uuid.uuid4())
        payload = _valid_ingest_payload()
        payload["replay_id"] = replay_id
        payload["replay_requested_start"] = "2026-08-01"
        payload["replay_delivery_mode"] = "at-least-once"

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1

        # Verify replay_id was passed through to the attempt row
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1
        # Verify the replay_id appears in the attempt insert params
        assert replay_id in str(attempt_inserts[0].args)

    @pytest.mark.asyncio
    async def test_payload_without_replay_metadata_still_works(self, monkeypatch):
        """Existing payloads without replay metadata fields are processed
        identically — backward compatible."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Standard payload without replay_* fields
        payload = _valid_ingest_payload()

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
        assert data["results"][0]["event_id"] is not None
        assert data["results"][0]["attempt_id"] is not None


class TestIngestRecordResultStatusVocabulary:
    """``IngestRecordResult`` supports the expanded outcome vocabulary."""

    def test_status_field_describes_expanded_vocabulary(self):
        """The IngestRecordResult schema doc mentions the expanded status values."""
        # Import inline to avoid top-level import side effects
        from app.api.ingest import IngestRecordResult

        doc = IngestRecordResult.__doc__ or ""
        assert "accepted" in doc
        assert "duplicate" in doc
        assert "updated" in doc
        assert "quarantined" in doc
        assert "conflict" in doc
        assert "rejected" in doc

    def test_event_id_and_attempt_id_are_optional(self):
        """``event_id`` and ``attempt_id`` are optional UUIDs — absent for
        non-accepted (first-time) outcomes."""
        from app.api.ingest import IngestRecordResult

        # Defaults should produce a valid instance
        result = IngestRecordResult(index=0, status="rejected", reason="test")
        assert result.event_id is None
        assert result.attempt_id is None
        assert result.model_dump()["event_id"] is None
        assert result.model_dump()["attempt_id"] is None


class TestIngestBatchFKOrdering:
    """Regression test: the ``ingest_batches`` row must be created BEFORE
    any ``usage_ingest_attempts`` row that references it via FK.

    Migration 0021 created ``usage_ingest_attempts.ingest_batch_id`` as an
    immediate, non-deferrable foreign key referencing ``ingest_batches.id``.
    Inserting an attempt before the batch row raises an FK violation on a
    real Postgres database.  This test verifies that the execute call order
    in the mock is correct: the batch INSERT precedes every attempt INSERT.
    """

    @pytest.mark.asyncio
    async def test_batch_inserted_before_attempts(self, monkeypatch):
        """Verify that ``INSERT INTO ingest_batches`` appears before
        ``INSERT INTO usage_ingest_attempts`` in the execute call sequence."""
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
            records=[
                {
                    "source_record_id": "rec-fk-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-fk-002",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
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

        # Collect execute calls and their positions
        all_execute_calls = mock_conn.execute.call_args_list
        batch_insert_pos: int | None = None
        attempt_positions: list[int] = []

        for i, call in enumerate(all_execute_calls):
            sql = str(call)
            if "INSERT INTO ingest_batches" in sql:
                if batch_insert_pos is None:
                    batch_insert_pos = i
            if "INSERT INTO usage_ingest_attempts" in sql:
                attempt_positions.append(i)

        assert batch_insert_pos is not None, (
            "Expected INSERT INTO ingest_batches in execute calls"
        )
        assert len(attempt_positions) == 2, (
            f"Expected 2 INSERT INTO usage_ingest_attempts, "
            f"got {len(attempt_positions)}"
        )

        # Every attempt INSERT must come AFTER the batch INSERT
        for pos in attempt_positions:
            assert pos > batch_insert_pos, (
                f"usage_ingest_attempts INSERT at position {pos} must come after "
                f"ingest_batches INSERT at position {batch_insert_pos} — "
                f"otherwise the FK constraint would fail on a real database"
            )


# ════════════════════════════════════════════════════════════════════════════
#  Issue #389 — Ingest endpoint: quarantine + conflict routing
# ════════════════════════════════════════════════════════════════════════════


class TestQuarantinedIdentity:
    """Records whose resolved canonical identity has an active quarantine
    resolve to outcome ``quarantined`` — no canonical event created, no
    session aggregate updated, ingest attempt recorded with full JSONB."""

    @pytest.mark.asyncio
    async def test_quarantined_identity_returns_quarantined(self, monkeypatch):
        """A record from a source identity with an active (uncleared) quarantine
        returns status=quarantined, records an attempt, and creates no canonical event.
        Quarantine check runs BEFORE _process_one_record() — no raw record insert,
        no session aggregate update (issue #389 — Finding 2)."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,               # 0. auth row
            None,               # 1. source_database check (new)
        ]

        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Override after _build_ingest_app so quarantine check returns True
        mock_conn.fetchval = AsyncMock(return_value=True)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "quarantined"
        assert "quarantine" in (data["results"][0]["reason"] or "").lower()
        assert data["results"][0]["event_id"] is None
        assert data["results"][0]["attempt_id"] is not None

        # Finding 2: session aggregate must NOT be updated
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Quarantined record must NOT resolve session or bump session aggregates"
        )

        # Verify a usage_ingest_attempts row was inserted with outcome=quarantined
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1
        assert "quarantined" in str(attempt_inserts[0])

        # Verify no canonical event INSERT was issued
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 0, (
            "No canonical event should be created for quarantined records"
        )

    @pytest.mark.asyncio
    async def test_new_identity_with_overlap_triggers_quarantine(self, monkeypatch):
        """When a new source identity has records that overlap with an existing
        identity (detected via check_quarantine_overlap), a quarantine entry is
        created and the record returns status=quarantined.  Quarantine check
        runs BEFORE _process_one_record() — no session aggregate update
        (issue #389 — Finding 2)."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        # quarantine_identity calls fetchrow to INSERT ... RETURNING id
        mock_quarantine_row = MagicMock()
        mock_quarantine_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. source_database check (new)
            mock_quarantine_row, # 2. quarantine_identity: INSERT INTO source_identity_quarantine
        ]

        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # check_quarantine_overlap via fetch → returns overlap evidence
        overlapping_identity_id = uuid.uuid4()
        mock_overlap_row = MagicMock()
        mock_overlap_row.__getitem__.side_effect = {
            "overlapping_identity_id": overlapping_identity_id,
            "overlap_count": 3,
        }.__getitem__
        mock_conn.fetch = AsyncMock(return_value=[mock_overlap_row])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "quarantined"
        assert "quarantine" in (data["results"][0]["reason"] or "").lower()
        assert data["results"][0]["event_id"] is None
        assert data["results"][0]["attempt_id"] is not None

        # Finding 2: session aggregate must NOT be updated
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Overlap-quarantined record must NOT resolve session"
        )

        # Verify quarantine INSERT was called
        quarantine_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO source_identity_quarantine" in str(call)
        ]
        assert len(quarantine_inserts) == 1, (
            "Expected quarantine entry to be created for overlapping identity"
        )

        # Verify attempt recorded with quarantined outcome
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1
        assert "quarantined" in str(attempt_inserts[0])

        # Verify no canonical event created
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 0

    @pytest.mark.asyncio
    async def test_cross_identity_conflict(self, monkeypatch):
        """When a canonical event exists for a different unresolved identity,
        the record returns status=conflict with no merge across identities.
        Conflict check runs BEFORE _process_one_record() — no session aggregate
        update (issue #389 — Finding 2)."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        # cross-identity check: event exists under a DIFFERENT identity
        different_identity_id = uuid.uuid4()
        cross_event_row = MagicMock()
        cross_event_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "canonical_source_identity_id": different_identity_id,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. source_database check
            cross_event_row,     # 2. cross-identity conflict check
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
        assert data["results"][0]["status"] == "conflict"
        assert "cross-identity" in (data["results"][0]["reason"] or "").lower()
        assert data["results"][0]["event_id"] is None
        assert data["results"][0]["attempt_id"] is not None

        # Finding 2: session aggregate must NOT be updated
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Conflict record must NOT resolve session or bump session aggregates"
        )

        # Verify attempt recorded with conflict outcome
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1
        assert "conflict" in str(attempt_inserts[0])

        # Verify no canonical event INSERT
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 0, (
            "No canonical event should be created for conflict records"
        )

    @pytest.mark.asyncio
    async def test_cross_identity_conflict_checks_attempts_history(self, monkeypatch):
        """The per-record cross-identity conflict check consults BOTH the
        canonical events table AND the complete delivery history
        (usage_ingest_attempts) — so legacy pre-canonical records that were
        accepted before usage_events existed cannot escape the conflict
        check (PR #418 review finding)."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        # cross-identity check: evidence comes back from the conflict-check
        # fetchrow (the attempts leg of the UNION ALL query); only its
        # non-None-ness matters for routing to "conflict".
        different_identity_id = uuid.uuid4()
        cross_event_row = MagicMock()
        cross_event_row.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "canonical_source_identity_id": different_identity_id,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. source_database check
            cross_event_row,     # 2. cross-identity conflict check
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
        assert data["results"][0]["status"] == "conflict"

        # The conflict-check fetchrow is the ONLY fetchrow whose SQL unions
        # usage_ingest_attempts (the batch overlap check uses conn.fetch).
        conflict_checks = [
            call for call in mock_conn.fetchrow.call_args_list
            if "usage_ingest_attempts" in call.args[0]
            and "UNION ALL" in call.args[0]
        ]
        assert len(conflict_checks) == 1, (
            "Expected exactly one conflict-check fetchrow consulting "
            "usage_ingest_attempts"
        )
        conflict_sql = conflict_checks[0].args[0]
        assert "usage_ingest_attempts" in conflict_sql
        assert "original_source_record_id" in conflict_sql
        assert "a.outcome IN ('accepted', 'duplicate', 'updated')" in conflict_sql
        assert "canonical_parent_id IS NOT NULL" in conflict_sql

        # Unchanged routing behaviour: conflict record is not merged
        assert "cross-identity" in (data["results"][0]["reason"] or "").lower()
        assert data["results"][0]["event_id"] is None
        assert data["results"][0]["attempt_id"] is not None

        # Finding 2: session aggregate must NOT be updated
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Conflict record must NOT resolve session or bump session aggregates"
        )

        # Verify attempt recorded with conflict outcome
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 1
        assert "conflict" in str(attempt_inserts[0])

        # Verify no canonical event INSERT
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 0, (
            "No canonical event should be created for conflict records"
        )


class TestReplayBatchQuarantinedIdentity:
    """A replay batch arriving for an event whose source identity is
    quarantined quarantines every record in the batch independently —
    including genuine dedup replays (issue #389 — Finding 1)."""

    @pytest.mark.asyncio
    async def test_replay_batch_all_records_quarantined(self, monkeypatch):
        """When a replay batch has 2 records for a quarantined identity,
        both resolve to quarantined independently (no partial acceptance).
        Quarantine check runs BEFORE _process_one_record()."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. sd check
        ]

        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)
        mock_conn.fetchval = AsyncMock(return_value=True)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-replay-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-replay-002",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
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
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 2
        assert data["results"][0]["status"] == "quarantined"
        assert data["results"][1]["status"] == "quarantined"
        assert data["results"][0]["attempt_id"] is not None
        assert data["results"][0]["event_id"] is None
        assert data["results"][1]["attempt_id"] is not None
        assert data["results"][1]["event_id"] is None

        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 2
        for call in attempt_inserts:
            assert "quarantined" in str(call)

        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        assert len(event_inserts) == 0

    @pytest.mark.asyncio
    async def test_genuine_dedup_replay_quarantined_identity(self, monkeypatch):
        """Finding 1: a genuine dedup replay for a quarantined identity must
        resolve to ``quarantined``, NOT ``accepted``.  The quarantine check
        runs BEFORE _process_one_record(), independent of the is_new gate."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. sd check
        ]

        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)
        mock_conn.fetchval = AsyncMock(return_value=True)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-dedup-replay-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {
                    "source_record_id": "rec-dedup-replay-002",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 200,
                    "output_tokens": 75,
                    "cached_tokens": 0,
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
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 2
        assert data["results"][0]["status"] == "quarantined"
        assert data["results"][1]["status"] == "quarantined"

        # _process_one_record must NOT have been called
        model_or_insert_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(model_or_insert_calls) == 0, (
            "Genuine dedup replay for quarantined identity must NOT call "
            "_process_one_record — quarantine check runs first"
        )

        # _resolve_session must NOT have been called
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Session aggregates must NOT be updated for quarantined replay records"
        )

        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(attempt_inserts) == 2
        for call in attempt_inserts:
            assert "quarantined" in str(call)

    @pytest.mark.asyncio
    async def test_quarantined_record_no_raw_record_insert(self, monkeypatch):
        """Finding 2: a quarantined record must NOT insert a raw
        ``opencode_usage_records`` row, must NOT bump
        ``source_databases.record_count``, and must NOT call
        ``_resolve_session``."""
        mock_conn = AsyncMock()
        auth = _auth_row()

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,                # 0. auth
            None,                # 1. source_database check
        ]

        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)
        mock_conn.fetchval = AsyncMock(return_value=True)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["results"][0]["status"] == "quarantined"

        # No raw usage record INSERT
        record_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        assert len(record_inserts) == 0, (
            "Quarantined record must NOT insert a raw opencode_usage_records row"
        )

        # No session resolution
        session_calls = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO sessions" in str(call)
        ]
        assert len(session_calls) == 0, (
            "Quarantined record must NOT call _resolve_session"
        )

        # No source database record_count bump
        sd_bumps = [
            call for call in mock_conn.execute.call_args_list
            if "record_count = record_count + 1" in str(call)
        ]
        assert len(sd_bumps) == 0, (
            "Quarantined record must NOT bump source_databases.record_count"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Issue #388 — Ingest duplicate detection + replay merge on canonical events
# ──────────────────────────────────────────────────────────────────────────────
# Adjusted for #389 handler routing: _build_ingest_app monkeypatches
# resolve_canonical_identity, is_quarantined, and check_quarantine_overlap.
# The handler's cross-identity conflict check consumes one fetchrow slot
# per record (via _handler_routing_side_effect_items).


class TestCanonicalDuplicateDetection:
    """Duplicate detection on the canonical event layer (issue #388).

    When ``_record_canonical_event`` finds an existing canonical event
    for ``(canonical_source_identity_id, source_record_id)``, the incoming
    record is compared against the stored event: identical → ``"duplicate"``
    (no event modification); differing → ``"updated"`` (replay merge).
    """

    # ── Shared helpers ──────────────────────────────────────────────────

    @staticmethod
    def _existing_canonical_mock(event_id: uuid.UUID) -> MagicMock:
        """Return a findrow row signalling an existing canonical event."""
        row = MagicMock()
        row.__getitem__.side_effect = {"id": event_id}.__getitem__
        return row

    @staticmethod
    def _full_event_mock(*, input_tokens: int = 100, output_tokens: int = 50,
                         cached_tokens: int = 0, reasoning_tokens: int = 0,
                         cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                         estimated_cost_usd: Decimal | None = Decimal("0.0035"),
                         provider: str | None = None, mode: str | None = None,
                         finish_reason: str | None = None) -> MagicMock:
        """Return a fetchrow row with full canonical event field values."""
        values: dict = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "provider": provider,
            "mode": mode,
            "finish_reason": finish_reason,
        }
        row = MagicMock()
        row.__getitem__.side_effect = values.__getitem__
        return row

    @staticmethod
    def _enrichment_read_mock() -> MagicMock:
        """Return a fetchrow row for the text enrichment FOR UPDATE read."""
        row = MagicMock()
        row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
        }.__getitem__
        return row

    def _build_existing_canonical_side_effect(
        self,
        *,
        event_id: uuid.UUID,
        stored_event: MagicMock,
        extra_fetchrow: list | None = None,
    ) -> list:
        """Build a fetchrow side-effect list for a single new record that
        maps to an EXISTING canonical event.

        The legacy _process_one_record layer sees the record as a winner
        (new → atomic INSERT returns a row).  The _record_canonical_event
        layer then finds an existing canonical event and reads its full
        fields for comparison.

        After the #389 restructure: resolve_canonical_identity is
        monkeypatched (no fetchrow slot), and the handler's cross-identity
        conflict check consumes one fetchrow slot per record.

        extra_fetchrow: additional fetchrow responses needed after the
            full event read (e.g. for enrichment FOR UPDATE during merge).
        """
        auth = _auth_row()
        items: list = [auth, None]  # auth + sd_check

        # handler: cross-identity conflict check
        items.extend(_handler_routing_side_effect_items())

        # _process_one_record: model, atomic INSERT(winner), session upsert
        insert_row = MagicMock()
        insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row = MagicMock()
        session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([None, insert_row, session_row])

        # model lookup, session lookup (_record_canonical_event)
        # resolve_canonical_identity is monkeypatched — no fetchrow slots
        model_row = MagicMock()
        model_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_lookup_row = MagicMock()
        session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        items.extend([model_row, session_lookup_row])

        # event lookup → returns existing event id
        items.append(self._existing_canonical_mock(event_id))

        # full event read for comparison
        items.append(stored_event)

        if extra_fetchrow:
            items.extend(extra_fetchrow)

        return items

    # ── Tests ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_identical_replay_returns_duplicate_no_event_change(self, monkeypatch):
        """An identical replay returns status ``"duplicate"`` with no
        modification to the canonical event.  The attempt is recorded
        with outcome ``"duplicate"``."""
        mock_conn = AsyncMock()
        stored_event = self._full_event_mock()
        event_id = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
            )
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-identical-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        result = data["results"][0]
        assert result["status"] == "duplicate", (
            f"Expected 'duplicate' for identical replay, got '{result['status']}'"
        )
        assert result["event_id"] is not None
        assert result["attempt_id"] is not None

        # Verify no change to the event — no UPDATE on usage_events
        update_events = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE usage_events" in str(c)
        ]
        assert len(update_events) == 0, (
            f"Expected 0 UPDATES to usage_events for identical replay, got {len(update_events)}"
        )

        # Verify attempt was recorded with "duplicate" outcome
        attempt_inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(c)
        ]
        assert len(attempt_inserts) == 1
        assert "duplicate" in str(attempt_inserts[0].args), (
            "Ingest Attempt outcome must be 'duplicate'"
        )

    @pytest.mark.asyncio
    async def test_single_token_field_change_returns_updated(self, monkeypatch):
        """A replay with a single differing token field triggers replay
        merge and returns status ``"updated"``."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()
        # Stored event: input_tokens=100, incoming: input_tokens=200
        stored_event = self._full_event_mock(input_tokens=100, output_tokens=50)
        mock_conn.fetchrow = AsyncMock()

        # Extra fetchrow: enrichment FOR UPDATE read (all NULL → no fill)
        enrichment_read = self._enrichment_read_mock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
                extra_fetchrow=[enrichment_read],
            )
        )
        mock_conn.fetchval = AsyncMock(return_value=None)  # advisory lock
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        # Mock apply_replay_merge to return UPDATED
        import app.core.reconciliation as _recon
        monkeypatch.setattr(
            _recon, "apply_replay_merge",
            AsyncMock(return_value=_recon.IngestOutcome.UPDATED),
        )

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-diff-input-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 200,   # differs from stored (100)
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        result = data["results"][0]
        assert result["status"] == "updated", (
            f"Expected 'updated' for differing replay, got '{result['status']}'"
        )
        assert result["event_id"] is not None
        assert result["attempt_id"] is not None

        # Verify apply_replay_merge was called with correct new_values
        apply_call = _recon.apply_replay_merge.call_args
        assert apply_call is not None, "apply_replay_merge should have been called"
        new_values_arg = apply_call[0][2]  # (conn, event_id, new_values)
        assert new_values_arg["input_tokens"] == 200
        assert new_values_arg["output_tokens"] == 50

        # Verify attempt recorded with "updated" outcome
        attempt_inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(c)
        ]
        assert len(attempt_inserts) == 1
        assert "updated" in str(attempt_inserts[0].args), (
            "Ingest Attempt outcome must be 'updated'"
        )

        # Verify last_ingested_at was updated
        last_ingested_updates = [
            c for c in mock_conn.execute.call_args_list
            if "SET last_ingested_at" in str(c)
        ]
        assert len(last_ingested_updates) == 1

    @pytest.mark.asyncio
    async def test_multi_field_change_triggers_replay_merge(self, monkeypatch):
        """A replay with multiple differing fields (input_tokens, output_tokens,
        cached_tokens) triggers replay merge and returns ``"updated"``."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()
        stored_event = self._full_event_mock(
            input_tokens=100, output_tokens=50, cached_tokens=0,
        )
        enrichment_read = self._enrichment_read_mock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
                extra_fetchrow=[enrichment_read],
            )
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        import app.core.reconciliation as _recon
        monkeypatch.setattr(
            _recon, "apply_replay_merge",
            AsyncMock(return_value=_recon.IngestOutcome.UPDATED),
        )

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-multi-diff-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 300,    # stored: 100
                "output_tokens": 150,   # stored: 50
                "cached_tokens": 50,    # stored: 0
                "estimated_cost_usd": "0.0150",  # stored: 0.0035
                "reported_at": _mk_ts().isoformat(),
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated"

        # verify the merge was triggered
        assert _recon.apply_replay_merge.called

    @pytest.mark.asyncio
    async def test_text_enrichment_change_returns_updated_with_coalesce_fill(
        self, monkeypatch,
    ):
        """A replay whose token/cost fields are identical but whose text
        enrichment fields differ triggers a COALESCE fill and returns
        ``"updated"``."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()

        # Stored event: no enrichment; incoming: provider="openai", mode="chat"
        stored_event = self._full_event_mock(
            provider=None, mode=None, finish_reason=None,
        )
        # enrichment FOR UPDATE read: stored values are all NULL
        enrichment_read = self._enrichment_read_mock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
                extra_fetchrow=[enrichment_read],
            )
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        # apply_replay_merge returns DUPLICATE (zero delta on token/cost)
        import app.core.reconciliation as _recon
        monkeypatch.setattr(
            _recon, "apply_replay_merge",
            AsyncMock(return_value=_recon.IngestOutcome.DUPLICATE),
        )

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[{
                "source_record_id": "rec-enrich-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
                "provider": "openai",       # stored: NULL
                "mode": "chat",             # stored: NULL
                "finish_reason": "stop",    # stored: NULL
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        # Enrichment was filled → outcome is "updated"
        assert result["status"] == "updated", (
            f"Expected 'updated' for enrichment fill, got '{result['status']}'"
        )

        # Verify COALESCE UPDATE was issued for text enrichment
        coalesce_updates = [
            c for c in mock_conn.execute.call_args_list
            if "COALESCE" in str(c)
        ]
        assert len(coalesce_updates) == 1, (
            f"Expected 1 COALESCE UPDATE for text enrichment, got {len(coalesce_updates)}"
        )

        # Verify attempt recorded with "updated"
        attempt_inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(c)
        ]
        assert len(attempt_inserts) == 1
        assert "updated" in str(attempt_inserts[0].args)

    @pytest.mark.asyncio
    async def test_incoming_null_enrichment_does_not_erase_populated_value(
        self, monkeypatch,
    ):
        """When the incoming record has ``provider=None`` but the stored
        canonical event already has ``provider="openai"``, the populated
        value is NOT erased.  The comparison treats them as identical
        (stored non-None, incoming None → match passes), so the replay
        is classified as ``"duplicate"``.

        This tests the null-handling / non-erasing semantic of ADR 0011:
        null/omitted collector values never erase populated stored values.
        """
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()

        # Stored event has provider="openai", incoming has provider=None
        stored_event = self._full_event_mock(provider="openai")
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
            )
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        # Incoming: provider=None (omitted), same token values
        record = {
            "source_record_id": "rec-null-enrich-001",
            "session_id": str(_SESSION_ID),
            "model": "gpt-4",
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": "0.0035",
            "reported_at": _mk_ts().isoformat(),
            # provider deliberately omitted (None)
        }

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=[record]),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        # stored provider="openai" vs incoming None → should be "duplicate"
        assert result["status"] == "duplicate", (
            f"Expected 'duplicate' (null incoming does not erase), got '{result['status']}'"
        )

        # No UPDATE on usage_events (no erasure)
        update_events = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE usage_events" in str(c)
        ]
        assert len(update_events) == 0, (
            "Expected 0 UPDATES — null incoming must not erase"
        )

    @pytest.mark.asyncio
    async def test_mixed_batch_accepted_duplicate_updated_independent(
        self, monkeypatch,
    ):
        """A replay batch containing a mix of accepted, duplicate, and updated
        records processes each record independently."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)

        # new → accepted (event id unused; only fetchrow ordering matters)
        event_id_2 = uuid.uuid4()  # identical → duplicate
        event_id_3 = uuid.uuid4()  # different → updated

        # ── Build fetchrow side effects for 3 records ─────────────────
        auth = _auth_row()
        fetchrow_responses: list = [auth, None]  # auth + sd_check

        # Record 1: NEW — handler routing → _process_one_record winner → _record_canonical_event NEW
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        insert_row_1 = MagicMock()
        insert_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row_1 = MagicMock()
        session_row_1.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend([None, insert_row_1, session_row_1])
        fetchrow_responses.extend(_canonical_event_side_effect_items())

        # Record 2: winner → EXISTING canonical → identical
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        insert_row_2 = MagicMock()
        insert_row_2.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row_2 = MagicMock()
        session_row_2.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend([None, insert_row_2, session_row_2])
        # resolve_canonical_identity is monkeypatched — no fetchrow slots
        model_r2 = MagicMock()
        model_r2.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        sess_r2 = MagicMock()
        sess_r2.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend([model_r2, sess_r2])
        fetchrow_responses.append(self._existing_canonical_mock(event_id_2))
        fetchrow_responses.append(self._full_event_mock())  # identical to record 2

        # Record 3: winner → EXISTING canonical → different
        fetchrow_responses.extend(_handler_routing_side_effect_items())
        insert_row_3 = MagicMock()
        insert_row_3.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        session_row_3 = MagicMock()
        session_row_3.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend([None, insert_row_3, session_row_3])
        # resolve_canonical_identity is monkeypatched — no fetchrow slots
        model_r3 = MagicMock()
        model_r3.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        sess_r3 = MagicMock()
        sess_r3.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        fetchrow_responses.extend([model_r3, sess_r3])
        fetchrow_responses.append(self._existing_canonical_mock(event_id_3))
        # Stored: input_tokens=100, incoming will be 200
        fetchrow_responses.append(self._full_event_mock(input_tokens=100))
        # Enrichment FOR UPDATE read
        enrichment_read = self._enrichment_read_mock()
        fetchrow_responses.append(enrichment_read)

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = fetchrow_responses
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        # Mock apply_replay_merge for record 3
        import app.core.reconciliation as _recon
        monkeypatch.setattr(
            _recon, "apply_replay_merge",
            AsyncMock(return_value=_recon.IngestOutcome.UPDATED),
        )

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {  # Record 0: new (accepted)
                    "source_record_id": "rec-mix-new",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {  # Record 1: identical (duplicate)
                    "source_record_id": "rec-mix-dup",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
                {  # Record 2: different (updated)
                    "source_record_id": "rec-mix-upd",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 200,       # differs from stored (100)
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _mk_ts().isoformat(),
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        results = data["results"]
        assert len(results) == 3

        # Batch counters: accepted / duplicate / updated are all successful
        # outcomes and count as accepted, so the invariant
        # accepted_count + rejected_count == len(records) holds.
        assert data["accepted_count"] == 3, (
            f"Expected 3 accepted (incl. duplicate/updated), got {data['accepted_count']}"
        )
        assert data["rejected_count"] == 0, (
            f"Expected 0 rejected, got {data['rejected_count']}"
        )
        assert data["accepted_count"] + data["rejected_count"] == len(results)

        assert results[0]["status"] == "accepted", (
            f"Record 0 should be accepted, got '{results[0]['status']}'"
        )
        assert results[1]["status"] == "duplicate", (
            f"Record 1 should be duplicate, got '{results[1]['status']}'"
        )
        assert results[2]["status"] == "updated", (
            f"Record 2 should be updated, got '{results[2]['status']}'"
        )

        # All three should have event_id and attempt_id
        for i, result in enumerate(results):
            assert result["event_id"] is not None, (
                f"Record {i}: event_id should not be None for status '{result['status']}'"
            )
            assert result["attempt_id"] is not None, (
                f"Record {i}: attempt_id should not be None for status '{result['status']}'"
            )

        # Verify attempt outcomes: one accepted, one duplicate, one updated
        attempt_inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(c)
        ]
        assert len(attempt_inserts) == 3
        outcomes = [str(c.args) for c in attempt_inserts]
        assert any("accepted" in o for o in outcomes), "Missing 'accepted' attempt"
        assert any("duplicate" in o for o in outcomes), "Missing 'duplicate' attempt"
        assert any("updated" in o for o in outcomes), "Missing 'updated' attempt"

        # The ingest_batches UPDATE must carry the consistent totals:
        # accepted_count = 3, rejected_count = 0 (sum == record_count).
        batch_updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE ingest_batches" in str(c)
        ]
        assert len(batch_updates) == 1
        # conn.execute(sql, accepted, rejected, batch_id) — params follow the SQL string.
        update_args = batch_updates[0].args
        assert update_args[1] == 3, f"accepted_count written to ingest_batches: {update_args[1]}"
        assert update_args[2] == 0, f"rejected_count written to ingest_batches: {update_args[2]}"

    @pytest.mark.asyncio
    async def test_cache_tokens_field_change_triggers_delta(self, monkeypatch):
        """A replay with differing cache_read_tokens and cache_write_tokens
        triggers replay merge with those fields in the new_values."""
        mock_conn = AsyncMock()
        event_id = uuid.uuid4()
        stored_event = self._full_event_mock(
            cache_read_tokens=0, cache_write_tokens=0,
        )
        enrichment_read = self._enrichment_read_mock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = (
            self._build_existing_canonical_side_effect(
                event_id=event_id, stored_event=stored_event,
                extra_fetchrow=[enrichment_read],
            )
        )
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        _add_transaction_support(mock_conn)

        import app.core.reconciliation as _recon
        monkeypatch.setattr(
            _recon, "apply_replay_merge",
            AsyncMock(return_value=_recon.IngestOutcome.UPDATED),
        )

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[{
                "source_record_id": "rec-cache-diff-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
                "cache_read_tokens": 30,      # stored: 0
                "cache_write_tokens": 15,     # stored: 0
            }],
        )

        async with client as c:
            response = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        result = response.json()["data"]["results"][0]
        assert result["status"] == "updated"

        # Verify cache token fields are passed to apply_replay_merge
        apply_call = _recon.apply_replay_merge.call_args
        new_values = apply_call[0][2]
        assert new_values["cache_read_tokens"] == 30
        assert new_values["cache_write_tokens"] == 15


class TestBatchOverlapQueryCount:
    """Issue #416 batch overlap routing regressions."""

    @staticmethod
    def _records(count: int) -> list[dict]:
        return [
            {
                "source_record_id": f"batch-rec-{index:03d}",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            }
            for index in range(count)
        ]

    @pytest.mark.asyncio
    async def test_100_records_execute_one_overlap_query(self, monkeypatch):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), *_new_record_side_effect(100)],
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=self._records(100)),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 100
        assert mock_conn.fetch.await_count == 1
        sql = mock_conn.fetch.call_args.args[0]
        assert "source_record_id = ANY($2::text[])" in sql
        assert "usage_ingest_attempts" in sql

    @pytest.mark.asyncio
    async def test_overlap_quarantines_batch_before_accounting(self, monkeypatch):
        mock_conn = AsyncMock()
        quarantine_row = MagicMock()
        quarantine_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, quarantine_row],
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        overlap_row = MagicMock()
        overlap_row.__getitem__.side_effect = {
            "overlapping_identity_id": uuid.uuid4(),
            "overlap_count": 50,
        }.__getitem__
        mock_conn.fetch = AsyncMock(return_value=[overlap_row])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=self._records(100)),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 100
        assert all(result["status"] == "quarantined" for result in data["results"])
        assert mock_conn.fetch.await_count == 1

        fetchrow_sql = [str(call.args[0]) for call in mock_conn.fetchrow.call_args_list]
        execute_sql = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
        assert sum("INSERT INTO source_identity_quarantine" in sql for sql in fetchrow_sql) == 1
        assert not any("INSERT INTO opencode_usage_records" in sql for sql in fetchrow_sql)
        assert not any("INSERT INTO sessions" in sql for sql in fetchrow_sql)
        assert not any("INSERT INTO usage_events" in sql for sql in execute_sql)
        assert sum("INSERT INTO usage_ingest_attempts" in sql for sql in execute_sql) == 100

    @pytest.mark.asyncio
    async def test_invalid_record_remains_rejected_in_overlapping_batch(self, monkeypatch):
        mock_conn = AsyncMock()
        quarantine_row = MagicMock()
        quarantine_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, quarantine_row],
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        overlap_row = MagicMock()
        overlap_row.__getitem__.side_effect = {
            "overlapping_identity_id": uuid.uuid4(),
            "overlap_count": 1,
        }.__getitem__
        mock_conn.fetch = AsyncMock(return_value=[overlap_row])
        records = self._records(2)
        records[0]["input_tokens"] = -1

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=records),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        results = response.json()["data"]["results"]
        assert results[0]["status"] == "rejected"
        assert results[0]["reason"] == "Negative token value"
        assert results[1]["status"] == "quarantined"
        assert "overlap detected" in results[1]["reason"]

    @pytest.mark.asyncio
    async def test_legacy_attempts_overlap_quarantines_batch(self, monkeypatch):
        """Overlap evidence surfaced via the attempts leg still routes the
        entire batch to quarantine before any accounting is written."""
        mock_conn = AsyncMock()
        quarantine_row = MagicMock()
        quarantine_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, quarantine_row],
        )
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        overlap_row = MagicMock()
        overlap_row.__getitem__.side_effect = {
            "overlapping_identity_id": uuid.uuid4(),
            "overlap_count": 2,
        }.__getitem__
        mock_conn.fetch = AsyncMock(return_value=[overlap_row])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_ingest_payload(records=self._records(2)),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 2
        assert all(result["status"] == "quarantined" for result in data["results"])
        assert mock_conn.fetch.await_count == 1
