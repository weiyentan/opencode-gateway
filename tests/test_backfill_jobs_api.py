"""Tests for the API-triggered AFK backfill endpoints.

Covers the dedicated-key authentication contract (disabled → 503, wrong key
→ 401), request validation (required ``from``/``until``, max 31-day window,
inverted window, rejected credential fields), the synchronous dry-run path
(reused ``run_backfill``, no outcome writes, completed audit row), the
asynchronous write path (202 + durable job id), job status reads, paginated
listing, and queued-only cancellation.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from afk_outcomes.models import Provider
from scripts.afk_backfill import BackfillReport

_BACKFILL_KEY = "test-backfill-key"
os.environ.setdefault("GATEWAY_BACKFILL_API_KEY", _BACKFILL_KEY)
os.environ.setdefault("GATEWAY_BACKFILL_API_KEY_LABEL", "test-backfill-label")

_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)  # noqa: UP017
_UNTIL = datetime(2026, 8, 8, tzinfo=timezone.utc)  # noqa: UP017
_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_BODY = {
    "provider": "github",
    "repository": "acme/proj",
    "from": _FROM.isoformat(),
    "until": _UNTIL.isoformat(),
}


def _key_headers() -> dict[str, str]:
    return {"X-Backfill-Key": _BACKFILL_KEY}


def _job_row(**overrides: object) -> dict:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "status": "queued",
        "provider": "github",
        "repository": "acme/proj",
        "window_from": _FROM,
        "window_until": _UNTIL,
        "dry_run": False,
        "show_evidence": False,
        "requested_by": "test-backfill-label",
        "retry_count": 0,
        "failure_category": None,
        "failure_message": None,
        "evidence": None,
        "change_requests_scanned": None,
        "issues_scanned": None,
        "sessions_considered": None,
        "explicit_matches": None,
        "high_matches": None,
        "inferred_matches": None,
        "ambiguous": None,
        "unmatched": None,
        "created_at": _NOW,
        "started_at": None,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _completed_row(*, dry_run: bool = False) -> dict:
    return _job_row(
        status="completed",
        dry_run=dry_run,
        completed_at=_NOW,
        change_requests_scanned=3,
        issues_scanned=2,
        sessions_considered=1,
        explicit_matches=0,
        high_matches=1,
        inferred_matches=0,
        ambiguous=0,
        unmatched=0,
    )


def _report() -> BackfillReport:
    return BackfillReport(
        provider=Provider.GITHUB,
        repository="acme/proj",
        since=_FROM,
        until=_UNTIL,
        dry_run=True,
        change_requests_scanned=3,
        issues_scanned=2,
        sessions_considered=1,
        explicit_matches=0,
        high_matches=1,
        inferred_matches=0,
        ambiguous=0,
        unmatched=0,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Authentication
# ═══════════════════════════════════════════════════════════════════════════


async def test_endpoint_disabled_without_backfill_key(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_BACKFILL_API_KEY")
    resp = await client.post(
        "/api/v1/backfill/jobs", json=_BODY, headers={"X-Backfill-Key": "anything"}
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


async def test_rejects_wrong_backfill_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/backfill/jobs", json=_BODY, headers={"X-Backfill-Key": "wrong"}
    )
    assert resp.status_code == 401


async def test_rejects_missing_backfill_key_header(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/backfill/jobs", json=_BODY)
    assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
#  Request validation
# ═══════════════════════════════════════════════════════════════════════════


async def test_requires_from_and_until(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/backfill/jobs",
        json={"provider": "github", "repository": "acme/proj"},
        headers=_key_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_rejects_window_longer_than_max(client: AsyncClient) -> None:
    body = {
        **_BODY,
        "from": "2026-07-01T00:00:00Z",
        "until": "2026-08-20T00:00:00Z",  # 50 days
    }
    resp = await client.post("/api/v1/backfill/jobs", json=body, headers=_key_headers())
    assert resp.status_code == 400
    assert "exceeds the maximum" in resp.json()["error"]["message"]


async def test_rejects_inverted_window(client: AsyncClient) -> None:
    body = {**_BODY, "from": _UNTIL.isoformat(), "until": _FROM.isoformat()}
    resp = await client.post("/api/v1/backfill/jobs", json=body, headers=_key_headers())
    assert resp.status_code == 422


async def test_rejects_provider_credentials_in_body(client: AsyncClient) -> None:
    body = {**_BODY, "github_token": "ghp_secret"}
    resp = await client.post("/api/v1/backfill/jobs", json=body, headers=_key_headers())
    assert resp.status_code == 422
    assert "github_token" not in str(resp.json())


async def test_accepts_z_suffix_and_naive_timestamps(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = _job_row()
    body = {
        "provider": "github",
        "repository": "acme/proj",
        "from": "2026-08-01T00:00:00Z",
        "until": "2026-08-05T00:00:00",  # naive → assumed UTC
        "dry_run": False,
    }
    resp = await client.post("/api/v1/backfill/jobs", json=body, headers=_key_headers())
    assert resp.status_code == 202


# ═══════════════════════════════════════════════════════════════════════════
#  Write path (async, durable)
# ═══════════════════════════════════════════════════════════════════════════


async def test_write_request_returns_202_and_job_id(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = _job_row()
    resp = await client.post(
        "/api/v1/backfill/jobs",
        json={**_BODY, "dry_run": False},
        headers=_key_headers(),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "queued"
    assert data["job_id"]
    assert data["requested_by"] == "test-backfill-label"
    assert resp.headers["location"].endswith(f"/api/v1/backfill/jobs/{data['job_id']}")


# ═══════════════════════════════════════════════════════════════════════════
#  Dry-run path (synchronous, reuses run_backfill)
# ═══════════════════════════════════════════════════════════════════════════


async def test_dry_run_executes_synchronously_and_returns_report(
    client: AsyncClient, mock_conn
) -> None:
    mock_conn.fetchrow.side_effect = [_job_row(dry_run=True), _completed_row(dry_run=True)]
    adapter = AsyncMock()
    adapter_client = AsyncMock()
    adapter_client.aclose = AsyncMock()
    with (
        patch("app.api.backfill_jobs.run_backfill", new=AsyncMock(return_value=_report())),
        patch(
            "app.api.backfill_jobs._build_adapter",
            return_value=(adapter, adapter_client),
        ),
    ):
        resp = await client.post(
            "/api/v1/backfill/jobs",
            json={**_BODY, "dry_run": True},
            headers=_key_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["report"]["change_requests_scanned"] == 3
    assert data["report"]["issues_scanned"] == 2
    assert data["report"]["high_matches"] == 1
    assert data["dry_run"] is True


async def test_dry_run_calls_run_backfill_with_dry_run_true(
    client: AsyncClient, mock_conn
) -> None:
    mock_conn.fetchrow.side_effect = [_job_row(dry_run=True), _completed_row(dry_run=True)]
    run_backfill_mock = AsyncMock(return_value=_report())
    with (
        patch("app.api.backfill_jobs.run_backfill", new=run_backfill_mock),
        patch(
            "app.api.backfill_jobs._build_adapter",
            return_value=(AsyncMock(), AsyncMock()),
        ),
    ):
        await client.post(
            "/api/v1/backfill/jobs",
            json={**_BODY, "dry_run": True, "show_evidence": True},
            headers=_key_headers(),
        )
    call_kwargs = run_backfill_mock.await_args.kwargs
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["show_evidence"] is True
    assert call_kwargs["repository"] == "acme/proj"


# ═══════════════════════════════════════════════════════════════════════════
#  Status / list / cancel
# ═══════════════════════════════════════════════════════════════════════════


async def test_get_job_returns_status(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = _job_row(status="running", started_at=_NOW)
    resp = await client.get(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000001",
        headers=_key_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"


async def test_get_job_404(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = None
    resp = await client.get(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000009",
        headers=_key_headers(),
    )
    assert resp.status_code == 404


async def test_get_completed_job_attaches_report(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = _completed_row()
    resp = await client.get(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000001",
        headers=_key_headers(),
    )
    data = resp.json()["data"]
    assert data["report"]["explicit_matches"] == 0
    assert data["report"]["ambiguous"] == 0


async def test_list_jobs_is_paginated(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchval.return_value = 2
    mock_conn.fetch.return_value = [_job_row(), _completed_row()]
    resp = await client.get(
        "/api/v1/backfill/jobs?status=completed&limit=10", headers=_key_headers()
    )
    data = resp.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2


async def test_list_jobs_rejects_invalid_status(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/backfill/jobs?status=bogus", headers=_key_headers()
    )
    assert resp.status_code == 400


async def test_cancel_queued_job(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.side_effect = [
        _job_row(),
        _job_row(status="cancelled", completed_at=_NOW),
    ]
    resp = await client.post(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000001/cancel",
        headers=_key_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"


async def test_cancel_running_job_conflicts(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.side_effect = [_job_row(status="running"), None]
    resp = await client.post(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000001/cancel",
        headers=_key_headers(),
    )
    assert resp.status_code == 409
    assert "queued" in resp.json()["error"]["message"]


async def test_cancel_unknown_job_404(client: AsyncClient, mock_conn) -> None:
    mock_conn.fetchrow.return_value = None
    resp = await client.post(
        "/api/v1/backfill/jobs/00000000-0000-0000-0000-000000000009/cancel",
        headers=_key_headers(),
    )
    assert resp.status_code == 404


async def test_dry_run_bounded_evidence_is_persisted(client: AsyncClient, mock_conn) -> None:
    report = _report()
    report.evidence_lines = ["match change_request:1 method=explicit_run_id"]
    completed = _completed_row(dry_run=True)
    completed["evidence"] = ["match change_request:1 method=explicit_run_id"]
    completed["show_evidence"] = True
    mock_conn.fetchrow.side_effect = [
        _job_row(dry_run=True, show_evidence=True),
        completed,
    ]
    with (
        patch("app.api.backfill_jobs.run_backfill", new=AsyncMock(return_value=report)),
        patch(
            "app.api.backfill_jobs._build_adapter",
            return_value=(AsyncMock(), AsyncMock()),
        ),
    ):
        resp = await client.post(
            "/api/v1/backfill/jobs",
            json={**_BODY, "dry_run": True, "show_evidence": True},
            headers=_key_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["evidence"] == completed["evidence"]


async def test_window_boundary_exactly_max_days_is_accepted(
    client: AsyncClient, mock_conn
) -> None:
    mock_conn.fetchrow.return_value = _job_row()
    body = {
        **_BODY,
        "from": "2026-07-01T00:00:00Z",
        "until": "2026-08-01T00:00:00Z",  # exactly 31 days
    }
    resp = await client.post(
        "/api/v1/backfill/jobs", json={**body, "dry_run": False}, headers=_key_headers()
    )
    assert resp.status_code == 202


async def test_rejects_unknown_provider(client: AsyncClient) -> None:
    body = {**_BODY, "provider": "bitbucket"}
    resp = await client.post("/api/v1/backfill/jobs", json=body, headers=_key_headers())
    assert resp.status_code == 422
