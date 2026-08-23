"""Tests for the execution-binding REST API (issue #549).

Covers the three endpoints under ``/api/v1/afk/executions``:

- ``POST /executions``          — write path (idempotent, conflict, validation)
- ``GET /executions/{awx_job_id}`` — single-binding read (found / not-found)
- ``GET /executions``           — resource history (filtered, deterministic order)

Tests exercise both GitHub pull request and GitLab merge request resource
identities, the failed-to-successful retry flow, and the redaction guarantee
(no sensitive data in responses).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from tests.conftest import mock_row

# ── Test data ────────────────────────────────────────────────────────────────

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()


# ── Mock helpers ─────────────────────────────────────────────────────────────


def _auth_row() -> MagicMock:
    """Return a mock row that passes require_collector_token.

    The credential is attributable to the dedicated AWX execution-binding
    integration client (issue #550) — the only client accepted by the
    write path.
    """
    from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME

    return mock_row(
        {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": None,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": AWX_EXECUTION_BINDING_CLIENT_NAME,
            "client_is_active": True,
        }
    )


def _mk_binding_row(
    *,
    binding_id: str = "00000000-0000-0000-0000-000000000001",
    awx_job_id: int = 42,
    job_template_id: int = 7,
    external_session_id: str | None = "ses_abc123",
    provider: str = "github",
    repository_url: str = "github.com/acme/proj",
    entity_type: str = "change_request",
    entity_number: str = "99",
    outcome: str = "completed",
    source_event_id: str | None = None,
    afk_run_id: str | None = None,
    trigger_type: str | None = None,
    branch: str | None = None,
    title: str | None = None,
    failure_reason: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
):
    """Build a mock asyncpg Record for an execution_bindings row."""
    return mock_row(
        {
            "id": uuid.UUID(binding_id),
            "binding_id": binding_id,
            "awx_job_id": awx_job_id,
            "job_template_id": job_template_id,
            "external_session_id": external_session_id,
            "provider": provider,
            "repository_url": repository_url,
            "entity_type": entity_type,
            "entity_number": entity_number,
            "outcome": outcome,
            "source_event_id": source_event_id,
            "afk_run_id": afk_run_id,
            "trigger_type": trigger_type,
            "branch": branch,
            "title": title,
            "failure_reason": failure_reason,
            "started_at": started_at,
            "finished_at": finished_at,
        }
    )


def _create_write_client(mock_conn: AsyncMock) -> AsyncClient:
    """Build a client for write-path tests (passes both API key and collector token).

    The mock connection returns the auth row for require_collector_token
    lookups, followed by whatever side_effect is configured for business
    logic queries.
    """
    from tests.conftest import create_client

    return create_client(mock_conn)


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions — write path
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateExecutionBinding:
    """POST /api/v1/afk/executions — persist one final binding."""

    @pytest.mark.asyncio
    async def test_create_github_pr_binding(self) -> None:
        """Persist a valid GitHub pull request binding."""
        from tests.conftest import create_client

        conn = AsyncMock()
        saved_row = _mk_binding_row(awx_job_id=42)
        # Call sequence: auth lookup → atomic insert → re-read after save
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), saved_row]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
            "source_event_id": "evt_001",
            "branch": "feat/auth",
            "title": "Implement auth",
            "started_at": "2026-08-01T12:00:00Z",
            "finished_at": "2026-08-02T12:00:00Z",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["awx_job"]["job_id"] == "42"
        assert binding["resource"]["resource_type"] == "change_request"
        assert binding["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_create_gitlab_mr_binding(self) -> None:
        """Persist a valid GitLab merge request binding."""
        from tests.conftest import create_client

        conn = AsyncMock()
        saved_row = _mk_binding_row(
            awx_job_id=101,
            provider="gitlab",
            repository_url="gitlab.com/cloudnative-pg/cloudnative-pg",
            entity_number="6",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), saved_row]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "101", "job_template_id": 12},
            "external_session_id": "ses_gl456",
            "resource": {
                "provider": "gitlab",
                "repository": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
                "resource_type": "merge_request",
                "resource_number": "6",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["resource"]["provider"] == "gitlab"
        assert binding["resource"]["resource_type"] == "change_request"

    @pytest.mark.asyncio
    async def test_idempotent_replay_returns_existing(self) -> None:
        """Replayed identical callback returns 200 with existing binding."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        # Auth lookup → atomic insert conflicts → fetch existing → identical → 200
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        # Idempotent replay returns 200 (not 201) — no new row created.
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["awx_job"]["job_id"] == "42"

    @pytest.mark.asyncio
    async def test_conflicting_data_returns_409(self) -> None:
        """Different data for same AWX job ID returns 409 Conflict."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        # Auth lookup → atomic insert conflicts → fetch existing → conflict → 409
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_different",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_resource_number_returns_409(self) -> None:
        """Different resource_number for same AWX job ID returns 409 Conflict."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "123",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_outcome_returns_409(self) -> None:
        """A replay changing only the outcome is a conflict, not a silent accept."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42, outcome="completed")
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "failed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_repository_returns_409(self) -> None:
        """A replay changing the resource repository is a conflict."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/other-repo",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_metadata_returns_409(self) -> None:
        """A replay changing optional metadata (source_event_id) is a conflict."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
            "source_event_id": "evt_changed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_non_numeric_awx_job_id_rejected_400(self) -> None:
        """A non-numeric awx_job_id in the body is rejected with 400, not 500."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "abc", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_outcome_rejected(self) -> None:
        """Invalid outcome value is rejected with 422."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "running",  # Invalid — not a terminal outcome
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_resource_type_rejected(self) -> None:
        """Invalid resource_type is rejected with 422."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "issue",  # Invalid — not a change request
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_sensitive_data_in_response(self) -> None:
        """Response does not include collector tokens, stdout, or prompts."""
        from tests.conftest import create_client

        conn = AsyncMock()
        existing_row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        binding = data["data"]
        for key in (
            "collector_token",
            "token",
            "stdout",
            "prompt",
            "extra_vars",
            "payload",
        ):
            assert key not in binding, f"Sensitive key '{key}' found in response"


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions/{awx_job_id} — single-binding read
# ═══════════════════════════════════════════════════════════════════════════


class TestGetExecutionBinding:
    """GET /api/v1/afk/executions/{awx_job_id} — single-binding read."""

    @pytest.mark.asyncio
    async def test_get_existing_binding(self) -> None:
        """Return an existing execution binding by AWX job ID."""
        from tests.conftest import create_client

        conn = AsyncMock()
        row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["awx_job"]["job_id"] == "42"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self) -> None:
        """Return 404 when AWX job ID does not exist."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/99999")
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_get_non_numeric_awx_job_id_returns_400(self) -> None:
        """A non-numeric path awx_job_id is rejected with 400, not 500."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock()
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/abc")
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_get_binding_with_null_session_returns_null(self) -> None:
        """A binding whose external_session_id is NULL reads back as None."""
        from tests.conftest import create_client

        conn = AsyncMock()
        row = _mk_binding_row(awx_job_id=42, external_session_id=None)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["external_session_id"] is None

    @pytest.mark.asyncio
    async def test_get_binding_with_afk_run_id_returns_ulid(self) -> None:
        """A new row with afk_run_id populated returns the 26-char ULID."""
        from tests.conftest import create_client

        conn = AsyncMock()
        row = _mk_binding_row(
            awx_job_id=42,
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWX",
            trigger_type="eda",
        )
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWX"
        assert data["data"]["trigger_type"] == "eda"

    @pytest.mark.asyncio
    async def test_get_binding_legacy_row_returns_null_for_new_fields(self) -> None:
        """A legacy row without afk_run_id returns null for all three new fields."""
        from tests.conftest import create_client

        conn = AsyncMock()
        row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["afk_run_id"] is None
        assert data["data"]["trigger_type"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions — resource history (filtered)
# ═══════════════════════════════════════════════════════════════════════════


class TestListExecutionBindings:
    """GET /api/v1/afk/executions — list bindings for a resource."""

    @pytest.mark.asyncio
    async def test_list_bindings_for_github_pr(self) -> None:
        """Return all bindings for a GitHub pull request resource."""
        from tests.conftest import create_client

        conn = AsyncMock()
        rows = [
            _mk_binding_row(awx_job_id=10, outcome="failed"),
            _mk_binding_row(awx_job_id=20, outcome="completed"),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        history = data["data"]
        assert history["resource"]["provider"] == "github"
        assert history["resource"]["resource_type"] == "change_request"
        assert len(history["bindings"]) == 2
        assert history["bindings"][0]["outcome"] == "failed"
        assert history["bindings"][1]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_list_bindings_for_gitlab_mr(self) -> None:
        """Return all bindings for a GitLab merge request resource."""
        from tests.conftest import create_client

        conn = AsyncMock()
        rows = [
            _mk_binding_row(
                awx_job_id=101,
                provider="gitlab",
                repository_url="gitlab.com/cloudnative-pg/cloudnative-pg",
                entity_number="6",
                outcome="completed",
            ),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "gitlab",
                "repository_url": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
                "entity_type": "change_request",
                "entity_number": "6",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["data"]["bindings"]) == 1
        assert data["data"]["bindings"][0]["resource"]["provider"] == "gitlab"

    @pytest.mark.asyncio
    async def test_list_bindings_empty_result(self) -> None:
        """Return empty bindings list for resource with no history."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "0",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["bindings"] == []

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(self) -> None:
        """Return 400 for invalid provider value."""
        from tests.conftest import create_client

        conn = AsyncMock()
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "bitbucket",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "error"
        assert "Invalid provider" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_invalid_entity_type_returns_400(self) -> None:
        """Return 400 for invalid entity_type value."""
        from tests.conftest import create_client

        conn = AsyncMock()
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "issue",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "error"
        assert "Invalid entity_type" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_retry_flow_deterministic_order(self) -> None:
        """History includes failed attempt before successful retry."""
        from tests.conftest import create_client

        conn = AsyncMock()
        rows = [
            _mk_binding_row(
                awx_job_id=10,
                outcome="failed",
                failure_reason="Timeout after 300s",
                started_at=_A_TS,
                finished_at=_A_TS,
            ),
            _mk_binding_row(
                awx_job_id=20,
                outcome="completed",
                started_at=_B_TS,
                finished_at=_B_TS,
            ),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        bindings = data["data"]["bindings"]
        assert len(bindings) == 2
        assert bindings[0]["outcome"] == "failed"
        assert bindings[0]["failure_reason"] == "Timeout after 300s"
        assert bindings[1]["outcome"] == "completed"
        assert bindings[1]["failure_reason"] is None

    @pytest.mark.asyncio
    async def test_list_bindings_mixed_legacy_and_new_rows(self) -> None:
        """History items include nullable afk_run_id/trigger_type for new and legacy rows."""
        from tests.conftest import create_client

        conn = AsyncMock()
        rows = [
            _mk_binding_row(
                awx_job_id=10,
                outcome="failed",
                afk_run_id=None,
                trigger_type=None,
            ),
            _mk_binding_row(
                awx_job_id=20,
                outcome="completed",
                afk_run_id="01JZABCDEFGHJKLMNPQRSTVWX",
                trigger_type="manual",
            ),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        bindings = data["data"]["bindings"]
        assert len(bindings) == 2
        # Legacy row — null for new fields
        assert bindings[0]["afk_run_id"] is None
        assert bindings[0]["trigger_type"] is None
        # New row — populated values
        assert bindings[1]["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWX"
        assert bindings[1]["trigger_type"] == "manual"


# ═══════════════════════════════════════════════════════════════════════════
#  Auth tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAuth:
    """Write path requires collector credential; read paths use API key."""

    @pytest.mark.asyncio
    async def test_write_requires_collector_token(self) -> None:
        """POST without collector token returns 401."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_read_requires_api_key(self) -> None:
        """GET without API key returns 401."""
        from tests.conftest import create_client

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code in (401, 403)
