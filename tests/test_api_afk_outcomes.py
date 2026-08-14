"""Tests for the AFK outcomes read-only REST API (issue #452).

Covers the four endpoints under ``/api/v1/afk-outcomes``:

- ``GET /runs``          — list runs (filterable by repository, window, status,
  outcome, origin; paginated)
- ``GET /runs/{id}``     — run detail (full chain with per-link provenance and
  provisional markers)
- ``GET /entities``      — list entities (links with provenance + superseded state)
- ``GET /correlations``  — list correlations (unresolved, with provenance)

Also covers envelope shape, 401 for unauthenticated requests, empty results,
and 400 for invalid filter values.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.conftest import mock_row

_RUN_ID = "01J8ABCDEFGHJKMNPQRSTVWXYZ"

# Sentinel so callers can explicitly request outcome=None (vs. the default).
_DEFAULT_OUTCOME = object()

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_SESSION_ID = uuid.uuid4()


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
    resolver_version: str | None = "1",
    owning_change_request_id: str | None = None,
    correlation_source: str = "direct",
    superseded_at: datetime | None = None,
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
            "superseded_at": superseded_at,
        }
    )


def _mk_session_row(
    *,
    afk_run_id: str = _RUN_ID,
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
            "afk_run_id": afk_run_id,
            "session_id": session_id,
            "external_session_id": external_session_id,
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


def _mk_correlation_row(
    *,
    provider: str = "github",
    repository: str = "acme/proj",
    entity_type: str = "issue",
    external_id: str = "37",
    afk_run_id: str | None = None,
    method: str = "temporal_inference",
    reason: str | None = None,
    correlation_confidence: float = 0.4,
    candidates: list | None = None,
    evidence: list | None = None,
    resolver_version: str | None = "1",
    created_at: datetime | None = _A_TS,
):
    return mock_row(
        {
            "provider": provider,
            "repository": repository,
            "entity_type": entity_type,
            "external_id": external_id,
            "afk_run_id": afk_run_id,
            "method": method,
            "reason": reason,
            "correlation_confidence": correlation_confidence,
            "candidates": candidates if candidates is not None else [],
            "evidence": evidence or [],
            "resolver_version": resolver_version,
            "created_at": created_at,
        }
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """All four endpoints require API-key auth and return the 401 envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/afk-outcomes/runs",
            f"/api/v1/afk-outcomes/runs/{_RUN_ID}",
            "/api/v1/afk-outcomes/entities",
            "/api/v1/afk-outcomes/correlations",
        ],
    )
    async def test_requires_auth(self, mock_conn: AsyncMock, path: str):
        from httpx import ASGITransport

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(path)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  List runs
# ══════════════════════════════════════════════════════════════════════════


class TestListRuns:
    """Tests for GET /api/v1/afk-outcomes/runs."""

    @pytest.mark.asyncio
    async def test_returns_paginated_runs(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_run_row()])

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/runs")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["afk_run_id"] == _RUN_ID
        assert item["status"] == "completed"
        assert item["outcome_status"] == "merged"

    @pytest.mark.asyncio
    async def test_filters_by_repository(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"repository": "acme/proj"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "afk_run_entities" in sql
        assert "re.repository = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_status(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"status": "completed"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.status = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_outcome(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"outcome": "merged"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.outcome_status = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_origin(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"origin": "gitlab"}
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.provider = $1" in sql

    @pytest.mark.asyncio
    async def test_filters_by_window(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs",
                params={
                    "started_from": "2026-08-01T00:00:00Z",
                    "started_to": "2026-08-02T00:00:00Z",
                },
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        assert "r.started_at >= $1" in sql
        assert "r.started_at <= $2" in sql

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"status": "bogus"}
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_invalid_outcome_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"outcome": "bogus"}
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_origin_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs", params={"origin": "bitbucket"}
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_inverted_window_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/runs",
                params={
                    "started_from": "2026-08-02T00:00:00Z",
                    "started_to": "2026-08-01T00:00:00Z",
                },
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/runs")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Run detail
# ══════════════════════════════════════════════════════════════════════════


class TestRunDetail:
    """Tests for GET /api/v1/afk-outcomes/runs/{afk_run_id}."""

    @pytest.mark.asyncio
    async def test_returns_full_chain(self, client: AsyncClient, mock_conn: AsyncMock):
        entity_rows = [
            _mk_entity_row(entity_type="issue", external_id="37", role="resolved"),
            _mk_entity_row(
                entity_type="change_request",
                external_id="42",
                role="resolved",
                correlation_method="issue_reference",
            ),
            _mk_entity_row(
                entity_type="issue",
                external_id="88",
                role="referenced",
                correlation_method="temporal_inference",
                correlation_confidence=0.4,
            ),
        ]
        session_rows = [_mk_session_row()]
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        mock_conn.fetch = AsyncMock(side_effect=[entity_rows, session_rows])

        async with client as c:
            response = await c.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")

        assert response.status_code == 200
        data = response.json()["data"]

        # Run aggregate
        assert data["run"]["afk_run_id"] == _RUN_ID
        assert data["run"]["status"] == "completed"

        # Outcome
        assert data["outcome"]["status"] == "merged"
        assert data["outcome"]["resolved_issue_ids"] == ["issue:37"]

        # Entity links grouped by type
        assert len(data["issues"]) == 2
        assert len(data["change_requests"]) == 1

        # Sessions + usage + agents
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["inferred"] is True
        assert data["sessions"][0]["agent"] == "code-editor"
        assert data["agents"] == ["code-editor"]
        assert data["usage"]["active_tokens"] == 750  # 500 + 250
        assert data["usage"]["cache_read_tokens"] == 100

    @pytest.mark.asyncio
    async def test_entity_links_carry_provenance(self, client: AsyncClient, mock_conn: AsyncMock):
        entity_rows = [
            _mk_entity_row(
                entity_type="issue",
                external_id="37",
                role="resolved",
                correlation_method="issue_reference",
                correlation_confidence=1.0,
                resolver_version="1",
                owning_change_request_id="442",
                correlation_source="owning_change_request",
            ),
            _mk_entity_row(
                entity_type="issue",
                external_id="99",
                role="resolved",
            ),
        ]
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        mock_conn.fetch = AsyncMock(side_effect=[entity_rows, []])

        async with client as c:
            response = await c.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")

        data = response.json()["data"]
        lineage_link = next(
            link for link in data["issues"] if link["external_id"] == "37"
        )
        assert lineage_link["entity_id"] == "issue:37"
        assert lineage_link["correlation_method"] == "issue_reference"
        assert lineage_link["correlation_confidence"] == 1.0
        assert lineage_link["resolver_version"] == "1"
        assert lineage_link["evidence"][0]["kind"] == "issue_reference"
        assert lineage_link["provisional"] is False
        # Owning-branch lineage provenance surfaces through the read API.
        assert lineage_link["owning_change_request_id"] == "442"
        assert lineage_link["correlation_source"] == "owning_change_request"
        # A link without lineage falls back to the direct-source default.
        direct_link = next(
            link for link in data["issues"] if link["external_id"] == "99"
        )
        assert direct_link["owning_change_request_id"] is None
        assert direct_link["correlation_source"] == "direct"

    @pytest.mark.asyncio
    async def test_provisional_links_are_marked(self, client: AsyncClient, mock_conn: AsyncMock):
        entity_rows = [
            _mk_entity_row(
                entity_type="issue",
                external_id="88",
                role="referenced",
                correlation_method="temporal_inference",
                correlation_confidence=0.4,
            )
        ]
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        mock_conn.fetch = AsyncMock(side_effect=[entity_rows, []])

        async with client as c:
            response = await c.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")

        data = response.json()["data"]
        link = data["issues"][0]
        assert link["provisional"] is True
        assert link["role"] == "referenced"
        assert link["correlation_method"] == "temporal_inference"

    @pytest.mark.asyncio
    async def test_unknown_run_returns_404(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_empty_chain(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row(outcome=None))
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["outcome"] is None
        assert data["issues"] == []
        assert data["sessions"] == []
        assert data["agents"] == []
        assert data["usage"]["active_tokens"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  List entities
# ══════════════════════════════════════════════════════════════════════════


class TestListEntities:
    """Tests for GET /api/v1/afk-outcomes/entities."""

    @pytest.mark.asyncio
    async def test_returns_entities_with_provenance(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=2)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_entity_row(
                    entity_type="issue",
                    external_id="37",
                    owning_change_request_id="442",
                    correlation_source="owning_change_request",
                ),
                _mk_entity_row(
                    entity_type="issue", external_id="88", superseded_at=_A_TS
                ),
            ]
        )

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/entities")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        items = data["items"]
        assert items[0]["entity_id"] == "issue:37"
        assert items[0]["correlation_method"] == "issue_reference"
        assert items[0]["superseded_at"] is None
        assert items[0]["provisional"] is False
        # Owning-branch lineage provenance surfaces through the read API.
        assert items[0]["owning_change_request_id"] == "442"
        assert items[0]["correlation_source"] == "owning_change_request"
        # A row without lineage falls back to the direct-source default.
        assert items[1]["owning_change_request_id"] is None
        assert items[1]["correlation_source"] == "direct"
        # Superseded state is surfaced, not hidden
        assert items[1]["superseded_at"] is not None

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/entities")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  List correlations
# ══════════════════════════════════════════════════════════════════════════


class TestListCorrelations:
    """Tests for GET /api/v1/afk-outcomes/correlations."""

    @pytest.mark.asyncio
    async def test_returns_correlations_with_provenance(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_correlation_row(method="temporal_inference", correlation_confidence=0.4)
            ]
        )

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/correlations")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["entity_id"] == "issue:37"
        assert item["method"] == "temporal_inference"
        assert item["correlation_confidence"] == 0.4
        assert item["resolver_version"] == "1"
        assert item["provisional"] is True
        assert item["afk_run_id"] is None  # unresolved state

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/correlations")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_returns_ambiguous_entries_with_reason_and_candidates(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_correlation_row(
                    entity_type="afk_run",
                    external_id=_RUN_ID,
                    afk_run_id=_RUN_ID,
                    method="ambiguous",
                    reason="ambiguous",
                    correlation_confidence=0.0,
                    candidates=["change_request:300", "change_request:310"],
                )
            ]
        )

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/correlations")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["reason"] == "ambiguous"
        assert item["candidates"] == ["change_request:300", "change_request:310"]
        assert item["entity_id"] == f"afk_run:{_RUN_ID}"
        assert item["provisional"] is True

    @pytest.mark.asyncio
    async def test_unmatched_entries_have_empty_candidates(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_correlation_row(
                    entity_type="afk_run",
                    external_id=_RUN_ID,
                    afk_run_id=_RUN_ID,
                    method="unmatched",
                    reason="unmatched",
                    correlation_confidence=0.0,
                    candidates=[],
                )
            ]
        )

        async with client as c:
            response = await c.get("/api/v1/afk-outcomes/correlations")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["reason"] == "unmatched"
        assert item["candidates"] == []

    @pytest.mark.asyncio
    async def test_reason_filter_narrows_query(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/correlations", params={"reason": "ambiguous"}
            )

        assert response.status_code == 200
        count_sql = mock_conn.fetchval.call_args[0][0]
        assert "reason = $1" in count_sql

    @pytest.mark.asyncio
    async def test_invalid_reason_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/correlations", params={"reason": "bogus"}
            )

        assert response.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
#  Envelope + read-only shape
# ══════════════════════════════════════════════════════════════════════════


class TestEnvelopeAndReadOnly:
    """Envelope shape and read-only surface."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,setup",
        [
            ("/api/v1/afk-outcomes/runs", "list"),
            (f"/api/v1/afk-outcomes/runs/{_RUN_ID}", "detail"),
            ("/api/v1/afk-outcomes/entities", "list"),
            ("/api/v1/afk-outcomes/correlations", "list"),
        ],
    )
    async def test_envelope_shape(
        self, client: AsyncClient, mock_conn: AsyncMock, path: str, setup: str
    ):
        if setup == "detail":
            mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
            mock_conn.fetch = AsyncMock(side_effect=[[], []])
        else:
            mock_conn.fetchval = AsyncMock(return_value=0)
            mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(path)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_router_exposes_only_get_routes(self, mock_conn: AsyncMock):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        afk_paths = {
            path
            for path in app.openapi()["paths"]
            if path.startswith("/api/v1/afk-outcomes")
        }
        assert afk_paths, "no /api/v1/afk-outcomes paths exposed in the OpenAPI schema"
        methods = {
            method.upper()
            for path in afk_paths
            for method in app.openapi()["paths"][path]
        }
        assert methods == {"GET"}
