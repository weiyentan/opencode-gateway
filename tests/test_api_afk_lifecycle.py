"""Tests for the provisional AFK run lifecycle REST API (issue #589).

Covers the two lifecycle endpoints mounted on the AFK executions router:

- ``POST /api/v1/afk/executions/runs`` — provisioning (idempotent,
  conflict-aware, recovery reference).
- ``POST /api/v1/afk/executions/runs/{afk_run_id}/change-request`` —
  explicit change-request binding (idempotent, 1:1 conflict-aware).

Tests exercise replay, conflicting replay, recovery without predecessor
mutation, missing-predecessor rejection, binding idempotency, the 1:1
lifecycle<->change-request conflict, validation, and auth.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import mock_row

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()

_RUN_ID = "01JZABCDEFGHJKLMNPQRSTVWX"
_PREDECESSOR_ID = "01HPRED000000000000000001"


# ── Mock helpers ─────────────────────────────────────────────────────────────


def _auth_row() -> MagicMock:
    """Return a mock row that passes require_collector_token (dedicated client)."""
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


def _mk_conn() -> AsyncMock:
    """Build a mock asyncpg connection with transaction support."""
    conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


def _mk_lifecycle_row(
    *,
    afk_run_id: str = _RUN_ID,
    provider: str = "github",
    status: str = "pending",
    host: str | None = "awx-01.internal",
    source_event_id: str | None = "eda-1234",
    repository: str | None = "github.com/acme/proj",
    trigger_type: str | None = "eda",
    title: str | None = "Implement auth",
    change_request_provider: str | None = None,
    change_request_repository: str | None = None,
    change_request_external_id: str | None = None,
    recovered_from_afk_run_id: str | None = None,
) -> MagicMock:
    """Build a mock asyncpg Record for an afk_runs lifecycle row."""
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "provider": provider,
            "status": status,
            "host": host,
            "source_event_id": source_event_id,
            "repository": repository,
            "trigger_type": trigger_type,
            "title": title,
            "change_request_provider": change_request_provider,
            "change_request_repository": change_request_repository,
            "change_request_external_id": change_request_external_id,
            "recovered_from_afk_run_id": recovered_from_afk_run_id,
            "first_seen_at": None,
            "last_seen_at": None,
        }
    )


def _existing_key_row(**overrides) -> MagicMock:
    """Build the row returned by the provisioning idempotency-key SELECT."""
    row = {
        "afk_run_id": _RUN_ID,
        "repository": "github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
        "recovered_from_afk_run_id": None,
    }
    row.update(overrides)
    return mock_row(row)


def _provision_payload(**overrides) -> dict:
    payload = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
    }
    payload.update(overrides)
    return payload


def _binding_payload(**overrides) -> dict:
    payload = {
        "provider": "gitlab",
        "repository": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
        "external_id": "6",
    }
    payload.update(overrides)
    return payload


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions/runs — provisioning
# ═══════════════════════════════════════════════════════════════════════════


class TestProvisionLifecycle:
    """POST /runs — provision one provisional lifecycle."""

    @pytest.mark.asyncio
    async def test_provision_creates_lifecycle(self) -> None:
        """A valid provisioning request returns 201 with the lifecycle."""
        from tests.conftest import create_client

        conn = _mk_conn()
        # auth → existing (None) → readback
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, _mk_lifecycle_row()]
        )
        conn.fetch = AsyncMock(
            return_value=[mock_row({"afk_run_id": _RUN_ID})]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs", json=_provision_payload()
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        lifecycle = data["data"]
        assert lifecycle["afk_run_id"] == _RUN_ID
        assert lifecycle["provider"] == "github"
        assert lifecycle["status"] == "pending"
        assert lifecycle["host"] == "awx-01.internal"
        assert lifecycle["source_event_id"] == "eda-1234"
        # Repository identity is normalized at the API boundary.
        assert lifecycle["repository"] == "github.com/acme/proj"
        assert lifecycle["trigger_type"] == "eda"
        assert lifecycle["change_request"] is None
        assert lifecycle["recovered_from_afk_run_id"] is None

    @pytest.mark.asyncio
    async def test_provision_idempotent_replay_returns_existing(self) -> None:
        """An identical replay returns 200 with the existing lifecycle."""
        from tests.conftest import create_client

        conn = _mk_conn()
        # auth → existing (matches payload) → readback
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), _existing_key_row(), _mk_lifecycle_row()]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs", json=_provision_payload()
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["afk_run_id"] == _RUN_ID

    @pytest.mark.asyncio
    async def test_provision_conflicting_replay_returns_409(self) -> None:
        """The same key with a different payload returns 409 without mutation."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                _existing_key_row(title="Different title"),
            ]
        )
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs", json=_provision_payload()
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_provision_recovery_references_predecessor(self) -> None:
        """A recovery lifecycle returns its predecessor reference."""
        from tests.conftest import create_client

        conn = _mk_conn()
        # auth → existing (None) → predecessor → readback
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row({"afk_run_id": _PREDECESSOR_ID}),
                _mk_lifecycle_row(recovered_from_afk_run_id=_PREDECESSOR_ID),
            ]
        )
        conn.fetch = AsyncMock(
            return_value=[mock_row({"afk_run_id": _RUN_ID})]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _provision_payload(
            trigger_type="recovery",
            recovered_from_afk_run_id=_PREDECESSOR_ID,
        )
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["recovered_from_afk_run_id"] == _PREDECESSOR_ID

    @pytest.mark.asyncio
    async def test_provision_missing_predecessor_returns_404(self) -> None:
        """A recovered_from_afk_run_id referencing no run returns 404."""
        from tests.conftest import create_client

        conn = _mk_conn()
        # auth → existing (None) → predecessor (None)
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None, None])
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _provision_payload(
            trigger_type="recovery",
            recovered_from_afk_run_id="01HMISSING0000000000000001",
        )
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_provision_invalid_repository_returns_400(self) -> None:
        """A non-normalizable repository identity is rejected with 400."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _provision_payload(repository="not-a-url")
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_provision_missing_required_fields_returns_422(self) -> None:
        """Missing host / source_event_id / trigger_type are rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _provision_payload()
        del payload["host"]
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 422

        payload = _provision_payload()
        del payload["source_event_id"]
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_provision_invalid_trigger_type_returns_422(self) -> None:
        """An unknown trigger_type is rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs",
            json=_provision_payload(trigger_type="teleport"),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_provision_recovery_without_predecessor_returns_422(self) -> None:
        """A recovery trigger without recovered_from_afk_run_id is rejected."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs",
            json=_provision_payload(trigger_type="recovery"),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_provision_rejects_unknown_fields(self) -> None:
        """Unknown fields on the provisioning payload are rejected (extra=forbid)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _provision_payload(awx_job_id="42")
        resp = await client.post("/api/v1/afk/executions/runs", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_provision_requires_collector_credential(self) -> None:
        """POST without a collector credential returns 401."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.post(
            "/api/v1/afk/executions/runs", json=_provision_payload()
        )
        assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions/runs/{afk_run_id}/change-request — binding
# ═══════════════════════════════════════════════════════════════════════════


class TestBindChangeRequest:
    """POST /runs/{id}/change-request — bind one change request."""

    @pytest.mark.asyncio
    async def test_bind_sets_change_request(self) -> None:
        """A first bind returns 200 with the change request populated."""
        from tests.conftest import create_client

        conn = _mk_conn()
        # auth → run (unbound) → other-owner (None) → readback (bound)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                _mk_lifecycle_row(),
                None,
                _mk_lifecycle_row(
                    change_request_provider="gitlab",
                    change_request_repository=(
                        "gitlab.com/cloudnative-pg/cloudnative-pg"
                    ),
                    change_request_external_id="6",
                ),
            ]
        )
        conn.execute = AsyncMock(return_value="UPDATE 1")
        client = create_client(conn)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        change_request = data["data"]["change_request"]
        assert change_request["provider"] == "gitlab"
        assert change_request["repository"] == "gitlab.com/cloudnative-pg/cloudnative-pg"
        assert change_request["resource_type"] == "change_request"
        assert change_request["resource_number"] == "6"

    @pytest.mark.asyncio
    async def test_bind_replay_same_identity_returns_200(self) -> None:
        """Re-binding the same identity is idempotent (200, no mutation)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        bound_row = _mk_lifecycle_row(
            change_request_provider="gitlab",
            change_request_repository="gitlab.com/cloudnative-pg/cloudnative-pg",
            change_request_external_id="6",
        )
        # auth → run (already bound same) → readback
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), bound_row, bound_row]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["change_request"]["resource_number"] == "6"

    @pytest.mark.asyncio
    async def test_bind_different_identity_returns_409(self) -> None:
        """Binding a different change request to a bound lifecycle is a conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        bound_row = _mk_lifecycle_row(
            change_request_provider="gitlab",
            change_request_repository="gitlab.com/cloudnative-pg/cloudnative-pg",
            change_request_external_id="7",
        )
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), bound_row])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_bind_change_request_owned_by_other_lifecycle_returns_409(
        self,
    ) -> None:
        """The 1:1 invariant: a change request owned elsewhere is a conflict."""
        from tests.conftest import create_client

        conn = _mk_conn()
        other = mock_row({"afk_run_id": "01HOTHER000000000000000001"})
        # auth → run (unbound) → other-owner row
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), _mk_lifecycle_row(), other]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_bind_missing_lifecycle_returns_404(self) -> None:
        """Binding to an unknown lifecycle returns 404."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs/01HMISSING0000000000000001/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_bind_invalid_repository_returns_400(self) -> None:
        """A non-normalizable repository identity is rejected with 400."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(repository="not-a-url"),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bind_missing_fields_returns_422(self) -> None:
        """Missing binding identity fields are rejected with 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = _binding_payload()
        del payload["external_id"]
        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=payload,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_bind_requires_collector_credential(self) -> None:
        """POST without a collector credential returns 401."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.post(
            f"/api/v1/afk/executions/runs/{_RUN_ID}/change-request",
            json=_binding_payload(),
        )
        assert resp.status_code in (401, 403)
