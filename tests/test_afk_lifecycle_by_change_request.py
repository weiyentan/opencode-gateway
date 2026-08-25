"""Tests for the change-request -> owning run lookup API (issue #597).

Covers ``GET /api/v1/afk/executions/runs/by-change-request`` — the read-only
control-path lookup that resolves a provider-qualified change-request
identity (GitHub PR / GitLab MR) to its owning ``afk_run_id`` via the
explicit durable binding on ``afk_runs``.

Tests exercise GitHub/GitLab lookup, repository normalization (``.git`` and
trailing-slash variants), provider/repository/number mismatches,
invalid/unknown/unbound requests, the compact response shape, read-only /
no-mutation behavior, deterministic reads, the 1:1 invariant (409), and
API-key read auth.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from tests.conftest import mock_row

_RUN_ID = "01JZABCDEFGHJKLMNPQRSTVWXY"


def _mk_conn() -> AsyncMock:
    return AsyncMock()


def _lookup_url(
    *,
    provider: str = "gitlab",
    repository: str = "https://gitlab.com/cloudnative-pg/cloudnative-pg",
    external_id: str = "6",
) -> str:
    qs = urlencode(
        {"provider": provider, "repository": repository, "external_id": external_id}
    )
    return f"/api/v1/afk/executions/runs/by-change-request?{qs}"


class TestGetRunByChangeRequest:
    """GET /runs/by-change-request — resolve change request to owning run."""

    @pytest.mark.asyncio
    async def test_lookup_returns_owning_run(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn)

        resp = await client.get(_lookup_url())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        body = data["data"]
        assert body["afk_run_id"] == _RUN_ID
        assert len(body["afk_run_id"]) == 26  # ULID length
        cr = body["change_request"]
        assert cr["provider"] == "gitlab"
        assert cr["repository"] == "gitlab.com/cloudnative-pg/cloudnative-pg"
        assert cr["resource_type"] == "change_request"
        assert cr["resource_number"] == "6"

    @pytest.mark.asyncio
    async def test_lookup_github_pr(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn)

        resp = await client.get(
            _lookup_url(
                provider="github",
                repository="https://github.com/acme/proj",
                external_id="42",
            )
        )
        assert resp.status_code == 200
        cr = resp.json()["data"]["change_request"]
        assert cr["provider"] == "github"
        assert cr["repository"] == "github.com/acme/proj"
        assert cr["resource_number"] == "42"

    @pytest.mark.asyncio
    async def test_lookup_normalizes_repository(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn)

        # .git suffix and trailing slash both normalize to the same identity.
        resp = await client.get(
            _lookup_url(
                repository="https://gitlab.com/cloudnative-pg/cloudnative-pg.git/"
            )
        )
        assert resp.status_code == 200
        cr = resp.json()["data"]["change_request"]
        assert cr["repository"] == "gitlab.com/cloudnative-pg/cloudnative-pg"

    @pytest.mark.asyncio
    async def test_lookup_unknown_returns_404(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        resp = await client.get(_lookup_url(external_id="999"))
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_lookup_invalid_provider_returns_400(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        resp = await client.get(_lookup_url(provider="bitbucket"))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_lookup_invalid_repository_returns_400(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        resp = await client.get(_lookup_url(repository="not-a-url"))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_lookup_empty_external_id_returns_400(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        resp = await client.get(_lookup_url(external_id=""))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_lookup_ownership_conflict_returns_409(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(
            return_value=[
                mock_row({"afk_run_id": _RUN_ID}),
                mock_row({"afk_run_id": "01HOTHER000000000000000001"}),
            ]
        )
        client = create_client(conn)

        resp = await client.get(_lookup_url())
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_lookup_is_read_only(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn)

        resp = await client.get(_lookup_url())
        assert resp.status_code == 200
        # Only a SELECT was issued — no writes.
        assert conn.execute.call_count == 0
        assert conn.fetchrow.call_count == 0

    @pytest.mark.asyncio
    async def test_lookup_deterministic_repeated_reads(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn)

        first = await client.get(_lookup_url())
        second = await client.get(_lookup_url())
        assert first.status_code == 200
        assert first.json() == second.json()

    @pytest.mark.asyncio
    async def test_lookup_requires_api_key(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _RUN_ID})])
        client = create_client(conn, api_key=None)

        resp = await client.get(_lookup_url())
        assert resp.status_code in (401, 403)
