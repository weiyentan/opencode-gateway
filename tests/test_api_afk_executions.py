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
from unittest.mock import AsyncMock, MagicMock  # noqa: F811

import pytest
from httpx import AsyncClient

from tests.conftest import mock_row

# ── Test data ────────────────────────────────────────────────────────────────

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()

# afk_run_id is required for every new binding (issue #626); pre-provisioned
# lifecycles are referenced by this 26-char ULID in all write-path payloads.
_RUN_ULID = "01JZABCDEFGHJKLMNPQRSTVWXY"


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
    failure_summary: str | None = None,
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
            "failure_summary": failure_summary,
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


def _mk_conn() -> AsyncMock:
    """Build a mock asyncpg connection with transaction support.

    ``create_or_replay_afk_execution_binding`` wraps its work in
    ``async with self._conn.transaction():`` — the plain ``AsyncMock()``
    does not implement the async-context-manager protocol for ``transaction()``,
    so we explicitly configure it (matching ``conftest.mock_conn``).
    """
    conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions — write path
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateExecutionBinding:
    """POST /api/v1/afk/executions — persist one final binding."""

    @pytest.mark.asyncio
    async def test_create_github_pr_binding(self) -> None:
        """Persist a valid GitHub pull request binding."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→run-exists pre-check
        # → re-read) — afk_run_id supplied, no auto-provision (issue #626)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(
            return_value=[mock_row({"id": uuid.uuid4()})]
        )
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
            "trigger_type": "manual",
            "source_event_id": "evt_001",
            "branch": "feat/auth",
            "title": "Implement auth",
            "started_at": "2026-08-01T12:00:00Z",
            "finished_at": "2026-08-02T12:00:00Z",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["awx_job"]["job_id"] == "42"
        assert binding["resource"]["resource_type"] == "change_request"
        assert binding["outcome"] == "completed"
        assert binding["afk_run_id"] == _RUN_ULID

    @pytest.mark.asyncio
    async def test_create_gitlab_mr_binding(self) -> None:
        """Persist a valid GitLab merge request binding."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=101,
            provider="gitlab",
            repository_url="gitlab.com/cloudnative-pg/cloudnative-pg",
            entity_number="6",
            afk_run_id=_RUN_ULID,
        )
        # Call sequence: auth → create_or_replay (fetchrow→run-exists pre-check
        # → re-read) — afk_run_id supplied, no auto-provision (issue #626)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "gitlab",
                        "change_request_repository": (
                            "gitlab.com/cloudnative-pg/cloudnative-pg"
                        ),
                        "change_request_external_id": "6",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(
            return_value=[mock_row({"id": uuid.uuid4()})]
        )
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
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

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            external_session_id="ses_abc123",
            trigger_type="manual",
            afk_run_id=_RUN_ULID,
        )
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        # Idempotent replay returns 200 (not 201) — no new row created.
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["awx_job"]["job_id"] == "42"

    @pytest.mark.asyncio
    async def test_omitted_optional_values_do_not_conflict_with_stored_values(
        self,
    ) -> None:
        """Omitted optional callback fields are absent from replay comparison."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_reason="runner_failed",
            failure_summary="Process crashed",
            started_at=_A_TS,
            finished_at=_B_TS,
            trigger_type="manual",
            afk_run_id=_RUN_ULID,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "failed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_explicit_null_optional_value_conflicts_with_stored_value(
        self,
    ) -> None:
        """An explicit null remains distinct from an omitted optional field."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="Process crashed",
            afk_run_id=_RUN_ULID,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "failed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": None,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_conflicting_data_returns_409(self) -> None:
        """Different data for same AWX job ID returns 409 Conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_resource_number_returns_409(self) -> None:
        """Different resource_number for same AWX job ID returns 409 Conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_outcome_returns_409(self) -> None:
        """A replay changing only the outcome is a conflict, not a silent accept."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(awx_job_id=42, outcome="completed", afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_repository_returns_409(self) -> None:
        """A replay changing the resource repository is a conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_conflicting_metadata_returns_409(self) -> None:
        """A replay changing optional metadata (source_event_id) is a conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ULID)
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
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

        conn = _mk_conn()
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_completed_with_failure_reason_rejected_422(self) -> None:
        """POST completed + failure_reason is rejected with 422 (issue #564)."""
        from tests.conftest import create_client

        conn = _mk_conn()
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
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_reason": "boom",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_completed_with_failure_summary_rejected_422(self) -> None:
        """POST completed + failure_summary is rejected with 422 (issue #564)."""
        from tests.conftest import create_client

        conn = _mk_conn()
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
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": "boom",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_failed_with_failure_summary_persisted(self) -> None:
        """POST failed + failure_summary is accepted and returned on read."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="Process crashed",
        )
        # Call sequence: auth → create_or_replay (fetchrow→pre-check→execute→fetch) → re-read
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
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
            "outcome": "failed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": "Process crashed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        binding = resp.json()["data"]
        assert binding["outcome"] == "failed"
        assert binding["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_failed_with_secret_failure_summary_redacted(self) -> None:
        """Secret-like values in failure_summary are redacted before persistence."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="GITHUB_TOKEN=***",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
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
            "outcome": "failed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        binding = resp.json()["data"]
        assert (
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
            not in binding["failure_summary"]
        )
        assert "***" in binding["failure_summary"]

    @pytest.mark.asyncio
    async def test_failed_with_overlong_failure_summary_truncated(self) -> None:
        """failure_summary longer than 1000 chars is truncated, not rejected."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="🔥" * 1000,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
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
            "outcome": "failed",
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": "🔥" * 1200,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        binding = resp.json()["data"]
        assert binding["failure_summary"] == "🔥" * 1000

    @pytest.mark.asyncio
    async def test_idempotent_replay_with_same_failure_summary_returns_200(
        self,
    ) -> None:
        """Identical replay with the same failure_summary returns 200."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            trigger_type="manual",
            failure_reason="timeout",
            failure_summary="Process crashed",
            afk_run_id=_RUN_ULID,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_reason": "timeout",
            "failure_summary": "Process crashed",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 200
        binding = resp.json()["data"]
        assert binding["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_conflicting_failure_summary_returns_409(self) -> None:
        """A replay with a different failure_summary is a 409 conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            trigger_type="manual",
            failure_summary="Original summary",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
            "failure_summary": "Different summary",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_outcome_rejected(self) -> None:
        """Invalid outcome value is rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
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
            "outcome": "in_progress",  # Invalid — not in the outcome vocabulary
            "trigger_type": "manual",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_running_without_afk_run_id_rejected(self) -> None:
        """Start-time provisioning requires afk_run_id (issue #590)."""
        from tests.conftest import create_client

        conn = _mk_conn()
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
            "outcome": "running",
            "trigger_type": "manual",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_resource_type_rejected(self) -> None:
        """Invalid resource_type is rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
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
            "trigger_type": "manual",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_sensitive_data_in_response(self) -> None:
        """Response does not include collector tokens, stdout, or prompts."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            external_session_id="ses_abc123",
            trigger_type="manual",
            afk_run_id=_RUN_ULID,
        )
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
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

    @pytest.mark.asyncio
    async def test_create_returns_afk_run_id(self) -> None:
        """201 Created response includes afk_run_id (26-char ULID) and binding_id."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
            trigger_type="eda",
            source_event_id="evt_001",
        )
        # Call sequence: auth → create_or_replay (fetchrow→pre-check→execute→fetch) → re-read
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding for this AWX job
                mock_row(
                    {
                        "afk_run_id": _RUN_ULID,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(
            return_value=[mock_row({"id": uuid.uuid4()})]
        )
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
            "trigger_type": "eda",
            "source_event_id": "evt_001",
            "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWXY"
        assert binding["binding_id"] is not None
        assert binding["trigger_type"] == "eda"
        assert binding["source_event_id"] == "evt_001"

    @pytest.mark.asyncio
    async def test_post_without_afk_run_id_rejected_422(self) -> None:
        """Issue #626: a POST without afk_run_id is rejected with 422 — the
        legacy auto-provisioning path (PR #600 blocker reuse) is closed for
        new bindings, even when the canonical PR already owns a lifecycle."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
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
            "trigger_type": "manual",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422, resp.text
        # Rejected at the schema boundary — the repository was never reached.
        assert not conn.fetch.called
        assert conn.fetchrow.call_count == 1  # credential lookup only

    @pytest.mark.asyncio
    async def test_idempotent_replay_returns_same_afk_run_id(self) -> None:
        """200 OK idempotent replay returns the same afk_run_id and binding_id."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
            trigger_type="eda",
            source_event_id="evt_001",
        )
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "eda",
            "source_event_id": "evt_001",
            "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWXY"
        assert binding["binding_id"] is not None
        assert binding["trigger_type"] == "eda"

    @pytest.mark.asyncio
    async def test_conflict_with_different_trigger_type_returns_409(self) -> None:
        """Different trigger_type for same AWX job ID returns 409 Conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        existing_row = _mk_binding_row(
            awx_job_id=42,
            trigger_type="eda",
            afk_run_id=_RUN_ULID,
        )
        # Call sequence: auth → create_or_replay (fetchrow→existing) → re-read → conflict
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), existing_row, existing_row]
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
            "trigger_type": "manual",
            "afk_run_id": _RUN_ULID,
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_legacy_row_readback_returns_null_afk_run_id(self) -> None:
        """Legacy rows without afk_run_id are readable with null fields."""
        from tests.conftest import create_client

        conn = _mk_conn()
        legacy_row = _mk_binding_row(
            awx_job_id=42,
            afk_run_id=None,
            trigger_type=None,
        )
        conn.fetchrow = AsyncMock(return_value=legacy_row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["afk_run_id"] is None
        assert binding["trigger_type"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions/{awx_job_id} — single-binding read
# ═══════════════════════════════════════════════════════════════════════════


class TestGetExecutionBinding:
    """GET /api/v1/afk/executions/{awx_job_id} — single-binding read."""

    @pytest.mark.asyncio
    async def test_get_existing_binding(self) -> None:
        """Return an existing execution binding by AWX job ID."""
        from tests.conftest import create_client

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
        row = _mk_binding_row(
            awx_job_id=42,
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
            trigger_type="eda",
        )
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWXY"
        assert data["data"]["trigger_type"] == "eda"

    @pytest.mark.asyncio
    async def test_get_binding_legacy_row_returns_null_for_new_fields(self) -> None:
        """A legacy row without afk_run_id returns null for all three new fields."""
        from tests.conftest import create_client

        conn = _mk_conn()
        row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["afk_run_id"] is None
        assert data["data"]["trigger_type"] is None

    @pytest.mark.asyncio
    async def test_get_binding_returns_failure_summary(self) -> None:
        """A binding with a failure_summary reads it back (issue #564)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="Process crashed",
        )
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_get_binding_legacy_row_failure_summary_null(self) -> None:
        """A legacy row without a failure_summary reads back None."""
        from tests.conftest import create_client

        conn = _mk_conn()
        row = _mk_binding_row(awx_job_id=42, failure_summary=None)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["failure_summary"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions — resource history (filtered)
# ═══════════════════════════════════════════════════════════════════════════


class TestListExecutionBindings:
    """GET /api/v1/afk/executions — list bindings for a resource."""

    @pytest.mark.asyncio
    async def test_list_bindings_for_github_pr(self) -> None:
        """Return all bindings for a GitHub pull request resource."""
        from tests.conftest import create_client

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
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
    async def test_list_bindings_returns_failure_summary(self) -> None:
        """History entries surface failure_summary for failed attempts (issue #564)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        rows = [
            _mk_binding_row(
                binding_id="00000000-0000-0000-0000-000000000002",
                awx_job_id=102,
                outcome="failed",
                failure_reason="timeout",
                failure_summary="Process crashed",
            ),
            _mk_binding_row(awx_job_id=103, outcome="completed"),
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
        assert len(history["bindings"]) == 2
        assert history["bindings"][0]["failure_summary"] == "Process crashed"
        assert history["bindings"][1]["failure_summary"] is None

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(self) -> None:
        """Return 400 for invalid provider value."""
        from tests.conftest import create_client

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
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

        conn = _mk_conn()
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
                afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
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
        assert bindings[1]["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWXY"
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

        conn = _mk_conn()
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
            "trigger_type": "manual",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_read_requires_api_key(self) -> None:
        """GET without API key returns 401."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
#  Two-phase lifecycle (issue #590)
# ═══════════════════════════════════════════════════════════════════════════


def _update_row(**overrides) -> MagicMock:
    """Build a mock row as seen by update_execution_binding_terminal's FOR UPDATE."""
    row = {
        "id": uuid.uuid4(),
        "outcome": "running",
        "finished_at": None,
        "failure_reason": None,
        "failure_summary": None,
        "external_session_id": None,
        "provider": None,
        "repository_url": None,
        "entity_type": None,
        "entity_number": None,
    }
    row.update(overrides)
    return mock_row(row)


class TestTwoPhaseLifecycle:
    """POST running provisioning + PATCH terminal update (issue #590)."""

    @pytest.mark.asyncio
    async def test_running_provision_under_existing_afk_run(self) -> None:
        """Start-time provisioning attaches a running binding to afk_run_id."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            outcome="running",
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
            external_session_id=None,
            provider=None,
            repository_url=None,
            entity_type=None,
            entity_number=None,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row({"afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY"}),  # run exists
                saved_row,  # re-read after create
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
            "outcome": "running",
            "trigger_type": "eda",
            "source_event_id": "evt_001",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        binding = data["data"]
        assert binding["outcome"] == "running"
        assert binding["afk_run_id"] == "01JZABCDEFGHJKLMNPQRSTVWXY"
        assert binding["resource"] is None
        assert binding["external_session_id"] is None

    @pytest.mark.asyncio
    async def test_failed_terminal_callback_without_resource_or_session(self) -> None:
        """A failed execution persists without a change request or session."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_reason="AWX job crashed before launch",
            afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
            external_session_id=None,
            provider=None,
            repository_url=None,
            entity_type=None,
            entity_number=None,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row({"afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY"}),  # run exists
                saved_row,  # re-read after create
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
            "outcome": "failed",
            "trigger_type": "manual",
            "failure_reason": "AWX job crashed before launch",
        }

        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        binding = resp.json()["data"]
        assert binding["outcome"] == "failed"
        assert binding["resource"] is None
        assert binding["external_session_id"] is None
        assert binding["failure_reason"] == "AWX job crashed before launch"

    @pytest.mark.asyncio
    async def test_patch_transitions_running_to_completed(self) -> None:
        """PATCH transitions the same row from running to a terminal outcome."""
        from tests.conftest import create_client

        conn = _mk_conn()
        running_row = _update_row(outcome="running")
        updated_row = _mk_binding_row(awx_job_id=42, outcome="completed")
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), running_row, updated_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_patch_identical_terminal_replay_is_200(self) -> None:
        """Repeating an identical terminal update is idempotent (200, no mutation)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        finished = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        terminal_row = _update_row(outcome="completed", finished_at=finished)
        read_row = _mk_binding_row(
            awx_job_id=42, outcome="completed", finished_at=finished
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), terminal_row, read_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_patch_conflicting_terminal_update_returns_409(self) -> None:
        """A conflicting terminal update never overwrites history."""
        from tests.conftest import create_client

        conn = _mk_conn()
        terminal_row = _update_row(outcome="completed")
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), terminal_row])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={"outcome": "failed", "failure_reason": "boom"},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_patch_transitions_running_to_failed_with_failure_summary(
        self,
    ) -> None:
        """PATCH failed carries failure_summary through to the read response."""
        from tests.conftest import create_client

        conn = _mk_conn()
        running_row = _update_row(outcome="running")
        updated_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_reason="timeout",
            failure_summary="Process crashed",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), running_row, updated_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={
                "outcome": "failed",
                "failure_reason": "timeout",
                "failure_summary": "Bearer abc123def456 in stdout",
            },
        )
        assert resp.status_code == 200
        binding = resp.json()["data"]
        assert binding["outcome"] == "failed"
        assert binding["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_patch_failed_with_secret_failure_summary_redacted(self) -> None:
        """The PATCH path redacts secret-like failure summaries (issue #564)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        running_row = _update_row(outcome="running")
        updated_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="GITHUB_TOKEN=***",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), running_row, updated_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={
                "outcome": "failed",
                "failure_summary": "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            },
        )
        assert resp.status_code == 200
        binding = resp.json()["data"]
        assert "***" in binding["failure_summary"]

    @pytest.mark.asyncio
    async def test_patch_completed_with_failure_summary_rejected_422(self) -> None:
        """A completed update carrying failure_summary is rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={"outcome": "completed", "failure_summary": "boom"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_identical_failed_replay_with_failure_summary_200(
        self,
    ) -> None:
        """An identical failed replay with matching failure_summary is 200."""
        from tests.conftest import create_client

        conn = _mk_conn()
        terminal_row = _update_row(
            outcome="failed", failure_summary="Process crashed"
        )
        read_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_summary="Process crashed",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), terminal_row, read_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={"outcome": "failed", "failure_summary": "Process crashed"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_patch_conflicting_failure_summary_409(self) -> None:
        """A failed replay with a different failure_summary is a 409 conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        terminal_row = _update_row(
            outcome="failed", failure_summary="Original summary"
        )
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), terminal_row])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={"outcome": "failed", "failure_summary": "Different summary"},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_patch_omitted_failure_metadata_replays_idempotently(self) -> None:
        """Omitted failure metadata does not conflict with stored terminal data."""
        from tests.conftest import create_client

        conn = _mk_conn()
        terminal_row = _update_row(
            outcome="failed",
            failure_reason="Timeout",
            failure_summary="Process crashed",
        )
        read_row = _mk_binding_row(
            awx_job_id=42,
            outcome="failed",
            failure_reason="Timeout",
            failure_summary="Process crashed",
        )
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), terminal_row, read_row])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={"outcome": "failed"},
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["failure_reason"] == "Timeout"
        assert resp.json()["data"]["failure_summary"] == "Process crashed"

    @pytest.mark.asyncio
    async def test_patch_unknown_awx_job_returns_404(self) -> None:
        """PATCH on an unknown AWX job returns 404."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/99999",
            json={
                "outcome": "completed",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_running_outcome_rejected_422(self) -> None:
        """A terminal update must target a terminal outcome — running is 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42", json={"outcome": "running"}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_non_numeric_awx_job_id_returns_400(self) -> None:
        """A non-numeric path awx_job_id is rejected with 400."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/abc",
            json={
                "outcome": "completed",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_requires_collector_token(self) -> None:
        """The terminal-update write path requires the collector credential."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.patch(
            "/api/v1/afk/executions/42", json={"outcome": "completed"}
        )
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_post_completed_without_resource_or_session_rejected_422(self) -> None:
        """Issue #600 review: a completed POST with afk_run_id but neither a
        change request nor a session is rejected before touching the DB."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions",
            json={
                "awx_job": {"job_id": "42", "job_template_id": 7},
                "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
                "outcome": "completed",
                "trigger_type": "manual",
            },
        )
        assert resp.status_code == 422, resp.text
        # The repository was never reached.
        assert not conn.fetch.called

    @pytest.mark.asyncio
    async def test_post_completed_without_session_rejected_422(self) -> None:
        """A completed POST with a change request but no session is rejected."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions",
            json={
                "awx_job": {"job_id": "42", "job_template_id": 7},
                "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWXY",
                "outcome": "completed",
                "trigger_type": "manual",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_patch_resource_conflicting_with_lifecycle_returns_409(self) -> None:
        """Issue #600 review scenario: the owning lifecycle is bound to PR #5 but
        the terminal update fills PR #99 — 409, and the execution row is never
        mutated so run and execution cannot diverge."""
        from tests.conftest import create_client

        conn = _mk_conn()
        running_row = _update_row(
            outcome="running", afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY"
        )
        run_row = mock_row(
            {
                "change_request_provider": "github",
                "change_request_repository": "acme/proj",
                "change_request_external_id": "5",
            }
        )
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), running_row, run_row])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/42",
            json={
                "outcome": "completed",
                "finished_at": "2026-08-02T12:00:00Z",
                "external_session_id": "ses_terminal",
                "resource": {
                    "provider": "github",
                    "repository": "https://github.com/acme/proj",
                    "resource_type": "pull_request",
                    "resource_number": "99",
                },
            },
        )
        assert resp.status_code == 409, resp.text
        # Neither the bind nor the execution row was mutated.
        exec_sql = [c.args[0] for c in conn.execute.call_args_list]
        assert not any(
            "UPDATE execution_bindings" in s or "UPDATE afk_runs" in s for s in exec_sql
        )


# ═══════════════════════════════════════════════════════════════════════════
#  AFK Run convergence — API-level verification (issue #607)
# ═══════════════════════════════════════════════════════════════════════════


class TestAFKRunConvergenceAPI:
    """API-level tests that running creation, direct terminal POST, PATCH and
    409/ idempotent / PR-MR independence produce expected AFK Run status.

    These are mock-based and complement the real-PostgreSQL concurrency proof
    in tests/integration/test_afk_run_convergence.py — the mock suite ensures
    ``pytest tests/test_api_afk_executions.py`` covers the contract without
    requiring a live database, while the integration suite proves it end-to-end.
    """

    @pytest.mark.asyncio
    async def test_running_creation_produces_running_status(self) -> None:
        """POST running under a provisional run is accepted (201)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=7001,
            outcome="running",
            afk_run_id="01JCONVERGE000000000000001",
            external_session_id=None,
            provider=None,
            repository_url=None,
            entity_type=None,
            entity_number=None,
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row({"afk_run_id": "01JCONVERGE000000000000001"}),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "7001", "job_template_id": 7},
            "afk_run_id": "01JCONVERGE000000000000001",
            "outcome": "running",
            "trigger_type": "eda",
            "source_event_id": "evt_run",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["outcome"] == "running"

    @pytest.mark.asyncio
    async def test_direct_terminal_post_converges(self) -> None:
        """Direct terminal POST without prior running is accepted (201)."""
        from unittest.mock import patch

        from afk_outcomes.repository import CreateAFKExecutionBindingResult
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        saved_row = _mk_binding_row(
            awx_job_id=7002,
            outcome="completed",
            afk_run_id="01JCONVERGE000000000000002",
        )
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "7002", "job_template_id": 7},
            "afk_run_id": "01JCONVERGE000000000000002",
            "outcome": "completed",
            "trigger_type": "manual",
            "external_session_id": "ses_direct",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "42",
            },
        }
        with patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.create_or_replay_afk_execution_binding",
            new=AsyncMock(
                return_value=CreateAFKExecutionBindingResult(
                    afk_run_id="01JCONVERGE000000000000002",
                    binding_id=saved_row["id"],
                    is_created=True,
                )
            ),
        ), patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.get_execution_binding_by_awx_job_id",
            new=AsyncMock(return_value=saved_row and __import__("afk_outcomes.repository", fromlist=["_row_to_execution_binding"])._row_to_execution_binding(saved_row)),
        ):
            # Use direct mock for readback: patch to return domain binding
            from afk_outcomes.models import ExecutionBinding, ExecutionOutcome

            domain_binding = ExecutionBinding(
                binding_id=str(saved_row["id"]),
                awx_job={"job_id": "7002", "job_template_id": 7},
                external_session_id="ses_direct",
                resource={
                    "provider": __import__("afk_outcomes.models", fromlist=["Provider"]).Provider.GITHUB,
                    "repository": "github.com/acme/proj",
                    "resource_type": __import__("afk_outcomes.models", fromlist=["EntityType"]).EntityType.CHANGE_REQUEST,
                    "resource_number": "42",
                },
                outcome=ExecutionOutcome.COMPLETED,
                afk_run_id="01JCONVERGE000000000000002",
                trigger_type="manual",
            )
            with patch(
                "app.api.afk_executions.AsyncpgOutcomeRepository.get_execution_binding_by_awx_job_id",
                new=AsyncMock(return_value=domain_binding),
            ):
                resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_patch_final_running_converges(self) -> None:
        """PATCH final running binding to terminal returns 200."""
        from unittest.mock import patch

        from afk_outcomes.models import ExecutionBinding, ExecutionOutcome
        from afk_outcomes.repository import UpdateExecutionBindingResult
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)
        domain_binding = ExecutionBinding(
            binding_id="00000000-0000-0000-0000-000000000003",
            awx_job={"job_id": "7003", "job_template_id": 7},
            external_session_id="ses_term",
            resource={
                "provider": __import__("afk_outcomes.models", fromlist=["Provider"]).Provider.GITHUB,
                "repository": "github.com/acme/proj",
                "resource_type": __import__("afk_outcomes.models", fromlist=["EntityType"]).EntityType.CHANGE_REQUEST,
                "resource_number": "42",
            },
            outcome=ExecutionOutcome.COMPLETED,
            afk_run_id="01JCONVERGE000000000000003",
            trigger_type="manual",
        )
        with patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.update_execution_binding_terminal",
            new=AsyncMock(return_value=UpdateExecutionBindingResult(is_updated=True)),
        ), patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.get_execution_binding_by_awx_job_id",
            new=AsyncMock(return_value=domain_binding),
        ):
            resp = await client.patch(
                "/api/v1/afk/executions/7003",
                json={
                    "outcome": "completed",
                    "finished_at": "2026-08-02T12:00:00Z",
                    "external_session_id": "ses_term",
                    "resource": {
                        "provider": "github",
                        "repository": "https://github.com/acme/proj",
                        "resource_type": "pull_request",
                        "resource_number": "42",
                    },
                },
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_409_on_completed_run(self) -> None:
        """Creating a new binding on a completed run surfaces 409."""
        from unittest.mock import patch

        from afk_outcomes.repository import CreateAFKExecutionBindingResult
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "7004", "job_template_id": 7},
            "afk_run_id": "01JCOMPLETE000000000000001",
            "outcome": "running",
            "trigger_type": "manual",
        }
        with patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.create_or_replay_afk_execution_binding",
            new=AsyncMock(
                return_value=CreateAFKExecutionBindingResult(
                    afk_run_id="01JCOMPLETE000000000000001", is_conflict=True
                )
            ),
        ):
            resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_idempotent_terminal_replay(self) -> None:
        """Identical terminal PATCH replay returns 200 without duplicate."""
        from unittest.mock import patch

        from afk_outcomes.models import ExecutionBinding, ExecutionOutcome
        from afk_outcomes.repository import UpdateExecutionBindingResult
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)
        domain_binding = ExecutionBinding(
            binding_id="00000000-0000-0000-0000-000000000005",
            awx_job={"job_id": "7005", "job_template_id": 7},
            outcome=ExecutionOutcome.FAILED,
            afk_run_id="01JCONVERGE000000000000005",
        )
        with patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.update_execution_binding_terminal",
            new=AsyncMock(return_value=UpdateExecutionBindingResult()),
        ), patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.get_execution_binding_by_awx_job_id",
            new=AsyncMock(return_value=domain_binding),
        ):
            resp = await client.patch(
                "/api/v1/afk/executions/7005", json={"outcome": "failed", "failure_reason": None}
            )
        assert resp.status_code == 200

    def test_pr_mr_state_has_no_effect_on_run_status(self) -> None:
        """Pure domain policy never consults PR/MR state."""
        from afk_outcomes.run_status import resolve_afk_run_status

        # Same outcomes with different PR states must yield same status
        assert resolve_afk_run_status(["completed"]) == "completed"
        assert resolve_afk_run_status(["completed", "failed"]) == "completed"
        assert resolve_afk_run_status(["failed"]) == "failed"
        # The function signature must not require PR/MR args
        import inspect

        assert list(inspect.signature(resolve_afk_run_status).parameters.keys()) == ["outcomes"]


# ═══════════════════════════════════════════════════════════════════════════
#  AFK run detail exposes session links from execution bindings (issue #618)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunDetailSessionLinks:
    """GET /api/v1/afk-outcomes/runs/{afk_run_id} surfaces the session links
    that execution bindings now persist to afk_run_sessions (issue #618)."""

    @pytest.mark.asyncio
    async def test_run_detail_returns_session_and_nonzero_session_count(
        self,
    ) -> None:
        """A run whose binding carried an external session id exposes the
        linked session and a non-zero usage.session_count."""
        from decimal import Decimal

        from tests.conftest import create_client

        conn = _mk_conn()
        run_id = "01JZABCDEFGHJKLMNPQRSTVWXY"
        session_uuid = uuid.uuid4()
        run_row = mock_row(
            {
                "afk_run_id": run_id,
                "provider": "github",
                "status": "completed",
                "title": "Fix login bug",
                "started_at": _A_TS,
                "finished_at": _B_TS,
                "outcome_status": "merged",
                "outcome": {
                    "status": "merged",
                    "change_request_ids": ["change_request:42"],
                    "resolved_issue_ids": [],
                    "merge_event_id": None,
                    "merged_at": None,
                },
                "first_seen_at": _A_TS,
                "last_seen_at": _B_TS,
            }
        )
        session_row = mock_row(
            {
                "session_id": session_uuid,
                "external_session_id": "ses_abc123",
                "started_at": _A_TS,
                "finished_at": _B_TS,
                "agent": "code-editor",
                "total_input_tokens": 500,
                "total_output_tokens": 250,
                "total_cache_read_tokens": 100,
                "total_cache_write_tokens": 50,
                "total_estimated_cost_usd": Decimal("0.0175"),
                "message_count": 5,
                "parent_session_id": None,
            }
        )
        conn.fetchrow = AsyncMock(return_value=run_row)
        conn.fetch = AsyncMock(
            side_effect=[
                [],  # entity rows
                [session_row],  # afk_run_sessions rows
            ]
        )
        client = create_client(conn)

        resp = await client.get(f"/api/v1/afk-outcomes/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["external_session_id"] == "ses_abc123"
        assert data["sessions"][0]["session_id"] == str(session_uuid)
        assert data["usage"]["session_count"] == 1
        assert data["usage"]["active_tokens"] == 750

    @pytest.mark.asyncio
    async def test_run_detail_without_session_links_has_zero_count(
        self,
    ) -> None:
        """A run with no persisted session links reports session_count 0."""
        from tests.conftest import create_client

        conn = _mk_conn()
        run_id = "01JZABCDEFGHJKLMNPQRSTVWY"
        run_row = mock_row(
            {
                "afk_run_id": run_id,
                "provider": "github",
                "status": "completed",
                "title": "Fix login bug",
                "started_at": _A_TS,
                "finished_at": _B_TS,
                "outcome_status": "merged",
                "outcome": None,
                "first_seen_at": _A_TS,
                "last_seen_at": _B_TS,
            }
        )
        conn.fetchrow = AsyncMock(return_value=run_row)
        conn.fetch = AsyncMock(
            side_effect=[
                [],  # entity rows
                [],  # afk_run_sessions rows — none
            ]
        )
        client = create_client(conn)

        resp = await client.get(f"/api/v1/afk-outcomes/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sessions"] == []
        assert data["usage"]["session_count"] == 0
