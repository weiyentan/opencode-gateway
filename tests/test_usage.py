"""Tests for the usage reporting API — aggregates, records, sessions.

Covers:
- Aggregates with filters and grouping
- Records pagination, sorting, filtering
- Session summaries
- Loki URL generation
- Empty results
- 401 for unauthenticated requests
- 400 for invalid parameters
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.core.loki import build_loki_search_url

# ── Shared test data ──────────────────────────────────────────────────────

_CLIENT_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()

_A_TS = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
_B_TS = datetime(2025, 7, 16, 14, 0, 0, tzinfo=timezone.utc)
_C_TS = datetime(2025, 7, 17, 8, 0, 0, tzinfo=timezone.utc)


def _mk_record_row(
    *,
    record_id: uuid.UUID | None = None,
    client_id: uuid.UUID = _CLIENT_ID,
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    session_id: uuid.UUID = _SESSION_ID,
    model_name: str = "gpt-4",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_tokens: int = 0,
    cost: Decimal | None = Decimal("0.0035"),
    reported_at: datetime = _A_TS,
    ingested_at: datetime = _A_TS,
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for opencode_usage_records."""
    row = MagicMock()
    data = {
        "id": record_id or uuid.uuid4(),
        "client_id": client_id,
        "source_database_id": source_database_id,
        "session_id": session_id,
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "estimated_cost_usd": cost,
        "reported_at": reported_at,
        "ingested_at": ingested_at,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _mk_session_row(
    *,
    session_id: uuid.UUID | None = None,
    client_id: uuid.UUID = _CLIENT_ID,
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    first_message_at: datetime = _A_TS,
    last_message_at: datetime = _B_TS,
    message_count: int = 5,
    total_input_tokens: int = 500,
    total_output_tokens: int = 250,
    total_cached_tokens: int = 0,
    cost: Decimal | None = Decimal("0.0175"),
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for sessions."""
    row = MagicMock()
    data = {
        "id": session_id or uuid.uuid4(),
        "client_id": client_id,
        "source_database_id": source_database_id,
        "first_message_at": first_message_at,
        "last_message_at": last_message_at,
        "message_count": message_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_estimated_cost_usd": cost,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _mk_aggregate_row(
    *,
    group_value: str = "total",
    total_input_tokens: int = 300,
    total_output_tokens: int = 150,
    total_cached_tokens: int = 10,
    cost: Decimal | None = Decimal("0.0105"),
    record_count: int = 3,
) -> MagicMock:
    """Return a MagicMock for an aggregate query result row."""
    row = MagicMock()
    data = {
        "group_value": group_value,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_estimated_cost_usd": cost,
        "record_count": record_count,
    }
    row.__getitem__.side_effect = data.__getitem__
    return row


# ══════════════════════════════════════════════════════════════════════════
#  Loki URL tests
# ══════════════════════════════════════════════════════════════════════════


class TestLokiSearchUrl:
    """Unit tests for the Loki/Grafana URL generator."""

    def test_builds_url_with_required_fields(self):
        """The URL contains client_id and source_database_id in the stream selector."""
        url = build_loki_search_url(
            client_id=_CLIENT_ID,
            source_database_id=_SOURCE_DB_ID,
            session_id=None,
            start_time=_A_TS,
            end_time=_B_TS,
            grafana_base_url="http://grafana:3000",
        )
        assert url.startswith("http://grafana:3000/explore?orgId=1&left=")
        assert str(_CLIENT_ID) in url
        assert str(_SOURCE_DB_ID) in url

    def test_includes_session_id_when_provided(self):
        """When session_id is given, it appears in the stream selector."""
        url = build_loki_search_url(
            client_id=_CLIENT_ID,
            source_database_id=_SOURCE_DB_ID,
            session_id=_SESSION_ID,
            start_time=_A_TS,
            end_time=_B_TS,
        )
        assert str(_SESSION_ID) in url

    def test_contains_time_range(self):
        """The URL includes ISO-formatted start and end times (URL-encoded)."""
        from urllib.parse import unquote

        url = build_loki_search_url(
            client_id=_CLIENT_ID,
            source_database_id=_SOURCE_DB_ID,
            session_id=None,
            start_time=_A_TS,
            end_time=_B_TS,
        )
        decoded = unquote(url)
        assert _A_TS.isoformat() in decoded
        assert _B_TS.isoformat() in decoded

    def test_uses_default_grafana_url(self):
        """Without grafana_base_url, defaults to http://localhost:3000."""
        url = build_loki_search_url(
            client_id=_CLIENT_ID,
            source_database_id=_SOURCE_DB_ID,
            session_id=None,
            start_time=_A_TS,
            end_time=_B_TS,
        )
        assert url.startswith("http://localhost:3000/explore")


# ══════════════════════════════════════════════════════════════════════════
#  Aggregates endpoint tests
# ══════════════════════════════════════════════════════════════════════════


class TestAggregates:
    """Tests for GET /api/v1/usage/aggregates."""

    @pytest.mark.asyncio
    async def test_total_row_without_group_by(self, client: AsyncClient, mock_conn: AsyncMock):
        """Without group_by, a single total row is returned."""
        total_row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=total_row)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["group_value"] == "total"
        assert data[0]["record_count"] == 3

    @pytest.mark.asyncio
    async def test_groups_by_model(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=model returns one row per model."""
        rows = [
            _mk_aggregate_row(group_value="gpt-4", record_count=2),
            _mk_aggregate_row(group_value="claude-3", record_count=1),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "model",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["group_value"] == "gpt-4"
        assert data[1]["group_value"] == "claude-3"

    @pytest.mark.asyncio
    async def test_filters_by_client_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """client_id query param filters results."""
        total_row = _mk_aggregate_row(record_count=1)
        mock_conn.fetchrow = AsyncMock(return_value=total_row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "client_id": str(_CLIENT_ID),
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_filters_by_model(self, client: AsyncClient, mock_conn: AsyncMock):
        """model query param filters results."""
        total_row = _mk_aggregate_row(record_count=1)
        mock_conn.fetchrow = AsyncMock(return_value=total_row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "model": "gpt-4",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_groups_by_day(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=day returns rows truncated to day boundaries."""
        rows = [
            _mk_aggregate_row(group_value="2025-07-16 00:00:00", record_count=2),
            _mk_aggregate_row(group_value="2025-07-17 00:00:00", record_count=1),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "day",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_invalid_group_by_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """An unrecognised group_by value yields a 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "invalid_dim",
                },
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"

    @pytest.mark.asyncio
    async def test_start_after_end_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """When start_date > end_date, return 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-08-01T00:00:00Z",
                    "end_date": "2025-07-01T00:00:00Z",
                },
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no records match, a total row with zeros is returned."""
        total_row = _mk_aggregate_row(
            record_count=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cached_tokens=0,
            cost=Decimal("0"),
        )
        mock_conn.fetchrow = AsyncMock(return_value=total_row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "client_id": str(uuid.uuid4()),
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["record_count"] == 0
        assert data[0]["total_input_tokens"] == 0
        assert data[0]["total_output_tokens"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Records endpoint tests
# ══════════════════════════════════════════════════════════════════════════


class TestRecords:
    """Tests for GET /api/v1/usage/records."""

    @pytest.mark.asyncio
    async def test_returns_paginated_records(self, client: AsyncClient, mock_conn: AsyncMock):
        """The records endpoint returns items, total, limit, and offset."""
        rows = [_mk_record_row() for _ in range(3)]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=3)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "limit": 10,
                    "offset": 0,
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 3
        assert data["total"] == 3
        assert data["limit"] == 10
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_records_include_loki_url(self, client: AsyncClient, mock_conn: AsyncMock):
        """Each record has a loki_search_url field."""
        row = _mk_record_row()
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert "loki_search_url" in item
        assert item["loki_search_url"] is not None
        assert "explore" in item["loki_search_url"]

    @pytest.mark.asyncio
    async def test_limit_and_offset_are_respected(self, client: AsyncClient, mock_conn: AsyncMock):
        """The SQL query includes LIMIT and OFFSET placeholders."""
        rows = [_mk_record_row() for _ in range(2)]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=10)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "limit": 5,
                    "offset": 10,
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["limit"] == 5
        assert data["offset"] == 10

        # Verify the last two query params (limit, offset) are correct
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        sql = call_args[0][0]
        assert "LIMIT" in sql.upper()
        assert "OFFSET" in sql.upper()

    @pytest.mark.asyncio
    async def test_filters_by_model(self, client: AsyncClient, mock_conn: AsyncMock):
        """model query param filters records."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "model": "gpt-4",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_filters_by_session_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """session_id query param filters records."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "session_id": str(_SESSION_ID),
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_sort_by_ingested_at_desc(self, client: AsyncClient, mock_conn: AsyncMock):
        """sort_by=ingested_at&sort_dir=desc is accepted."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "sort_by": "ingested_at",
                    "sort_dir": "desc",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_sort_by_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """An invalid sort_by value returns 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "sort_by": "invalid_field",
                },
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_sort_dir_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """An invalid sort_dir value returns 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "sort_dir": "invalid",
                },
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_limit_exceeds_max_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """A limit > 1000 returns 422 from Pydantic validation."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "limit": 2000,
                },
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no records match, items is empty and total is 0."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Sessions endpoint tests
# ══════════════════════════════════════════════════════════════════════════


class TestSessions:
    """Tests for GET /api/v1/usage/sessions."""

    @pytest.mark.asyncio
    async def test_returns_paginated_sessions(self, client: AsyncClient, mock_conn: AsyncMock):
        """The sessions endpoint returns items with pagination metadata."""
        rows = [_mk_session_row() for _ in range(2)]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=2)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 2
        assert data["total"] == 2
        assert "limit" in data
        assert "offset" in data

    @pytest.mark.asyncio
    async def test_sessions_include_loki_url(self, client: AsyncClient, mock_conn: AsyncMock):
        """Each session summary has a loki_search_url."""
        row = _mk_session_row()
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert "loki_search_url" in item
        assert item["loki_search_url"] is not None
        assert "explore" in item["loki_search_url"]

    @pytest.mark.asyncio
    async def test_filters_by_client_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """client_id query param filters sessions."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "client_id": str(_CLIENT_ID),
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no sessions match, items is empty and total is 0."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_default_limit_is_50(self, client: AsyncClient, mock_conn: AsyncMock):
        """Without explicit limit, the default 50 is used."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["limit"] == 50


# ══════════════════════════════════════════════════════════════════════════
#  Authentication tests
# ══════════════════════════════════════════════════════════════════════════


class TestAuthentication:
    """Usage endpoints require API-key auth."""

    @pytest.mark.asyncio
    async def test_aggregates_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/aggregates without auth returns 401."""
        from httpx import ASGITransport, AsyncClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_records_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/records without auth returns 401."""
        from httpx import ASGITransport, AsyncClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_sessions_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/sessions without auth returns 401."""
        from httpx import ASGITransport, AsyncClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  Envelope format tests
# ══════════════════════════════════════════════════════════════════════════


class TestEnvelopeFormat:
    """All usage endpoints return envelope-formatted JSON."""

    @pytest.mark.asyncio
    async def test_aggregates_envelope(self, client: AsyncClient, mock_conn: AsyncMock):
        """Aggregates response has status: ok and data wrapper."""
        total_row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=total_row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_records_envelope(self, client: AsyncClient, mock_conn: AsyncMock):
        """Records response has status: ok and data wrapper."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_sessions_envelope(self, client: AsyncClient, mock_conn: AsyncMock):
        """Sessions response has status: ok and data wrapper."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload
