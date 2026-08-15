"""Tests for the reporting read-only REST API (issue #484).

Covers the three GET endpoints under ``/api/v1/reporting``:

- ``GET /resources``         — list ingested resources (paginated), filterable
  by stable resource identity, each carrying its current aggregate.
- ``GET /resources/detail``  — full detail for one resource addressed by the
  four identity components: aggregate + per-delivery state trail + session
  links.
- ``GET /session-links``     — the session links that currently exist
  (``afk_run_sessions``), surfaced as provisional/inferred with empty
  ``source_references`` until exact correlation (#481) lands.

Also covers envelope shape, 401 for unauthenticated requests, empty results,
read-only enforcement (only GET routes), and the absence of any
completion/finished/outcome claims in resource response shapes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from tests.conftest import mock_row

# ── Shared test data ────────────────────────────────────────────────────────

_RUN_ID = "01J8ABCDEFGHJKMNPQRSTVWXYZ"
_SESSION_ID = uuid.uuid4()

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_RESOURCE_ID = "github:https://github.com/acme/backend:issue:42"

# Resource identity components (stable resource identity).
_PROVIDER = "github"
_REPOSITORY_URL = "https://github.com/acme/backend"
_RESOURCE_TYPE = "issue"
_RESOURCE_NUMBER = "42"

# Keys that would constitute a completion/finished/outcome claim on a resource.
_COMPLETION_CLAIM_KEYS = frozenset(
    {
        "completed",
        "completion",
        "finished",
        "finished_at",
        "done",
        "outcome",
        "outcome_status",
        "succeeded",
    }
)


def _assert_no_completion_claims(value: object, *, path: str = "data") -> None:
    """Recursively assert no completion/finished/outcome keys are present."""
    if isinstance(value, dict):
        for key, child in value.items():
            assert key.lower() not in _COMPLETION_CLAIM_KEYS, (
                f"completion claim key {key!r} found at {path}"
            )
            _assert_no_completion_claims(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _assert_no_completion_claims(child, path=f"{path}[{i}]")


# ── Mock row builders ───────────────────────────────────────────────────────


def _mk_resource_row(
    *,
    provider: str = _PROVIDER,
    repository_url: str = _REPOSITORY_URL,
    resource_type: str = _RESOURCE_TYPE,
    resource_number: str = _RESOURCE_NUMBER,
    delivery_count: int = 3,
    last_delivery_id: str = "delivery-003",
    last_ingested_at: datetime | None = _B_TS,
    payload: dict | None = None,
):
    return mock_row(
        {
            "provider": provider,
            "repository_url": repository_url,
            "resource_type": resource_type,
            "resource_number": resource_number,
            "delivery_count": delivery_count,
            "last_delivery_id": last_delivery_id,
            "last_ingested_at": last_ingested_at,
            "payload": payload
            if payload is not None
            else {
                "resource": {
                    "repository_url": repository_url,
                    "resource_type": resource_type,
                    "resource_number": resource_number,
                },
                "title": "Fix login bug",
            },
        }
    )


def _mk_trail_row(
    *,
    provider: str = _PROVIDER,
    delivery_id: str = "delivery-003",
    state: str = "persisted",
    occurred_at: datetime | None = _A_TS,
    detail: dict | None = None,
    created_at: datetime | None = _B_TS,
):
    return mock_row(
        {
            "provider": provider,
            "delivery_id": delivery_id,
            "state": state,
            "occurred_at": occurred_at,
            "detail": detail,
            "created_at": created_at,
        }
    )


def _mk_session_link_row(
    *,
    afk_run_id: str | None = _RUN_ID,
    session_id: uuid.UUID | None = _SESSION_ID,
    external_session_id: str | None = "ses_abc123",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = _B_TS,
    agent: str | None = "code-editor",
):
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "session_id": session_id,
            "external_session_id": external_session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "agent": agent,
        }
    )


def _identity_params() -> dict[str, str]:
    return {
        "provider": _PROVIDER,
        "repository_url": _REPOSITORY_URL,
        "resource_type": _RESOURCE_TYPE,
        "resource_number": _RESOURCE_NUMBER,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """All three endpoints require API-key auth and return the 401 envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/reporting/resources",
            "/api/v1/reporting/session-links",
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

    @pytest.mark.asyncio
    async def test_detail_requires_auth(self, mock_conn: AsyncMock):
        from httpx import ASGITransport

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  List resources
# ══════════════════════════════════════════════════════════════════════════


class TestListResources:
    """Tests for GET /api/v1/reporting/resources."""

    @pytest.mark.asyncio
    async def test_returns_paginated_resources(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_resource_row()])

        async with client as c:
            response = await c.get("/api/v1/reporting/resources")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["resource_id"] == _RESOURCE_ID
        assert item["provider"] == _PROVIDER
        assert item["repository_url"] == _REPOSITORY_URL
        assert item["resource_type"] == _RESOURCE_TYPE
        assert item["resource_number"] == _RESOURCE_NUMBER
        assert item["delivery_count"] == 3
        assert item["last_delivery_id"] == "delivery-003"
        assert item["last_ingested_at"] is not None
        assert item["payload"]["title"] == "Fix login bug"
        _assert_no_completion_claims(item)

    @pytest.mark.asyncio
    async def test_filters_by_stable_identity(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources", params=_identity_params()
            )

        assert response.status_code == 200
        sql = mock_conn.fetch.call_args[0][0]
        # The identity filter is parameterised against the JSONB resource object.
        assert "d.provider = $1" in sql
        assert "$2" in sql  # repository_url
        assert "$3" in sql  # resource_type
        assert "$4" in sql  # resource_number

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/reporting/resources")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Resource detail
# ══════════════════════════════════════════════════════════════════════════


class TestResourceDetail:
    """Tests for GET /api/v1/reporting/resources/detail."""

    @pytest.mark.asyncio
    async def test_returns_aggregate_state_trail_and_links(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_resource_row())
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_trail_row(state="received"),
                _mk_trail_row(state="persisted"),
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 200
        data = response.json()["data"]

        # Aggregate (current resource state, no completion claims).
        resource = data["resource"]
        assert resource["resource_id"] == _RESOURCE_ID
        assert resource["delivery_count"] == 3
        _assert_no_completion_claims(resource)

        # Per-delivery state trail.
        assert len(data["state_trail"]) == 2
        assert data["state_trail"][0]["state"] == "received"
        assert data["state_trail"][1]["state"] == "persisted"
        assert data["state_trail"][0]["delivery_id"] == "delivery-003"

        # Session links: no provable resource→session link exists yet (#481
        # absent) — the field is present but empty, never fabricated.
        assert data["session_links"] == []

    @pytest.mark.asyncio
    async def test_unknown_resource_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_missing_identity_param_returns_422(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail",
                params={"provider": _PROVIDER},
            )

        assert response.status_code == 422
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_state_trail_is_chronological(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_resource_row())
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 200
        # The state-trail query orders chronologically by occurred_at.
        trail_sql = mock_conn.fetch.call_args[0][0]
        assert "ORDER BY" in trail_sql


# ══════════════════════════════════════════════════════════════════════════
#  Session links
# ══════════════════════════════════════════════════════════════════════════


class TestSessionLinks:
    """Tests for GET /api/v1/reporting/session-links."""

    @pytest.mark.asyncio
    async def test_returns_provisional_links_with_empty_source_references(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_session_link_row()])

        async with client as c:
            response = await c.get("/api/v1/reporting/session-links")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["external_session_id"] == "ses_abc123"
        assert item["afk_run_id"] == _RUN_ID
        assert item["agent"] == "code-editor"
        # Exact correlation (#481) is not yet implemented: every link is
        # provisional with no source references — never silently exact.
        assert item["provisional"] is True
        assert item["source_references"] == []

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/reporting/session-links")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  JSONB decoding (asyncpg returns JSONB columns as JSON strings)
# ══════════════════════════════════════════════════════════════════════════


class TestJsonbDecoding:
    """The read path must parse asyncpg's string-encoded JSONB columns.

    asyncpg returns JSONB columns as JSON-encoded ``str`` (no type codec is
    registered in this codebase).  The unit-test mock rows elsewhere return
    already-decoded dicts; these tests feed the *string* shape and assert
    the endpoint returns parsed objects rather than a 500.
    """

    _STR_PAYLOAD = (
        '{"resource": {"repository_url": "https://github.com/acme/backend", '
        '"resource_type": "issue", "resource_number": "42"}, '
        '"title": "Fix login bug"}'
    )

    @pytest.mark.asyncio
    async def test_list_resources_parses_string_payload(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(
            return_value=[_mk_resource_row(payload=self._STR_PAYLOAD)]
        )

        async with client as c:
            response = await c.get("/api/v1/reporting/resources")

        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["payload"]["title"] == "Fix login bug"
        _assert_no_completion_claims(item)

    @pytest.mark.asyncio
    async def test_resource_detail_parses_string_payload_and_detail(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_resource_row(payload=self._STR_PAYLOAD)
        )
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_trail_row(state="rejected", detail='{"reason": "boom"}')
            ]
        )

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["resource"]["payload"]["title"] == "Fix login bug"
        assert data["state_trail"][0]["detail"]["reason"] == "boom"

    @pytest.mark.asyncio
    async def test_null_detail_passes_through(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=_mk_resource_row())
        mock_conn.fetch = AsyncMock(return_value=[_mk_trail_row(detail=None)])

        async with client as c:
            response = await c.get(
                "/api/v1/reporting/resources/detail", params=_identity_params()
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["state_trail"][0]["detail"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Envelope + read-only shape
# ══════════════════════════════════════════════════════════════════════════


class TestEnvelopeAndReadOnly:
    """Envelope shape and read-only surface."""

    @pytest.mark.asyncio
    async def test_envelope_shape(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/reporting/resources")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_router_exposes_only_get_routes(self, mock_conn: AsyncMock):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        reporting_paths = {
            path
            for path in app.openapi()["paths"]
            if path.startswith("/api/v1/reporting")
            and not path.startswith("/api/v1/reporting/ingest")
        }
        assert reporting_paths, "no /api/v1/reporting read paths exposed in OpenAPI"
        methods = {
            method.upper()
            for path in reporting_paths
            for method in app.openapi()["paths"][path]
        }
        assert methods == {"GET"}
