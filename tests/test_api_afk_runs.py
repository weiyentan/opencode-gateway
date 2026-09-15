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
from unittest.mock import AsyncMock, MagicMock

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
# ══════════════════════════════════════════════════════════════════════════
#  Guarded updates — PATCH /api/v1/afk/runs/{afk_run_id} (issue #673)
# ══════════════════════════════════════════════════════════════════════════


def _credential_auth_row() -> MagicMock:
    """Mock row that passes require_collector_token (dedicated AWX client)."""
    from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME

    return mock_row(
        {
            "credential_id": uuid.uuid4(),
            "revoked_at": None,
            "last_used_at": None,
            "client_id": uuid.uuid4(),
            "client_name": AWX_EXECUTION_BINDING_CLIENT_NAME,
            "client_is_active": True,
        }
    )


class TestAFKRunUpdate:
    """Tests for PATCH /api/v1/afk/runs/{afk_run_id} (issue #673).

    Mutable fields are exactly ``{title, status}``.  Terminal stored
    statuses (completed/failed/cancelled/timed_out) are frozen → 409;
    ``pending`` runs are patchable (every transition applies → 200);
    identical values → 200 idempotent no-op; empty bodies → 422.
    Serialization uses ``SELECT ... FOR UPDATE``; the write path
    requires the Admin API Key AND the ``awx-execution-bindings`` collector
    credential; the response is the updated ``AFKRunSummary``.
    """

    def _patch_client(self, mock_conn: AsyncMock) -> AsyncClient:
        from tests.conftest import create_client

        return create_client(mock_conn)

    @pytest.mark.asyncio
    async def test_afk_run_update_title_and_status_success(
        self, mock_conn: AsyncMock
    ):
        """A non-terminal run accepts title and status changes → 200."""
        stored = _mk_run_row(status="running", outcome=None, outcome_status=None)
        updated = _mk_run_row(
            status="completed",
            title="Renamed run",
            outcome=None,
            outcome_status=None,
        )
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, updated]
        )

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "Renamed run", "status": "completed"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["afk_run_id"] == _RUN_ID
        assert data["title"] == "Renamed run"
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_afk_run_update_same_status_is_idempotent_noop(
        self, mock_conn: AsyncMock
    ):
        """Identical supplied values → 200 with no UPDATE issued."""
        stored = _mk_run_row(status="running", title="Fix login bug", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, stored]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "running"
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_afk_run_update_title_only(self, mock_conn: AsyncMock):
        """A title-only change on a non-terminal run → 200."""
        stored = _mk_run_row(status="running", title="Old title", outcome=None, outcome_status=None)
        updated = _mk_run_row(status="running", title="New title", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, updated]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"title": "New title"}
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["title"] == "New title"
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_afk_run_update_preserves_linked_relationships(
        self, mock_conn: AsyncMock
    ):
        """Only the run's own title/status columns are written — execution
        bindings, change-request bindings, entity and session links are
        never touched by the guarded update."""
        stored = _mk_run_row(
            status="running",
            title="Old",
            outcome=None,
            outcome_status=None,
            change_request_provider="gitlab",
            change_request_repository="acme/proj",
            change_request_external_id="442",
        )
        updated = _mk_run_row(
            status="completed",
            title="New",
            outcome=None,
            outcome_status=None,
            change_request_provider="gitlab",
            change_request_repository="acme/proj",
            change_request_external_id="442",
        )
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, updated]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "New", "status": "completed"},
            )

        assert response.status_code == 200
        # Exactly one business write: the afk_runs title/status UPDATE
        # (the other execute is the credential last_used_at touch).
        updates = [
            call[0][0]
            for call in mock_conn.execute.call_args_list
            if "UPDATE afk_runs" in call[0][0]
        ]
        assert len(updates) == 1
        assert "SET title = $2" in updates[0]
        assert "status = $3" in updates[0]
        # No other table is written.
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        for table in (
            "execution_bindings",
            "afk_run_entities",
            "afk_run_sessions",
            "afk_run_change_requests",
            "unresolved_correlations",
        ):
            assert not any(table in sql for sql in executed_sql)
        # The change-request binding survives on the returned summary.
        assert response.json()["data"]["change_request"] == {
            "provider": "gitlab",
            "repository": "acme/proj",
            "external_id": "442",
        }

    @pytest.mark.asyncio
    async def test_afk_run_update_title_is_trimmed(
        self, mock_conn: AsyncMock
    ):
        """Supplied titles are whitespace-trimmed before being stored."""
        stored = _mk_run_row(status="running", title="Old title", outcome=None, outcome_status=None)
        updated = _mk_run_row(status="running", title="Padded title", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, updated]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"title": "  Padded title  "}
            )

        assert response.status_code == 200
        updates = [
            call for call in mock_conn.execute.call_args_list
            if "UPDATE afk_runs" in call[0][0]
        ]
        assert len(updates) == 1
        # execute(sql, afk_run_id, new_title, new_status)
        assert updates[0].args[2] == "Padded title"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_title",
        [
            "x" * 1001,  # exceeds the 1000-character bound
            "   ",  # whitespace-only — empty after trim
        ],
    )
    async def test_afk_run_update_invalid_titles_return_422(
        self, mock_conn: AsyncMock, bad_title: str
    ):
        """Whitespace-only titles (empty after trim) and titles over the
        1000-character bound are invalid → 422, nothing mutated."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"title": bad_title}
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_afk_run_update_unknown_field_returns_422(
        self, mock_conn: AsyncMock
    ):
        """Unknown fields are rejected — mutable set is exactly {title, status}."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"status": "running", "repository": "acme/proj"},
            )

        assert response.status_code == 422
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_afk_run_update_invalid_status_returns_422(
        self, mock_conn: AsyncMock
    ):
        """status is validated against the RunStatus vocabulary."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "bogus"}
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["title", "status"])
    async def test_afk_run_update_explicit_null_returns_422(
        self, mock_conn: AsyncMock, field: str
    ):
        """Explicit nulls never erase — the update path is non-erasing."""
        stored = _mk_run_row(status="running", title="Fix login bug", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row(), stored])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={field: None}
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        # Nothing was mutated.
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_afk_run_update_empty_body_returns_422(
        self, mock_conn: AsyncMock
    ):
        """An empty body (no mutable field supplied) is an invalid request —
        422, nothing queried beyond the credential gate."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={}
            )

        assert response.status_code == 422
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "VALIDATION_ERROR"
        # Only the credential lookup ran — no run-row query.
        assert mock_conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target_status",
        ["running", "completed", "blocked", "stale", "timed_out", "failed", "cancelled"],
    )
    async def test_afk_run_update_pending_run_applies_transition(
        self, mock_conn: AsyncMock, target_status: str
    ):
        """``pending`` runs are provisional and patchable — every RunStatus
        transition is applied (200), not rejected."""
        pending_row = _mk_run_row(
            status="pending", title=None, outcome=None, outcome_status=None
        )
        updated = _mk_run_row(
            status=target_status, title="New title", outcome=None, outcome_status=None
        )
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), pending_row, updated]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "New title", "status": target_status},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["data"]["status"] == target_status
        assert payload["data"]["title"] == "New title"
        # The transition is applied — exactly one afk_runs UPDATE.
        updates = [
            call[0][0] for call in mock_conn.execute.call_args_list
            if "UPDATE afk_runs" in call[0][0]
        ]
        assert len(updates) == 1

    @pytest.mark.asyncio
    async def test_afk_run_update_missing_run_returns_404(
        self, mock_conn: AsyncMock
    ):
        """A well-formed but unknown afk_run_id → 404, nothing mutated."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row(), None])
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled", "timed_out"])
    async def test_afk_run_update_terminal_run_is_frozen_409(
        self, mock_conn: AsyncMock, terminal_status: str
    ):
        """Terminal runs are frozen — any change attempt → 409, no mutation."""
        stored = _mk_run_row(
            status=terminal_status, title="Done", outcome=None, outcome_status=None
        )
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row(), stored])
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "Rewrite history", "status": "running"},
            )

        assert response.status_code == 409
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "CONFLICT"
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_afk_run_update_terminal_same_values_idempotent_200(
        self, mock_conn: AsyncMock
    ):
        """A terminal run receiving its own current values → 200 no-op."""
        stored = _mk_run_row(status="completed", title="Done", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, stored]
        )
        mock_conn.execute = AsyncMock()

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "Done", "status": "completed"},
            )

        assert response.status_code == 200
        executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_afk_run_update_requires_auth(self, mock_conn: AsyncMock):
        """PATCH requires the Admin API Key — 401 without it."""
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_afk_run_update_malformed_id_returns_400(
        self, mock_conn: AsyncMock
    ):
        """Non-ULID path ids are rejected with 400 before any business query
        (the collector-credential gate precedes handler validation)."""
        mock_conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                "/api/v1/afk/runs/not-a-ulid", json={"status": "running"}
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"
        # Only the credential lookup ran — no run-row query.
        assert mock_conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    async def test_afk_run_update_serializes_with_for_update(
        self, mock_conn: AsyncMock
    ):
        """The lifecycle row is locked with SELECT ... FOR UPDATE."""
        stored = _mk_run_row(status="running", outcome=None, outcome_status=None)
        updated = _mk_run_row(status="blocked", outcome=None, outcome_status=None)
        mock_conn.fetchrow = AsyncMock(
            side_effect=[_credential_auth_row(), stored, updated]
        )

        client = self._patch_client(mock_conn)
        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "blocked"}
            )

        assert response.status_code == 200
        # fetchrow call #2 is the repository's guarded read.
        guard_sql = mock_conn.fetchrow.call_args_list[1][0][0]
        assert "FOR UPDATE" in guard_sql


# ══════════════════════════════════════════════════════════════════════════
#  Delete run — DELETE /api/v1/afk/runs/{afk_run_id} (issue #674)
# ══════════════════════════════════════════════════════════════════════════


class TestDeleteRun:
    """Tests for orphan-only AFK Run deletion (DELETE /api/v1/afk/runs/{id})."""

    @pytest.mark.asyncio
    async def test_eligible_orphan_run_deletes_204(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An orphan run (no bindings, no bound CR, no delivery log) is deleted."""
        # Orphan run row: no bound change request.
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        # Eligibility probes: no execution bindings, no delivery_log rows.
        mock_conn.fetchval = AsyncMock(side_effect=[False, False])
        # Link deletes then the run delete.
        mock_conn.execute = AsyncMock(
            side_effect=["DELETE 0", "DELETE 0", "DELETE 0", "DELETE 1"]
        )

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_run_with_execution_bindings_returns_409(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A run with execution bindings is ineligible — 409, nothing deleted."""
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        # Probe 1: execution bindings exist → blocked.
        mock_conn.fetchval = AsyncMock(return_value=True)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "CONFLICT"
        assert "execution bindings" in payload["error"]["message"]
        # Only the lock + probes ran — no DELETE statements were issued.
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_with_bound_change_request_returns_409(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A run with a bound change request is ineligible — 409, nothing deleted."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_run_row(
                change_request_provider="gitlab",
                change_request_repository="acme/proj",
                change_request_external_id="442",
            )
        )

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        payload = response.json()
        assert payload["error"]["code"] == "CONFLICT"
        assert "bound change request" in payload["error"]["message"]
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_with_delivery_log_rows_returns_409(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A run with delivery_log rows is ineligible — 409, nothing deleted."""
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        # Probe 1: no bindings; Probe 2: delivery_log rows exist → blocked.
        mock_conn.fetchval = AsyncMock(side_effect=[False, True])

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        payload = response.json()
        assert payload["error"]["code"] == "CONFLICT"
        assert "delivery log" in payload["error"]["message"]
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_run_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A well-formed but unknown afk_run_id returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["short", "0" * 27, "0" * 25 + "I"])
    async def test_delete_malformed_afk_run_id_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock, bad_id: str
    ):
        """Non-ULID path ids are rejected with 400 before any DB access."""

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{bad_id}")

        assert response.status_code == 400
        payload = response.json()
        assert payload["error"]["code"] == "BAD_REQUEST"
        mock_conn.fetchrow.assert_not_called()
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_without_api_key_returns_401(
        self, mock_conn: AsyncMock
    ):
        """The Admin API Key (ApiKeyMiddleware) is required for deletion."""
        client = create_client(mock_conn, api_key=None)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_delete_without_operator_token_returns_401(
        self, mock_conn: AsyncMock
    ):
        """The dedicated operator token is required — API key alone is not enough."""
        client = create_client(mock_conn, operator_token=None)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"
        mock_conn.fetchrow.assert_not_called()
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_delete_preserves_linked_execution_records(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Deletion removes only the run's aggregate links — never execution data."""
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        mock_conn.fetchval = AsyncMock(side_effect=[False, False])
        mock_conn.execute = AsyncMock(
            side_effect=["DELETE 2", "DELETE 1", "DELETE 3", "DELETE 1"]
        )

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 204

        # The whole check-then-delete sequence runs in a single transaction
        # that locks the run row (SELECT … FOR UPDATE).
        lock_sql = mock_conn.fetchrow.call_args[0][0]
        assert "FOR UPDATE" in lock_sql
        mock_conn.transaction.assert_called_once()

        # Only the run's own aggregate tables are deleted; execution
        # bindings, delivery log, sessions, and engineering events are
        # never written by the deletion path.
        deleted_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert len(deleted_sql) == 4
        for table in (
            "afk_run_entities",
            "afk_run_sessions",
            "unresolved_correlations",
            "afk_runs",
        ):
            assert (
                f"DELETE FROM {table} WHERE afk_run_id = $1" in deleted_sql
            ), f"expected DELETE for {table}, got {deleted_sql}"
        for forbidden in (
            "execution_bindings",
            "delivery_log",
            "sessions",
            "engineering_events",
        ):
            assert not any(
                f"DELETE FROM {forbidden}" in sql for sql in deleted_sql
            ), f"deletion must not touch {forbidden}"

        # The eligibility probes READ (never write) the guarded tables.
        probe_sql = [call[0][0] for call in mock_conn.fetchval.call_args_list]
        assert any("execution_bindings" in sql for sql in probe_sql)
        assert any("delivery_log" in sql for sql in probe_sql)


# ══════════════════════════════════════════════════════════════════════════
#  Coexistence with the execution-scoped endpoints (contract §6)
# ══════════════════════════════════════════════════════════════════════════


class TestRouteCoexistence:
    """The canonical namespace must not collide with existing executions paths."""

    @pytest.mark.asyncio
    async def test_canonical_and_execution_routes_coexist(self):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        paths = {getattr(route, "path", None) for route in app.routes}

        # Canonical routes (new, issue #672).
        assert "/api/v1/afk/runs" in paths
        assert "/api/v1/afk/runs/{afk_run_id}" in paths
        # Execution-scoped routes (unchanged, issues #549/#589/#590/#626).
        assert "/api/v1/afk/executions" in paths
        assert "/api/v1/afk/executions/runs/{afk_run_id}" in paths
