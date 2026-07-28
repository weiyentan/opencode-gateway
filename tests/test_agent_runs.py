"""Tests for the Agent Run Summary API — list and detail endpoints.

Covers:
- List/detail response shape
- Filters: client_id, date range, agent, external_project_id, status
- Computed status derivation (running, completed, blocked, unknown)
- Child counts and child detail data
- Missing context/project rows
- 404 for non-existent detail
- 400 for invalid status filter
- Authentication requirements
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


# ── Shared test data ──────────────────────────────────────────────────────

_CLIENT_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()

_NOW = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

# External session IDs — OpenCode-style ses_* IDs
_EXTERNAL_ID_A = "ses_a001"
_EXTERNAL_ID_B = "ses_b002"
_EXTERNAL_ID_C = "ses_c003"


# ── Row builders ───────────────────────────────────────────────────────────


def _mk_session_row(
    *,
    session_id: uuid.UUID | None = None,
    client_id: uuid.UUID = _CLIENT_ID,
    source_database_id: uuid.UUID = _SOURCE_DB_ID,
    external_session_id: str | None = _EXTERNAL_ID_A,
    first_message_at: datetime = _NOW - timedelta(hours=1),
    last_message_at: datetime = _NOW - timedelta(minutes=5),
    message_count: int = 5,
    total_input_tokens: int = 500,
    total_output_tokens: int = 250,
    total_cached_tokens: int = 0,
    project_id: str | None = None,
    workspace_id: str | None = None,
    agent: str | None = None,
    parent_session_id: str | None = None,
    cost: Decimal | None = Decimal("0.0100"),
    computed_status: str = "running",
    child_run_count: int = 0,
    session_title: str | None = None,
    project_label: str | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for sessions.

    Includes both stored session columns and computed columns (_status,
    child_run_count, project_label) that the agent-runs SQL query produces.
    """
    row = MagicMock()
    data = {
        "id": session_id or uuid.uuid4(),
        "client_id": client_id,
        "source_database_id": source_database_id,
        "external_session_id": external_session_id,
        "first_message_at": first_message_at,
        "last_message_at": last_message_at,
        "message_count": message_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "agent": agent,
        "parent_session_id": parent_session_id,
        "total_estimated_cost_usd": cost,
        "_status": computed_status,
        "child_run_count": child_run_count,
        "session_title": session_title,
        "project_label": project_label if project_label is not None else project_id,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _mk_child_row(
    *,
    session_id: uuid.UUID | None = None,
    external_session_id: str | None = _EXTERNAL_ID_B,
    agent: str | None = "code-editor",
    message_count: int = 3,
    last_message_at: datetime = _NOW - timedelta(minutes=10),
) -> MagicMock:
    """Return a MagicMock for a child session row."""
    row = MagicMock()
    data = {
        "id": session_id or uuid.uuid4(),
        "external_session_id": external_session_id,
        "agent": agent,
        "message_count": message_count,
        "last_message_at": last_message_at,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


# ══════════════════════════════════════════════════════════════════════════
#  Status computation tests (unit — no HTTP)
# ══════════════════════════════════════════════════════════════════════════


class TestComputeStatus:
    """Unit tests for the _compute_status helper."""

    def test_running_when_recently_active(self):
        """Session active within quiet threshold → running."""
        from app.api.usage import _compute_status

        recent = _NOW - timedelta(minutes=5)
        result = _compute_status(
            last_message_at=recent,
            message_count=10,
            has_parent=False,
            now=_NOW,
        )
        assert result == "running"

    def test_completed_when_inactive_no_parent(self):
        """Session beyond quiet threshold, no parent → completed."""
        from app.api.usage import _compute_status

        old = _NOW - timedelta(hours=2)
        result = _compute_status(
            last_message_at=old,
            message_count=10,
            has_parent=False,
            now=_NOW,
        )
        assert result == "completed"

    def test_blocked_when_inactive_with_parent(self):
        """Session beyond quiet threshold with parent → blocked."""
        from app.api.usage import _compute_status

        old = _NOW - timedelta(hours=2)
        result = _compute_status(
            last_message_at=old,
            message_count=10,
            has_parent=True,
            now=_NOW,
        )
        assert result == "blocked"

    def test_unknown_when_no_messages(self):
        """Session with zero messages → unknown."""
        from app.api.usage import _compute_status

        result = _compute_status(
            last_message_at=_NOW,
            message_count=0,
            has_parent=False,
            now=_NOW,
        )
        assert result == "unknown"

    def test_unknown_when_no_last_message_at(self):
        """Session with None last_message_at → unknown."""
        from app.api.usage import _compute_status

        result = _compute_status(
            last_message_at=None,
            message_count=5,
            has_parent=False,
            now=_NOW,
        )
        assert result == "unknown"

    def test_unknown_when_exceeds_unknown_threshold(self):
        """Session older than _UNKNOWN_THRESHOLD_HOURS → unknown."""
        from app.api.usage import _compute_status

        very_old = _NOW - timedelta(hours=48)
        result = _compute_status(
            last_message_at=very_old,
            message_count=10,
            has_parent=False,
            now=_NOW,
        )
        assert result == "unknown"


# ══════════════════════════════════════════════════════════════════════════
#  Title derivation tests (unit)
# ══════════════════════════════════════════════════════════════════════════


class TestDeriveTitle:
    """Unit tests for _derive_title."""

    def test_combines_agent_and_external_id(self):
        from app.api.usage import _derive_title

        title = _derive_title("code-editor", "ses_abc123def456ghi")
        # External ID truncated to 12 chars: "ses_abc123de"
        assert title == "code-editor — ses_abc123de"
        assert "ses_abc123de" in title

    def test_agent_only_when_no_external_id(self):
        from app.api.usage import _derive_title

        title = _derive_title("code-editor", None)
        assert title == "code-editor"

    def test_truncated_external_id_only(self):
        from app.api.usage import _derive_title

        title = _derive_title(None, "ses_abc123def456ghi")
        assert title == "ses_abc123de"

    def test_none_when_both_none(self):
        from app.api.usage import _derive_title

        title = _derive_title(None, None)
        assert title is None


# ══════════════════════════════════════════════════════════════════════════
#  List endpoint tests
# ══════════════════════════════════════════════════════════════════════════


class TestAgentRunsList:
    """Tests for GET /api/v1/usage/agent-runs."""

    @pytest.mark.asyncio
    async def test_returns_paginated_rows(self, client: AsyncClient, mock_conn: AsyncMock):
        """List returns items with pagination metadata — total, limit, offset."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            agent="code-editor",
            project_id="proj-1",
            workspace_id="ws-1",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_row_has_required_fields(self, client: AsyncClient, mock_conn: AsyncMock):
        """Each list row includes both internal and external IDs, computed status, etc."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            agent="code-editor",
            external_session_id=_EXTERNAL_ID_A,
            project_id="proj-1",
            workspace_id="ws-1",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]

        # Internal Gateway identifiers
        assert "id" in item
        assert "client_id" in item
        assert "source_database_id" in item

        # External OpenCode identifiers
        assert item["external_session_id"] == _EXTERNAL_ID_A

        # Computed fields
        assert item["status"] in ("running", "completed", "blocked", "unknown")
        assert "child_run_count" in item
        assert isinstance(item["child_run_count"], int)
        assert item["child_run_count"] >= 0

        # Title and identity
        assert "title" in item
        assert item["agent"] == "code-editor"
        assert item["project_id"] == "proj-1"
        assert item["workspace_id"] == "ws-1"

        # Placeholder counts
        assert item["todo_total"] == 0
        assert item["todo_completed"] == 0
        assert item["todo_blocked"] == 0
        assert item["code_changes_total"] == 0

        # Usage totals
        assert "total_input_tokens" in item
        assert "total_output_tokens" in item
        assert "total_cached_tokens" in item
        assert "message_count" in item

        # Timestamp
        assert "last_updated_at" in item

    @pytest.mark.asyncio
    async def test_running_status_for_recent_session(self, client: AsyncClient, mock_conn: AsyncMock):
        """A session with recent last_message_at is marked running."""
        from datetime import timezone

        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = _mk_session_row(
            session_id=_SESSION_ID,
            last_message_at=recent,
            message_count=10,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        # Status from SQL computation uses now() which should be close
        # to real now; a 5-min-old session should be "running"
        assert item["status"] == "running"

    @pytest.mark.asyncio
    async def test_filters_by_client_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """client_id query param filters results."""
        row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"client_id": str(_CLIENT_ID)},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 1

        # Verify client_id was passed in query params
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        args = call_args[0]
        # client_id param is passed as positional param to the query
        params_list = list(args[1:])  # skip SQL string
        found = False
        for p in params_list:
            if isinstance(p, uuid.UUID) and str(p) == str(_CLIENT_ID):
                found = True
                break
        assert found, "client_id not found in query parameters"

    @pytest.mark.asyncio
    async def test_filters_by_agent(self, client: AsyncClient, mock_conn: AsyncMock):
        """agent query param filters results."""
        row = _mk_session_row(session_id=_SESSION_ID, agent="code-editor")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"agent": "code-editor"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_filters_by_external_project_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """external_project_id query param filters results."""
        row = _mk_session_row(session_id=_SESSION_ID, project_id="proj-abc")
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"external_project_id": "proj-abc"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_filters_by_status(self, client: AsyncClient, mock_conn: AsyncMock):
        """status query param filters by computed status."""
        from datetime import timezone

        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = _mk_session_row(
            session_id=_SESSION_ID,
            last_message_at=recent,
            message_count=10,
            computed_status="running",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"status": "running"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_filters_by_date_range(self, client: AsyncClient, mock_conn: AsyncMock):
        """from_date and to_date filter by last_message_at range."""
        row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={
                    "from_date": "2025-07-01T00:00:00Z",
                    "to_date": "2025-08-01T00:00:00Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        """An unrecognized status value returns 400."""
        async with client as c:
            response = await c.get(
                "/api/v1/usage/agent-runs",
                params={"status": "invalid_status"},
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        """When no sessions match, items is empty and total is 0."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_child_run_count_in_list(self, client: AsyncClient, mock_conn: AsyncMock):
        """List rows include child_run_count populated by the subquery."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
            child_run_count=3,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["child_run_count"] == 3

    @pytest.mark.asyncio
    async def test_missing_context_fields_return_zeros(self, client: AsyncClient, mock_conn: AsyncMock):
        """Sessions without project_id/workspace_id/agent return None for those fields."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            project_id=None,
            workspace_id=None,
            agent=None,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["project_id"] is None
        assert item["workspace_id"] is None
        assert item["agent"] is None
        assert item["todo_total"] == 0
        assert item["todo_completed"] == 0
        assert item["code_changes_total"] == 0


def _mk_ctx_row(
    *,
    code_change_count: int = 5,
    code_change_additions: int = 120,
    code_change_deletions: int = 30,
    session_model: str | None = "claude-sonnet-4",
    session_cost: Decimal | None = Decimal("0.0450"),
    title: str | None = "Implement user auth",
    source_directory: str | None = "/home/user/project/src",
    source_path: str | None = "/home/user/project",
    source_input_tokens: int = 8000,
    source_output_tokens: int = 2000,
    source_cached_tokens: int = 500,
    source_reasoning_tokens: int = 0,
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for opencode_session_contexts."""
    row = MagicMock()
    data: dict[str, Any] = {
        "code_change_count": code_change_count,
        "code_change_additions": code_change_additions,
        "code_change_deletions": code_change_deletions,
        "session_model": session_model,
        "session_cost": session_cost,
        "title": title,
        "source_directory": source_directory,
        "source_path": source_path,
        "source_input_tokens": source_input_tokens,
        "source_output_tokens": source_output_tokens,
        "source_cached_tokens": source_cached_tokens,
        "source_reasoning_tokens": source_reasoning_tokens,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _mk_todo_row(
    *,
    content: str = "Fix login bug",
    status: str | None = "pending",
    priority: str | None = "high",
    position: int = 1,
) -> MagicMock:
    """Return a MagicMock that looks like an asyncpg Record row for opencode_session_todos."""
    row = MagicMock()
    data: dict[str, Any] = {
        "content": content,
        "status": status,
        "priority": priority,
        "position": position,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


# ══════════════════════════════════════════════════════════════════════════
#  Detail endpoint tests
# ══════════════════════════════════════════════════════════════════════════


class TestAgentRunsDetail:
    """Tests for GET /api/v1/usage/agent-runs/{session_id}."""

    @pytest.mark.asyncio
    async def test_returns_detail_by_internal_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail is keyed by internal Gateway session UUID."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
            agent="code-editor",
            project_id="proj-1",
        )
        # fetchrow: session → context (not found) → parent (not called — no parent)
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])  # children, todos

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
        assert data["id"] == str(_SESSION_ID)
        assert data["external_session_id"] == _EXTERNAL_ID_A

    @pytest.mark.asyncio
    async def test_detail_includes_loki_url(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail includes a loki_search_url."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert "loki_search_url" in data
        assert data["loki_search_url"] is not None
        assert "explore" in data["loki_search_url"]

    @pytest.mark.asyncio
    async def test_detail_includes_parent_identifiers(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail includes parent_session_id (external) and parent_internal_id when resolved."""
        parent_internal_id = uuid.uuid4()
        parent_external = "ses_parent_001"

        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            parent_session_id=parent_external,
        )

        # First fetchrow: session row
        # Second fetchrow: parent resolution
        # Third fetchrow: session context
        parent_row = MagicMock()
        parent_row.__getitem__.side_effect = {"id": parent_internal_id}.__getitem__

        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, parent_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["parent_session_id"] == parent_external
        assert data["parent_internal_id"] == str(parent_internal_id)

    @pytest.mark.asyncio
    async def test_detail_parent_none_when_no_parent(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail parent fields are None when session has no parent."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            parent_session_id=None,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["parent_session_id"] is None
        assert data["parent_internal_id"] is None

    @pytest.mark.asyncio
    async def test_detail_includes_child_summaries(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail includes child_run summaries with id, external_session_id, status, agent, message_count."""
        from datetime import timezone

        now = datetime.now(timezone.utc)
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
        )
        child_id = uuid.uuid4()
        child_rows = [
            _mk_child_row(
                session_id=child_id,
                external_session_id=_EXTERNAL_ID_B,
                agent="code-editor-junior",
                message_count=3,
                last_message_at=now - timedelta(minutes=10),
            ),
            _mk_child_row(
                session_id=uuid.uuid4(),
                external_session_id=_EXTERNAL_ID_C,
                agent="code-editor-mid",
                message_count=1,
                last_message_at=now - timedelta(hours=3),
            ),
        ]

        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[child_rows, []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["child_summaries"]) == 2

        child0 = data["child_summaries"][0]
        assert child0["id"] == str(child_id)
        assert child0["external_session_id"] == _EXTERNAL_ID_B
        assert child0["status"] in ("running", "completed", "blocked", "unknown")
        assert child0["agent"] == "code-editor-junior"
        assert child0["message_count"] == 3

    @pytest.mark.asyncio
    async def test_detail_empty_child_summaries(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail child_summaries is empty when no children exist."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["child_summaries"] == []

    @pytest.mark.asyncio
    async def test_detail_defaults_when_no_context_or_todos(self, client: AsyncClient, mock_conn: AsyncMock):
        """Defaults (todo_rows, session_context, todo counts) when no context/todos exist."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["todo_rows"] == []
        assert data["todo_total"] == 0
        assert data["todo_completed"] == 0
        assert data["todo_blocked"] == 0
        assert data["code_changes_total"] == 0
        assert data["session_context"] is None

    @pytest.mark.asyncio
    async def test_detail_404_for_unknown_id(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail returns 404 when session is not found."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        unknown_id = uuid.uuid4()
        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{unknown_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_includes_usage_totals(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail includes all usage total fields."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            total_input_tokens=1000,
            total_output_tokens=500,
            total_cached_tokens=100,
            cost=Decimal("0.0250"),
            message_count=20,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_input_tokens"] == 1000
        assert data["total_output_tokens"] == 500
        assert data["total_cached_tokens"] == 100
        assert data["message_count"] == 20
        assert data["first_message_at"] is not None
        assert data["last_message_at"] is not None

    @pytest.mark.asyncio
    async def test_detail_title_derived(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail title is derived from agent and external_session_id."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            agent="code-editor",
            external_session_id="ses_abc123def456",
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["title"] == "code-editor — ses_abc123de"

    @pytest.mark.asyncio
    async def test_detail_status_computed_on_read(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail status is computed from session recency, not stored."""
        from datetime import timezone

        # Active session → running
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            last_message_at=recent,
            message_count=10,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_detail_blocked_status_with_parent(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail status is blocked when session has parent and is beyond quiet threshold."""
        from datetime import timezone

        old = datetime.now(timezone.utc) - timedelta(hours=2)
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            last_message_at=old,
            message_count=5,
            parent_session_id="ses_parent_001",
        )

        # First fetchrow: session row
        # Second fetchrow: parent resolution (not found)
        # Third fetchrow: session context (not found)
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_detail_missing_context_project_rows(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail gracefully handles sessions without project/context/agent data."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            project_id=None,
            workspace_id=None,
            agent=None,
            external_session_id=None,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["project_id"] is None
        assert data["workspace_id"] is None
        assert data["agent"] is None
        assert data["external_session_id"] is None
        assert data["title"] is None
        assert data["todo_rows"] == []
        assert data["session_context"] is None

    @pytest.mark.asyncio
    async def test_detail_with_session_context(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail returns populated session_context when a context record exists."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        ctx_row = _mk_ctx_row(
            title="Implement user auth",
            session_model="claude-sonnet-4",
            source_directory="/home/user/project/src",
            code_change_additions=120,
            code_change_deletions=30,
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, ctx_row])
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        ctx = data["session_context"]
        assert ctx is not None
        assert ctx["title"] == "Implement user auth"
        assert ctx["session_model"] == "claude-sonnet-4"
        assert ctx["source_directory"] == "/home/user/project/src"
        assert ctx["code_change_additions"] == 120
        assert ctx["code_change_deletions"] == 30

    @pytest.mark.asyncio
    async def test_detail_with_todos(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail returns populated todo_rows (sorted by position) when todos exist."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        todo_rows = [
            _mk_todo_row(content="Fix login bug", status="pending", priority="high", position=1),
            _mk_todo_row(content="Write tests", status="completed", priority="medium", position=2),
        ]
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, None])
        mock_conn.fetch = AsyncMock(side_effect=[[], todo_rows])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["todo_rows"]) == 2
        assert data["todo_rows"][0]["description"] == "Fix login bug"
        assert data["todo_rows"][0]["status"] == "pending"
        assert data["todo_rows"][0]["priority"] == "high"
        assert data["todo_rows"][1]["description"] == "Write tests"
        assert data["todo_rows"][1]["status"] == "completed"
        assert data["todo_rows"][1]["priority"] == "medium"
        assert data["todo_total"] == 2
        assert data["todo_completed"] == 1
        assert data["todo_blocked"] == 0

    @pytest.mark.asyncio
    async def test_detail_with_context_and_todos(self, client: AsyncClient, mock_conn: AsyncMock):
        """Detail returns both session_context and todo_rows when both exist."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        ctx_row = _mk_ctx_row(title="Refactor API", session_model="gpt-4o")
        todo_rows = [
            _mk_todo_row(content="Add endpoint", status="in_progress", priority="high", position=1),
        ]
        mock_conn.fetchrow = AsyncMock(side_effect=[session_row, ctx_row])
        mock_conn.fetch = AsyncMock(side_effect=[[], todo_rows])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["session_context"] is not None
        assert data["session_context"]["title"] == "Refactor API"
        assert len(data["todo_rows"]) == 1
        assert data["todo_rows"][0]["description"] == "Add endpoint"
        assert data["todo_total"] == 1


# ══════════════════════════════════════════════════════════════════════════
#  Authentication tests
# ══════════════════════════════════════════════════════════════════════════


class TestAgentRunsAuth:
    """Agent run endpoints require API-key auth."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/agent-runs without auth returns 401."""
        from httpx import ASGITransport, AsyncClient as HttpxClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with HttpxClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_detail_requires_auth(self, mock_conn: AsyncMock):
        """GET /api/v1/usage/agent-runs/{id} without auth returns 401."""
        from httpx import ASGITransport, AsyncClient as HttpxClient

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with HttpxClient(transport=transport, base_url="http://test") as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"
