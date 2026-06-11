"""Gateway settings loaded from environment variables with sensible defaults."""

from pydantic import Field
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

    # Executor plugin
    executor_type: str = "local"

    # Database
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "opencode_gateway"
    database_user: str = "opencode"
    database_password: str = ""
    database_min_connections: int = 2
    database_max_connections: int = 10
    database_connection_timeout: int = 30

    # Pre-flight policy thresholds — used by ObservationBasedPolicy to
    # decide whether a runner VM is healthy enough to accept a new job.
    # Values are expressed as percentages (0–100) or seconds.
    disk_threshold_percent: float = Field(
        default=80.0,
        description="Maximum disk-usage percentage allowed on a runner VM (0–100).",
    )
    memory_threshold_percent: float = Field(
        default=85.0,
        description="Maximum memory-usage percentage allowed on a runner VM (0–100).",
    )
    staleness_seconds: int = Field(
        default=600,
        description="Maximum age (in seconds) of the last telemetry sample from a runner.",
    )

    # Cleanup retention — duration after which a workspace is eligible for
    # automatic deletion, keyed by job outcome.
    cleanup_success_retention_hours: int = 72       # 3 days
    cleanup_failure_retention_hours: int = 168       # 7 days

    # Cleanup scheduler — controls the background cleanup loop.
    cleanup_interval_seconds: int = 900    # 15 minutes
    cleanup_batch_size: int = 10           # workspaces per tick


def get_settings() -> Settings:
    """Return a Settings instance for use as a FastAPI dependency."""
    return Settings()
