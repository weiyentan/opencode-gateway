"""Gateway settings loaded from environment variables with sensible defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level settings for the OpenCode Gateway."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "opencode_gateway"
    database_user: str = "opencode"
    database_password: str = ""
    database_min_connections: int = 2
    database_max_connections: int = 10
    database_connection_timeout: int = 30


def get_settings() -> Settings:
    """Return a Settings instance for use as a FastAPI dependency."""
    return Settings()
