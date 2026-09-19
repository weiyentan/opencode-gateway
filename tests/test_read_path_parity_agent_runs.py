"""Agent Run read-path fixture parity tests (issue #708, T2).

Exercises ``GET /api/v1/usage/agent-runs`` and the detail endpoint through
the HTTP surface with fixture rows shaped exactly like the SQL result, and
asserts the canonical response fields.  Each scenario is independently
runnable in CI (mock connection) and carries no live-database requirement.

Scenarios covered (per issue #708 acceptance criteria):

- Page-before-enrichment: a page of runs is identified first and the
  enrichment columns (child counts, Todo Snapshot aggregates) attach to the
  returned page rows; the page metadata stays consistent with the full match
  count.
- Activity-window overlap: date filters bind to ``last_message_at`` and are
  emitted in the documented order.
- Status filters: the computed-status filter narrows the returned rows and
  binds a single reference timestamp shared by the count and data queries.
- Parent/child counts: ``child_run_count`` surfaces on the list and the child
  rows on the detail endpoint.
- Todo Snapshots: ``todo_total`` / ``todo_completed`` / ``todo_blocked``
  surface on the list and the todo rows on the detail endpoint.

Protocol reference: ``docs/adr/0017-migration-0019-index-measurement.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.test_agent_runs import (
    _EXTERNAL_ID_A,
    _EXTERNAL_ID_B,
    _mk_child_row,
    _mk_session_row,
    _mk_todo_row,
)

_CLIENT_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()
_NOW = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
#  Page-before-enrichment
# ══════════════════════════════════════════════════════════════════════════


class TestPageBeforeEnrichment:
    @pytest.mark.asyncio
    async def test_page_rows_carry_enrichment_and_page_metadata(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A page of two runs (identified by the filtered/unordered base set)
        is returned with the enrichment columns already attached; the page
        metadata reflects the full match count, not the page size."""
        rows = [
            _mk_session_row(
                session_id=uuid.uuid4(),
                external_session_id=f"ses_page_{i}",
                agent="code-editor",
                last_message_at=_NOW - timedelta(minutes=i),
                child_run_count=i,          # enrichment
                todo_total=3 + i,           # enrichment
                todo_completed=1 + i,
                todo_blocked=i,
            )
            for i in range(2)
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=57)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": "2", "offset": "4"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 57
        assert data["limit"] == 2
        assert data["offset"] == 4
        assert len(data["items"]) == 2

        for i, item in enumerate(data["items"]):
            # Enrichment is present on every returned page row.
            assert item["child_run_count"] == i
            assert item["todo_total"] == 3 + i
            assert item["todo_completed"] == 1 + i
            assert item["todo_blocked"] == i

    @pytest.mark.asyncio
    async def test_empty_page_is_well_formed(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An out-of-range page returns an empty item list with intact
        pagination metadata."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=3)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": "50", "offset": "999"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 3
        assert data["offset"] == 999


# ══════════════════════════════════════════════════════════════════════════
#  Activity-window overlap
# ══════════════════════════════════════════════════════════════════════════


class TestActivityWindowOverlap:
    @pytest.mark.asyncio
    async def test_window_filters_bind_to_last_message_at(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """``from_date``/``to_date`` bind to ``last_message_at`` in the
        documented order and the overlapping row is returned with its
        activity timestamps."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
            last_message_at=_NOW,
            first_message_at=_NOW - timedelta(hours=3),
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={
                    "from_date": "2025-07-01T00:00:00Z",
                    "to_date": "2025-08-01T00:00:00Z",
                },
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["id"] == str(_SESSION_ID)
        assert item["last_updated_at"] is not None

        count_sql = mock_conn.fetchval.call_args[0][0]
        data_sql = mock_conn.fetch.call_args[0][0]
        assert "s.last_message_at >= $1" in " ".join(count_sql.split())
        assert "s.last_message_at <= $2" in " ".join(count_sql.split())
        assert "s.last_message_at >= $1" in " ".join(data_sql.split())
        assert "s.last_message_at <= $2" in " ".join(data_sql.split())

    @pytest.mark.asyncio
    async def test_null_optional_fields_serialise_as_null(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A run with no agent/project/context/cost must serialise those
        optional fields as null rather than fabricating values."""
        row = _mk_session_row(
            session_id=_SESSION_ID,
            agent=None,
            project_id=None,
            project_label=None,
            session_title=None,
            session_model=None,
            cost=None,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/agent-runs")

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["agent"] is None
        assert item["project_label"] is None
        assert item["session_title"] is None
        assert item["model"] is None
        assert item["primary_provider"] is None
        assert item["total_estimated_cost_usd"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Status filters
# ══════════════════════════════════════════════════════════════════════════


class TestStatusFilterParity:
    @pytest.mark.asyncio
    async def test_status_filter_narrows_items_and_shares_reference_time(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A status filter returns only matching rows; count and data bind the
        same reference timestamp (already asserted structurally in the T1
        suite — here the response parity is asserted)."""
        rows = [
            _mk_session_row(
                session_id=uuid.uuid4(),
                external_session_id=f"ses_run_{i}",
                computed_status="running",
                last_message_at=_NOW - timedelta(minutes=i),
            )
            for i in range(3)
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=3)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"status": "running"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert all(i["status"] == "running" for i in data["items"])

        count_params = mock_conn.fetchval.call_args[0][1:]
        data_params = mock_conn.fetch.call_args[0][1:]
        assert count_params[0] == "running"
        assert data_params[0] == "running"
        assert count_params[1] == data_params[1]

    @pytest.mark.asyncio
    async def test_unknown_status_filter_is_rejected(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An unknown status is a 400, never a silent empty page."""
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"status": "nonsense"},
            )
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
#  Parent/child counts
# ══════════════════════════════════════════════════════════════════════════


class TestParentChildParity:
    @pytest.mark.asyncio
    async def test_list_surfaces_child_run_count(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _mk_session_row(
            session_id=_SESSION_ID,
            external_session_id=_EXTERNAL_ID_A,
            child_run_count=4,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/agent-runs")

        assert resp.status_code == 200
        assert resp.json()["data"]["items"][0]["child_run_count"] == 4

    @pytest.mark.asyncio
    async def test_detail_lists_children(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The detail endpoint returns the child run rows with their IDs and
        agents."""
        parent = _mk_session_row(
            session_id=_SESSION_ID,
            ctx_present=1,
            parent_session_id=None,
            parent_internal_id=None,
        )
        children = [
            _mk_child_row(
                external_session_id=_EXTERNAL_ID_B,
                agent="code-editor-junior",
            ),
            _mk_child_row(
                external_session_id="ses_d004",
                agent="code-editor-mid",
            ),
        ]
        mock_conn.fetchrow = AsyncMock(return_value=parent)
        mock_conn.fetch = AsyncMock(side_effect=[children, []])

        async with client as c:
            resp = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert resp.status_code == 200
        children_payload = resp.json()["data"]["child_summaries"]
        assert len(children_payload) == 2
        assert [ch["external_session_id"] for ch in children_payload] == [
            _EXTERNAL_ID_B,
            "ses_d004",
        ]


# ══════════════════════════════════════════════════════════════════════════
#  Todo Snapshots
# ══════════════════════════════════════════════════════════════════════════


class TestTodoSnapshotParity:
    @pytest.mark.asyncio
    async def test_list_surfaces_todo_snapshot_counts(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _mk_session_row(
            session_id=_SESSION_ID,
            todo_total=7,
            todo_completed=4,
            todo_blocked=1,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/agent-runs")

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["todo_total"] == 7
        assert item["todo_completed"] == 4
        assert item["todo_blocked"] == 1

    @pytest.mark.asyncio
    async def test_list_absent_todo_snapshot_defaults_to_zero(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A run with no observed todos reports zero counts, never null."""
        row = _mk_session_row(session_id=_SESSION_ID)
        # Simulate the aggregate COALESCE default arriving as NULL for a run
        # with no observed Todo Snapshot.
        _base = row.__getitem__.side_effect

        def _todo_null(k, _base=_base):
            if k in ("todo_total", "todo_completed", "todo_blocked"):
                return None
            return _base(k)

        row.__getitem__.side_effect = _todo_null
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/agent-runs")

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["todo_total"] == 0
        assert item["todo_completed"] == 0
        assert item["todo_blocked"] == 0

    @pytest.mark.asyncio
    async def test_detail_lists_todo_snapshot_rows(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        parent = _mk_session_row(session_id=_SESSION_ID, ctx_present=1)
        todos = [
            _mk_todo_row(content="Ship it", status="completed", position=0),
            _mk_todo_row(content="Review", status="in_progress", position=1),
        ]
        mock_conn.fetchrow = AsyncMock(return_value=parent)
        mock_conn.fetch = AsyncMock(side_effect=[[], todos])

        async with client as c:
            resp = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert resp.status_code == 200
        todo_payload = resp.json()["data"]["todo_rows"]
        assert [t["content"] for t in todo_payload] == ["Ship it", "Review"]
        assert [t["status"] for t in todo_payload] == ["completed", "in_progress"]
