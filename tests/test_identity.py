"""Tests for the identity layer — client CRUD, token management, and
collector token auth.

All tests use the mock database connection from conftest.py and follow
the existing test conventions (httpx AsyncClient + mock asyncpg).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.core.identity import generate_collector_token, hash_token

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
) -> MagicMock:
    """Return a mock row resembling an opencode_clients row."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "id": id,
        "name": name,
        "description": description,
        "is_active": is_active,
        "created_at": _TS,
        "updated_at": _TS,
    }.__getitem__
    row.get.side_effect = {
        "id": id,
        "name": name,
        "description": description,
        "is_active": is_active,
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
        original = _client_row(name="old-name")
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
        from fastapi import APIRouter, Depends, Request

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
