"""Tests for the AFK Dashboard summary API (issue #719).

Covers ``GET /api/v1/afk/dashboard/summary``:

- daily buckets and monthly aggregation (summed at query time),
- provider / repository / date-range filters (and their defaulting),
- ``derived_at`` freshness metadata (including the empty-window case),
- envelope shape, API-key auth, and 400s for invalid interval/provider/date
  values.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.conftest import create_client, mock_row

_PATH = "/api/v1/afk/dashboard/summary"

_TS_A = datetime(2026, 8, 1, 3, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_TS_B = datetime(2026, 8, 2, 3, 0, 0, tzinfo=timezone.utc)  # noqa: UP017


# ── Mock row builders ────────────────────────────────────────────────────────


def _mk_bucket_row(
    *,
    period_start: date = date(2026, 8, 1),
    provider: str = "github",
    repository: str = "acme/proj",
    runs_started: int = 2,
    change_requests_opened: int = 1,
    change_requests_merged: int = 1,
    change_requests_closed: int = 0,
    execution_count: int = 3,
    successful_execution_count: int = 2,
    failed_execution_count: int = 1,
    cancelled_execution_count: int = 0,
    session_count: int = 4,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read_tokens: int = 200,
    cache_write_tokens: int = 100,
    estimated_cost_usd: Decimal = Decimal("0.25"),
    derived_at: datetime | None = _TS_A,
):
    return mock_row(
        {
            "period_start": period_start,
            "provider": provider,
            "repository": repository,
            "runs_started": runs_started,
            "change_requests_opened": change_requests_opened,
            "change_requests_merged": change_requests_merged,
            "change_requests_closed": change_requests_closed,
            "execution_count": execution_count,
            "successful_execution_count": successful_execution_count,
            "failed_execution_count": failed_execution_count,
            "cancelled_execution_count": cancelled_execution_count,
            "session_count": session_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "derived_at": derived_at,
        }
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestAuth:
    @pytest.mark.asyncio
    async def test_requires_api_key(self, mock_conn: AsyncMock):
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.get(_PATH)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  Daily interval (default)
# ══════════════════════════════════════════════════════════════════════════


class TestDailySummary:
    @pytest.mark.asyncio
    async def test_returns_daily_buckets_with_freshness(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_bucket_row(period_start=date(2026, 8, 1), derived_at=_TS_A),
                _mk_bucket_row(
                    period_start=date(2026, 8, 2),
                    provider="gitlab",
                    repository="team/repo",
                    runs_started=5,
                    derived_at=_TS_B,
                ),
            ]
        )

        async with client as c:
            response = await c.get(
                _PATH, params={"from_date": "2026-08-01", "to_date": "2026-08-02"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["interval"] == "daily"
        assert data["from_date"] == "2026-08-01"
        assert data["to_date"] == "2026-08-02"
        assert len(data["buckets"]) == 2
        first = data["buckets"][0]
        assert first["period_start"] == "2026-08-01"
        assert first["provider"] == "github"
        assert first["repository"] == "acme/proj"
        assert first["runs_started"] == 2
        assert first["input_tokens"] == 1000
        assert first["cache_read_tokens"] == 200
        assert first["estimated_cost_usd"] == "0.25"
        # Freshness is the latest derived_at across the returned buckets.
        assert data["derived_at"].startswith("2026-08-02T03:00:00")

    @pytest.mark.asyncio
    async def test_daily_uses_day_column_without_month_truncation(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                _PATH, params={"from_date": "2026-08-01", "to_date": "2026-08-05"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "FROM afk_dashboard_daily" in sql
        assert "day AS period_start" in sql
        assert "date_trunc" not in sql
        assert mock_conn.fetch.call_args[0][1] == date(2026, 8, 1)
        assert mock_conn.fetch.call_args[0][2] == date(2026, 8, 5)


# ══════════════════════════════════════════════════════════════════════════
#  Monthly interval — daily rows aggregated at query time
# ══════════════════════════════════════════════════════════════════════════


class TestMonthlySummary:
    @pytest.mark.asyncio
    async def test_monthly_truncates_to_calendar_month(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_bucket_row(
                    period_start=date(2026, 8, 1),
                    runs_started=7,
                    derived_at=_TS_A,
                )
            ]
        )

        async with client as c:
            response = await c.get(
                _PATH,
                params={
                    "from_date": "2026-08-01",
                    "to_date": "2026-08-31",
                    "interval": "monthly",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["interval"] == "monthly"
        assert data["buckets"][0]["period_start"] == "2026-08-01"
        assert data["buckets"][0]["runs_started"] == 7
        sql = mock_conn.fetch.call_args[0][0]
        assert "date_trunc('month', day)::date AS period_start" in sql
        assert "GROUP BY date_trunc('month', day)::date, provider, repository" in sql

    @pytest.mark.asyncio
    async def test_invalid_interval_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_PATH, params={"interval": "weekly"})

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetch.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  Filters
# ══════════════════════════════════════════════════════════════════════════


class TestFilters:
    @pytest.mark.asyncio
    async def test_provider_and_repository_filters_are_parameterised(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                _PATH,
                params={
                    "from_date": "2026-08-01",
                    "to_date": "2026-08-02",
                    "provider": "gitlab",
                    "repository": "team/repo",
                },
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "provider = $3" in sql
        assert "repository = $4" in sql
        assert mock_conn.fetch.call_args[0][3] == "gitlab"
        assert mock_conn.fetch.call_args[0][4] == "team/repo"

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_PATH, params={"provider": "bitbucket"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_date_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(_PATH, params={"from_date": "not-a-date"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_inverted_range_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                _PATH,
                params={"from_date": "2026-08-05", "to_date": "2026-08-01"},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_end_date_aliases_accepted(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                _PATH,
                params={"start_date": "2026-07-01", "end_date": "2026-07-31"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["from_date"] == "2026-07-01"
        assert data["to_date"] == "2026-07-31"


# ══════════════════════════════════════════════════════════════════════════
#  Default window and empty results
# ══════════════════════════════════════════════════════════════════════════


class TestDefaultsAndEmpty:
    @pytest.mark.asyncio
    async def test_omitted_range_defaults_to_last_30_days(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(_PATH)

        assert response.status_code == 200
        data = response.json()["data"]
        today = datetime.now(timezone.utc).date()  # noqa: UP017
        assert data["to_date"] == today.isoformat()
        assert data["from_date"] == (today - timedelta(days=29)).isoformat()
        assert mock_conn.fetch.call_args[0][1] == today - timedelta(days=29)
        assert mock_conn.fetch.call_args[0][2] == today

    @pytest.mark.asyncio
    async def test_empty_window_returns_empty_buckets_and_null_freshness(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                _PATH, params={"from_date": "2026-01-01", "to_date": "2026-01-02"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["buckets"] == []
        assert data["derived_at"] is None
