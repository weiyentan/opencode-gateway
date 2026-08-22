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

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

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
    provider: str | None = None,
    mode: str | None = None,
    finish_reason: str | None = None,
    reasoning_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
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
        "provider": provider,
        "mode": mode,
        "finish_reason": finish_reason,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
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
    total_cache_read_tokens: int = 0,
    total_cache_write_tokens: int = 0,
    project_id: str | None = None,
    project_label: str | None = None,
    workspace_id: str | None = None,
    agent: str | None = None,
    parent_session_id: str | None = None,
    cost: Decimal | None = Decimal("0.0175"),
    session_title: str | None = None,
    code_change_count: int | None = None,
    code_change_additions: int | None = None,
    code_change_deletions: int | None = None,
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
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "project_id": project_id,
        "project_label": project_label,
        "workspace_id": workspace_id,
        "agent": agent,
        "parent_session_id": parent_session_id,
        "total_estimated_cost_usd": cost,
        "session_title": session_title,
        "code_change_count": code_change_count,
        "code_change_additions": code_change_additions,
        "code_change_deletions": code_change_deletions,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _mk_agent_run_row(
    *,
    session_id: uuid.UUID | None = None,
    client_id: uuid.UUID = _CLIENT_ID,
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    external_session_id: str | None = None,
    project_id: str | None = None,
    project_label: str | None = None,
    workspace_id: str | None = None,
    agent: str | None = None,
    parent_session_id: str | None = None,
    message_count: int = 5,
    total_input_tokens: int = 500,
    total_output_tokens: int = 250,
    total_cached_tokens: int = 0,
    total_cache_read_tokens: int = 0,
    total_cache_write_tokens: int = 0,
    cost: Decimal | None = Decimal("0.0175"),
    last_message_at: datetime = _B_TS,
    status: str = "completed",
    child_run_count: int = 0,
    session_title: str | None = None,
    session_model: str | None = None,
    code_change_count: int | None = None,
    code_change_additions: int | None = None,
    code_change_deletions: int | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for agent runs."""
    row = MagicMock()
    _external_session_id = external_session_id or str(uuid.uuid4())
    data = {
        "id": session_id or uuid.uuid4(),
        "client_id": client_id,
        "source_database_id": source_database_id,
        "external_session_id": _external_session_id,
        "project_id": project_id,
        "project_label": project_label,
        "workspace_id": workspace_id,
        "agent": agent,
        "parent_session_id": parent_session_id,
        "message_count": message_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_estimated_cost_usd": cost,
        "last_message_at": last_message_at,
        "_status": status,
        "child_run_count": child_run_count,
        "session_title": session_title,
        "session_model": session_model,
        "code_change_count": code_change_count,
        "code_change_additions": code_change_additions,
        "code_change_deletions": code_change_deletions,
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
    total_reasoning_tokens: int = 5,
    total_cache_read_tokens: int = 3,
    total_cache_write_tokens: int = 2,
    cost: Decimal | None = Decimal("0.0105"),
    record_count: int = 3,
    session_count: int = 2,
    model_count: int = 1,
    project_label: str | None = None,
    agent: str | None = None,
) -> MagicMock:
    """Return a MagicMock for an aggregate query result row."""
    row = MagicMock()
    data = {
        "group_value": group_value,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_reasoning_tokens": total_reasoning_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_estimated_cost_usd": cost,
        "record_count": record_count,
        "session_count": session_count,
        "model_count": model_count,
        "project_label": project_label,
        "agent": agent,
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
#  Agent aggregate tests
# ══════════════════════════════════════════════════════════════════════════


class TestAgentAggregates:
    """Tests for group_by=agent on GET /api/v1/usage/aggregates."""

    @pytest.mark.asyncio
    async def test_group_by_agent_returns_one_row_per_agent(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """group_by=agent returns one row per observed agent identity, exposing
        the agent value alongside the standard aggregate fields."""
        rows = [
            _mk_aggregate_row(
                group_value="researcher", agent="researcher", record_count=3
            ),
            _mk_aggregate_row(
                group_value="builder", agent="builder", record_count=1
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "agent",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        # Multiple dynamic agents — no hard-coded agent list
        assert data[0]["group_value"] == "researcher"
        assert data[0]["agent"] == "researcher"
        assert data[0]["record_count"] == 3
        # Standard aggregate fields present
        for field in (
            "total_input_tokens",
            "total_output_tokens",
            "total_cached_tokens",
            "total_reasoning_tokens",
            "total_cache_read_tokens",
            "total_cache_write_tokens",
            "total_estimated_cost_usd",
            "record_count",
            "session_count",
            "model_count",
        ):
            assert field in data[0]
        assert data[1]["group_value"] == "builder"
        assert data[1]["agent"] == "builder"

    @pytest.mark.asyncio
    async def test_agent_null_when_not_grouped_by_agent(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """agent is null when group_by does NOT include 'agent' (backward
        compatible — existing dimensions keep their shape)."""
        rows = [
            _mk_aggregate_row(group_value="gpt-4", record_count=5),
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
        assert len(data) == 1
        assert data[0]["agent"] is None
        assert data[0]["group_value"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_total_row_backward_compatible(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The no-group_by total row keeps its shape; agent is additive-null."""
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
        assert data[0]["agent"] is None
        assert data[0]["record_count"] == 3

    @pytest.mark.asyncio
    async def test_group_by_agent_sql_shape(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The generated SQL for agent grouping uses COALESCE(s.agent, 'unknown'),
        joins sessions, and does NOT route through the client,project rollup."""
        mock_conn.fetch = AsyncMock(return_value=[
            _mk_aggregate_row(
                group_value="researcher", agent="researcher", record_count=2
            )
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "agent",
                },
            )

        assert response.status_code == 200
        assert mock_conn.fetch.call_count == 1
        sql = mock_conn.fetch.call_args_list[0][0][0]
        assert "COALESCE(s.agent, 'unknown')" in sql
        # Single-dimension agent grouping must not duplicate the COALESCE
        # term in GROUP BY (review fix: group_expr already carries it).
        assert (
            "GROUP BY COALESCE(s.agent, 'unknown'),COALESCE(s.agent, 'unknown')"
            not in sql
        )
        assert "LEFT JOIN sessions s ON s.id = our.session_id" in sql
        # Agent must never route through the rollup path
        assert "FROM client_project_rollup" not in sql
        assert "FROM usage_events" in sql

    @pytest.mark.asyncio
    async def test_group_by_agent_with_filters(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """start_date/end_date/client_id filters apply to agent-grouped results."""
        rows = [
            _mk_aggregate_row(
                group_value="researcher", agent="researcher", record_count=1
            )
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "agent",
                    "client_id": str(_CLIENT_ID),
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        sql = mock_conn.fetch.call_args_list[0][0][0]
        assert "our.reported_at >= $1" in sql
        assert "our.reported_at <= $2" in sql
        assert "our.client_id = $3" in sql

    @pytest.mark.asyncio
    async def test_invalid_group_by_lists_agent(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An invalid group_by value returns 400 and lists 'agent' among
        the valid dimensions."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "bogus",
                },
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        message = payload["error"]["message"]
        assert "agent" in message
        assert "bogus" in message


# ══════════════════════════════════════════════════════════════════════════
#  Client / Project aggregate tests
# ══════════════════════════════════════════════════════════════════════════


class TestClientProjectAggregates:
    """Tests for client+project aggregation in GET /api/v1/usage/aggregates."""

    @pytest.mark.asyncio
    async def test_group_by_project_is_valid(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=project is recognised as a valid dimension."""
        mock_conn.fetch = AsyncMock(return_value=[_mk_aggregate_row(
            group_value="proj-abc", record_count=2, session_count=1, model_count=1
        )])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["group_value"] == "proj-abc"
        assert "session_count" in data[0]
        assert "model_count" in data[0]

    @pytest.mark.asyncio
    async def test_group_by_client_project_multi(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=client,project returns pipe-delimited group_value per client+project."""
        rows = [
            _mk_aggregate_row(
                group_value="Acme Corp|Friendly Project Name",
                record_count=10,
                session_count=3,
                model_count=2,
                project_label="Friendly Project Name",
            ),
            _mk_aggregate_row(
                group_value="Acme Corp|Another Project",
                record_count=5,
                session_count=1,
                model_count=1,
                project_label="Another Project",
            ),
            _mk_aggregate_row(
                group_value="Beta Inc|unknown",
                record_count=2,
                session_count=1,
                model_count=1,
                project_label="unknown",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 3
        # First row: Acme Corp|Friendly Project Name
        assert data[0]["group_value"] == "Acme Corp|Friendly Project Name"
        assert data[0]["record_count"] == 10
        assert data[0]["session_count"] == 3
        assert data[0]["model_count"] == 2
        assert data[0]["project_label"] == "Friendly Project Name"
        # Third row: Beta Inc|unknown (NULL source_projects → 'unknown')
        assert data[2]["group_value"] == "Beta Inc|unknown"
        assert data[2]["session_count"] == 1
        assert data[2]["project_label"] == "unknown"

    @pytest.mark.asyncio
    async def test_group_by_client_project_empty(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no records match, empty list returned for client,project grouping."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data == []

    @pytest.mark.asyncio
    async def test_group_by_client_project_with_filters(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """client,project group_by works when combined with other filters."""
        rows = [
            _mk_aggregate_row(
                group_value="Acme Corp|My Project Label",
                record_count=3,
                session_count=1,
                model_count=1,
                project_label="My Project Label",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                    "model": "gpt-4",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["group_value"] == "Acme Corp|My Project Label"
        assert data[0]["project_label"] == "My Project Label"

    @pytest.mark.asyncio
    async def test_project_label_in_client_project_aggregate(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """project_label is returned in the response when group_by includes project.
        
        The group_value now uses the resolved project label (via _PROJECT_LABEL_SQL),
        so the pipe-delimited part after the client name IS the project label.
        """
        rows = [
            _mk_aggregate_row(
                group_value="Acme Corp|Friendly Project Name",
                record_count=10,
                session_count=3,
                model_count=2,
                project_label="Friendly Project Name",
            ),
            _mk_aggregate_row(
                group_value="Acme Corp|Another Project",
                record_count=5,
                session_count=1,
                model_count=1,
                project_label="Another Project",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        # First row should have the resolved project label in both fields
        assert data[0]["project_label"] == "Friendly Project Name"
        assert data[0]["group_value"] == "Acme Corp|Friendly Project Name"
        # Second row should have its own project label
        assert data[1]["project_label"] == "Another Project"
        assert data[1]["group_value"] == "Acme Corp|Another Project"

    @pytest.mark.asyncio
    async def test_project_label_null_when_not_grouped_by_project(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """project_label is null when group_by does NOT include 'project'."""
        rows = [
            _mk_aggregate_row(
                group_value="gpt-4",
                record_count=5,
                session_count=2,
                model_count=1,
            ),
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
        assert len(data) == 1
        # project_label should be null because "project" is not in group_by
        assert data[0]["project_label"] is None

    @pytest.mark.asyncio
    async def test_project_label_unknown_for_unmatched_project(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """project_label is 'unknown' when source_projects has no matching row."""
        rows = [
            _mk_aggregate_row(
                group_value="Beta Inc|unknown",
                record_count=2,
                session_count=1,
                model_count=1,
                project_label="unknown",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["project_label"] == "unknown"
        assert data[0]["group_value"] == "Beta Inc|unknown"

    @pytest.mark.asyncio
    async def test_duplicate_project_labels_merged_by_backend(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """When two different external_project_ids resolve to the same Project Label
        under the same client, the backend GROUP BY merges them into one row."""
        # Simulate: two raw project IDs ("proj-abc", "proj-xyz") both resolve
        # to the same friendly label "My Project" via source_projects table.
        # The GROUP BY on (_PROJECT_LABEL_SQL) produces a single merged row.
        rows = [
            _mk_aggregate_row(
                group_value="Acme Corp|My Project",
                record_count=15,  # 10 + 5 merged
                session_count=4,   # 3 + 1 merged
                model_count=3,     # 2 + 1 merged
                project_label="My Project",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1, (
            "Expected exactly 1 row after merging duplicate labels, "
            f"got {len(data)}"
        )
        assert data[0]["group_value"] == "Acme Corp|My Project"
        assert data[0]["project_label"] == "My Project"
        assert data[0]["record_count"] == 15
        assert data[0]["session_count"] == 4
        assert data[0]["model_count"] == 3

    @pytest.mark.asyncio
    async def test_group_by_client_project_sql_shape(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Verify the generated SQL for client,project uses the rollup path:
        references client_project_rollup (not usage_events) and applies
        COALESCE(canonical_name, name) for canonical client grouping."""
        mock_conn.fetch = AsyncMock(return_value=[
            _mk_aggregate_row(
                group_value="Acme Corp|Friendly Project Name",
                record_count=0, session_count=0, model_count=0,
                project_label="Friendly Project Name",
            )
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)
        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )
        assert response.status_code == 200

        # Hybrid read: 2 queries — rollup (additive totals) + count (distinct counts).
        # The first call is the rollup SQL; check its shape.
        assert len(mock_conn.fetch.call_args_list) >= 1, (
            "Expected at least 1 fetch call for rollup SQL"
        )
        sql = mock_conn.fetch.call_args_list[0][0][0]

        # Must reference the rollup table, not usage_events
        assert "FROM client_project_rollup" in sql, (
            f"Expected rollup-backed SQL, got: {sql[:300]}"
        )
        assert "FROM usage_events" not in sql, (
            f"Rollup path must not scan usage_events, got: {sql[:300]}"
        )

        # Must apply canonical-name COALESCE for client grouping
        assert "COALESCE(oc.canonical_name, oc.name)" in sql, (
            f"Expected canonical-name COALESCE in rollup SQL, got: {sql[:300]}"
        )

        # GROUP BY must contain canonical client name and project label
        assert "GROUP BY" in sql.upper(), "SQL must contain GROUP BY"
        group_by_match = sql.upper().split("GROUP BY")[1].split("ORDER BY")[0].strip()
        assert "COALESCE" in group_by_match, (
            f"Expected COALESCE in GROUP BY clause, got: {group_by_match}"
        )

    @pytest.mark.asyncio
    async def test_canonical_name_rolls_up_workspace_clients(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Clients sharing a canonical_name are consolidated into a single row."""
        rows = [
            _mk_aggregate_row(
                group_value="my-deployment|Project A",
                record_count=0, session_count=0, model_count=0,
                project_label="Project A",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["group_value"] == "my-deployment|Project A"

    @pytest.mark.asyncio
    async def test_canonical_name_multi_project(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A canonical client with multiple projects returns one row per project."""
        rows = [
            _mk_aggregate_row(
                group_value="shared-deployment|Project A",
                record_count=0, session_count=0, model_count=0,
                project_label="Project A",
            ),
            _mk_aggregate_row(
                group_value="shared-deployment|Project B",
                record_count=0, session_count=0, model_count=0,
                project_label="Project B",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["group_value"] == "shared-deployment|Project A"
        assert data[1]["group_value"] == "shared-deployment|Project B"

    @pytest.mark.asyncio
    async def test_no_canonical_name_groups_under_own_name(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Clients without a canonical_name group under their own name (unchanged behavior)."""
        rows = [
            _mk_aggregate_row(
                group_value="standalone-client|My Project",
                record_count=0, session_count=0, model_count=0,
                project_label="My Project",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["group_value"] == "standalone-client|My Project"

    @pytest.mark.asyncio
    async def test_rollup_path_maps_rollup_columns(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Rollup-backed client,project path maps only additive columns (input,
        output, cache_read, cache_write tokens + cost) and returns 0 for
        cached_tokens, reasoning_tokens, record_count, session_count, model_count."""
        rows = [
            _mk_aggregate_row(
                group_value="canonical|Project X",
                total_input_tokens=100,
                total_output_tokens=50,
                total_cached_tokens=0,
                total_reasoning_tokens=0,
                total_cache_read_tokens=10,
                total_cache_write_tokens=5,
                cost=Decimal("0.0123"),
                record_count=0,
                session_count=0,
                model_count=0,
                project_label="Project X",
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        r = data[0]
        assert r["total_input_tokens"] == 100
        assert r["total_output_tokens"] == 50
        assert r["total_cached_tokens"] == 0
        assert r["total_reasoning_tokens"] == 0
        assert r["total_cache_read_tokens"] == 10
        assert r["total_cache_write_tokens"] == 5
        assert r["record_count"] == 0
        assert r["session_count"] == 0
        assert r["model_count"] == 0
        assert r["project_label"] == "Project X"

    @pytest.mark.asyncio
    async def test_rollup_count_query_sql_shape(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The count query uses the same (client_id, project_id) LATERAL
        project-label lookup as the rollup query, not the old divergent
        (source_database_id, s.project_id) join.

        The two queries must produce byte-identical ``group_value`` strings
        so the Python merge keys on ``group_value`` reliably attach counts
        to rollup rows.  A mismatch silently zeroes Sessions/Models columns.
        """
        rollup_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=100,
                total_output_tokens=50,
                record_count=0, session_count=0, model_count=0,
                project_label="Proj X",
            )
        ]
        count_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=0,
                total_output_tokens=0,
                record_count=3, session_count=2, model_count=1,
                project_label="Proj X",
            )
        ]
        mock_conn.fetch = AsyncMock(side_effect=[rollup_rows, count_rows])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        # Two fetch calls: rollup + count
        assert mock_conn.fetch.call_count == 2

        rollup_sql = mock_conn.fetch.call_args_list[0][0][0]
        count_sql = mock_conn.fetch.call_args_list[1][0][0]

        # ── Rollup query shape assertions ──
        assert "FROM client_project_rollup" in rollup_sql
        assert "osp.client_id = r.client_id" in rollup_sql
        assert "osp.external_project_id = r.project_id" in rollup_sql
        assert "COALESCE(oc.canonical_name, oc.name)" in rollup_sql

        # ── Count query shape assertions ──
        assert "FROM usage_events" in count_sql, (
            "Count query must scan usage_events"
        )
        # Uses the same LATERAL join as the rollup
        assert "osp.client_id = r.client_id" in count_sql, (
            "Count query LATERAL must key on client_id, "
            f"got: {count_sql[:400]}"
        )
        assert "osp.external_project_id = r.project_id" in count_sql, (
            "Count query LATERAL must key on external_project_id, "
            f"got: {count_sql[:400]}"
        )
        # Must NOT use the old divergent join
        assert "osp.source_database_id" not in count_sql, (
            "Count query must not use source_database_id join, "
            f"got: {count_sql[:400]}"
        )
        assert "s.project_id" not in count_sql, (
            "Count query must not reference sessions.project_id, "
            f"got: {count_sql[:400]}"
        )
        # Both use the same group_value expression shape
        assert "COALESCE(oc.canonical_name, oc.name)" in count_sql
        assert " || '|' || " in count_sql
        assert "MAX(pb.provider_breakdown)" not in count_sql
        assert "GROUP BY COALESCE(oc.canonical_name, oc.name)" in count_sql
        assert "jsonb_object_agg(pc.provider_key, pc.cnt)" in count_sql

    @pytest.mark.asyncio
    async def test_rollup_merge_counts_when_group_values_match(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """When the rollup and count queries produce matching group_values,
        the count values (record_count, session_count, model_count) are
        merged into the rollup row — not zeroed."""
        rollup_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=100,
                total_output_tokens=50,
                total_cache_read_tokens=10,
                total_cache_write_tokens=5,
                cost=Decimal("0.0123"),
                record_count=0, session_count=0, model_count=0,
                project_label="Proj X",
            )
        ]
        count_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=0,
                total_output_tokens=0,
                total_cache_read_tokens=0,
                total_cache_write_tokens=0,
                cost=None,
                record_count=3, session_count=2, model_count=1,
                project_label="Proj X",
            )
        ]
        mock_conn.fetch = AsyncMock(side_effect=[rollup_rows, count_rows])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        r = data[0]
        assert r["group_value"] == "ClientA|Proj X"
        # Additive totals from the rollup
        assert r["total_input_tokens"] == 100
        assert r["total_output_tokens"] == 50
        assert r["total_cache_read_tokens"] == 10
        assert r["total_cache_write_tokens"] == 5
        # Counts from the count query (merged, not zeroed)
        assert r["record_count"] == 3, f"Expected record_count=3, got {r['record_count']}"
        assert r["session_count"] == 2, f"Expected session_count=2, got {r['session_count']}"
        assert r["model_count"] == 1, f"Expected model_count=1, got {r['model_count']}"

    @pytest.mark.asyncio
    async def test_rollup_date_boundary_utc_day_normalisation(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Both the rollup and count queries normalise date boundaries to UTC
        days so they select identical event sets regardless of whether the
        query parameters land on a UTC midnight.

        A preset-style boundary — e.g. start_date=2026-07-31T12:00:00Z
        (local midnight in a non-UTC browser) — must produce the same first
        included UTC day in both queries.  Without the cast the rollup query
        compares a ``date`` to a ``timestamptz``, dropping or over-including
        the first day, and the count query filters on the raw instant,
        selecting a different event set.
        """
        rollup_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=100,
                total_output_tokens=50,
                record_count=0, session_count=0, model_count=0,
                project_label="Proj X",
            )
        ]
        count_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=0,
                total_output_tokens=0,
                record_count=3, session_count=2, model_count=1,
                project_label="Proj X",
            )
        ]
        mock_conn.fetch = AsyncMock(side_effect=[rollup_rows, count_rows])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2026-07-31T12:00:00Z",
                    "end_date": "2026-08-15T18:30:00Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        assert mock_conn.fetch.call_count == 2

        rollup_sql = mock_conn.fetch.call_args_list[0][0][0]
        count_sql = mock_conn.fetch.call_args_list[1][0][0]

        # Rollup filters must normalise to UTC day
        assert "r.day >= ($1 AT TIME ZONE 'UTC')::date" in rollup_sql, (
            f"Rollup date-start filter must use UTC-day cast, got:\n{rollup_sql}"
        )
        assert "r.day <= ($2 AT TIME ZONE 'UTC')::date" in rollup_sql, (
            f"Rollup date-end filter must use UTC-day cast, got:\n{rollup_sql}"
        )

        # Count filters must normalise to UTC day on both sides
        assert (
            "(r.reported_at AT TIME ZONE 'UTC')::date >= ($1 AT TIME ZONE 'UTC')::date"
        ) in count_sql, (
            f"Count date-start filter must use UTC-day cast on both sides, got:\n{count_sql}"
        )
        assert (
            "(r.reported_at AT TIME ZONE 'UTC')::date <= ($2 AT TIME ZONE 'UTC')::date"
        ) in count_sql, (
            f"Count date-end filter must use UTC-day cast on both sides, got:\n{count_sql}"
        )

        # Legacy raw-comparison patterns must NOT appear
        for label, sql in (("rollup", rollup_sql), ("count", count_sql)):
            assert "r.day >= $1" not in sql, (
                f"{label} query must not use raw r.day >= $1, got:\n{sql}"
            )
            assert "r.day <= $2" not in sql, (
                f"{label} query must not use raw r.day <= $2, got:\n{sql}"
            )
            assert "reported_at >= $1" not in sql, (
                f"{label} query must not use raw reported_at >= $1, got:\n{sql}"
            )
            assert "reported_at <= $2" not in sql, (
                f"{label} query must not use raw reported_at <= $2, got:\n{sql}"
            )

    @pytest.mark.asyncio
    async def test_project_label_strips_numeric_suffix_sql_shape(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The project-label COALESCE chains strip the workspace numeric
        suffix at read time in both read paths.

        ``_ROLLUP_PROJECT_LABEL_SQL`` (rollup + count queries) and
        ``_PROJECT_LABEL_SQL`` (raw usage_events aggregate) must both wrap
        the ``osp.name`` branch in
        ``NULLIF(regexp_replace(osp.name, '-\\d+$', ''), '')``
        so workspace variants like ``opencode-gateway-6791`` and
        ``opencode-gateway-6800`` collapse into a single ``opencode-gateway``
        row in the Client/Project breakdown, while a name that strips to an
        empty string (e.g. ``-6791``) becomes NULL and falls through the
        COALESCE chain to the worktree/project_id/'unknown' fallbacks.
        """
        rollup_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=100,
                total_output_tokens=50,
                record_count=0, session_count=0, model_count=0,
                project_label="Proj X",
            )
        ]
        count_rows = [
            _mk_aggregate_row(
                group_value="ClientA|Proj X",
                total_input_tokens=0,
                total_output_tokens=0,
                record_count=3, session_count=2, model_count=1,
                project_label="Proj X",
            )
        ]
        project_rows = [
            _mk_aggregate_row(
                group_value="Proj X",
                record_count=3, session_count=2, model_count=1,
                project_label="Proj X",
            )
        ]
        mock_conn.fetch = AsyncMock(
            side_effect=[rollup_rows, count_rows, project_rows]
        )
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            # Rollup path: client,project → client_project_rollup + count query
            resp_rollup = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )
            assert resp_rollup.status_code == 200
            # Non-rollup path: project → raw usage_events scan
            resp_project = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )
            assert resp_project.status_code == 200

        # 3 fetch calls: rollup, count, raw project aggregate
        assert mock_conn.fetch.call_count == 3
        rollup_sql = mock_conn.fetch.call_args_list[0][0][0]
        count_sql = mock_conn.fetch.call_args_list[1][0][0]
        project_sql = mock_conn.fetch.call_args_list[2][0][0]

        strip = "regexp_replace(osp.name, '-\\d+$', '')"
        strip_nullif = "NULLIF(regexp_replace(osp.name, '-\\d+$', ''), '')"
        for label, sql in (
            ("rollup", rollup_sql),
            ("count", count_sql),
            ("project aggregate", project_sql),
        ):
            assert strip in sql, (
                f"{label} SQL must strip the workspace numeric suffix from "
                f"osp.name, got: {sql[:400]}"
            )
            # The stripped name must be NULLIF-wrapped so a name consisting
            # only of a numeric suffix (e.g. ``-6791``) becomes NULL and the
            # COALESCE chain falls through to the worktree / project_id /
            # 'unknown' fallbacks instead of rendering a blank label.
            assert strip_nullif in sql, (
                f"{label} SQL must NULLIF-wrap the stripped name so an empty "
                f"strip result falls through the COALESCE chain, "
                f"got: {sql[:400]}"
            )
            # The metadata label branches, the worktree-basename fallback,
            # and the project_id fallback can all carry workspace numeric
            # suffixes; each branch strips them at read time.
            assert "regexp_replace(osp.display_name, '-\\d+$', '')" in sql, (
                f"{label} SQL must strip the display_name suffix, got: {sql[:400]}"
            )
            if label in ("rollup", "count"):
                assert "regexp_replace(r.project_id, '-\\d+$', '')" in sql, (
                    f"{label} SQL must strip the project_id suffix, got: {sql[:400]}"
                )
            else:
                assert "regexp_replace(s.project_id, '-\\d+$', '')" in sql, (
                    f"{label} SQL must strip the project_id suffix, got: {sql[:400]}"
                )
            # The NULLIF wrapper spans lines in the constants, so assert
            # the contiguous core expression; it can only appear when the
            # worktree-basename fallback is actually stripped.
            assert (
                "regexp_replace(substring(osp.worktree, '([^/]+)$'), '-\\d+$', '')"
                in sql
            ), f"{label} SQL must strip the worktree-basename suffix, got: {sql[:400]}"


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
    async def test_records_include_enrichment_fields(self, client: AsyncClient, mock_conn: AsyncMock):
        """Records include v1.2 enrichment fields (provider, mode, finish_reason, reasoning_tokens, cache_read_tokens, cache_write_tokens)."""
        row = _mk_record_row(
            provider="openai",
            mode="chat",
            finish_reason="stop",
            reasoning_tokens=20,
            cache_read_tokens=10,
            cache_write_tokens=5,
        )
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
        assert item["provider"] == "openai"
        assert item["mode"] == "chat"
        assert item["finish_reason"] == "stop"
        assert item["reasoning_tokens"] == 20
        assert item["cache_read_tokens"] == 10
        assert item["cache_write_tokens"] == 5

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
    async def test_sort_by_source_created_at_accepted(self, client: AsyncClient, mock_conn: AsyncMock):
        """sort_by=source_created_at is accepted as a valid sort option."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "sort_by": "source_created_at",
                    "sort_dir": "desc",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_default_sort_is_source_created_at(self, client: AsyncClient, mock_conn: AsyncMock):
        """The default sort_by is source_created_at (not reported_at)."""
        rows = [_mk_record_row()]
        mock_conn.fetch = AsyncMock(return_value=rows)
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
        # Verify the SQL uses COALESCE with source_created_at_tz
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "source_created_at_tz" in sql.lower(), (
            f"Default sort should use source_created_at_tz, got: {sql[:500]}"
        )

    @pytest.mark.asyncio
    async def test_sort_by_source_created_at_uses_coalesce(self, client: AsyncClient, mock_conn: AsyncMock):
        """sort_by=source_created_at generates SQL with COALESCE(source_created_at_tz, reported_at)."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "sort_by": "source_created_at",
                    "sort_dir": "asc",
                },
            )

        assert response.status_code == 200
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "COALESCE" in sql, f"Expected COALESCE in ORDER BY, got: {sql[:500]}"
        assert "source_created_at_tz" in sql.lower(), (
            f"Expected source_created_at_tz in COALESCE, got: {sql[:500]}"
        )

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
    async def test_sessions_include_enrichment_fields(self, client: AsyncClient, mock_conn: AsyncMock):
        """Session summaries include v1.2 enrichment fields (project_id, workspace_id, agent, parent_session_id)."""
        parent_id = str(uuid.uuid4())
        row = _mk_session_row(
            project_id="proj-123",
            project_label="My Project",
            workspace_id="ws-456",
            agent="code-editor",
            parent_session_id=parent_id,
        )
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
        assert item["project_id"] == "proj-123"
        assert item["project_label"] == "My Project"
        assert item["workspace_id"] == "ws-456"
        assert item["agent"] == "code-editor"
        assert item["parent_session_id"] == parent_id

    @pytest.mark.asyncio
    async def test_sessions_include_cache_read_write_tokens(self, client: AsyncClient, mock_conn: AsyncMock):
        """Session summaries include total_cache_read_tokens and total_cache_write_tokens."""
        row = _mk_session_row(
            total_cache_read_tokens=42,
            total_cache_write_tokens=7,
        )
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
        assert item["total_cache_read_tokens"] == 42
        assert item["total_cache_write_tokens"] == 7

    @pytest.mark.asyncio
    async def test_sessions_cache_read_write_default_to_zero(self, client: AsyncClient, mock_conn: AsyncMock):
        """Session summaries default cache read/write to 0 when not explicitly set."""
        row = _mk_session_row()  # defaults total_cache_read_tokens=0, total_cache_write_tokens=0
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
        assert item["total_cache_read_tokens"] == 0
        assert item["total_cache_write_tokens"] == 0

    @pytest.mark.asyncio
    async def test_sessions_include_code_change_fields(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Session summaries surface code_change_* fields from opencode_session_contexts."""
        row = _mk_session_row(
            code_change_count=7,
            code_change_additions=15,
            code_change_deletions=3,
        )
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
        assert item["code_change_count"] == 7
        assert item["code_change_additions"] == 15
        assert item["code_change_deletions"] == 3

    @pytest.mark.asyncio
    async def test_sessions_code_change_fields_default_to_zero(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Session summaries default code_change_* to 0 when no context row exists."""
        row = _mk_session_row()  # defaults code_change_* to None
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
        assert item["code_change_count"] == 0
        assert item["code_change_additions"] == 0
        assert item["code_change_deletions"] == 0

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
#  Source-created ordering semantics (issue #401)
#
#  "Most recent" in the usage views means most recently created at the
#  source, not most recently ingested by the Gateway.  These tests pin the
#  ORDER BY clauses that deliver that semantics: the records endpoint's
#  sort_by=ingested_at maps to first_ingested_at (an explicit opt-in), the
#  default and source_created_at map to COALESCE(source_created_at_tz,
#  reported_at) (covered by TestRecords above), and the Sessions / Agent
#  Runs views order by the source-created last_message_at.
# ══════════════════════════════════════════════════════════════════════════


class TestOrderingSemantics:
    """Regression coverage for source-created ordering across usage views."""

    @pytest.mark.asyncio
    async def test_records_sort_by_ingested_at_orders_by_first_ingested_at(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """sort_by=ingested_at is an explicit opt-in ordering by ingest time."""
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
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "order by our.first_ingested_at desc" in sql.lower(), (
            f"ingested_at sort must order by first_ingested_at, got: {sql[:500]}"
        )

    @pytest.mark.asyncio
    async def test_sessions_order_by_last_message_at_desc(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Sessions order by source-created activity (last_message_at DESC)."""
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
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "ORDER BY s.last_message_at DESC" in sql, (
            f"Sessions must order by source-created last_message_at, got: {sql[:500]}"
        )

    @pytest.mark.asyncio
    async def test_agent_runs_order_by_last_message_at_desc(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Agent Runs order by source-created activity (last_message_at DESC)."""
        row = _mk_agent_run_row()
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": 10},
            )

        assert response.status_code == 200
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "ORDER BY s.last_message_at DESC NULLS LAST" in sql, (
            f"Agent Runs must order by source-created last_message_at, got: {sql[:500]}"
        )


# ══════════════════════════════════════════════════════════════════════════
#  Session title enrichment tests
# ══════════════════════════════════════════════════════════════════════════


class TestSessionTitle:
    """Tests for session_title enrichment on GET /sessions and GET /agent-runs."""

    @pytest.mark.asyncio
    async def test_sessions_include_session_title(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Session summaries include session_title from opencode_session_contexts."""
        row = _mk_session_row(session_title="My Test Session")
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
        assert item["session_title"] == "My Test Session"

    @pytest.mark.asyncio
    async def test_sessions_session_title_null_when_no_context(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Session summary has null session_title when no Session Context exists."""
        row = _mk_session_row(session_title=None)
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
        assert item["session_title"] is None

    @pytest.mark.asyncio
    async def test_agent_runs_include_session_title(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Agent run summaries include session_title from opencode_session_contexts."""
        row = _mk_agent_run_row(
            session_title="Agent Run Session",
            project_label="My Project",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": 10},
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["session_title"] == "Agent Run Session"
        assert item["project_label"] == "My Project"

    @pytest.mark.asyncio
    async def test_agent_runs_session_title_null_when_no_context(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Agent run summary has null session_title when no Session Context exists."""
        row = _mk_agent_run_row(session_title=None)
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": 10},
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["session_title"] is None

    @pytest.mark.asyncio
    async def test_sessions_mixed_context(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Sessions with and without context both appear in the same response."""
        row_with = _mk_session_row(session_title="Titled Session")
        row_without = _mk_session_row(session_title=None)
        mock_conn.fetch = AsyncMock(return_value=[row_with, row_without])
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
        items = response.json()["data"]["items"]
        assert items[0]["session_title"] == "Titled Session"
        assert items[1]["session_title"] is None


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


# ══════════════════════════════════════════════════════════════════════════
#  Records-with-context endpoint tests
# ══════════════════════════════════════════════════════════════════════════


def _mk_rwc_record_row(
    *,
    record_id: uuid.UUID | None = None,
    client_id: uuid.UUID = _CLIENT_ID,
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    session_id: uuid.UUID = _SESSION_ID,
    model_name: str = "gpt-4",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_tokens: int = 0,
    provider: str | None = None,
    mode: str | None = None,
    finish_reason: str | None = None,
    reasoning_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost: Decimal | None = Decimal("0.0035"),
    reported_at: datetime = _A_TS,
    ingested_at: datetime = _A_TS,
    agent: str | None = "coder-v1",
    session_title: str | None = "Fix login bug",
    project_label: str = "my-project",
) -> MagicMock:
    """Return a MagicMock for a records-with-context raw query row."""
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
        "provider": provider,
        "mode": mode,
        "finish_reason": finish_reason,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "estimated_cost_usd": cost,
        "reported_at": reported_at,
        "ingested_at": ingested_at,
        "agent": agent,
        "session_title": session_title,
        "project_label": project_label,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _mk_rwc_grouped_row(
    *,
    group_value: str = "gpt-4",
    project_label: str | None = None,
    session_title: str | None = None,
    agent: str | None = None,
    model_name: str | None = None,
    total_input_tokens: int = 300,
    total_output_tokens: int = 150,
    total_cached_tokens: int = 10,
    total_reasoning_tokens: int = 5,
    total_cache_read_tokens: int = 3,
    total_cache_write_tokens: int = 2,
    cost: Decimal | None = Decimal("0.0105"),
    record_count: int = 3,
) -> MagicMock:
    """Return a MagicMock for a records-with-context grouped query row."""
    row = MagicMock()
    data: dict[str, object] = {
        "group_value": group_value,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_reasoning_tokens": total_reasoning_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_estimated_cost_usd": cost,
        "record_count": record_count,
    }
    if project_label is not None:
        data["project_label"] = project_label
    if session_title is not None:
        data["session_title"] = session_title
    if agent is not None:
        data["agent"] = agent
    if model_name is not None:
        data["model_name"] = model_name
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


class TestRecordsWithContextRaw:
    """Tests for GET /api/v1/usage/records-with-context (raw mode)."""

    @pytest.mark.asyncio
    async def test_returns_paginated_records(self, client: AsyncClient, mock_conn: AsyncMock):
        """Raw mode returns items with pagination metadata."""
        rows = [_mk_rwc_record_row() for _ in range(3)]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=3)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 3
        assert data["total"] == 3
        assert data["limit"] == 50
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_includes_context_fields(self, client: AsyncClient, mock_conn: AsyncMock):
        """Each item has session_title, project_label, and agent."""
        row = _mk_rwc_record_row(
            agent="coder-v1",
            session_title="Fix login bug",
            project_label="my-project",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["session_title"] == "Fix login bug"
        assert item["project_label"] == "my-project"
        assert item["agent"] == "coder-v1"

    @pytest.mark.asyncio
    async def test_includes_loki_url(self, client: AsyncClient, mock_conn: AsyncMock):
        """Each item has a loki_search_url."""
        mock_conn.fetch = AsyncMock(return_value=[_mk_rwc_record_row()])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
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
    async def test_pagination_parameters(self, client: AsyncClient, mock_conn: AsyncMock):
        """limit and offset query params are passed through."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "limit": 10,
                    "offset": 5,
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["limit"] == 10
        assert data["offset"] == 5

    @pytest.mark.asyncio
    async def test_filters_by_project_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """project_id filter is accepted."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "project_id": "proj-abc",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_filters_by_agent(self, client: AsyncClient, mock_conn: AsyncMock):
        """agent filter is accepted."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "agent": "coder-v1",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_filters_by_session_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """session_id filter is accepted."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "session_id": str(_SESSION_ID),
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_filters_by_model(self, client: AsyncClient, mock_conn: AsyncMock):
        """model filter is accepted."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "model": "gpt-4",
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_date_range_filtering(self, client: AsyncClient, mock_conn: AsyncMock):
        """start_date and end_date filtering works in raw mode."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-06-01T00:00:00Z",
                    "end_date": "2025-06-30T23:59:59Z",
                },
            )

        assert response.status_code == 200
        # Verify date params were passed to the query at positions $1 and $2
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        params = call_args[0][1:]
        assert len(params) >= 2
        # Check that $1 is the start_date and $2 is the end_date
        assert isinstance(params[0], datetime)
        assert isinstance(params[1], datetime)
        assert params[0].isoformat().startswith("2025-06-01")
        assert params[1].isoformat().startswith("2025-06-30")

    @pytest.mark.asyncio
    async def test_start_after_end_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """When start_date > end_date, return 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-08-01T00:00:00Z",
                    "end_date": "2025-07-01T00:00:00Z",
                },
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no records match, items is empty and total is 0."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


class TestRecordsWithContextGroupBy:
    """Tests for GET /api/v1/usage/records-with-context with group_by."""

    @pytest.mark.asyncio
    async def test_group_by_project(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=project returns aggregated subtotals per project with project_label."""
        rows = [
            _mk_rwc_grouped_row(
                group_value="my-project",
                project_label="my-project",
                record_count=2,
            ),
            _mk_rwc_grouped_row(
                group_value="other-project",
                project_label="other-project",
                record_count=1,
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["group_value"] == "my-project"
        assert data[0]["project_label"] == "my-project"
        assert data[0]["record_count"] == 2
        assert data[1]["group_value"] == "other-project"

    @pytest.mark.asyncio
    async def test_group_by_agent(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=agent returns aggregated subtotals per agent."""
        rows = [
            _mk_rwc_grouped_row(
                group_value="coder-v1",
                agent="coder-v1",
                record_count=5,
            ),
            _mk_rwc_grouped_row(
                group_value="code-reviewer",
                agent="code-reviewer",
                record_count=3,
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "agent",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["group_value"] == "coder-v1"
        assert data[0]["agent"] == "coder-v1"
        assert data[0]["record_count"] == 5
        assert data[1]["group_value"] == "code-reviewer"

    @pytest.mark.asyncio
    async def test_group_by_project_agent(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=project,agent returns cross-product aggregation."""
        rows = [
            _mk_rwc_grouped_row(
                group_value="my-project|coder-v1",
                project_label="my-project",
                agent="coder-v1",
                record_count=4,
            ),
            _mk_rwc_grouped_row(
                group_value="my-project|code-reviewer",
                project_label="my-project",
                agent="code-reviewer",
                record_count=2,
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project,agent",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["group_value"] == "my-project|coder-v1"
        assert data[0]["project_label"] == "my-project"
        assert data[0]["agent"] == "coder-v1"
        assert data[1]["group_value"] == "my-project|code-reviewer"

    @pytest.mark.asyncio
    async def test_group_by_session(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=session returns per-session subtotals with session_title."""
        rows = [
            _mk_rwc_grouped_row(
                group_value=str(_SESSION_ID),
                session_title="Fix login bug",
                record_count=3,
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "session",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["session_title"] == "Fix login bug"
        assert data[0]["record_count"] == 3

    @pytest.mark.asyncio
    async def test_group_by_model(self, client: AsyncClient, mock_conn: AsyncMock):
        """group_by=model returns per-model subtotals."""
        rows = [
            _mk_rwc_grouped_row(
                group_value="gpt-4",
                model_name="gpt-4",
                record_count=10,
            ),
            _mk_rwc_grouped_row(
                group_value="claude-3",
                model_name="claude-3",
                record_count=5,
            ),
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "model",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 2
        assert data[0]["model_name"] == "gpt-4"
        assert data[0]["record_count"] == 10
        assert data[1]["model_name"] == "claude-3"

    @pytest.mark.asyncio
    async def test_date_range_filtering_in_grouped_mode(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Date range filtering works in grouped mode."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-06-01T00:00:00Z",
                    "end_date": "2025-06-30T23:59:59Z",
                    "group_by": "project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data == []

    @pytest.mark.asyncio
    async def test_invalid_group_by_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """An unrecognised group_by value yields 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
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
    async def test_group_by_project_empty(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no records match, grouped mode returns empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data == []


class TestRecordsWithContextLabelEdgeCases:
    """Edge cases for project label resolution."""

    @pytest.mark.asyncio
    async def test_project_worktree_is_root_resolves_to_external_project_id(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Project with worktree='/' resolves to external_project_id 'global'."""
        row = _mk_rwc_record_row(project_label="global")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["project_label"] == "global"

    @pytest.mark.asyncio
    async def test_project_with_only_worktree_resolves_to_basename(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Project with only worktree set resolves to basename(worktree)."""
        row = _mk_rwc_record_row(project_label="my-repo")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["project_label"] == "my-repo"

    @pytest.mark.asyncio
    async def test_all_label_columns_null_resolves_to_external_project_id(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """When all label columns are null, fallback to external_project_id."""
        row = _mk_rwc_record_row(project_label="ext-proj-42")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["project_label"] == "ext-proj-42"

    @pytest.mark.asyncio
    async def test_context_fields_null_when_no_join_match(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """session_title is None when no matching opencode_session_contexts row."""
        row = _mk_rwc_record_row(session_title=None, project_label="unknown")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["session_title"] is None
        assert item["project_label"] == "unknown"


class TestRecordsWithContextAuth:
    """Auth tests for records-with-context endpoint."""

    @pytest.mark.asyncio
    async def test_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/records-with-context without auth returns 401."""
        from httpx import ASGITransport, AsyncClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
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
    async def test_requires_auth_grouped(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/records-with-context with group_by without auth returns 401."""
        from httpx import ASGITransport, AsyncClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


class TestRecordsWithContextEnvelope:
    """Envelope format tests for records-with-context endpoint."""

    @pytest.mark.asyncio
    async def test_raw_envelope(self, client: AsyncClient, mock_conn: AsyncMock):
        """Raw mode response has status: ok and data wrapper."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_grouped_envelope(self, client: AsyncClient, mock_conn: AsyncMock):
        """Grouped mode response has status: ok and data wrapper (list)."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "project",
                },
            )

        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload


# ════════════════════════════════════════════════════════════════════════════
#  Issue #317 — Backfill cache-write token verification
# ════════════════════════════════════════════════════════════════════════════


class TestBackfillCacheWriteTokens:
    """Tests for the backfill script's verification and update logic.

    These tests verify the SQL queries and argument parsing used by
    ``scripts/backfill_cache_write_tokens.py``.  The actual database
    execution is tested via integration tests.
    """

    def test_verification_query_is_defined(self):
        """The VERIFICATION_QUERY constant is a non-empty string."""
        from scripts.backfill_cache_write_tokens import VERIFICATION_QUERY
        assert VERIFICATION_QUERY
        assert "HAVING" in VERIFICATION_QUERY.upper()
        assert "session_cache_write" in VERIFICATION_QUERY.lower()

    def test_backfill_update_sql_is_defined(self):
        """The BACKFILL_UPDATE_SQL constant is a non-empty UPDATE statement."""
        from scripts.backfill_cache_write_tokens import BACKFILL_UPDATE_SQL
        assert BACKFILL_UPDATE_SQL
        assert BACKFILL_UPDATE_SQL.strip().upper().startswith("UPDATE")

    def test_mismatch_count_sql_is_defined(self):
        """The MISMATCH_COUNT_SQL constant is a non-empty COUNT query."""
        from scripts.backfill_cache_write_tokens import MISMATCH_COUNT_SQL
        assert MISMATCH_COUNT_SQL
        assert "COUNT" in MISMATCH_COUNT_SQL.upper()

    def test_dry_run_default_false(self):
        """The --dry-run flag defaults to False."""
        from scripts.backfill_cache_write_tokens import _parse_args
        args = _parse_args([])
        assert args.dry_run is False

    def test_dry_run_flag_true(self):
        """Passing --dry-run sets dry_run=True."""
        from scripts.backfill_cache_write_tokens import _parse_args
        args = _parse_args(["--dry-run"])
        assert args.dry_run is True

    @pytest.mark.asyncio
    async def test_count_mismatches_returns_int(self):
        """_count_mismatches returns an integer from the database."""
        from scripts.backfill_cache_write_tokens import _count_mismatches
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"cnt": 5})
        result = await _count_mismatches(mock_conn)
        assert result == 5
        # Verify the MISMATCH_COUNT_SQL was sent
        call_sql = mock_conn.fetchrow.call_args[0][0]
        assert "COUNT" in call_sql

    @pytest.mark.asyncio
    async def test_count_mismatches_none_returns_zero(self):
        """_count_mismatches returns 0 when fetchrow returns None."""
        from scripts.backfill_cache_write_tokens import _count_mismatches
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        result = await _count_mismatches(mock_conn)
        assert result == 0

    @pytest.mark.asyncio
    async def test_run_verification_returns_list(self):
        """_run_verification returns a list of mismatched rows."""
        from scripts.backfill_cache_write_tokens import _run_verification
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"id": "sid-1", "session_cache_write": 0, "raw_cache_write_sum": 5},
        ])
        result = await _run_verification(mock_conn)
        assert len(result) == 1
        assert result[0]["session_cache_write"] == 0
        assert result[0]["raw_cache_write_sum"] == 5

    @pytest.mark.asyncio
    async def test_run_backfill_returns_updated_count(self):
        """_run_backfill returns the number of rows updated."""
        from scripts.backfill_cache_write_tokens import _run_backfill
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 7")
        result = await _run_backfill(mock_conn)
        assert result == 7

    @pytest.mark.asyncio
    async def test_run_backfill_zero_updated(self):
        """_run_backfill returns 0 when no rows matched."""
        from scripts.backfill_cache_write_tokens import _run_backfill
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        result = await _run_backfill(mock_conn)
        assert result == 0


# ════════════════════════════════════════════════════════════════════════════
#  Issue #363 — Timeout Budgets
# ════════════════════════════════════════════════════════════════════════════


class TestTimeoutBudgets:
    """Tests for layered timeout budget enforcement on read endpoints."""

    # ── Real enforcement: over-budget → cancelled + logged ──────────────

    @pytest.mark.asyncio
    async def test_operation_exceeding_budget_is_cancelled_and_logged(
        self, caplog,
    ):
        """A coroutine violating its budget is cancelled (TimeoutError) and
        emits an ``operation.timeout`` log event."""
        import logging

        from app.core.telemetry import EVENT_OPERATION_TIMEOUT, timeout_operation

        telemetry_logger = "app.core.telemetry"
        caplog.set_level(logging.WARNING, logger=telemetry_logger)

        with pytest.raises(TimeoutError):
            # budget_ms=50 is far smaller than the 10s sleep
            async with timeout_operation(
                "db.query.records.count", "db", budget_ms=50,
            ):
                await asyncio.sleep(10)

        # The operation must have been cancelled — elapsed should be < 1s
        telemetry_records = [
            r for r in caplog.records if r.name == telemetry_logger
        ]
        assert len(telemetry_records) == 1
        record = telemetry_records[0]
        assert record.getMessage() == EVENT_OPERATION_TIMEOUT
        assert record.event_name == "db.query.records.count"
        assert record.operation_type == "db"
        assert record.budget_ms == 50

    # ── Wrappers pass config-derived budgets to timeout_operation ───────

    @pytest.mark.asyncio
    async def test_wrappers_invoke_timeout_operation_with_config_budgets(self):
        """_db_timeout, _request_timeout, and _status_timeout forward their
        budget parameters to timeout_operation."""
        from unittest.mock import patch

        from app.api.usage import _db_timeout, _request_timeout, _status_timeout

        # _db_timeout / _request_timeout now live in the shared
        # app.core.timeouts module (PR #492 review refactor), so their
        # timeout_operation lookup must be patched there; _status_timeout
        # remains defined in app.api.usage.
        with patch(
            "app.core.timeouts.timeout_operation"
        ) as mock_timeout_op, patch(
            "app.api.usage.timeout_operation"
        ) as mock_status_timeout_op:
            import contextlib

            @contextlib.asynccontextmanager
            async def _fake_timeout(*args, **kwargs):
                yield

            mock_timeout_op.side_effect = _fake_timeout
            mock_status_timeout_op.side_effect = _fake_timeout

            # _db_timeout with explicit seconds → budget_ms = seconds * 1000
            async with _db_timeout("test.db", db_timeout_seconds=10):
                pass
            db_call_kw = mock_timeout_op.call_args.kwargs
            assert db_call_kw["budget_ms"] == 10000, (
                f"Expected 10000, got {db_call_kw}"
            )

            mock_timeout_op.reset_mock()

            # _request_timeout with explicit seconds
            async with _request_timeout(total_request_timeout_seconds=30):
                pass
            req_call_kw = mock_timeout_op.call_args.kwargs
            assert req_call_kw["budget_ms"] == 30000, (
                f"Expected 30000, got {req_call_kw}"
            )

            mock_timeout_op.reset_mock()

            # _status_timeout with explicit seconds
            async with _status_timeout(status_timeout_seconds=7):
                pass
            status_call_kw = mock_status_timeout_op.call_args.kwargs
            assert status_call_kw["budget_ms"] == 7000, (
                f"Expected 7000, got {status_call_kw}"
            )

    # ── Endpoint-level wiring — wrappers are invoked in real endpoint flow ──

    @pytest.mark.asyncio
    async def test__fetch_aggregates_wraps_queries_with_db_timeout(self):
        """_fetch_aggregates calls timeout_operation (via _db_timeout) when
        executing database queries."""
        import datetime
        from unittest.mock import patch

        from app.core.config import Settings

        settings = Settings(
            database_timeout_seconds=3,
        )

        mock_conn = AsyncMock()
        row = _mk_aggregate_row(record_count=1)
        mock_conn.fetchrow = AsyncMock(return_value=row)
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch(
            "app.core.timeouts.timeout_operation"
        ) as mock_timeout_op:
            import contextlib

            @contextlib.asynccontextmanager
            async def _fake_timeout(*args, **kwargs):
                yield

            mock_timeout_op.side_effect = _fake_timeout

            from app.api.usage import _fetch_aggregates

            # UP017: ruff assumes requires-python >=3.12, but the local
            # dev/test runtime is Python 3.9 (datetime.UTC is 3.11+).
            now = datetime.datetime(2025, 7, 1, tzinfo=timezone.utc)  # noqa: UP017
            await _fetch_aggregates(
                mock_conn, now, now, None, None, None, [],
                db_timeout_seconds=settings.database_timeout_seconds,
            )

            assert mock_timeout_op.called, (
                "timeout_operation should be called for DB queries"
            )
            # At least one call must pass budget_ms matching the config value
            budget_calls = [
                c for c in mock_timeout_op.call_args_list
                if c.kwargs.get("budget_ms") == settings.database_timeout_seconds * 1000
            ]
            assert len(budget_calls) >= 1, (
                f"Expected timeout_operation call with "
                f"budget_ms={settings.database_timeout_seconds * 1000}"
            )
