"""Query-count verification tests for #362 dashboard read path optimization.

Verifies:
- Agent Run Detail query count reduced from 5 to <=3
- Agent Run List uses 2 queries (no N+1 via CTE)
- Sessions uses 2 queries (no N+1, already optimal)
- Aggregates uses 1 query (already optimal)
- Telemetry middleware: X-Correlation-ID header + request.completed log emission
"""

import logging
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.test_agent_runs import _mk_child_row, _mk_session_row, _mk_todo_row
from tests.test_usage import _mk_aggregate_row
from tests.test_usage import _mk_session_row as _mk_usage_session_row

# ── Shared test data ────────────────────────────────────────────────────────
_SESSION_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════════════
#  Query counting tests — Agent Run Detail
# ══════════════════════════════════════════════════════════════════════════════


class TestAgentRunDetailQueryCount:
    """Verify Agent Run Detail uses <=3 queries (from 5 before optimization)."""

    @pytest.mark.asyncio
    async def test_detail_uses_three_queries_with_parent_and_context(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """With parent and context: 1 fetchrow (session+context+parent) +
        2 fetch (children, todos) = 3 total queries."""
        session_row = _mk_session_row(
            session_id=_SESSION_ID,
            ctx_present=1,
            parent_session_id="ses_parent_001",
            parent_internal_id=uuid.uuid4(),
            ctx_title="My Session",
            ctx_session_model="gpt-4",
        )
        child_rows = [_mk_child_row()]
        todo_rows = [_mk_todo_row()]

        mock_conn.fetchrow = AsyncMock(return_value=session_row)
        mock_conn.fetch = AsyncMock(side_effect=[child_rows, todo_rows])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        assert mock_conn.fetchrow.call_count == 1, (
            f"Expected 1 fetchrow call, got {mock_conn.fetchrow.call_count}"
        )
        assert mock_conn.fetch.call_count == 2, (
            f"Expected 2 fetch calls (children + todos), got {mock_conn.fetch.call_count}"
        )
        total_queries = mock_conn.fetchrow.call_count + mock_conn.fetch.call_count
        assert total_queries <= 3, (
            f"Expected <=3 queries for detail, got {total_queries}"
        )

    @pytest.mark.asyncio
    async def test_detail_uses_three_queries_without_parent(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Without parent: 1 fetchrow (session+context) + 2 fetch
        (children, todos) = 3 total queries."""
        session_row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetchrow = AsyncMock(return_value=session_row)
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{_SESSION_ID}")

        assert response.status_code == 200
        assert mock_conn.fetchrow.call_count == 1
        assert mock_conn.fetch.call_count == 2
        assert mock_conn.fetchrow.call_count + mock_conn.fetch.call_count == 3


# ══════════════════════════════════════════════════════════════════════════════
#  Query counting tests — Agent Run List
# ══════════════════════════════════════════════════════════════════════════════


class TestAgentRunListQueryCount:
    """Verify Agent Run List uses 2 queries (count + data, no N+1)."""

    @pytest.mark.asyncio
    async def test_list_uses_two_queries(self, client: AsyncClient, mock_conn: AsyncMock):
        """Agent run list: 1 fetchval (count) + 1 fetch (data) = 2 queries."""
        row = _mk_session_row(session_id=_SESSION_ID)
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        assert mock_conn.fetchval.call_count == 1, (
            f"Expected 1 fetchval (count), got {mock_conn.fetchval.call_count}"
        )
        assert mock_conn.fetch.call_count == 1, (
            f"Expected 1 fetch (data), got {mock_conn.fetch.call_count}"
        )
        assert mock_conn.fetchval.call_count + mock_conn.fetch.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
#  Query counting tests — Sessions
# ══════════════════════════════════════════════════════════════════════════════


class TestSessionsQueryCount:
    """Verify Sessions uses 2 queries (count + data, no N+1)."""

    @pytest.mark.asyncio
    async def test_sessions_uses_two_queries(self, client: AsyncClient, mock_conn: AsyncMock):
        """Sessions: 1 fetchval (count) + 1 fetch (data) = 2 queries."""
        row = _mk_usage_session_row()
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
        assert mock_conn.fetchval.call_count == 1
        assert mock_conn.fetch.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
#  Query counting tests — Aggregates
# ══════════════════════════════════════════════════════════════════════════════


class TestAggregatesQueryCount:
    """Verify Aggregates uses 1 query."""

    @pytest.mark.asyncio
    async def test_aggregates_total_uses_one_query(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Without group_by: 1 fetchrow = 1 query."""
        row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        assert mock_conn.fetchrow.call_count == 1
        assert mock_conn.fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_aggregates_grouped_uses_one_query(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """With group_by: 1 fetch = 1 query."""
        rows = [_mk_aggregate_row(group_value="gpt-4")]
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
        assert mock_conn.fetch.call_count == 1
        assert mock_conn.fetchrow.call_count == 0

    @pytest.mark.asyncio
    async def test_aggregates_client_project_hits_rollup(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The client,project aggregate path uses the Client Project Rollup
        (contains `FROM client_project_rollup`, not `FROM usage_events`)."""
        rows = [
            _mk_aggregate_row(
                group_value="canonical-client|My Project",
                record_count=0, session_count=0, model_count=0,
                project_label="My Project",
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
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        # Hybrid read: 2 queries — rollup (additive totals) + count (distinct counts)
        assert mock_conn.fetch.call_count == 2, (
            f"Expected 2 fetch calls (rollup + count), got {mock_conn.fetch.call_count}"
        )
        assert mock_conn.fetchrow.call_count == 0

        # SQL-shape: the first call is the rollup query, which must reference
        # client_project_rollup (not usage_events)
        assert len(mock_conn.fetch.call_args_list) >= 1
        first_call_args = mock_conn.fetch.call_args_list[0]
        sql = first_call_args[0][0]
        assert "FROM client_project_rollup" in sql, (
            f"Expected rollup-backed SQL, got: {sql[:200]}"
        )
        assert "FROM usage_events" not in sql, (
            f"Rollup path must not scan usage_events, got: {sql[:200]}"
        )
        assert "COALESCE(oc.canonical_name, oc.name)" in sql, (
            f"Expected canonical-name COALESCE in rollup SQL, got: {sql[:200]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Telemetry middleware integration tests
# ══════════════════════════════════════════════════════════════════════════════


class TestTelemetryMiddlewareIntegration:
    """Verify structured timing logs are emitted by middleware and operations."""

    @pytest.mark.asyncio
    async def test_x_correlation_id_header_in_response(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Response includes X-Correlation-ID header set by middleware."""
        row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=row)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        assert "X-Correlation-ID" in response.headers
        cid = response.headers["X-Correlation-ID"]
        assert len(cid) == 32  # uuid4().hex = 32 hex chars

    @pytest.mark.asyncio
    async def test_request_completed_log_emitted(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Middleware emits a request.completed log entry per request."""
        row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=row)

        # Capture log records from the telemetry logger
        telemetry_logger = logging.getLogger("app.core.telemetry")
        records = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _CaptureHandler()
        old_level = telemetry_logger.level
        telemetry_logger.setLevel(logging.DEBUG)
        telemetry_logger.addHandler(handler)
        try:
            async with client as c:
                response = await c.get(
                    "/api/v1/usage/aggregates",
                    params={
                        "start_date": "2025-07-01T00:00:00Z",
                        "end_date": "2025-07-31T23:59:59Z",
                    },
                )
        finally:
            telemetry_logger.removeHandler(handler)
            telemetry_logger.setLevel(old_level)

        assert response.status_code == 200

        # Find the request.completed log entry
        request_logs = [
            r for r in records if r.getMessage() == "request.completed"
        ]
        assert len(request_logs) >= 1, (
            f"Expected at least 1 request.completed log, got {len(request_logs)}"
        )
        request_log = request_logs[0]
        assert hasattr(request_log, "endpoint")
        assert hasattr(request_log, "duration_ms")
        assert hasattr(request_log, "status_code")
        assert hasattr(request_log, "correlation_id")
