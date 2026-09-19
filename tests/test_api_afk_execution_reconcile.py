# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the AFK AWX execution reconciliation trigger endpoint (issue #637).

``POST /admin/afk-executions/reconcile`` runs one bounded reconciliation
pass over execution bindings stuck in ``running``:

- Terminal outcomes discovered from AWX are persisted through the
  existing terminal-update path (failed → ``failed``, successful →
  ``completed``, canceled → ``cancelled``).
- Missing AWX jobs and lookup failures are reported gracefully — never a
  crash, never leaked credentials.
- An unconfigured AWX integration responds gracefully (no discovery, no
  writes).
- Repeated runs over already-terminal records are no-ops.
- Responses never contain AWX credentials.

Auth: the global Admin API Key middleware protects the route (an
operator-triggered admin action, mirroring the usage reconciliation
endpoint).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session
from tests.conftest import create_client
from tests.test_afk_execution_reconciliation import (  # noqa: F401
    FakeLookup,
    FakeRepository,
    _running_binding,
)
from afk_outcomes.service.execution_reconciliation import AWXJobState

from app.api import admin_afk_reconcile

_FINISHED = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_SECRET = "super-secret-awx-bearer-token"


def _build_client(
    repository: FakeRepository,
    awx_lookup: object | None,
) -> AsyncClient:
    """Build the app with the reconciliation seams overridden by fakes."""
    app: FastAPI = create_app(configure_logging=False)
    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override_get_session(request):
        yield AsyncMock()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[
        admin_afk_reconcile._provide_repository
    ] = lambda: repository
    app.dependency_overrides[
        admin_afk_reconcile._provide_awx_lookup
    ] = lambda: awx_lookup

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_reconcile_endpoint_persists_failed_outcome():
    repo = FakeRepository(running_bindings=[_running_binding(101)])
    lookup = FakeLookup(jobs={101: AWXJobState(status="failed", finished_at=_FINISHED)})
    client = _build_client(repo, lookup)

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    data = body["data"]
    assert data["configured"] is True
    assert data["examined"] == 1
    assert data["updated"] == 1
    assert data["conflicts"] == 0
    assert data["results"][0]["result"] == "updated"
    assert data["results"][0]["outcome"] == "failed"
    assert len(repo.update_calls) == 1
    assert repo.update_calls[0]["outcome"].value == "failed"


async def test_reconcile_endpoint_missing_job_is_graceful():
    repo = FakeRepository(running_bindings=[_running_binding(404)])
    lookup = FakeLookup(jobs={})  # AWX knows no such job
    client = _build_client(repo, lookup)

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["missing_job"] == 1
    assert data["updated"] == 0
    assert repo.update_calls == []


async def test_reconcile_endpoint_lookup_failure_is_reported_and_redacted():
    class Boom(Exception):
        pass

    class LeakyLookup:
        async def get_job(self, job_id: int) -> AWXJobState | None:
            raise Boom(f"AWX request failed with Authorization: Bearer {_SECRET}")

    repo = FakeRepository(running_bindings=[_running_binding(9)])
    client = _build_client(repo, LeakyLookup())

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["lookup_errors"] == 1
    assert data["results"][0]["detail"] == "Boom"
    assert _SECRET not in resp.text


async def test_reconcile_endpoint_already_terminal_records_are_noop():
    repo = FakeRepository()  # nothing running — already-terminal history
    lookup = FakeLookup()
    client = _build_client(repo, lookup)

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["examined"] == 0
    assert data["updated"] == 0
    assert repo.update_calls == []


# ── Configuration ────────────────────────────────────────────────────────────


async def test_reconcile_endpoint_unconfigured_awx_is_graceful():
    repo = FakeRepository(running_bindings=[_running_binding(1)])
    client = _build_client(repo, None)  # AWX integration not configured

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["configured"] is False
    assert data["examined"] == 0
    assert repo.update_calls == []


# ── Auth ─────────────────────────────────────────────────────────────────────


async def test_reconcile_endpoint_requires_api_key():
    client = create_client(AsyncMock(), api_key=None)

    resp = await client.post("/admin/afk-executions/reconcile")

    assert resp.status_code == 401


# ── Credential hygiene on the production client construction ────────────────


async def test_provide_awx_lookup_none_when_base_url_unset(monkeypatch):
    monkeypatch.setenv("GATEWAY_AWX_RECONCILIATION_BASE_URL", "")
    # get_settings() is uncached — each call reads current env.
    gen = admin_afk_reconcile._provide_awx_lookup()
    value = await gen.__anext__()
    assert value is None


async def test_provide_awx_lookup_none_when_token_empty(monkeypatch):
    monkeypatch.setenv("GATEWAY_AWX_RECONCILIATION_BASE_URL", "https://awx.example.com")
    monkeypatch.setenv("GATEWAY_AWX_RECONCILIATION_API_TOKEN", "")
    gen = admin_afk_reconcile._provide_awx_lookup()
    value = await gen.__anext__()
    assert value is None
