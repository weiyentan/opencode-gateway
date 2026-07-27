"""Tests for the GET /cursor endpoint — collector cursor recovery.

Covers:
- Valid source_database_id returns last_seen_at, record_count, is_active
- Unknown source_database_id returns 404
- Missing Authorization header returns 401
- Invalid bearer token returns 401
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session

# ── Shared test data ────────────────────────────────────────────────────────

_SOURCE_DB_ID = uuid.uuid4()
_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ts() -> datetime:
    return datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


# ── Mock helpers ─────────────────────────────────────────────────────────────


def _source_db_row(
    *,
    last_seen_at: datetime | None = None,
    record_count: int = 5000,
    is_active: bool = True,
) -> MagicMock:
    """Return a mock row representing a source_databases entry."""
    row = MagicMock()
    data = {
        "last_seen_at": last_seen_at or _mk_ts(),
        "record_count": record_count,
        "is_active": is_active,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


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


def _build_cursor_app(
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


# ── Tests ────────────────────────────────────────────────────────────────────


class TestCursorSuccess:
    """GET /cursor returns cursor state for a known source_database_id."""

    @pytest.mark.asyncio
    async def test_returns_cursor_state(self, monkeypatch):
        """Known source_database_id returns last_seen_at, record_count, is_active."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        source_row = _source_db_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, source_row]
        mock_conn.execute = AsyncMock()

        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={_SOURCE_DB_ID}",
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["source_database_id"] == str(_SOURCE_DB_ID)
        assert data["last_seen_at"] == _mk_ts().strftime("%Y-%m-%dT%H:%M:%SZ")
        assert data["record_count"] == 5000
        assert data["is_active"] is True

    @pytest.mark.asyncio
    async def test_returns_cursor_state_inactive(self, monkeypatch):
        """Known inactive source_database_id returns is_active=False."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        source_row = _source_db_row(is_active=False)
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, source_row]
        mock_conn.execute = AsyncMock()

        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={_SOURCE_DB_ID}",
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["source_database_id"] == str(_SOURCE_DB_ID)
        assert data["is_active"] is False

    @pytest.mark.asyncio
    async def test_returns_cursor_state_zero_records(self, monkeypatch):
        """Source database with zero records returns record_count=0."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        source_row = _source_db_row(record_count=0)
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, source_row]
        mock_conn.execute = AsyncMock()

        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={_SOURCE_DB_ID}",
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["record_count"] == 0


class TestCursorNotFound:
    """GET /cursor returns 404 for an unknown source_database_id."""

    @pytest.mark.asyncio
    async def test_unknown_source_database_id_returns_404(self, monkeypatch):
        """Unknown source_database_id returns 404 with clear error message."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, None]  # auth succeeds, db lookup returns None
        mock_conn.execute = AsyncMock()

        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        unknown_id = uuid.uuid4()
        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={unknown_id}",
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 404
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "NOT_FOUND"
        assert str(unknown_id) in body["error"]["message"]


class TestCursorUnauthenticated:
    """Requests without a valid collector token return 401."""

    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self, monkeypatch):
        """No Authorization header → 401 from collector token auth."""
        mock_conn = AsyncMock()
        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(f"/cursor?source_database_id={_SOURCE_DB_ID}")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, monkeypatch):
        """Unrecognised bearer token → 401."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # auth not found
        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={_SOURCE_DB_ID}",
                headers={"Authorization": "Bearer invalid-token-here"},
            )

        assert response.status_code == 401


class TestCursorValidation:
    """GET /cursor returns 422 for invalid query parameter formats."""

    @pytest.mark.asyncio
    async def test_invalid_uuid_format_returns_422(self, monkeypatch):
        """Non-UUID source_database_id → 422 from FastAPI type validation."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth]
        client = _build_cursor_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                "/cursor?source_database_id=not-a-uuid",
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 422
        body = response.json()
        # App uses custom validation_exception_handler → envelope format
        assert body["status"] == "error"
        assert body["error"]["code"] == "VALIDATION_ERROR"
