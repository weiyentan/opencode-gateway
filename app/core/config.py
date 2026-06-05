"""Gateway settings loaded from environment variables with sensible defaults."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Top-level settings for the OpenCode Gateway."""

    model_config = {"env_prefix": "GATEWAY_", "case_sensitive": False}

    host: str = "0.0.0.0"
    port: int = 8000
