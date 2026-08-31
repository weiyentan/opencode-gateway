"""Tests for the change-request summary endpoint (issue #610).

``GET /api/v1/afk-outcomes/change-requests`` returns one row per
provider/repository/change-request identity, aggregating:

* **provider state** — derived from observed ``engineering_events`` facts
  (``merged`` / ``closed`` / ``open``);
* **AFK automation state** — the owning lifecycle's ``afk_runs.status``
  (``pending`` / ``running`` / ``completed`` / ``failed`` / ``cancelled``);
* **total estimated USD cost** — summed linked-session cost, ``null`` when no
  cost telemetry is available (unavailable, never zero);
* **latest linked activity** — the most recent timestamp across linked runs,
  facts, and executions;
* **execution counts** — AWX Execution Binding outcomes aggregated per
  identity (implementation, review, and retry executions all converge on one
  row).

Executions without a durable change-request identity (any NULL resource
identity column) are excluded from the row universe and never contribute
counts.  Coverage: GitHub, GitLab, mixed results, terminal-state aggregation,
cost aggregation, missing telemetry, empty results, filtering, pagination,
auth, and the query-builder/mapper layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import mock_row

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_ENDPOINT = "/api/v1/afk-outcomes/change-requests"


# ── Mock row builders ────────────────────────────────────────────────────────


def _mk_summary_row(
    *,
    provider: str = "github",
    repository: str = "acme/proj",
    external_id: str = "42",
    provider_state: str | None = "merged",
    automation_state: str | None = "completed",
    latest_activity_at: datetime | None = _B_TS,
    total_estimated_cost_usd: Decimal | None = Decimal("0.08"),
    execution_total: int = 3,
    execution_running: int = 0,
    execution_completed: int = 2,
    execution_failed: int = 1,
    execution_cancelled: int = 0,
):
    """Build a mock asyncpg row with the summary query's column shape."""
    return mock_row(
        {
            "provider": provider,
            "repository": repository,
            "external_id": external_id,
            "provider_state": provider_state,
            "automation_state": automation_state,
            "latest_activity_at": latest_activity_at,
            "total_estimated_cost_usd": total_estimated_cost_usd,
            "execution_total": execution_total,
            "execution_running": execution_running,
            "execution_completed": execution_completed,
            "execution_failed": execution_failed,
            "execution_cancelled": execution_cancelled,
        }
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestSummaryAuth:
    """The endpoint requires API-key auth and returns the 401 envelope."""

    @pytest.mark.asyncio
    async def test_requires_auth(self, mock_conn: AsyncMock):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  List change requests
# ══════════════════════════════════════════════════════════════════════════


class TestListChangeRequests:
    """Tests for GET /api/v1/afk-outcomes/change-requests."""

    @pytest.mark.asyncio
    async def test_returns_paginated_summary_rows(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=2)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_summary_row(),
                _mk_summary_row(
                    provider="gitlab",
                    repository="cloudnative-pg/cloudnative-pg",
                    external_id="6",
                    provider_state="open",
                    automation_state="running",
                    latest_activity_at=_A_TS,
                    total_estimated_cost_usd=None,
                    execution_total=1,
                    execution_completed=0,
                    execution_failed=0,
                    execution_running=1,
                ),
            ]
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 2

        # GitHub row: full shape.
        github_row = data["items"][0]
        assert github_row["provider"] == "github"
        assert github_row["repository"] == "acme/proj"
        assert github_row["external_id"] == "42"
        assert github_row["resource_type"] == "change_request"
        assert github_row["provider_state"] == "merged"
        assert github_row["automation_state"] == "completed"
        assert github_row["latest_linked_activity"] is not None
        assert github_row["executions"] == {
            "total": 3,
            "running": 0,
            "completed": 2,
            "failed": 1,
            "cancelled": 0,
        }
        assert Decimal(str(github_row["total_estimated_cost_usd"])) == Decimal("0.08")

        # GitLab row: cost unavailable is null, never zero.
        gitlab_row = data["items"][1]
        assert gitlab_row["provider"] == "gitlab"
        assert gitlab_row["provider_state"] == "open"
        assert gitlab_row["automation_state"] == "running"
        assert gitlab_row["total_estimated_cost_usd"] is None

    @pytest.mark.asyncio
    async def test_multiple_executions_aggregate_into_one_row(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Implementation, review, and retry executions never duplicate rows."""
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_summary_row()])

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["executions"]["total"] == 3
        assert item["executions"]["completed"] == 2
        assert item["executions"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_missing_automation_and_provider_state_are_null(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A change request known only from executions has no derived states."""
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_summary_row(provider_state=None, automation_state=None)
            ]
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["provider_state"] is None
        assert item["automation_state"] is None

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_pagination_applied_in_sql(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT, params={"limit": 25, "offset": 10})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["limit"] == 25
        assert data["offset"] == 10
        sql = mock_conn.fetch.call_args[0][0]
        assert "LIMIT $1" in sql
        assert "OFFSET $2" in sql


# ══════════════════════════════════════════════════════════════════════════
#  Filters
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestSummaryFilters:
    """Provider, repository, state, and date-range filters follow existing
    conventions: parameterised SQL, 400 on invalid values, inverted windows."""

    @pytest.mark.asyncio
    async def test_filters_by_provider(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT, params={"provider": "gitlab"})

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "i.provider = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_repository(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT, params={"repository": "acme/proj"})

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "i.repository = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_provider_state(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT, params={"provider_state": "merged"})

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "summary.provider_state = $1" in sql
        # The same filter must also gate the total count.
        count_sql = mock_conn.fetchval.call_args[0][0]
        assert "summary.provider_state = $1" in count_sql

    @pytest.mark.asyncio
    async def test_filters_by_automation_state(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_ENDPOINT, params={"automation_state": "failed"})

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "summary.automation_state = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_activity_window(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                _ENDPOINT,
                params={
                    "activity_from": "2026-08-01T00:00:00Z",
                    "activity_to": "2026-08-02T00:00:00Z",
                },
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "summary.latest_activity_at >= $1" in sql
        assert "summary.latest_activity_at <= $2" in sql

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_ENDPOINT, params={"provider": "bitbucket"})

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_invalid_provider_state_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_ENDPOINT, params={"provider_state": "bogus"})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_automation_state_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_ENDPOINT, params={"automation_state": "stale"})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_inverted_activity_window_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                _ENDPOINT,
                params={
                    "activity_from": "2026-08-02T00:00:00Z",
                    "activity_to": "2026-08-01T00:00:00Z",
                },
            )

        assert response.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
#  Query builder / mapper (repository layer)
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestSummaryQueries:
    """Direct checks of the SQL builder and row mapper behind the endpoint."""

    def _builder(self):
        from app.api.afk_outcomes import _build_change_request_queries

        return _build_change_request_queries

    def test_universe_joins_three_identity_sources(self):
        build = self._builder()
        count_sql, data_sql, params = build(None, None, None, None, None, None)
        assert params == []
        assert "WITH identities AS" in data_sql
        assert "FROM engineering_events" in data_sql
        assert "FROM afk_runs" in data_sql
        assert "FROM execution_bindings" in data_sql
        # Count wraps the same grouped body.
        assert count_sql.startswith("SELECT COUNT(*) FROM (")
        assert "GROUP BY i.provider, i.repository, i.external_id" in count_sql

    def test_excludes_executions_without_durable_change_request_identity(self):
        build = self._builder()
        _, data_sql, _ = build(None, None, None, None, None, None)
        # The execution-binding legs of the union and the count aggregation
        # both require every resource-identity column to be present.
        normalized = " ".join(data_sql.split())
        assert "entity_type = 'change_request'" in normalized
        assert (
            "AND provider IS NOT NULL AND repository_url IS NOT NULL "
            "AND entity_number IS NOT NULL"
        ) in normalized

    def test_provider_state_precedence_merged_over_closed_over_open(self):
        build = self._builder()
        _, data_sql, _ = build(None, None, None, None, None, None)
        assert "WHEN BOOL_OR(es.merged) THEN 'merged'" in data_sql
        assert "WHEN BOOL_OR(es.closed) THEN 'closed'" in data_sql
        assert "WHEN BOOL_OR(es.opened) THEN 'open'" in data_sql

    def test_automation_state_precedence_mirrors_run_status_policy(self):
        build = self._builder()
        _, data_sql, _ = build(None, None, None, None, None, None)
        # Success-aware precedence, mirroring resolve_afk_run_status.
        assert "WHEN BOOL_OR(r.status = 'running') THEN 'running'" in data_sql
        assert "WHEN BOOL_OR(r.status = 'completed') THEN 'completed'" in data_sql
        assert "WHEN BOOL_OR(r.status = 'failed') THEN 'failed'" in data_sql
        assert "WHEN BOOL_OR(r.status = 'cancelled') THEN 'cancelled'" in data_sql
        assert "WHEN BOOL_OR(r.status = 'pending') THEN 'pending'" in data_sql

    def test_cost_sum_never_coalesces_null_to_zero(self):
        build = self._builder()
        _, data_sql, _ = build(None, None, None, None, None, None)
        # Missing cost telemetry must surface as SQL NULL (unavailable), so the
        # mapper yields None — the cost column itself is a plain SUM, never
        # wrapped in a COALESCE that would rewrite missing telemetry to 0.
        assert "SUM(s.total_estimated_cost_usd) AS total_estimated_cost_usd" in data_sql
        assert "COALESCE(SUM(s.total_estimated_cost_usd)" not in data_sql

    def test_mapper_maps_row_columns(self):
        from app.api.afk_outcomes import _change_request_summary_row

        row = _mk_summary_row()
        mapped = _change_request_summary_row(row)
        assert mapped.provider == "github"
        assert mapped.repository == "acme/proj"
        assert mapped.external_id == "42"
        assert mapped.resource_type == "change_request"
        assert mapped.provider_state == "merged"
        assert mapped.automation_state == "completed"
        assert mapped.latest_linked_activity == _B_TS
        assert mapped.total_estimated_cost_usd == Decimal("0.08")
        assert mapped.executions.total == 3
        assert mapped.executions.completed == 2
        assert mapped.executions.failed == 1
        assert mapped.executions.running == 0
        assert mapped.executions.cancelled == 0

    def test_mapper_missing_cost_is_none_not_zero(self):
        from app.api.afk_outcomes import _change_request_summary_row

        mapped = _change_request_summary_row(
            _mk_summary_row(total_estimated_cost_usd=None)
        )
        assert mapped.total_estimated_cost_usd is None
