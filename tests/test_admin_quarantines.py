"""Tests for the admin quarantined-identities endpoint — ``GET /admin/quarantined-identities``.

Mirrors the admin-endpoint coverage style of ``tests/test_identity.py``:
mock database connection + httpx ASGI client through the real app
(middleware, response envelope, factory).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import create_client, mock_row

_CLIENT_ID = uuid.uuid4()
_QUARANTINE_ID = uuid.uuid4()
_SOURCE_IDENTITY_ID = uuid.uuid4()
_OVERLAPPING_IDENTITY_ID = uuid.uuid4()
_TS = datetime.fromisoformat("2026-07-15T10:30:00+00:00")


def _quarantine_row(**overrides: object) -> MagicMock:
    """Return a mock row resembling a joined quarantine + identities row."""
    data: dict[str, object] = {
        "quarantine_id": _QUARANTINE_ID,
        "source_identity_id": _SOURCE_IDENTITY_ID,
        "collector_source_id": "src-db-abc123",
        "overlapping_identity_id": _OVERLAPPING_IDENTITY_ID,
        "overlapping_collector_source_id": "src-db-def456",
        "overlap_count": 42,
        "quarantined_at": _TS,
    }
    data.update(overrides)
    return mock_row(data)


class TestListQuarantinedIdentities:
    """GET /admin/quarantined-identities"""

    async def test_lists_active_quarantines_with_all_fields(self, mock_conn: AsyncMock):
        """Authenticated listing returns paginated items with the full field set."""
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_quarantine_row()])
        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/admin/quarantined-identities")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        page = data["data"]
        assert page["total"] == 1
        assert page["limit"] == 50
        assert page["offset"] == 0
        assert len(page["items"]) == 1
        item = page["items"][0]
        assert item["quarantine_id"] == str(_QUARANTINE_ID)
        assert item["source_identity_id"] == str(_SOURCE_IDENTITY_ID)
        assert item["collector_source_id"] == "src-db-abc123"
        assert item["overlapping_identity_id"] == str(_OVERLAPPING_IDENTITY_ID)
        assert item["overlapping_collector_source_id"] == "src-db-def456"
        assert item["overlap_count"] == 42
        assert item["quarantined_at"] == "2026-07-15T10:30:00Z"
        # Only active (uncleared) quarantines are listed
        sql = mock_conn.fetch.call_args.args[0]
        assert "cleared_at IS NULL" in sql
        # Both identity joins are present to resolve collector source IDs
        assert sql.count("JOIN source_identities") == 2

    async def test_requires_api_key(self, mock_conn: AsyncMock):
        """A request without a valid Admin API Key is rejected with 401."""
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.get("/admin/quarantined-identities")

        assert response.status_code == 401
        assert response.json()["status"] == "error"
        # No queries should have run against the database
        mock_conn.fetch.assert_not_awaited()
        mock_conn.fetchval.assert_not_awaited()

    async def test_empty_result(self, mock_conn: AsyncMock):
        """No active quarantines yields a valid response with total=0."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])
        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/admin/quarantined-identities")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        page = data["data"]
        assert page["items"] == []
        assert page["total"] == 0
        assert page["limit"] == 50
        assert page["offset"] == 0

    async def test_filters_by_client_id(self, mock_conn: AsyncMock):
        """The client_id query parameter narrows the result to that client."""
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_quarantine_row()])
        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(
                "/admin/quarantined-identities", params={"client_id": str(_CLIENT_ID)}
            )

        assert response.status_code == 200
        assert response.json()["data"]["total"] == 1
        # Both the COUNT and the page query filter by the client
        count_sql, count_params = (
            mock_conn.fetchval.call_args.args[0],
            mock_conn.fetchval.call_args.args[1:],
        )
        assert "si.client_id = $1" in count_sql
        assert count_params == (_CLIENT_ID,)
        page_sql, page_params = (
            mock_conn.fetch.call_args.args[0],
            mock_conn.fetch.call_args.args[1:],
        )
        assert "si.client_id = $1" in page_sql
        assert page_params == (_CLIENT_ID, 50, 0)

    async def test_pagination_params(self, mock_conn: AsyncMock):
        """limit/offset query parameters drive the SQL and the envelope."""
        mock_conn.fetchval = AsyncMock(return_value=3)
        mock_conn.fetch = AsyncMock(return_value=[_quarantine_row()])
        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(
                "/admin/quarantined-identities", params={"limit": "10", "offset": "20"}
            )

        assert response.status_code == 200
        page = response.json()["data"]
        assert page["limit"] == 10
        assert page["offset"] == 20
        sql, params = (
            mock_conn.fetch.call_args.args[0],
            mock_conn.fetch.call_args.args[1:],
        )
        assert "LIMIT $1 OFFSET $2" in sql
        assert params == (10, 20)
