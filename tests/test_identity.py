"""Tests for the identity layer — client CRUD, token management, and
collector token auth.

All tests use the mock database connection from conftest.py and follow
the existing test conventions (httpx AsyncClient + mock asyncpg).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.core.identity import (
    OverlapEvidence,
    QuarantineRow,
    check_batch_overlap,
    check_quarantine_overlap,
    generate_collector_token,
    get_active_quarantines,
    hash_token,
    is_quarantined,
    quarantine_identity,
    resolve_canonical_identity,
    resolve_identity,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mk_ts() -> datetime:
    """Return a fixed timestamp for predictable test assertions."""
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


_CLIENT_ID = uuid.uuid4()
_CLIENT_ID2 = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()
_CREDENTIAL_ID2 = uuid.uuid4()

_TS = _mk_ts()


def _client_row(
    id: uuid.UUID = _CLIENT_ID,
    name: str = "test-client",
    description: str | None = "A test client",
    is_active: bool = True,
    canonical_name: str | None = None,
) -> MagicMock:
    """Return a mock row resembling an opencode_clients row."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "id": id,
        "name": name,
        "description": description,
        "is_active": is_active,
        "canonical_name": canonical_name,
        "created_at": _TS,
        "updated_at": _TS,
    }.__getitem__
    row.get.side_effect = {
        "id": id,
        "name": name,
        "description": description,
        "is_active": is_active,
        "canonical_name": canonical_name,
        "created_at": _TS,
        "updated_at": _TS,
    }.get
    return row


def _token_row(
    id: uuid.UUID = _CREDENTIAL_ID,
    client_id: uuid.UUID = _CLIENT_ID,
    token_prefix: str = "abcdefgh",
    label: str | None = "my-token",
    revoked_at: datetime | None = None,
) -> MagicMock:
    """Return a mock row resembling a collector_credentials row."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "id": id,
        "client_id": client_id,
        "token_prefix": token_prefix,
        "label": label,
        "last_used_at": None,
        "created_at": _TS,
        "revoked_at": revoked_at,
    }.__getitem__
    row.get.side_effect = {
        "id": id,
        "client_id": client_id,
        "token_prefix": token_prefix,
        "label": label,
        "last_used_at": None,
        "created_at": _TS,
        "revoked_at": revoked_at,
    }.get
    return row


def _build_client(mock_conn: AsyncMock, api_key: str = "test-api-key") -> AsyncClient:
    """Build an httpx test client with the given mock connection."""
    from app.db.session import get_session

    app = create_app(configure_logging=False)
    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {api_key}"},
    )


# ── Client CRUD tests ──────────────────────────────────────────────────────


class TestCreateClient:
    """POST /admin/clients"""

    @pytest.mark.asyncio
    async def test_create_client_returns_201(self):
        """Creating a valid client returns 201 with client data."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=_client_row())
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/admin/clients",
                json={"name": "test-client", "description": "A test client"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["name"] == "test-client"
        assert data["data"]["id"] == str(_CLIENT_ID)

    @pytest.mark.asyncio
    async def test_create_client_minimal_payload(self):
        """Creating a client with just a name (no description) works."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value=_client_row(description=None)
        )
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post("/admin/clients", json={"name": "minimal"})

        assert response.status_code == 201
        data = response.json()
        assert data["data"]["description"] is None

    @pytest.mark.asyncio
    async def test_create_client_response_includes_canonical_name_null(self):
        """A newly created client has canonical_name null in the response."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value=_client_row(name="deploy-1", canonical_name=None)
        )
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/admin/clients",
                json={"name": "deploy-1", "description": "Per-workspace client"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["data"]["canonical_name"] is None


class TestListClients:
    """GET /admin/clients"""

    @pytest.mark.asyncio
    async def test_list_clients_returns_list(self):
        """Listing clients returns a paginated response with client objects."""
        mock_conn = AsyncMock()
        c1 = _client_row(id=uuid.uuid4(), name="alpha")
        c2 = _client_row(id=uuid.uuid4(), name="beta")
        mock_conn.fetchval = AsyncMock(return_value=2)
        mock_conn.fetch = AsyncMock(return_value=[c1, c2])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get("/admin/clients")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        page = data["data"]
        assert page["total"] == 2
        assert page["limit"] == 50
        assert page["offset"] == 0
        assert len(page["items"]) == 2
        assert page["items"][0]["name"] == "alpha"
        assert page["items"][1]["name"] == "beta"

    @pytest.mark.asyncio
    async def test_list_clients_empty(self):
        """Listing clients when none exist returns empty paginated response."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get("/admin/clients")

        assert response.status_code == 200
        data = response.json()
        page = data["data"]
        assert page["items"] == []
        assert page["total"] == 0
        assert page["limit"] == 50
        assert page["offset"] == 0


class TestGetClient:
    """GET /admin/clients/{id}"""

    @pytest.mark.asyncio
    async def test_get_client_with_tokens(self):
        """Getting a client by ID returns client data with tokens."""
        mock_conn = AsyncMock()
        c_row = _client_row()
        t_row = _token_row()
        mock_conn.fetchrow = AsyncMock(return_value=c_row)
        mock_conn.fetch = AsyncMock(return_value=[t_row])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{_CLIENT_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["data"]["name"] == "test-client"
        assert len(data["data"]["tokens"]) == 1
        # Raw token must NOT be present
        token = data["data"]["tokens"][0]
        assert "token" not in token
        assert "token_hash" not in token
        assert token["token_prefix"] == "abcdefgh"

    @pytest.mark.asyncio
    async def test_get_client_includes_canonical_name(self):
        """Getting a client by ID returns canonical_name in the response (null when unset)."""
        mock_conn = AsyncMock()
        c_row = _client_row(canonical_name="deployment-1")
        t_row = _token_row()
        mock_conn.fetchrow = AsyncMock(return_value=c_row)
        mock_conn.fetch = AsyncMock(return_value=[t_row])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{_CLIENT_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["canonical_name"] == "deployment-1"

    @pytest.mark.asyncio
    async def test_get_client_canonical_name_null(self):
        """A client with no canonical name returns canonical_name as null."""
        mock_conn = AsyncMock()
        c_row = _client_row(canonical_name=None)
        t_row = _token_row()
        mock_conn.fetchrow = AsyncMock(return_value=c_row)
        mock_conn.fetch = AsyncMock(return_value=[t_row])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{_CLIENT_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["canonical_name"] is None

    @pytest.mark.asyncio
    async def test_get_client_not_found(self):
        """Getting a nonexistent client returns 404."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{uuid.uuid4()}")

        assert response.status_code == 404


class TestUpdateClient:
    """PATCH /admin/clients/{id}"""

    @pytest.mark.asyncio
    async def test_update_client_name(self):
        """Patching a client's name updates it."""
        mock_conn = AsyncMock()
        updated = _client_row(name="new-name")
        mock_conn.fetchrow = AsyncMock(return_value=updated)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{_CLIENT_ID}",
                json={"name": "new-name"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["name"] == "new-name"

    @pytest.mark.asyncio
    async def test_update_client_not_found(self):
        """Patching a nonexistent client returns 404."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # first fetch fails
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{uuid.uuid4()}",
                json={"name": "nope"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_client_set_canonical_name(self):
        """Patching canonical_name sets it on the client."""
        mock_conn = AsyncMock()
        updated = _client_row(name="test-client", canonical_name="deployment-1")
        mock_conn.fetchrow = AsyncMock(return_value=updated)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{_CLIENT_ID}",
                json={"canonical_name": "deployment-1"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["canonical_name"] == "deployment-1"

    @pytest.mark.asyncio
    async def test_update_client_clear_canonical_name(self):
        """Patching canonical_name with null clears it."""
        mock_conn = AsyncMock()
        updated = _client_row(name="test-client", canonical_name=None)
        mock_conn.fetchrow = AsyncMock(return_value=updated)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{_CLIENT_ID}",
                json={"canonical_name": None},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["canonical_name"] is None

    @pytest.mark.asyncio
    async def test_update_client_omit_canonical_name(self):
        """Patching without canonical_name leaves existing value unchanged."""
        mock_conn = AsyncMock()
        updated = _client_row(name="new-name", canonical_name="deployment-1")
        mock_conn.fetchrow = AsyncMock(return_value=updated)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{_CLIENT_ID}",
                json={"name": "new-name"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["name"] == "new-name"
        assert data["data"]["canonical_name"] == "deployment-1"

    @pytest.mark.asyncio
    async def test_update_client_update_canonical_name(self):
        """Patching canonical_name changes it from the prior value."""
        mock_conn = AsyncMock()
        updated = _client_row(name="test-client", canonical_name="deployment-2")
        mock_conn.fetchrow = AsyncMock(return_value=updated)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.patch(
                f"/admin/clients/{_CLIENT_ID}",
                json={"canonical_name": "deployment-2"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["canonical_name"] == "deployment-2"


class TestDeleteClient:
    """DELETE /admin/clients/{id}"""

    @pytest.mark.asyncio
    async def test_delete_client_returns_204(self):
        """Soft-deleting a client returns 204."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.delete(f"/admin/clients/{_CLIENT_ID}")

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_client_not_found(self):
        """Soft-deleting a nonexistent client returns 404."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.delete(f"/admin/clients/{uuid.uuid4()}")

        assert response.status_code == 404


# ── Token management tests ─────────────────────────────────────────────────


class TestProvisionToken:
    """POST /admin/clients/{id}/tokens"""

    @pytest.mark.asyncio
    async def test_provision_token_returns_raw_token_once(self):
        """Provisioning returns 201 with raw token — only time it's shown."""
        mock_conn = AsyncMock()
        client_row = MagicMock()
        client_row.__getitem__.side_effect = {
            "id": _CLIENT_ID,
            "is_active": True,
        }.__getitem__
        client_row.get.side_effect = {"id": _CLIENT_ID, "is_active": True}.get

        cred_row = _token_row(label="collector-1")
        # fetchrow is called twice: first for client existence, second for insert
        mock_conn.fetchrow = AsyncMock(side_effect=[client_row, cred_row])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/admin/clients/{_CLIENT_ID}/tokens",
                json={"label": "collector-1"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        inner = data["data"]
        # Raw token must be present
        assert "token" in inner
        assert len(inner["token"]) == 64  # token_urlsafe(48) → 64 chars
        assert len(inner["token_prefix"]) == 8
        assert inner["id"] == str(_CREDENTIAL_ID)
        assert inner["label"] == "collector-1"

    @pytest.mark.asyncio
    async def test_provision_token_client_not_found(self):
        """Provisioning for nonexistent client returns 404."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/admin/clients/{uuid.uuid4()}/tokens",
                json={},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_provision_token_inactive_client(self):
        """Provisioning for inactive client returns 409."""
        mock_conn = AsyncMock()
        inactive = MagicMock()
        inactive.__getitem__.side_effect = {
            "id": _CLIENT_ID,
            "is_active": False,
        }.__getitem__
        inactive.get.side_effect = {"id": _CLIENT_ID, "is_active": False}.get
        mock_conn.fetchrow = AsyncMock(return_value=inactive)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/admin/clients/{_CLIENT_ID}/tokens",
                json={"label": "doomed"},
            )

        assert response.status_code == 409


class TestListTokens:
    """GET /admin/clients/{id}/tokens"""

    @pytest.mark.asyncio
    async def test_list_tokens_metadata_only(self):
        """Listing tokens shows metadata — never the raw token or hash."""
        mock_conn = AsyncMock()
        c_row = MagicMock()
        c_row.__getitem__.side_effect = {"id": _CLIENT_ID}.__getitem__
        c_row.get.side_effect = {"id": _CLIENT_ID}.get
        mock_conn.fetchrow = AsyncMock(return_value=c_row)
        mock_conn.fetch = AsyncMock(return_value=[_token_row()])
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{_CLIENT_ID}/tokens")

        assert response.status_code == 200
        data = response.json()
        tokens = data["data"]
        assert len(tokens) == 1
        token = tokens[0]
        assert "token" not in token
        assert "token_hash" not in token
        assert token["token_prefix"] == "abcdefgh"

    @pytest.mark.asyncio
    async def test_list_tokens_client_not_found(self):
        """Listing tokens for nonexistent client returns 404."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.get(f"/admin/clients/{uuid.uuid4()}/tokens")

        assert response.status_code == 404


class TestRevokeToken:
    """POST /admin/clients/{id}/tokens/{token_id}/revoke"""

    @pytest.mark.asyncio
    async def test_revoke_token_returns_204(self):
        """Revoking a token returns 204."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/admin/clients/{_CLIENT_ID}/tokens/{_CREDENTIAL_ID}/revoke",
            )

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_revoke_already_revoked_returns_404(self):
        """Revoking an already-revoked token returns 404."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        client = _build_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/admin/clients/{_CLIENT_ID}/tokens/{_CREDENTIAL_ID}/revoke",
            )

        assert response.status_code == 404


# ── Auth middleware tests ──────────────────────────────────────────────────


class TestCollectorTokenAuth:
    """Tests for the ``require_collector_token`` FastAPI dependency."""

    @staticmethod
    def _setup_app(
        mock_conn: AsyncMock,
        *,
        monkeypatch,
    ):
        """Create a test app with the collector token route, overriding
        ``get_session`` and disabling API-key auth so only the collector
        token dependency is tested.
        """
        from fastapi import APIRouter, Depends

        from app.core.auth import require_collector_token
        from app.db.session import get_session

        # Disable API-key auth for these tests — we only want to test
        # the collector token dependency, not the admin API key middleware.
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "development")
        import importlib

        import app.core.config

        importlib.reload(app.core.config)

        app = create_app(configure_logging=False)

        test_router = APIRouter()

        @test_router.get("/test-collector-auth")
        async def _test_endpoint(
            identity: dict[str, str] = Depends(require_collector_token),
        ):
            return identity

        app.include_router(test_router)

        async def _override(request: Request):
            yield mock_conn

        app.dependency_overrides[get_session] = _override

        return app

    @pytest.mark.asyncio
    async def test_valid_token_passes(self, monkeypatch):
        """A valid, active token resolves successfully."""
        mock_conn = AsyncMock()
        auth_row = MagicMock()
        auth_row.__getitem__.side_effect = {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": None,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": "test-client",
            "client_is_active": True,
        }.__getitem__
        mock_conn.fetchrow = AsyncMock(return_value=auth_row)
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        raw_token, _, _ = generate_collector_token()

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {raw_token}"},
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["client_id"] == str(_CLIENT_ID)

    @pytest.mark.asyncio
    async def test_revoked_token_returns_401(self, monkeypatch):
        """A revoked token is rejected with 401."""
        mock_conn = AsyncMock()
        auth_row = MagicMock()
        auth_row.__getitem__.side_effect = {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": _TS,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": "test-client",
            "client_is_active": True,
        }.__getitem__
        mock_conn.fetchrow = AsyncMock(return_value=auth_row)

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer some-revoked-token"},
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, monkeypatch):
        """No Authorization header returns 401."""
        mock_conn = AsyncMock()

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_token_returns_401(self, monkeypatch):
        """A non-Bearer auth header returns 401."""
        mock_conn = AsyncMock()

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Basic some-token"},
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_inactive_client_token_returns_401(self, monkeypatch):
        """A token for an inactive client returns 401."""
        mock_conn = AsyncMock()
        auth_row = MagicMock()
        auth_row.__getitem__.side_effect = {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": None,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": "inactive-client",
            "client_is_active": False,
        }.__getitem__
        mock_conn.fetchrow = AsyncMock(return_value=auth_row)

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer some-token"},
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_token_returns_401(self, monkeypatch):
        """An empty Bearer token returns 401."""
        mock_conn = AsyncMock()

        app = self._setup_app(mock_conn, monkeypatch=monkeypatch)

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer  "},
        ) as c:
            response = await c.get("/test-collector-auth")

        assert response.status_code == 401


# ── Unit tests for token utilities ─────────────────────────────────────────


class TestTokenGeneration:
    """Unit tests for token generation and hashing."""

    def test_generate_collector_token_returns_tuple_of_three(self):
        """generate_collector_token returns (raw, hash, prefix)."""
        raw, h, prefix = generate_collector_token()
        assert isinstance(raw, str)
        assert isinstance(h, str)
        assert isinstance(prefix, str)
        assert len(raw) == 64  # token_urlsafe(48) → 64
        assert len(prefix) == 8

    def test_token_hash_is_deterministic(self):
        """hash_token produces the same hash for the same input."""
        raw, h, prefix = generate_collector_token()
        assert hash_token(raw) == h

    def test_token_prefix_matches_raw(self):
        """The prefix is the first 8 characters of the raw token."""
        raw, h, prefix = generate_collector_token()
        assert prefix == raw[:8]

    def test_unique_tokens_each_call(self):
        """Each call to generate_collector_token produces a different token."""
        t1 = generate_collector_token()
        t2 = generate_collector_token()
        assert t1[0] != t2[0]
        assert t1[1] != t2[1]


# ── Canonical source identity resolution tests ─────────────────────────────

_IDENTITY_A = uuid.uuid4()  # an existing canonical identity
_IDENTITY_B = uuid.uuid4()  # an identity resolved into _IDENTITY_A
_IDENTITY_PARENT = _IDENTITY_A
_QUARANTINE_ID = uuid.uuid4()
_RESOLUTION_ID = uuid.uuid4()
_COLLECTOR_SOURCE_ID = "collector-source-a"


def _row(data: dict) -> MagicMock:
    """Return a MagicMock that behaves like an asyncpg Record for dict access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _identity_row(
    id: uuid.UUID,
    canonical_parent_id: uuid.UUID | None = None,
) -> MagicMock:
    """Return a mock row resembling a source_identities row."""
    return _row(
        {
            "id": id,
            "client_id": _CLIENT_ID,
            "collector_source_id": _COLLECTOR_SOURCE_ID,
            "is_canonical": canonical_parent_id is None,
            "canonical_parent_id": canonical_parent_id,
            "resolved_at": None if canonical_parent_id is None else _TS,
            "created_at": _TS,
        }
    )


class TestResolveCanonicalIdentity:
    """resolve_canonical_identity maps collector source IDs to canonical UUIDs."""

    @pytest.mark.asyncio
    async def test_new_source_id_creates_identity_row(self, mock_conn: AsyncMock):
        """An unknown collector source ID creates a source_identities row."""
        new_id = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(side_effect=[None, _identity_row(new_id)])

        result = await resolve_canonical_identity(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == new_id
        calls = mock_conn.fetchrow.call_args_list
        assert len(calls) == 2
        lookup_sql, lookup_params = calls[0].args[0], calls[0].args[1:]
        assert "SELECT id, canonical_parent_id FROM source_identities" in lookup_sql
        assert lookup_params == (_CLIENT_ID, _COLLECTOR_SOURCE_ID)
        insert_sql, insert_params = calls[1].args[0], calls[1].args[1:]
        assert "INSERT INTO source_identities" in insert_sql
        assert "ON CONFLICT" in insert_sql
        assert insert_params == (_CLIENT_ID, _COLLECTOR_SOURCE_ID)

    @pytest.mark.asyncio
    async def test_known_source_id_returns_existing_identity(self, mock_conn: AsyncMock):
        """An already-known collector source ID returns its identity UUID."""
        mock_conn.fetchrow = AsyncMock(return_value=_identity_row(_IDENTITY_A))

        result = await resolve_canonical_identity(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == _IDENTITY_A
        assert mock_conn.fetchrow.await_count == 1
        sql = mock_conn.fetchrow.call_args.args[0]
        assert "SELECT id, canonical_parent_id FROM source_identities" in sql

    @pytest.mark.asyncio
    async def test_resolved_source_id_returns_parent_id(self, mock_conn: AsyncMock):
        """A resolved identity resolves to its canonical parent's UUID."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_identity_row(_IDENTITY_B, canonical_parent_id=_IDENTITY_PARENT)
        )

        result = await resolve_canonical_identity(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == _IDENTITY_PARENT

    @pytest.mark.asyncio
    async def test_lost_insert_race_re_reads_committed_row(self, mock_conn: AsyncMock):
        """A conflicting INSERT falls back to reading the winner's row."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[None, None, _identity_row(_IDENTITY_A)]
        )

        result = await resolve_canonical_identity(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == _IDENTITY_A
        assert mock_conn.fetchrow.await_count == 3


def _overlap_row(identity_id: uuid.UUID, count: int) -> MagicMock:
    """Return a mock row resembling one check_quarantine_overlap result row."""
    return _row({"overlapping_identity_id": identity_id, "overlap_count": count})


class TestCheckQuarantineOverlap:
    """check_quarantine_overlap detects record overlap with existing identities."""

    @pytest.mark.asyncio
    async def test_returns_overlap_evidence(self, mock_conn: AsyncMock):
        """Overlapping deliveries produce OverlapEvidence per existing identity."""
        mock_conn.fetch = AsyncMock(
            return_value=[
                _overlap_row(_IDENTITY_A, 4),
                _overlap_row(_IDENTITY_B, 2),
            ]
        )

        result = await check_quarantine_overlap(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == [
            OverlapEvidence(overlapping_identity_id=_IDENTITY_A, overlap_count=4),
            OverlapEvidence(overlapping_identity_id=_IDENTITY_B, overlap_count=2),
        ]
        sql, params = (
            mock_conn.fetch.call_args.args[0],
            mock_conn.fetch.call_args.args[1:],
        )
        assert "usage_ingest_attempts" in sql
        assert "GROUP BY" in sql
        assert params == (_CLIENT_ID, _COLLECTOR_SOURCE_ID)

    @pytest.mark.asyncio
    async def test_no_overlap_returns_empty_list(self, mock_conn: AsyncMock):
        """A candidate with no shared source_record_ids has no overlap evidence."""
        mock_conn.fetch = AsyncMock(return_value=[])

        result = await check_quarantine_overlap(
            mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_resolved_identities_are_not_overlap_targets(self, mock_conn: AsyncMock):
        """Identities already resolved into a parent do not trigger quarantine."""
        mock_conn.fetch = AsyncMock(return_value=[])

        await check_quarantine_overlap(mock_conn, _CLIENT_ID, _COLLECTOR_SOURCE_ID)

        sql = mock_conn.fetch.call_args.args[0]
        assert "canonical_parent_id IS NOT NULL" in sql


class TestCheckBatchOverlap:
    """check_batch_overlap performs one client-scoped set lookup."""

    @pytest.mark.asyncio
    async def test_returns_grouped_overlap_evidence(self, mock_conn: AsyncMock):
        other_identity = uuid.uuid4()
        record_ids = ["rec-1", "rec-2"]
        mock_conn.fetch = AsyncMock(return_value=[_overlap_row(other_identity, 2)])

        result = await check_batch_overlap(
            mock_conn, _CLIENT_ID, _IDENTITY_A, record_ids,
        )

        assert result == [OverlapEvidence(other_identity, 2)]
        assert mock_conn.fetch.await_count == 1
        sql, *params = mock_conn.fetch.call_args.args
        assert "usage_events" in sql
        assert "usage_ingest_attempts" in sql
        assert "original_source_record_id" in sql
        assert "UNION" in sql
        assert "source_record_id = ANY($2::text[])" in sql
        assert "si.client_id = $1" in sql
        assert params == [_CLIENT_ID, record_ids, _IDENTITY_A]

    @pytest.mark.asyncio
    async def test_attempts_leg_filters_to_accounting_outcomes(self, mock_conn: AsyncMock):
        """The attempts leg only flags accounting outcomes (accepted/duplicate/updated),
        not quarantined/conflicted/rejected deliveries that never produced accounting."""
        mock_conn.fetch = AsyncMock(return_value=[])
        await check_batch_overlap(mock_conn, _CLIENT_ID, _IDENTITY_A, ["rec-1"])
        sql = mock_conn.fetch.call_args.args[0]
        assert "a.outcome IN ('accepted', 'duplicate', 'updated')" in sql
        # Verify both exclusion rules appear in the SQL
        assert "canonical_parent_id IS NOT NULL" in sql

    @pytest.mark.asyncio
    async def test_legacy_only_overlap_returns_evidence(self, mock_conn: AsyncMock):
        """Overlap evidence surfaced exclusively from the attempts leg
        (legacy pre-canonical records) is still returned."""
        other_identity = uuid.uuid4()
        mock_conn.fetch = AsyncMock(
            return_value=[_overlap_row(other_identity, 3)],
        )
        result = await check_batch_overlap(
            mock_conn, _CLIENT_ID, _IDENTITY_A, ["rec-1"],
        )
        assert result == [OverlapEvidence(other_identity, 3)]
        assert mock_conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_batch_skips_database(self, mock_conn: AsyncMock):
        result = await check_batch_overlap(mock_conn, _CLIENT_ID, _IDENTITY_A, [])

        assert result == []
        mock_conn.fetch.assert_not_awaited()


class TestQuarantineIdentity:
    """quarantine_identity creates a quarantine entry for an overlapping identity."""

    @pytest.mark.asyncio
    async def test_creates_quarantine_and_returns_id(self, mock_conn: AsyncMock):
        """A quarantine row is inserted and its id returned."""
        mock_conn.fetchrow = AsyncMock(return_value=_row({"id": _QUARANTINE_ID}))

        result = await quarantine_identity(
            mock_conn, _IDENTITY_B, _IDENTITY_A, overlap_count=4
        )

        assert result == _QUARANTINE_ID
        sql, params = (
            mock_conn.fetchrow.call_args.args[0],
            mock_conn.fetchrow.call_args.args[1:],
        )
        assert "INSERT INTO source_identity_quarantine" in sql
        assert params == (_IDENTITY_B, _IDENTITY_A, 4)


def _quarantine_row(
    id: uuid.UUID = _QUARANTINE_ID,
    source_identity_id: uuid.UUID = _IDENTITY_B,
    overlapping_identity_id: uuid.UUID = _IDENTITY_A,
    overlap_count: int = 4,
    cleared_at: datetime | None = None,
    resolution_id: uuid.UUID | None = None,
) -> MagicMock:
    """Return a mock row resembling a source_identity_quarantine row."""
    return _row(
        {
            "id": id,
            "source_identity_id": source_identity_id,
            "overlapping_identity_id": overlapping_identity_id,
            "overlap_count": overlap_count,
            "quarantined_at": _TS,
            "cleared_at": cleared_at,
            "resolution_id": resolution_id,
        }
    )


class TestGetActiveQuarantines:
    """get_active_quarantines lists unresolved quarantines per client."""

    @pytest.mark.asyncio
    async def test_lists_unresolved_quarantines(self, mock_conn: AsyncMock):
        """Uncleared quarantines are returned as QuarantineRow objects."""
        other_quarantine = uuid.uuid4()
        mock_conn.fetch = AsyncMock(
            return_value=[
                _quarantine_row(),
                _quarantine_row(
                    id=other_quarantine,
                    source_identity_id=_IDENTITY_A,
                    overlapping_identity_id=_IDENTITY_B,
                    overlap_count=1,
                ),
            ]
        )

        result = await get_active_quarantines(mock_conn, _CLIENT_ID)

        assert result == [
            QuarantineRow(
                id=_QUARANTINE_ID,
                source_identity_id=_IDENTITY_B,
                overlapping_identity_id=_IDENTITY_A,
                overlap_count=4,
                quarantined_at=_TS,
                cleared_at=None,
                resolution_id=None,
            ),
            QuarantineRow(
                id=other_quarantine,
                source_identity_id=_IDENTITY_A,
                overlapping_identity_id=_IDENTITY_B,
                overlap_count=1,
                quarantined_at=_TS,
                cleared_at=None,
                resolution_id=None,
            ),
        ]
        sql, params = (
            mock_conn.fetch.call_args.args[0],
            mock_conn.fetch.call_args.args[1:],
        )
        assert "source_identity_quarantine" in sql
        assert "cleared_at IS NULL" in sql
        assert params == (_CLIENT_ID,)

    @pytest.mark.asyncio
    async def test_no_active_quarantines_returns_empty(self, mock_conn: AsyncMock):
        """A client with no unresolved quarantines gets an empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        result = await get_active_quarantines(mock_conn, _CLIENT_ID)

        assert result == []


class TestIsQuarantined:
    """is_quarantined reports whether an identity has an active quarantine."""

    @pytest.mark.asyncio
    async def test_true_when_quarantine_is_active(self, mock_conn: AsyncMock):
        """An uncleared quarantine means the identity is quarantined."""
        mock_conn.fetchval = AsyncMock(return_value=True)

        result = await is_quarantined(mock_conn, _IDENTITY_B)

        assert result is True
        sql, params = (
            mock_conn.fetchval.call_args.args[0],
            mock_conn.fetchval.call_args.args[1:],
        )
        assert "cleared_at IS NULL" in sql
        assert params == (_IDENTITY_B,)

    @pytest.mark.asyncio
    async def test_false_when_no_active_quarantine(self, mock_conn: AsyncMock):
        """A cleared or absent quarantine means the identity is not quarantined."""
        mock_conn.fetchval = AsyncMock(return_value=False)

        result = await is_quarantined(mock_conn, _IDENTITY_B)

        assert result is False


class TestResolveIdentity:
    """resolve_identity clears a quarantine and links the candidate to a parent."""

    @pytest.mark.asyncio
    async def test_records_resolution_clears_quarantine_and_links_identity(
        self, mock_conn: AsyncMock
    ):
        """Resolution is audited, the quarantine cleared, and the identity linked."""
        mock_conn.fetchrow = AsyncMock(return_value=_row({"id": _RESOLUTION_ID}))
        mock_conn.execute = AsyncMock()

        await resolve_identity(
            mock_conn,
            _QUARANTINE_ID,
            _IDENTITY_A,
            reason="same collector replayed after redeploy",
            resolved_by="admin@example.com",
        )

        # Audit row first: source_identity_resolutions
        insert_sql, insert_params = (
            mock_conn.fetchrow.call_args.args[0],
            mock_conn.fetchrow.call_args.args[1:],
        )
        assert "INSERT INTO source_identity_resolutions" in insert_sql
        assert insert_params == (
            _QUARANTINE_ID,
            _IDENTITY_A,
            "admin@example.com",
            "same collector replayed after redeploy",
        )

        # Then clear the quarantine and link the identity
        update_calls = mock_conn.execute.call_args_list
        assert len(update_calls) == 2
        clear_sql, clear_params = update_calls[0].args[0], update_calls[0].args[1:]
        assert "UPDATE source_identity_quarantine" in clear_sql
        assert "cleared_at" in clear_sql
        assert clear_params == (_QUARANTINE_ID, _RESOLUTION_ID)
        link_sql, link_params = update_calls[1].args[0], update_calls[1].args[1:]
        assert "UPDATE source_identities" in link_sql
        assert "is_canonical = false" in link_sql
        assert "canonical_parent_id" in link_sql
        assert link_params == (_QUARANTINE_ID, _IDENTITY_A)

    @pytest.mark.asyncio
    async def test_optional_reason_and_resolver_are_nullable(self, mock_conn: AsyncMock):
        """resolve_identity accepts None reason and resolved_by."""
        mock_conn.fetchrow = AsyncMock(return_value=_row({"id": _RESOLUTION_ID}))
        mock_conn.execute = AsyncMock()

        await resolve_identity(mock_conn, _QUARANTINE_ID, _IDENTITY_A, None, None)

        insert_params = mock_conn.fetchrow.call_args.args[1:]
        assert insert_params == (_QUARANTINE_ID, _IDENTITY_A, None, None)
        assert mock_conn.execute.await_count == 2
