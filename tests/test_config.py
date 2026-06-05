"""Tests for the Settings configuration."""


def test_settings_defaults():
    """Settings should load with sensible defaults out of the box."""
    from app.core.config import Settings

    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000


def test_settings_port_override_from_env(monkeypatch):
    """The GATEWAY_PORT env var should override the default port."""
    monkeypatch.setenv("GATEWAY_PORT", "9000")

    from app.core.config import Settings

    settings = Settings()
    assert settings.port == 9000
