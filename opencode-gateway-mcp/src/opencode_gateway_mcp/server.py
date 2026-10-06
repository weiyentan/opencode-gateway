"""MCP server bootstrap and read-only tools.

The adapter is deliberately read-only: ``get_gateway_health`` and
``get_change_request_story`` are the only registered tools. Each calls only
its single published Gateway API path, and no write/admin, issue reverse
lookup, or generic passthrough capability is exposed.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError

from opencode_gateway_mcp.client import (
    GatewayClient,
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
)

SERVER_NAME = "opencode-gateway-mcp"
SERVER_VERSION = "0.1.0"

HEALTH_TOOL_DESCRIPTION = (
    "Return OpenCode Gateway service and ingestion health from GET /health: "
    "Gateway status/version, database connectivity, most recent ingest, remote "
    "collector health, and source-database health. Values (including "
    "healthy/stale/unknown and nulls) are preserved exactly as the Gateway reports them."
)

CHANGE_REQUEST_TOOL_DESCRIPTION = (
    "Return AFK evidence for one GitHub PR or GitLab MR from "
    "GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}: "
    "linked AFK runs and AWX execution bindings/sessions with link provenance, "
    "provider lifecycle (open/closed/merged), merge time, timeline/provenance, "
    "usage and estimated cost where present. Null/unavailable values are preserved "
    "as null. Supports github and gitlab. No issue reverse lookup is performed."
)

_VALID_PROVIDERS = frozenset({"github", "gitlab"})


class CollectorHealth(BaseModel):
    """One remote-collector client health row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    client_id: str
    client_name: str
    last_heartbeat: str | None = None
    total_records_ingested: int = 0
    health: str


class SourceDatabaseHealth(BaseModel):
    """One source-database health row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    source_database_id: str
    client_name: str
    last_push: str | None = None
    record_count: int = 0
    health: str


class GatewayHealth(BaseModel):
    """Structured Gateway health facts returned by ``get_gateway_health``.

    Fields mirror the published ``GET /health`` response. Timestamps stay
    strings so the Gateway's own representation is preserved, ``None`` stays
    ``None``, and ``extra="allow"`` keeps unknown future Gateway fields.
    """

    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    version: str
    database: str = "disconnected"
    last_ingest_timestamp: str | None = None
    collectors: list[CollectorHealth] = []
    source_databases: list[SourceDatabaseHealth] = []


# ── Change-request story models (GET /api/v1/afk-outcomes/change-requests/...) ──


class ChangeRequestExecutionCounts(BaseModel):
    """Aggregated execution counts for one change request."""

    model_config = ConfigDict(extra="allow")

    total: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class ChangeRequestDetailSummary(BaseModel):
    """The change_request summary block inside the detail."""

    model_config = ConfigDict(extra="allow")

    provider: str
    repository: str
    external_id: str
    resource_type: str = "change_request"
    provider_state: str | None = None
    total_estimated_cost_usd: Any | None = None
    latest_linked_activity: str | None = None
    provider_state_observed_at: str | None = None
    merged_at: str | None = None
    title: str | None = None
    executions: ChangeRequestExecutionCounts | None = None


class ChangeRequestLinkedRun(BaseModel):
    """One AFK run linked to the change request, with link provenance."""

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    link_sources: list[str] = []


class ChangeRequestExecutionItem(BaseModel):
    """One linked AWX execution binding with per-execution telemetry."""

    model_config = ConfigDict(extra="allow")

    awx_job: dict[str, Any]
    external_session_id: str | None = None
    session_id: str | None = None
    afk_run_id: str | None = None
    outcome: str | None = None
    purpose: str | None = None
    trigger_type: str | None = None
    source_event_id: str | None = None
    branch: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    failure_reason: str | None = None
    failure_summary: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_cache_read_tokens: int | None = None
    total_cache_write_tokens: int | None = None
    estimated_cost_usd: Any | None = None


class ChangeRequestSessionLink(BaseModel):
    """One linked session with telemetry; nulls preserved."""

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    external_session_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    inferred: bool = True
    agent: str | None = None
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cost_usd: Any | None = None
    parent_session_id: str | None = None


class ChangeRequestUsageAggregate(BaseModel):
    """Aggregate usage/cost across linked sessions."""

    model_config = ConfigDict(extra="allow")

    active_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Any | None = None
    message_count: int = 0
    session_count: int = 0


class ChangeRequestMergeState(BaseModel):
    """Provider merge state derived from observed facts."""

    model_config = ConfigDict(extra="allow")

    state: str
    merged_at: str | None = None


class ChangeRequestTimelineEvent(BaseModel):
    """One observed change_request fact in the provenance timeline."""

    model_config = ConfigDict(extra="allow")

    event_type: str
    occurred_at: str
    observed_via: str | None = None
    snapshot_at: str | None = None
    actor: str | None = None


class ChangeRequestTimeline(BaseModel):
    """Optional provenance timeline (chronologically ordered)."""

    model_config = ConfigDict(extra="allow")

    events: list[ChangeRequestTimelineEvent] = []


class ChangeRequestStory(BaseModel):
    """Structured AFK evidence for one GitHub PR or GitLab MR.

    Fields mirror the published GET /api/v1/afk-outcomes/change-requests/... response.
    Timestamps stay strings so the Gateway's own representation is preserved,
    ``None`` stays ``None``, and ``extra="allow"`` keeps unknown future fields.
    No issue reverse lookup or unsupported join is performed.
    """

    model_config = ConfigDict(extra="allow")

    change_request: ChangeRequestDetailSummary
    afk_runs: list[ChangeRequestLinkedRun] = []
    executions: list[ChangeRequestExecutionItem] = []
    sessions: list[ChangeRequestSessionLink] = []
    usage: ChangeRequestUsageAggregate = ChangeRequestUsageAggregate()
    total_estimated_cost_usd: Any | None = None
    merge_state: ChangeRequestMergeState | None = None
    timeline: ChangeRequestTimeline | None = None


def create_server(
    config: GatewayConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Create the MCP server with the read-only tools registered.

    ``http_client`` lets callers (tests) supply a controlled HTTP transport;
    when omitted the client owns its own ``httpx.AsyncClient``.
    """
    gateway = GatewayClient(config, http_client=http_client)
    server: MCPServer = MCPServer(SERVER_NAME, version=SERVER_VERSION)

    @server.tool(description=HEALTH_TOOL_DESCRIPTION)
    async def get_gateway_health() -> GatewayHealth:
        try:
            payload: dict[str, Any] = await gateway.get_health()
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return GatewayHealth.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /health payload shape"
            ) from None

    @server.tool(description=CHANGE_REQUEST_TOOL_DESCRIPTION)
    async def get_change_request_story(
        provider: str,
        repository: str,
        external_number: str,
    ) -> ChangeRequestStory:
        # Lightweight local validation so invalid identities surface without an
        # unnecessary Gateway round-trip; the Gateway's own 400 is still surfaced
        # when the local check passes but the provider rejects.
        if provider not in _VALID_PROVIDERS:
            valid = ", ".join(sorted(_VALID_PROVIDERS))
            raise ToolError(f"Invalid provider: {provider!r}. Valid values: {valid}")
        if not repository or not repository.strip():
            raise ToolError(
                "Invalid change-request identity: repository must be a non-empty string"
            )
        if not external_number or not external_number.strip():
            raise ToolError(
                "Invalid change-request identity: external number must be a non-empty string"
            )
        try:
            payload: dict[str, Any] = await gateway.get_change_request_detail(
                provider.strip(), repository.strip(), external_number.strip()
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return ChangeRequestStory.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected change-request payload shape"
            ) from None

    return server


def main() -> None:
    """Run the MCP server over stdio using environment configuration."""
    try:
        config = GatewayConfig.from_env()
    except GatewayConfigError as exc:
        # stderr only: stdout is the MCP stdio protocol channel.
        print(f"{SERVER_NAME}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    create_server(config).run()


if __name__ == "__main__":
    main()
