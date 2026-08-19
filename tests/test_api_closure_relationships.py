"""Tests for the closure-relationships read-only REST API (issue #525).

Covers the three GET endpoints under ``/api/v1/closure-relationships``:

- ``GET /issues/current``              — the current issue→change-request
  answer (episode status + single-candidate attribution or
  unmatched/ambiguous marker + evidence links).
- ``GET /issues/episodes``             — auditable episode/evidence history
  (every immutable episode including ``superseded``, declaration/revocation
  link states, unresolved records).
- ``GET /change-requests/issues``      — reverse lookup: the issues a change
  request references and/or declares closing (paginated).

Also covers envelope shape, 401 for unauthenticated requests, 400s for
invalid enum/date filter values, and read-only surface (GET only).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.conftest import mock_row

_TS_A = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_TS_B = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_TS_C = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_ISSUE_PARAMS = {
    "provider": "gitlab",
    "repository": "cloudnative-pg",
    "external_id": "1",
}

_CR_PARAMS = {
    "provider": "gitlab",
    "repository": "cloudnative-pg",
    "external_id": "6",
}


# ── Mock row builders ────────────────────────────────────────────────────────


def _mk_episode_row(
    *,
    issue_provider: str = "gitlab",
    issue_repository: str = "cloudnative-pg",
    issue_external_id: str = "1",
    opened_at: datetime | None = _TS_A,
    closed_at: datetime | None = _TS_B,
    status: str = "inferred",
    change_request_provider: str | None = "gitlab",
    change_request_repository: str | None = "cloudnative-pg",
    change_request_external_id: str | None = "6",
    resolver_version: str | None = "1",
    derived_at: datetime = _TS_C,
    superseded_at: datetime | None = None,
):
    return mock_row(
        {
            "issue_provider": issue_provider,
            "issue_repository": issue_repository,
            "issue_external_id": issue_external_id,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "status": status,
            "change_request_provider": change_request_provider,
            "change_request_repository": change_request_repository,
            "change_request_external_id": change_request_external_id,
            "resolver_version": resolver_version,
            "derived_at": derived_at,
            "superseded_at": superseded_at,
        }
    )


def _mk_link_row(
    *,
    change_request_provider: str = "gitlab",
    change_request_repository: str = "cloudnative-pg",
    change_request_external_id: str = "6",
    issue_provider: str = "gitlab",
    issue_repository: str = "cloudnative-pg",
    issue_external_id: str = "1",
    kind: str = "declares_closure",
    state: str = "active",
    revoked_at: datetime | None = None,
    resolver_version: str | None = "1",
    derived_at: datetime = _TS_C,
):
    return mock_row(
        {
            "change_request_provider": change_request_provider,
            "change_request_repository": change_request_repository,
            "change_request_external_id": change_request_external_id,
            "issue_provider": issue_provider,
            "issue_repository": issue_repository,
            "issue_external_id": issue_external_id,
            "kind": kind,
            "state": state,
            "revoked_at": revoked_at,
            "resolver_version": resolver_version,
            "derived_at": derived_at,
        }
    )


def _mk_unresolved_row(
    *,
    issue_provider: str = "gitlab",
    issue_repository: str = "cloudnative-pg",
    issue_external_id: str = "1",
    closed_at: datetime = _TS_B,
    reason: str = "ambiguous",
    candidates: list | None = None,
    resolver_version: str | None = "1",
    derived_at: datetime = _TS_C,
):
    return mock_row(
        {
            "issue_provider": issue_provider,
            "issue_repository": issue_repository,
            "issue_external_id": issue_external_id,
            "closed_at": closed_at,
            "reason": reason,
            "candidates": (
                candidates
                if candidates is not None
                else [
                    {"provider": "gitlab", "repository": "cloudnative-pg", "external_id": "6"}
                ]
            ),
            "resolver_version": resolver_version,
            "derived_at": derived_at,
        }
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """All three endpoints require API-key auth and return the 401 envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/closure-relationships/issues/current",
            "/api/v1/closure-relationships/issues/episodes",
            "/api/v1/closure-relationships/change-requests/issues",
        ],
    )
    async def test_requires_auth(self, mock_conn: AsyncMock, path: str):
        from httpx import ASGITransport

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(path, params=_ISSUE_PARAMS)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  Current issue→change-request answer
# ══════════════════════════════════════════════════════════════════════════


class TestCurrentAnswer:
    """Tests for GET /api/v1/closure-relationships/issues/current."""

    @pytest.mark.asyncio
    async def test_returns_inferred_answer_with_evidence(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_episode_row(status="inferred"))
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [_mk_link_row(kind="declares_closure", state="active")],
                [],
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["issue_provider"] == "gitlab"
        assert data["issue_repository"] == "cloudnative-pg"
        assert data["issue_external_id"] == "1"
        assert data["episode"]["status"] == "inferred"
        assert data["episode"]["change_request_external_id"] == "6"
        assert data["episode"]["resolver_version"] == "1"
        assert data["episode"]["derived_at"] is not None
        assert data["evidence"][0]["kind"] == "declares_closure"
        assert data["evidence"][0]["state"] == "active"
        # Freshness: every projection response exposes derived_at + resolver_version
        assert data["derived_at"] is not None
        assert data["resolver_version"] == "1"

    @pytest.mark.asyncio
    async def test_envelope_shape(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_episode_row())
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,cr_provider,cr_repository,cr_external_id",
        [
            ("pending", None, None, None),
            ("awaiting_closure", None, None, None),
            ("unmatched", None, None, None),
            ("ambiguous", None, None, None),
            ("inferred", "github", "acme/proj", "42"),
        ],
    )
    async def test_surfaces_each_status_vocabulary_value(
        self,
        client: AsyncClient,
        mock_conn: AsyncMock,
        status: str,
        cr_provider: str | None,
        cr_repository: str | None,
        cr_external_id: str | None,
    ):
        """The current-answer endpoint surfaces every ClosureEpisodeStatus
        value of the per-issue current episode."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_episode_row(
                status=status,
                change_request_provider=cr_provider,
                change_request_repository=cr_repository,
                change_request_external_id=cr_external_id,
            )
        )
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        assert response.status_code == 200
        episode = response.json()["data"]["episode"]
        assert episode["status"] == status
        assert episode["change_request_external_id"] == cr_external_id

    @pytest.mark.asyncio
    async def test_ambiguous_episode_carries_candidates(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An ambiguous current episode surfaces the competing candidates from
        its unresolved record — never an arbitrary winner."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_episode_row(status="ambiguous", closed_at=_TS_B)
        )
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [],
                [
                    _mk_unresolved_row(
                        reason="ambiguous",
                        closed_at=_TS_B,
                        candidates=[
                            {
                                "provider": "gitlab",
                                "repository": "cloudnative-pg",
                                "external_id": "6",
                            },
                            {
                                "provider": "gitlab",
                                "repository": "cloudnative-pg",
                                "external_id": "7",
                            },
                        ],
                    )
                ],
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["episode"]["status"] == "ambiguous"
        assert [c["external_id"] for c in data["candidates"]] == ["6", "7"]

    @pytest.mark.asyncio
    async def test_unmatched_episode_has_empty_candidates(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_episode_row(status="unmatched", closed_at=_TS_B)
        )
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [],
                [_mk_unresolved_row(reason="unmatched", closed_at=_TS_B, candidates=[])],
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        data = response.json()["data"]
        assert data["episode"]["status"] == "unmatched"
        assert data["candidates"] == []

    @pytest.mark.asyncio
    async def test_superseded_episode_is_never_current(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """When a reopen/reclose cycle superseded an episode, the current
        endpoint returns the non-superseded episode (status is never
        ``superseded`` here — that value belongs to history)."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_episode_row(
                status="inferred",
                closed_at=_TS_C,
                change_request_external_id="9",
            )
        )
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        data = response.json()["data"]
        assert data["episode"]["status"] == "inferred"
        assert data["episode"]["superseded_at"] is None
        assert data["episode"]["change_request_external_id"] == "9"

    @pytest.mark.asyncio
    async def test_unknown_issue_returns_404(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current", params=_ISSUE_PARAMS
            )

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/current",
                params={**_ISSUE_PARAMS, "provider": "bitbucket"},
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"


# ══════════════════════════════════════════════════════════════════════════
#  Episode/evidence history
# ══════════════════════════════════════════════════════════════════════════


class TestEpisodeHistory:
    """Tests for GET /api/v1/closure-relationships/issues/episodes."""

    @pytest.mark.asyncio
    async def test_returns_all_episodes_including_superseded(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Superseded episodes are visible, never hidden."""
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [
                    _mk_episode_row(
                        status="superseded",
                        closed_at=_TS_B,
                        change_request_external_id="6",
                        superseded_at=_TS_C,
                    ),
                    _mk_episode_row(
                        status="inferred",
                        closed_at=_TS_C,
                        change_request_external_id="9",
                    ),
                ],
                [_mk_link_row(kind="declares_closure", state="active")],
                [],
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes", params=_ISSUE_PARAMS
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["episodes"]) == 2
        first, second = data["episodes"]
        assert first["status"] == "superseded"
        assert first["superseded_at"] is not None
        assert first["change_request_external_id"] == "6"
        assert second["status"] == "inferred"
        assert second["superseded_at"] is None
        # Evidence carries the declaration/revocation snapshot state.
        assert data["evidence"][0]["kind"] == "declares_closure"
        assert data["evidence"][0]["state"] == "active"
        # Freshness: response-level derived_at + resolver_version.
        assert data["derived_at"] is not None
        assert data["resolver_version"] == "1"

    @pytest.mark.asyncio
    async def test_unresolved_records_are_surfaced(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [_mk_episode_row(status="unmatched", closed_at=_TS_B)],
                [],
                [_mk_unresolved_row(reason="unmatched", closed_at=_TS_B, candidates=[])],
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes", params=_ISSUE_PARAMS
            )

        data = response.json()["data"]
        assert data["unresolved"][0]["reason"] == "unmatched"
        assert data["unresolved"][0]["candidates"] == []

    @pytest.mark.asyncio
    async def test_status_filter_narrows_query(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetch = AsyncMock(side_effect=[[_mk_episode_row(status="superseded")], [], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes",
                params={**_ISSUE_PARAMS, "status": "superseded"},
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args_list[0][0][0]
        assert "status = $4" in sql

    @pytest.mark.asyncio
    async def test_closed_window_filters_apply(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetch = AsyncMock(side_effect=[[_mk_episode_row()], [], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes",
                params={
                    **_ISSUE_PARAMS,
                    "closed_from": "2026-08-01T00:00:00Z",
                    "closed_to": "2026-08-31T00:00:00Z",
                },
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args_list[0][0][0]
        assert "closed_at >= $4" in sql
        assert "closed_at <= $5" in sql

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes",
                params={**_ISSUE_PARAMS, "status": "bogus"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_closed_from_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes",
                params={**_ISSUE_PARAMS, "closed_from": "not-a-date"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_inverted_window_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes",
                params={
                    **_ISSUE_PARAMS,
                    "closed_from": "2026-08-31T00:00:00Z",
                    "closed_to": "2026-08-01T00:00:00Z",
                },
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_issue_returns_404(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetch = AsyncMock(side_effect=[[], [], []])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/issues/episodes", params=_ISSUE_PARAMS
            )

        assert response.status_code == 404
        payload = response.json()
        assert payload["error"]["code"] == "NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════════
#  Reverse change-request→issues lookup
# ══════════════════════════════════════════════════════════════════════════


class TestReverseLookup:
    """Tests for GET /api/v1/closure-relationships/change-requests/issues."""

    @pytest.mark.asyncio
    async def test_returns_paginated_links(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=2)
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_link_row(kind="declares_closure", state="active"),
                _mk_link_row(
                    kind="references",
                    state="revoked",
                    issue_external_id="25",
                    revoked_at=_TS_B,
                ),
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/change-requests/issues",
                params=_CR_PARAMS,
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 2
        item = data["items"][0]
        assert item["change_request_external_id"] == "6"
        assert item["issue_external_id"] == "1"
        assert item["kind"] == "declares_closure"
        assert item["state"] == "active"
        assert item["derived_at"] is not None
        assert item["resolver_version"] == "1"
        revoked = data["items"][1]
        assert revoked["kind"] == "references"
        assert revoked["state"] == "revoked"
        assert revoked["revoked_at"] is not None

    @pytest.mark.asyncio
    async def test_kind_filter_narrows_query(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_link_row(kind="declares_closure")])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/change-requests/issues",
                params={**_CR_PARAMS, "kind": "declares_closure"},
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args_list[0][0][0]
        assert "kind = $4" in sql

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/change-requests/issues",
                params=_CR_PARAMS,
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_invalid_kind_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/change-requests/issues",
                params={**_CR_PARAMS, "kind": "bogus"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                "/api/v1/closure-relationships/change-requests/issues",
                params={**_CR_PARAMS, "provider": "bitbucket"},
            )

        assert response.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
#  Read-only surface
# ══════════════════════════════════════════════════════════════════════════


class TestReadOnlySurface:
    """The router exposes only GET routes — no write path."""

    @pytest.mark.asyncio
    async def test_router_exposes_only_get_routes(self, mock_conn: AsyncMock):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        closure_paths = {
            path
            for path in app.openapi()["paths"]
            if path.startswith("/api/v1/closure-relationships")
        }
        assert closure_paths, "no /api/v1/closure-relationships paths exposed"
        methods = {
            method.upper()
            for path in closure_paths
            for method in app.openapi()["paths"][path]
        }
        assert methods == {"GET"}


# ══════════════════════════════════════════════════════════════════════════
#  Metrics seam — projection recompute failures + last successful recompute
# ══════════════════════════════════════════════════════════════════════════


class TestProjectionMetricsSeam:
    """The registry snapshot seam surfaces the closure-projection recompute
    failure counter and last-success gauge (MetricsRegistry reset pattern)."""

    def test_registration_surfaces_zeroed_metrics(self):
        from app.core.metrics import (
            METRIC_CLOSURE_PROJECTION_RECOMPUTE_FAILURES,
            METRIC_CLOSURE_PROJECTION_RECOMPUTE_LAST_SUCCESS,
            MetricsRegistry,
            register_closure_projection_metrics,
        )

        registry = MetricsRegistry()
        register_closure_projection_metrics(registry)

        snapshot = registry.snapshot()
        assert snapshot[METRIC_CLOSURE_PROJECTION_RECOMPUTE_FAILURES] == 0
        assert snapshot[METRIC_CLOSURE_PROJECTION_RECOMPUTE_LAST_SUCCESS] == 0.0

    def test_failure_counter_and_last_success_gauge_reflect_updates(self):
        from app.core.metrics import (
            METRIC_CLOSURE_PROJECTION_RECOMPUTE_FAILURES,
            METRIC_CLOSURE_PROJECTION_RECOMPUTE_LAST_SUCCESS,
            MetricsRegistry,
            register_closure_projection_metrics,
        )

        registry = MetricsRegistry()
        register_closure_projection_metrics(registry)

        registry.counter(METRIC_CLOSURE_PROJECTION_RECOMPUTE_FAILURES).inc(2)
        registry.gauge(METRIC_CLOSURE_PROJECTION_RECOMPUTE_LAST_SUCCESS).set(
            1722688800.0
        )

        snapshot = registry.snapshot()
        assert snapshot[METRIC_CLOSURE_PROJECTION_RECOMPUTE_FAILURES] == 2
        assert snapshot[METRIC_CLOSURE_PROJECTION_RECOMPUTE_LAST_SUCCESS] == 1722688800.0

