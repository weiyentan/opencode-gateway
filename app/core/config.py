"""Gateway settings loaded from environment variables with sensible defaults."""

from __future__ import annotations

import warnings

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level settings for the OpenCode Gateway observability service."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # Deployment environment: "production" | "development"
    # Controls whether an API key is required (production) or optional (development).
    # Maps to env var GATEWAY_ENV.
    env: str = "production"

    # API authentication
    # Requests must include an ``Authorization: Bearer <api-key>`` header.
    # Required in production mode unless GATEWAY_ALLOW_INSECURE_AUTH is set.
    api_key: str = ""

    # Explicit insecure-auth opt-in.  When ``true``, the Gateway starts
    # without an API key even in production mode and logs a loud warning.
    # Prefer GATEWAY_ENV=development for local work.
    allow_insecure_auth: bool = False

    @model_validator(mode="after")
    def _validate_auth_requirements(self) -> Settings:
        """Fail fast when an API key is required but not configured.

        Production mode requires an API key unless the operator has
        explicitly opted into insecure auth via
        ``GATEWAY_ALLOW_INSECURE_AUTH=true``.
        """
        if (
            self.env != "development"
            and not self.allow_insecure_auth
            and not self.api_key
        ):
            raise ValueError(
                "GATEWAY_API_KEY must be set in production mode. "
                "Set GATEWAY_ENV=development for local development, "
                "or GATEWAY_ALLOW_INSECURE_AUTH=true to explicitly "
                "opt-in to insecure mode."
            )
        if self.allow_insecure_auth:
            warnings.warn(
                "INSECURE AUTH: GATEWAY_ALLOW_INSECURE_AUTH is enabled. "
                "The Gateway is running without API key authentication. "
                "This is NOT safe for production deployments.",
                UserWarning,
                stacklevel=2,
            )
        return self

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

    # Grafana/Loki
    grafana_base_url: str = "http://localhost:3000"

    # Heartbeat monitoring
    # Collectors that haven't pushed telemetry within this many seconds
    # are considered stale. Maps to env var GATEWAY_HEARTBEAT_THRESHOLD.
    heartbeat_threshold: int = 300


def get_settings() -> Settings:
    """Return a Settings instance for use as a FastAPI dependency."""
    return Settings()
