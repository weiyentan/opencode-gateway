"""Tests for the admin ``POST /admin/resolve-source-identity`` endpoint.

The endpoint resolves a quarantined source identity into a canonical
parent identity by delegating to ``resolve_identity()`` (unit-tested in
``tests/test_identity.py``), then returns the resolution details.  Tests
use the mock database connection from conftest.py and verify the
endpoint's validation behaviour and response shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import create_client

# ── Shared constants ───────────────────────────────────────────────────────

_QUARANTINE_ID = uuid.uuid4()
_QUARANTINED_IDENTITY_ID = uuid.uuid4()  # the identity being resolved
_RESOLVING_IDENTITY_ID = uuid.uuid4()  # the canonical parent it links to
_RESOLUTION_ID = uuid.uuid4()
_REASON = "Runner VM rebuilt, same physical machine"

_TS = datetime.fromisoformat("2026-08-09T12:00:00+00:00")


def _row(data: dict) -> MagicMock:
    """Return a MagicMock that behaves like an asyncpg Record for dict access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _quarantine_row(cleared_at: datetime | None = None) -> MagicMock:
    """Return a mock row resembling a source_identity_quarantine row."""
    return _row(
        {
            "id": _QUARANTINE_ID,
            "source_identity_id": _QUARANTINED_IDENTITY_ID,
            "cleared_at": cleared_at,
        }
    )


def _resolution_row() -> MagicMock:
    """Return a mock row resembling a source_identity_resolutions row."""
    return _row({"id": _RESOLUTION_ID, "resolved_at": _TS})


def _payload(**overrides) -> dict:
    payload = {
        "quarantine_id": str(_QUARANTINE_ID),
        "resolving_identity_id": str(_RESOLVING_IDENTITY_ID),
        "reason": _REASON,
    }
    payload.update(overrides)
    return payload


class TestResolveSourceIdentity:
    """POST /admin/resolve-source-identity"""

    @pytest.mark.asyncio
    async def test_valid_resolution_returns_resolution_details(self, monkeypatch):
        """A valid resolution delegates to resolve_identity and returns 200.

        The response echoes the quarantine and resolving identity ids and
        the recorded resolution's id and timestamp.
        """
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                _quarantine_row(),
                _row({"id": _RESOLVING_IDENTITY_ID}),
                _resolution_row(),
            ]
        )
        mock_resolve = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "app.api.admin_resolve_source_identity.resolve_identity", mock_resolve
        )
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post("/admin/resolve-source-identity", json=_payload())

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        inner = data["data"]
        assert inner["resolution_id"] == str(_RESOLUTION_ID)
        assert inner["quarantine_id"] == str(_QUARANTINE_ID)
        assert inner["resolved_identity_id"] == str(_QUARANTINED_IDENTITY_ID)
        assert inner["linked_to_identity_id"] == str(_RESOLVING_IDENTITY_ID)
        assert datetime.fromisoformat(
            inner["resolved_at"].replace("Z", "+00:00")
        ) == _TS

        # resolve_identity was called with the quarantine, the resolving
        # identity, the reason, and no authenticated user identity.
        mock_resolve.assert_awaited_once_with(
            mock_conn,
            _QUARANTINE_ID,
            _RESOLVING_IDENTITY_ID,
            _REASON,
            resolved_by=None,
        )

    @pytest.mark.asyncio
    async def test_resolution_id_read_from_audit_table(self, monkeypatch):
        """The resolution id and timestamp come from source_identity_resolutions."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                _quarantine_row(),
                _row({"id": _RESOLVING_IDENTITY_ID}),
                _resolution_row(),
            ]
        )
        monkeypatch.setattr(
            "app.api.admin_resolve_source_identity.resolve_identity", AsyncMock()
        )
        client = create_client(mock_conn)

        async with client as c:
            await c.post("/admin/resolve-source-identity", json=_payload())

        # Calls: quarantine lookup, resolving identity lookup, audit read-back.
        assert mock_conn.fetchrow.await_count == 3
        audit_sql = mock_conn.fetchrow.call_args.args[0]
        assert "source_identity_resolutions" in audit_sql
        assert mock_conn.fetchrow.call_args.args[1:] == (_QUARANTINE_ID,)

    @pytest.mark.asyncio
    async def test_unknown_quarantine_returns_404(self, monkeypatch):
        """A non-existent quarantine_id returns 404 without calling resolve."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_resolve = AsyncMock()
        monkeypatch.setattr(
            "app.api.admin_resolve_source_identity.resolve_identity", mock_resolve
        )
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/admin/resolve-source-identity",
                json=_payload(quarantine_id=str(uuid.uuid4())),
            )

        assert response.status_code == 404
        assert mock_resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_already_cleared_quarantine_returns_400(self, monkeypatch):
        """An already-resolved quarantine returns 400 without calling resolve."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=_quarantine_row(cleared_at=_TS))
        mock_resolve = AsyncMock()
        monkeypatch.setattr(
            "app.api.admin_resolve_source_identity.resolve_identity", mock_resolve
        )
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post("/admin/resolve-source-identity", json=_payload())

        assert response.status_code == 400
        assert mock_resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_unknown_resolving_identity_returns_400(self, monkeypatch):
        """A non-existent resolving identity returns 400 without calling resolve."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_quarantine_row(), None]
        )
        mock_resolve = AsyncMock()
        monkeypatch.setattr(
            "app.api.admin_resolve_source_identity.resolve_identity", mock_resolve
        )
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/admin/resolve-source-identity",
                json=_payload(resolving_identity_id=str(uuid.uuid4())),
            )

        assert response.status_code == 400
        assert mock_resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self):
        """Requests without a valid Admin API Key are rejected by the middleware."""
        mock_conn = AsyncMock()
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.post("/admin/resolve-source-identity", json=_payload())

        assert response.status_code == 401
        # The route handler must never run without auth.
        assert mock_conn.fetchrow.await_count == 0
