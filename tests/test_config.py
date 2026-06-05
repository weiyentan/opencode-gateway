"""Tests for the Settings configuration."""

import os


def test_settings_defaults():
    """Settings should load with sensible defaults out of the box."""
    from app.core.config import Settings

    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000


def test_database_settings_defaults():
    """Database settings should have sensible defaults."""
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
    monkeypatch.setenv("GATEWAY_PORT", "9000")

    from app.core.config import Settings

    settings = Settings()
    assert settings.port == 9000


def test_database_settings_override_from_env(monkeypatch):
    """The GATEWAY_DATABASE_* env vars should override database defaults."""
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


def test_dotenv_file_loading(tmp_path):
    """A .env file in the working directory should be loaded."""
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


def test_get_settings_dependency():
    """get_settings() should return a Settings instance."""
    from app.core.config import Settings, get_settings

    result = get_settings()
    assert isinstance(result, Settings)


def test_get_settings_dependency_uses_env(monkeypatch):
    """get_settings() should pick up env var changes."""
    monkeypatch.setenv("GATEWAY_PORT", "7777")

    from app.core.config import get_settings

    settings = get_settings()
    assert settings.port == 7777
