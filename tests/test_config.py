"""Tests for the Settings configuration."""

from __future__ import annotations

import os
import warnings

import pytest


def test_settings_defaults(monkeypatch):
    """Settings should load with sensible defaults out of the box.
    
    Note: defaults now require an API key (gateway_env defaults to
    'production'), so we set one for this test.
    """
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key-for-defaults")
    from app.core.config import Settings

    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.env == "production"
    assert settings.allow_insecure_auth is False


def test_database_settings_defaults(monkeypatch):
    """Database settings should have sensible defaults."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings

    settings = Settings()
    assert settings.database_host == "localhost"
    assert settings.database_port == 5432
    assert settings.database_name == "opencode_gateway"
    assert settings.database_user == "opencode"
    assert settings.database_password == ""
    assert settings.database_min_connections == 2
    assert settings.database_max_connections == 10
    assert settings.database_connection_timeout == 30


def test_settings_port_override_from_env(monkeypatch):
    """The GATEWAY_PORT env var should override the default port."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_PORT", "9000")

    from app.core.config import Settings

    settings = Settings()
    assert settings.port == 9000


def test_database_settings_override_from_env(monkeypatch):
    """The GATEWAY_DATABASE_* env vars should override database defaults."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_DATABASE_HOST", "db.example.com")
    monkeypatch.setenv("GATEWAY_DATABASE_PORT", "6543")
    monkeypatch.setenv("GATEWAY_DATABASE_NAME", "testdb")
    monkeypatch.setenv("GATEWAY_DATABASE_USER", "testuser")
    monkeypatch.setenv("GATEWAY_DATABASE_PASSWORD", "s3cret")
    monkeypatch.setenv("GATEWAY_DATABASE_MIN_CONNECTIONS", "5")
    monkeypatch.setenv("GATEWAY_DATABASE_MAX_CONNECTIONS", "20")
    monkeypatch.setenv("GATEWAY_DATABASE_CONNECTION_TIMEOUT", "60")

    from app.core.config import Settings

    settings = Settings()
    assert settings.database_host == "db.example.com"
    assert settings.database_port == 6543
    assert settings.database_name == "testdb"
    assert settings.database_user == "testuser"
    assert settings.database_password == "s3cret"
    assert settings.database_min_connections == 5
    assert settings.database_max_connections == 20
    assert settings.database_connection_timeout == 60


def test_dotenv_file_loading(tmp_path, monkeypatch):
    """A .env file in the working directory should be loaded."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY_PORT=9999\nGATEWAY_DATABASE_HOST=dotenv-host\n")

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)

        from importlib import reload

        import app.core.config

        reload(app.core.config)

        from app.core.config import Settings

        settings = Settings(_env_file=".env")
        assert settings.port == 9999
        assert settings.database_host == "dotenv-host"
    finally:
        os.chdir(cwd)


def test_get_settings_dependency(monkeypatch):
    """get_settings() should return a Settings instance."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings, get_settings

    result = get_settings()
    assert isinstance(result, Settings)


def test_get_settings_dependency_uses_env(monkeypatch):
    """get_settings() should pick up env var changes."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_PORT", "7777")

    from app.core.config import get_settings

    settings = get_settings()
    assert settings.port == 7777


# ── Issue #104: Auth hardening settings tests ───────────────────────────


def test_env_default(monkeypatch):
    """env defaults to 'production'."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings

    settings = Settings()
    assert settings.env == "production"


def test_env_override(monkeypatch):
    """GATEWAY_ENV can be overridden to 'development'."""
    monkeypatch.setenv("GATEWAY_ENV", "development")
    from app.core.config import Settings

    settings = Settings()
    assert settings.env == "development"


def test_allow_insecure_auth_default(monkeypatch):
    """allow_insecure_auth defaults to False."""
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = Settings()
    assert settings.allow_insecure_auth is False


def test_allow_insecure_auth_override(monkeypatch):
    """GATEWAY_ALLOW_INSECURE_AUTH can be set via environment variable."""
    monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_AUTH", "true")
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    from app.core.config import Settings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = Settings()
    assert settings.allow_insecure_auth is True


def test_missing_api_key_in_production_fails(monkeypatch):
    """Production mode with no API key raises ValueError."""
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

    from app.core.config import Settings

    with pytest.raises(ValueError, match="GATEWAY_API_KEY must be set"):
        Settings()


def test_missing_api_key_in_dev_succeeds(monkeypatch):
    """Development mode without API key is allowed."""
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("GATEWAY_ENV", "development")
    monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_AUTH", raising=False)

    from app.core.config import Settings

    settings = Settings()
    assert settings.api_key == ""
    assert settings.env == "development"


def test_insecure_auth_opt_in_without_key(monkeypatch):
    """Insecure auth opt-in allows production without API key and warns."""
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

    assert settings.allow_insecure_auth is True
    assert settings.api_key == ""
