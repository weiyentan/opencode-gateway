"""MCP server bootstrap and the read-only gateway tools.

The adapter is deliberately read-only: ``get_gateway_health`` and
``get_afk_run_story`` are the only registered tools, they call only
published Gateway GET endpoints on the OpenCode Gateway API, and no
write/admin or generic passthrough capability is exposed.
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

STORY_TOOL_DESCRIPTION = (
    "Return the complete AFK run story for one afk_run_id: the canonical run detail "
    "(issues, change requests, reviews, commits, merge events, agents, sessions, "
    "usage/cost) from GET /api/v1/afk-outcomes/runs/{afk_run_id} plus the full "
    "run-scoped AWX execution history including failed attempts and retries from "
    "GET /api/v1/afk/executions/runs/{afk_run_id} using the same explicit afk_run_id. "
    "Approved failure reason/summary metadata is preserved without raw prompts/stdout/secrets; "
    "no additional relationships are inferred; nulls and errors are preserved."
)


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


# ── AFK run story models ────────────────────────────────────────────────


class AfkRunSummary(BaseModel):
    """The run aggregate as reported by the canonical run-detail API."""

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class AfkUsageAggregate(BaseModel):
    """Per-run usage/cost aggregates preserving nulls and token vocabulary."""

    model_config = ConfigDict(extra="allow")

    active_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost_usd: float | str | None = None
    message_count: int | None = None
    session_count: int | None = None


class AfkExecutionBinding(BaseModel):
    """One AWX execution binding as reported by the run-scoped execution history.

    Approved failure metadata (failure_reason/failure_summary) is carried verbatim
    — bounded and redacted by the Gateway — without raw prompts, stdout, or secrets.
    ``extra="allow"`` preserves unknown future fields; nulls are preserved.
    """

    model_config = ConfigDict(extra="allow")

    binding_id: str | None = None
    awx_job: dict[str, Any] | None = None
    external_session_id: str | None = None
    external_session_ids: list[str] | None = None
    resource: dict[str, Any] | None = None
    outcome: str | None = None
    afk_run_id: str | None = None
    trigger_type: str | None = None
    source_event_id: str | None = None
    branch: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    failure_summary: str | None = None


class AfkRunStory(BaseModel):
    """Complete AFK run story combining canonical run-detail and execution history.

    All collections and scalars are preserved exactly as the Gateway reports them,
    including ``None``/``null``. No additional relationships are inferred by the
    adapter — the story is the mechanical composition of the two run-scoped APIs
    keyed by the same explicit ``afk_run_id``.
    """

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    run: AfkRunSummary | dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    issues: list[dict[str, Any]] = []
    change_requests: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    merge_events: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    agents: list[str] = []
    usage: AfkUsageAggregate | dict[str, Any] | None = None
    executions: list[AfkExecutionBinding] = []


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

    @server.tool(description=STORY_TOOL_DESCRIPTION)
    async def get_afk_run_story(afk_run_id: str) -> AfkRunStory:
        """Return the complete AFK run story for one ``afk_run_id``.

        Composition rule is mechanical: the same explicit ``afk_run_id`` is used
        for both ``GET /api/v1/afk-outcomes/runs/{afk_run_id}`` (canonical
        run-detail) and ``GET /api/v1/afk/executions/runs/{afk_run_id}``
        (run-scoped AWX execution history). No additional relationships are
        invented. A failure in either required call is surfaced as an
        MCP-visible error rather than a partial success. ``null`` values are
        preserved.
        """
        if not isinstance(afk_run_id, str) or not afk_run_id.strip():
            raise ToolError("afk_run_id must be a non-empty string")
        # Preserve the exact supplied id for composition; strip only for emptiness check.
        trimmed = afk_run_id.strip()
        try:
            detail = await gateway.get_afk_run_detail(trimmed)
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            executions_raw = await gateway.get_afk_executions_for_run(trimmed)
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        # Build the composite story without inventing relationships.
        try:
            # Coerce executions through the binding model to validate shape while
            # preserving unknown future fields via extra="allow". Nulls stay None.
            executions = [AfkExecutionBinding.model_validate(item) for item in executions_raw]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk/executions/runs/{afk_run_id} payload shape"
            ) from None
        # The canonical detail fields are carried verbatim — nulls preserved,
        # no remapping or inference.
        story_payload: dict[str, Any] = {
            "afk_run_id": trimmed,
            "run": detail.get("run"),
            "outcome": detail.get("outcome"),
            "issues": detail.get("issues", []),
            "change_requests": detail.get("change_requests", []),
            "reviews": detail.get("reviews", []),
            "commits": detail.get("commits", []),
            "merge_events": detail.get("merge_events", []),
            "sessions": detail.get("sessions", []),
            "agents": detail.get("agents", []),
            "usage": detail.get("usage"),
            "executions": [e.model_dump(mode="json", exclude_none=False) for e in executions],
        }
        try:
            return AfkRunStory.model_validate(story_payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected afk run story payload shape"
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
