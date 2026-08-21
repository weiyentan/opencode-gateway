"""Authentication tests for the execution-binding API (issue #550).

The execution-binding write path applies the existing collector-token
mechanism (``ApiKeyMiddleware`` + ``require_collector_token``) and
additionally requires the credential to be attributable to the dedicated
AWX execution-binding integration client — never the usage collector
(``opencode-collector``) or any other client.

Covered here:

* dedicated-client happy path through both auth layers,
* rejection of a valid credential owned by another client (403),
* the ``/ingest``-compatible rejection matrix (missing, malformed, empty,
  invalid, revoked, inactive → 401 with the same error codes/messages),
* read endpoints protected by the Admin API Key alone,
* raw bearer tokens never persisted, returned, or logged.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME
from app.core.identity import hash_token
from tests.conftest import create_client, mock_row

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()

# The bearer token used by the dedicated AWX integration in collector-only
# isolation tests (test value — never a real secret).
_COLLECTOR_BEARER = "awx-collector-bearer-token"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _auth_row(
    *,
    client_name: str = AWX_EXECUTION_BINDING_CLIENT_NAME,
    revoked: bool = False,
    active: bool = True,
) -> MagicMock:
    """Return a mock ``collector_credentials`` auth row for require_collector_token."""
    return mock_row(
        {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": "2026-01-01T00:00:00Z" if revoked else None,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": client_name,
            "client_is_active": active,
        }
    )


def _saved_row() -> MagicMock:
    """Return a mock ``execution_bindings`` row for a saved binding."""
    return mock_row(
        {
            "awx_job_id": 42,
            "external_session_id": "ses_abc123",
            "provider": "github",
            "repository_url": "acme/proj",
            "entity_type": "change_request",
            "entity_number": "99",
            "outcome": "completed",
            "source_event_id": "evt_001",
            "branch": None,
            "title": None,
            "failure_reason": None,
            "started_at": None,
            "finished_at": None,
        }
    )


def _valid_binding_payload() -> dict:
    """Minimal valid execution-binding POST body."""
    return {
        "awx_job": {"job_id": "42", "job_template_id": 7},
        "external_session_id": "ses_abc123",
        "resource": {
            "provider": "github",
            "repository": "acme/proj",
            "resource_type": "pull_request",
            "resource_number": "99",
        },
        "outcome": "completed",
    }


def _build_collector_only_client(mock_conn: AsyncMock, monkeypatch) -> AsyncClient:
    """Build a client where the collector-token layer is the ONLY auth layer.

    Mirrors ``tests/test_ingest.py`` isolation: development mode with no
    ``GATEWAY_API_KEY`` makes ``ApiKeyMiddleware`` transparent, so the
    collector credential matrix can be exercised directly.
    """
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_ENV", "development")

    from app.core.factory import create_app
    from app.db.session import get_session

    app = create_app(configure_logging=False)
    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


# ═══════════════════════════════════════════════════════════════════════════
#  Dedicated credential enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestDedicatedCredentialEnforcement:
    """POST requires a collector credential of the dedicated AWX client."""

    @pytest.mark.asyncio
    async def test_dedicated_client_credential_accepted_through_two_layers(
        self,
    ) -> None:
        """Valid credential of the dedicated client passes both auth layers.

        The client carries the Admin API Key on ``Authorization`` and the
        mock credential row attributes the token to the dedicated AWX
        execution-binding client — the write proceeds.
        """
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, _saved_row()]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions", json=_valid_binding_payload()
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_other_client_credential_rejected_403(self) -> None:
        """A valid credential owned by another client (usage collector) is 403.

        The token is a valid collector credential, but it is not
        attributable to the dedicated AWX integration client — the write
        path must not reuse another pipeline's credential.
        """
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value=_auth_row(client_name="opencode-collector")
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions", json=_valid_binding_payload()
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "FORBIDDEN"
        # Rejected at the auth gate — the only DB statements are the
        # credential lookup and its last_used_at touch; no business-logic
        # query (get_execution_binding) or binding insert ran.
        assert conn.fetchrow.call_count == 1
        assert conn.execute.call_count == 1
        assert "UPDATE collector_credentials" in str(conn.execute.call_args)


# ═══════════════════════════════════════════════════════════════════════════
#  Collector credential rejection matrix (same 401 behavior as /ingest)
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectorCredentialRejectionMatrix:
    """Missing/malformed/invalid/revoked/inactive credentials are 401.

    These exercise ``require_collector_token`` — the same dependency used
    by ``/ingest`` — in isolation (API-key middleware disabled), asserting
    the identical error codes and messages.
    """

    @pytest.mark.asyncio
    async def test_missing_authorization_header_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions", json=_valid_binding_payload()
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Missing or invalid Authorization header" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_malformed_authorization_header_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Missing or invalid Authorization header" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_empty_bearer_token_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Empty bearer token" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_invalid_token_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)  # hash not found
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Invalid token" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_revoked_token_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_auth_row(revoked=True))
        conn.execute = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Token has been revoked" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_inactive_client_401(self, monkeypatch) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_auth_row(active=False))
        conn.execute = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Client is deactivated" in data["error"]["message"]


# ═══════════════════════════════════════════════════════════════════════════
#  Two-layer auth boundary
# ═══════════════════════════════════════════════════════════════════════════


class TestTwoLayerAuthBoundary:
    """ApiKeyMiddleware still gates every route before the collector layer."""

    @pytest.mark.asyncio
    async def test_invalid_api_key_rejected_before_collector_lookup(self) -> None:
        """Wrong Admin API Key → 401 from the middleware; no credential lookup."""
        conn = AsyncMock()
        client = create_client(conn, api_key="wrong-api-key")

        resp = await client.post(
            "/api/v1/afk/executions", json=_valid_binding_payload()
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"
        # The collector credential lookup never ran.
        assert conn.fetchrow.call_count == 0

    @pytest.mark.asyncio
    async def test_read_requires_api_key_only(self) -> None:
        """GET needs the Admin API Key and no collector credential."""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_saved_row())
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["data"]["awx_job"]["job_id"] == "42"

    @pytest.mark.asyncio
    async def test_read_without_api_key_401(self) -> None:
        conn = AsyncMock()
        client = create_client(conn, api_key=None)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 401
        data = resp.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"


# ═══════════════════════════════════════════════════════════════════════════
#  Bearer token non-leakage
# ═══════════════════════════════════════════════════════════════════════════


class TestBearerTokenNonLeakage:
    """Raw bearer tokens are never persisted, returned, or logged."""

    @pytest.mark.asyncio
    async def test_raw_token_never_in_response(self, monkeypatch) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)  # invalid token
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
        )
        assert resp.status_code == 401
        assert _COLLECTOR_BEARER not in resp.text

    @pytest.mark.asyncio
    async def test_raw_token_never_logged(self, monkeypatch, caplog) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)  # invalid token
        client = _build_collector_only_client(conn, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await client.post(
                "/api/v1/afk/executions",
                json=_valid_binding_payload(),
                headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
            )
        assert _COLLECTOR_BEARER not in caplog.text

    @pytest.mark.asyncio
    async def test_auth_lookup_uses_hash_not_raw_token(self, monkeypatch) -> None:
        """The credential lookup queries by SHA-256 hash, never the raw token."""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, _saved_row()]
        )
        conn.execute = AsyncMock()
        client = _build_collector_only_client(conn, monkeypatch)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_valid_binding_payload(),
            headers={"Authorization": f"Bearer {_COLLECTOR_BEARER}"},
        )
        assert resp.status_code == 201

        # First fetchrow is the credential lookup — its second positional
        # argument must be the SHA-256 hash of the presented token.
        auth_call = conn.fetchrow.call_args_list[0]
        assert auth_call.args[1] == hash_token(_COLLECTOR_BEARER)
        assert _COLLECTOR_BEARER not in str(auth_call)
