"""Tests for the canonical AFK Run API (issue #672).

Covers the two GET endpoints under ``/api/v1/afk/runs`` (canonical contract
``docs/contracts/afk-run-api-v1.md`` §4.1/§4.2):

- ``GET /runs``          — paginated, filterable list (has_change_request,
  created_before, provider, repository, status, outcome, limit, offset)
- ``GET /runs/{id}``     — canonical detail (full chain with extended summary)

Also covers envelope shape, 401 for unauthenticated requests, 400 for
malformed ULIDs and invalid filters, 404 for unknown runs, and route
coexistence with the execution-scoped endpoints (no behavior change there).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.conftest import create_client, mock_row

_RUN_ID = "01J8ABCDEFGHJKMNPQRSTVWXYZ"
_OTHER_RUN_ID = "01J8BCDEFGHJKMNPQRSTVWXYZ"
_CUT_TS = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_SESSION_ID = uuid.uuid4()

# Sentinel so callers can explicitly request outcome=None (vs. the default).
_DEFAULT_OUTCOME = object()


# ── Mock row builders ────────────────────────────────────────────────────────


def _mk_run_row(
    *,
    afk_run_id: str = _RUN_ID,
    provider: str = "github",
    status: str = "completed",
    title: str | None = "Fix login bug",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = _B_TS,
    outcome_status: str | None = "merged",
    outcome: object = _DEFAULT_OUTCOME,
    first_seen_at: datetime | None = _A_TS,
    last_seen_at: datetime | None = _B_TS,
    repository: str | None = None,
    trigger_type: str | None = None,
    change_request_provider: str | None = None,
    change_request_repository: str | None = None,
    change_request_external_id: str | None = None,
    recovered_from_afk_run_id: str | None = None,
):
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "provider": provider,
            "status": status,
            "title": title,
            "started_at": started_at,
            "finished_at": finished_at,
            "outcome_status": outcome_status,
            "outcome": (
                {
                    "status": "merged",
                    "change_request_ids": ["change_request:42"],
                    "resolved_issue_ids": ["issue:37"],
                    "merge_event_id": "merge_event:99",
                    "merged_at": _B_TS.isoformat(),
                }
                if outcome is _DEFAULT_OUTCOME
                else outcome
            ),
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            # Provisional lifecycle columns (migration 0039).
            "repository": repository,
            "trigger_type": trigger_type,
            "change_request_provider": change_request_provider,
            "change_request_repository": change_request_repository,
            "change_request_external_id": change_request_external_id,
            "recovered_from_afk_run_id": recovered_from_afk_run_id,
        }
    )


def _mk_entity_row(
    *,
    afk_run_id: str = _RUN_ID,
    provider: str = "github",
    repository: str = "acme/proj",
    entity_type: str = "issue",
    external_id: str = "37",
    role: str = "resolved",
    correlation_method: str | None = "issue_reference",
    correlation_confidence: float = 1.0,
    evidence: list | None = None,
    resolver_version: str | None = "2",
    owning_change_request_id: str | None = None,
    correlation_source: str = "direct",
):
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "provider": provider,
            "repository": repository,
            "entity_type": entity_type,
            "external_id": external_id,
            "role": role,
            "correlation_method": correlation_method,
            "correlation_confidence": correlation_confidence,
            "evidence": evidence
            if evidence is not None
            else [
                {
                    "kind": "issue_reference",
                    "source_entity_id": "change_request:42",
                    "detail": "resolves #37",
                    "weight": 1.0,
                }
            ],
            "resolver_version": resolver_version,
            "owning_change_request_id": owning_change_request_id,
            "correlation_source": correlation_source,
        }
    )


def _mk_session_row(
    *,
    session_id: uuid.UUID | None = _SESSION_ID,
    external_session_id: str | None = "ses_abc123",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = _B_TS,
    agent: str | None = "code-editor",
    total_input_tokens: int = 500,
    total_output_tokens: int = 250,
    total_cache_read_tokens: int = 100,
    total_cache_write_tokens: int = 50,
    total_estimated_cost_usd: Decimal | None = Decimal("0.0175"),
    message_count: int = 5,
):
    return mock_row(
        {
            "session_id": session_id,
            "external_session_id": external_session_id,
            "parent_session_id": None,
            "started_at": started_at,
            "finished_at": finished_at,
            "agent": agent,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "total_estimated_cost_usd": total_estimated_cost_usd,
            "message_count": message_count,
        }
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """Both canonical endpoints require API-key auth and return the envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/afk/runs",
            f"/api/v1/afk/runs/{_RUN_ID}",
        ],
    )
    async def test_requires_auth(self, mock_conn: AsyncMock, path: str):
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.get(path)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  List runs — GET /api/v1/afk/runs
# ══════════════════════════════════════════════════════════════════════════


class TestCanonicalListRuns:
    """Tests for GET /api/v1/afk/runs."""

    @pytest.mark.asyncio
    async def test_returns_paginated_runs(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_run_row()])

        async with client as c:
            response = await c.get("/api/v1/afk/runs")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 1
        item = data["items"][0]
        # Canonical AFKRunSummary — the outcomes RunSummary extended additively.
        assert item["afk_run_id"] == _RUN_ID
        assert item["provider"] == "github"
        assert item["status"] == "completed"
        assert item["outcome_status"] == "merged"
        # Extended lifecycle fields (legacy row → nulls).
        assert item["repository"] is None
        assert item["trigger_type"] is None
        assert item["recovered_from_afk_run_id"] is None
        assert item["change_request"] is None

    @pytest.mark.asyncio
    async def test_change_request_embedded_when_bound(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_run_row(
                    repository="acme/proj",
                    trigger_type="eda",
                    change_request_provider="gitlab",
                    change_request_repository="acme/proj",
                    change_request_external_id="442",
                )
            ]
        )

        async with client as c:
            response = await c.get("/api/v1/afk/runs")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["repository"] == "acme/proj"
        assert item["trigger_type"] == "eda"
        assert item["change_request"] == {
            "provider": "gitlab",
            "repository": "acme/proj",
            "external_id": "442",
        }

    @pytest.mark.asyncio
    async def test_has_change_request_true_filters_bound(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"has_change_request": "true"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.change_request_provider IS NOT NULL" in sql

    @pytest.mark.asyncio
    async def test_has_change_request_false_filters_unbound(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"has_change_request": "false"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.change_request_provider IS NULL" in sql

    @pytest.mark.asyncio
    async def test_invalid_has_change_request_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"has_change_request": "maybe"}
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_created_before_filters_on_first_seen_at(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"created_before": "2026-08-01T00:00:00Z"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.first_seen_at < $1" in sql
        assert mock_conn.fetch.call_args[0][1] == _CUT_TS

    @pytest.mark.asyncio
    async def test_invalid_created_before_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"created_before": "not-a-date"}
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_filters_by_provider(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"provider": "gitlab"})

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.provider = $1" in sql

    @pytest.mark.asyncio
    async def test_repository_filter_matches_run_or_entity_links(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"repository": "acme/proj"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.repository = $1" in sql
        assert "re.repository = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_status_and_outcome(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs",
                params={"status": "pending", "outcome": "open"},
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.status = $1" in sql
        assert "r.outcome_status = $2" in sql

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"status": "bogus"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_invalid_outcome_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"outcome": "bogus"})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"provider": "bogus"})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_orders_by_last_seen_desc_nulls_last_then_run_id(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk/runs")

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "ORDER BY r.last_seen_at DESC NULLS LAST" in sql
        assert "r.afk_run_id ASC" in sql

    @pytest.mark.asyncio
    async def test_limit_offset_passed_to_data_query(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"limit": "5", "offset": "10"}
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["limit"] == 5
        assert data["offset"] == 10
        assert mock_conn.fetch.call_args[0][-2:] == (5, 10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["0", "1001", "abc", "-3"])
    async def test_out_of_range_limit_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock, raw: str
    ):
        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"limit": raw})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["-1", "abc"])
    async def test_out_of_range_offset_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock, raw: str
    ):
        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"offset": raw})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk/runs")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Run detail — GET /api/v1/afk/runs/{afk_run_id}
# ══════════════════════════════════════════════════════════════════════════


class TestCanonicalRunDetail:
    """Tests for GET /api/v1/afk/runs/{afk_run_id}."""

    @pytest.mark.asyncio
    async def test_returns_full_chain(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        entity_rows = [
            _mk_entity_row(entity_type="issue", external_id="37", role="resolved"),
            _mk_entity_row(
                entity_type="change_request",
                external_id="42",
                role="resolved",
                correlation_method="issue_reference",
            ),
        ]
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        mock_conn.fetch = AsyncMock(side_effect=[entity_rows, [_mk_session_row()]])

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        data = response.json()["data"]

        # Extended run block (canonical AFKRunSummary).
        run = data["run"]
        assert run["afk_run_id"] == _RUN_ID
        assert run["status"] == "completed"
        assert run["repository"] is None
        assert run["trigger_type"] is None
        assert run["change_request"] is None

        # Full chain preserved from the outcomes detail composition.
        assert data["outcome"]["status"] == "merged"
        assert len(data["issues"]) == 1
        assert data["issues"][0]["entity_id"] == "issue:37"
        assert len(data["change_requests"]) == 1
        assert data["change_requests"][0]["provisional"] is False
        assert data["agents"] == ["code-editor"]
        assert data["usage"]["active_tokens"] == 750
        assert data["usage"]["cache_read_tokens"] == 100

    @pytest.mark.asyncio
    async def test_run_block_carries_change_request_when_bound(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_run_row(
                repository="acme/proj",
                trigger_type="manual",
                change_request_provider="github",
                change_request_repository="acme/proj",
                change_request_external_id="442",
            )
        )
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 200
        run = response.json()["data"]["run"]
        assert run["repository"] == "acme/proj"
        assert run["trigger_type"] == "manual"
        assert run["change_request"] == {
            "provider": "github",
            "repository": "acme/proj",
            "external_id": "442",
        }

    @pytest.mark.asyncio
    async def test_unknown_run_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["short", "0" * 27, "0" * 25 + "I"])
    async def test_malformed_afk_run_id_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock, bad_id: str
    ):
        """Non-ULID path ids are rejected with 400 before any DB access."""

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{bad_id}")

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetchrow.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  Coexistence with the execution-scoped endpoints (contract §6)
# ══════════════════════════════════════════════════════════════════════════


class TestRouteCoexistence:
    """The canonical namespace must not collide with existing executions paths."""

    @pytest.mark.asyncio
    async def test_canonical_and_execution_routes_coexist(self):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        paths = {route.path for route in app.routes}

        # Canonical routes (new, issue #672).
        assert "/api/v1/afk/runs" in paths
        assert "/api/v1/afk/runs/{afk_run_id}" in paths
        # Execution-scoped routes (unchanged, issues #549/#589/#590/#626).
        assert "/api/v1/afk/executions" in paths
        assert "/api/v1/afk/executions/runs/{afk_run_id}" in paths
