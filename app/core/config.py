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
    database_max_inactive_connection_lifetime: int = 1800
    database_ssl: str | None = None

    # Timeout budgets (seconds) for layered request processing
    #   database: per-query timeout via asyncio.timeout
    #   status_computation: _compute_status timeout
    #   total_request: endpoint-level total budget
    database_timeout_seconds: int = 5
    status_computation_timeout_seconds: int = 2
    total_request_timeout_seconds: int = 20

    # Agent run status derivation thresholds (issue #300).
    #   quiet_threshold_minutes: last message within this window → "running"
    #   stale_threshold_hours:   quiet beyond this window (but within
    #                            unknown_threshold_hours) → "stale"
    #   unknown_threshold_hours: quiet beyond this window → "unknown"
    # Consumed by ``_compute_status`` and ``_status_case_expression`` in
    # ``app/api/usage.py``.  Maps to GATEWAY_QUIET_THRESHOLD_MINUTES,
    # GATEWAY_STALE_THRESHOLD_HOURS, GATEWAY_UNKNOWN_THRESHOLD_HOURS.
    quiet_threshold_minutes: int = 15
    stale_threshold_hours: int = 2
    unknown_threshold_hours: int = 48

    # Grafana/Loki
    grafana_base_url: str = "http://localhost:3000"

    # Heartbeat monitoring
    # Collectors that haven't pushed telemetry within this many seconds
    # are considered stale. Maps to env var GATEWAY_HEARTBEAT_THRESHOLD.
    heartbeat_threshold: int = 300

    # Validation detail logging
    # When ``true``, structured field-level validation details are logged
    # for 422 validation errors on the ingest endpoint.  Field values are
    # passed through the existing redaction path before logging, but DO
    # NOT enable this in production unless you have reviewed what data
    # your collectors send — the redaction patterns may not catch all
    # sensitive information embedded in field values.
    log_validation_detail: bool = False

    # Kafka consumer — bridges usage records to the Gateway ingest API.
    # The consumer runs as a separate container alongside the Gateway
    # and reads from the ``opencode-usage`` topic.
    kafka_brokers: str = "localhost:9092"
    kafka_topic: str = "opencode-usage"
    kafka_dlq_topic: str = "opencode-usage-dlq"
    consumer_group_id: str = "opencode-gateway"

    # Gateway URL and collector token for the Kafka consumer to POST to
    # the Gateway's /ingest endpoint.
    base_url: str = "http://localhost:8000"
    collector_token: str = ""

    # AFK outcome consumer (issue #451) — the live ingestion side of AFK
    # Outcome Observability.  Runs in its OWN Kafka consumer group
    # (``opencode-outcomes``, never the usage consumer's ``opencode-gateway``
    # group), consuming the existing provider-events topic (external — the
    # topic is not created here) and mapping message types to canonical
    # engineering events.  Terminal states the topic does not carry
    # (merged/closed) are converged by a scheduled reconciliation loop that
    # reuses the backfill engine over a bounded window.
    afk_outcomes_topic: str = "afk.events"
    afk_outcomes_dlq_topic: str = "afk.events-dlq"
    afk_outcomes_consumer_group_id: str = "opencode-outcomes"
    afk_outcomes_provider: str = "github"
    afk_outcomes_repository: str = ""
    afk_outcomes_reconcile_cadence_seconds: float = 3600.0
    afk_outcomes_reconcile_window_seconds: float = 86400.0

    # Whether the AFK outcome consumer/backfill is in use for this process.
    # The Gateway API is read-only over the AFK read-model, so it does not
    # require ``afk_outcomes_repository``; the companion consumer/backfill
    # containers do.  When enabled, the repository (owner/repo to reconcile)
    # must be configured or the consumer's reconcile loop would retry forever
    # against an empty repository (adapter error, caught and logged).
    afk_outcomes_consumer_enabled: bool = False

    @model_validator(mode="after")
    def _validate_afk_outcomes_requirements(self) -> Settings:
        """Fail fast when the AFK consumer is enabled without a repository.

        The AFK consumer reconciles a bounded window against
        ``GATEWAY_AFK_OUTCOMES_REPOSITORY``; an empty repository makes the
        backfill adapter error inside the silently-logging reconcile loop.
        The read-only API never needs it, so the check is gated on
        ``GATEWAY_AFK_OUTCOMES_CONSUMER_ENABLED``.
        """
        if self.afk_outcomes_consumer_enabled and not self.afk_outcomes_repository.strip():
            raise ValueError(
                "GATEWAY_AFK_OUTCOMES_REPOSITORY must be set when the AFK "
                "outcome consumer is enabled "
                "(GATEWAY_AFK_OUTCOMES_CONSUMER_ENABLED=true)."
            )
        return self

    # Execution-transcript retention (issue #470, ADR 0016 "Redaction and
    # Privacy").  Per-table retention windows (days) for the append-only
    # transcript tables (observed_messages / observed_parts /
    # observed_tool_calls), enforced by ``scripts/retention_transcripts.py``
    # on a schedule.  Transcript data is higher-volume and lower-longevity
    # than accounting data: parts and tool calls default to 90 days while
    # messages (the session reconstruction surface) keep a longer 365-day
    # window.  Retention is keyed on the transcript timestamps
    # (``source_created_at_tz``), never ingest time.  Accounting aggregates
    # (usage events, rollup) keep their existing, longer retention and are
    # never touched by the job.  Maps to
    # GATEWAY_TRANSCRIPT_RETENTION_MESSAGES_DAYS,
    # GATEWAY_TRANSCRIPT_RETENTION_PARTS_DAYS,
    # GATEWAY_TRANSCRIPT_RETENTION_TOOL_CALLS_DAYS.
    transcript_retention_messages_days: int = 365
    transcript_retention_parts_days: int = 90
    transcript_retention_tool_calls_days: int = 90


def get_settings() -> Settings:
    """Return a Settings instance for use as a FastAPI dependency."""
    return Settings()
