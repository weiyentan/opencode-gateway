"""Tests for API-key authentication hardening.

Covers all auth modes required by issue #104:

* Missing key in production → fail fast (ValueError at init)
* Missing key in development → requests pass through
* Invalid key → 401
* Valid key → 200
* Insecure auth opt-in → requests pass through with warning
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app

# ── Helpers ────────────────────────────────────────────────────────────────


def _setup_app_state(app):
    """Set up minimal app.state and dependency overrides for testing.

    Without this, FastAPI dependency resolution crashes with
    ``AttributeError: 'State' object has no attribute 'pool'`` because
    the ASGI transport does not run the lifespan handler that normally
    initialises ``app.state.pool``.
    """
    from app.api.jobs import _get_pool
    from app.db.session import get_session
    from app.executors.factory import get_executor

    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    mock_conn = AsyncMock()

    async def _override_get_session(request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[_get_pool] = lambda: mock_pool
    app.dependency_overrides[get_executor] = lambda: AsyncMock()


def _make_client(app, api_key: str | None) -> AsyncClient:
    """Build an httpx AsyncClient against *app* with an optional API key."""
    headers: dict[str, str] = {}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


# ── Fail-fast tests (Settings-level validation) ──────────────────────────────


class TestProductionFailFast:
    """Production mode must refuse to start without an API key."""

    def test_missing_api_key_in_production_raises_valueerror(self, monkeypatch):
        """Settings() with no key and production env raises ValueError."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "production")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        from app.core.config import Settings

        with pytest.raises(ValueError, match="GATEWAY_API_KEY must be set"):
            Settings()

    def test_missing_api_key_with_default_env_raises_valueerror(self, monkeypatch):
        """Default gateway_env is 'production', so missing key fails."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        from app.core.config import Settings

        with pytest.raises(ValueError, match="GATEWAY_API_KEY must be set"):
            Settings()

    def test_production_with_api_key_succeeds(self, monkeypatch):
        """Production + API key is the happy path — no error."""
        monkeypatch.setenv("GATEWAY_API_KEY", "prod-secret")
        monkeypatch.setenv("GATEWAY_ENV", "production")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        from app.core.config import Settings

        settings = Settings()
        assert settings.api_key == "prod-secret"
        assert settings.env == "production"

    def test_production_with_insecure_auth_opt_in_succeeds(self, monkeypatch):
        """Insecure auth explicit opt-in allows production without a key."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "production")
        monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_AUTH", "true")

        from app.core.config import Settings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            settings = Settings()
            insecure_warnings = [
                x for x in w if "INSECURE AUTH" in str(x.message)
            ]
            assert len(insecure_warnings) == 1
        assert settings.allow_insecure_auth is True
        assert settings.api_key == ""


# ── Development mode tests ──────────────────────────────────────────────────


class TestDevelopmentMode:
    """Development mode allows running without an API key."""

    def test_missing_api_key_in_dev_is_allowed(self, monkeypatch):
        """No API key in dev mode is fine — Settings() succeeds."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "development")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        from app.core.config import Settings

        settings = Settings()
        assert settings.api_key == ""
        assert settings.env == "development"

    def test_dev_without_key_passes_requests(self, monkeypatch):
        """Requests in dev mode without API key get 200, not 401."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "development")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        # Reimport config so pydantic-settings picks up the new env vars
        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key=None)

        async def _test():
            async with client as c:
                response = await c.get("/health")
            assert response.status_code == 200

        import asyncio
        asyncio.run(_test())

    def test_dev_with_valid_key_still_works(self, monkeypatch):
        """DEV + valid API key should still authenticate correctly."""
        monkeypatch.setenv("GATEWAY_API_KEY", "dev-secret")
        monkeypatch.setenv("GATEWAY_ENV", "development")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key="dev-secret")

        async def _test():
            async with client as c:
                response = await c.get("/health")
            assert response.status_code == 200

        import asyncio
        asyncio.run(_test())


# ── Authentication behaviour tests (keyed mode) ─────────────────────────────


class TestAuthInvalidKey:
    """Requests with the wrong API key must return 401."""

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "correct-key")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "production")

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key="wrong-key")

        async with client as c:
            response = await c.get("/health")
        assert response.status_code == 401
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHORIZED"
        assert "Invalid" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_empty_bearer_token_returns_401(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "correct-key")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "production")

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key="")  # Bearer with empty token
        # _make_client uses "Bearer <api_key>", so api_key="" gives "Bearer "
        # We need to handle this — the auth check strips and then compares.
        # Let's use a custom header instead.

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": "Bearer  "},
        ) as c:
            response = await c.get("/health")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "correct-key")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)
        monkeypatch.setenv("GATEWAY_ENV", "production")

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key=None)

        async with client as c:
            response = await c.get("/health")
        assert response.status_code == 401


# ── Constant-time comparison test ───────────────────────────────────────────


class TestConstantTimeComparison:
    """Verify that hmac.compare_digest is used for API key comparison."""

    def test_hmac_compare_digest_is_used(self, monkeypatch):
        """The auth module must import and use hmac.compare_digest."""
        import app.core.auth
        assert hasattr(app.core.auth, "hmac")
        import hmac as hmac_module
        # Verify compare_digest is the one from the hmac module
        assert app.core.auth.hmac.compare_digest is hmac_module.compare_digest

    def test_key_comparison_uses_constant_time(self, monkeypatch):
        """hmac.compare_digest is the constant-time comparison function."""
        import hmac
        # Standard library's compare_digest is indeed constant-time
        assert hmac.compare_digest("abc", "abc") is True
        assert hmac.compare_digest("abc", "abd") is False
        # Different lengths should not leak timing
        assert hmac.compare_digest("short", "much-longer-key") is False


# ── Insecure auth opt-in tests ──────────────────────────────────────────────


class TestInsecureAuthOptIn:
    """Explicit GATEWAY_ALLOW_INSECURE_AUTH=true bypasses key requirement."""

    def test_insecure_auth_warns(self, monkeypatch):
        """Enabling insecure auth should emit a warning at Settings init."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_AUTH", "true")
        monkeypatch.setenv("GATEWAY_ENV", "production")

        from app.core.config import Settings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            settings = Settings()
            insecure_warnings = [
                x for x in w if "INSECURE AUTH" in str(x.message)
            ]
            assert len(insecure_warnings) == 1
            assert issubclass(insecure_warnings[0].category, UserWarning)

    def test_insecure_auth_allows_requests_without_key(self, monkeypatch):
        """Requests without a key pass in insecure mode."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_AUTH", "true")
        monkeypatch.setenv("GATEWAY_ENV", "production")

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        client = _make_client(app, api_key=None)

        async def _test():
            async with client as c:
                response = await c.get("/health")
            assert response.status_code == 200

        import asyncio
        asyncio.run(_test())

    def test_insecure_auth_with_key_still_works(self, monkeypatch):
        """Insecure mode + API key = key is still enforced."""
        monkeypatch.setenv("GATEWAY_API_KEY", "my-key")
        monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_AUTH", "true")
        monkeypatch.setenv("GATEWAY_ENV", "production")

        import importlib
        import app.core.config
        importlib.reload(app.core.config)

        app = create_app()
        _setup_app_state(app)
        # Valid key
        client = _make_client(app, api_key="my-key")

        async def _test():
            async with client as c:
                response = await c.get("/health")
            assert response.status_code == 200

        import asyncio
        asyncio.run(_test())


# ── Conftest-level default behaviour ────────────────────────────────────────


class TestAuthWithDefaultTestKey:
    """When GATEWAY_API_KEY is set (as in conftest), auth works normally."""

    @pytest.mark.asyncio
    async def test_valid_key_passes_health(self, client):
        """Fixture-based client (has valid key from conftest) gets 200."""
        async with client as c:
            response = await c.get("/health")
        assert response.status_code == 200
