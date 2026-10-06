"""MCP server bootstrap and the read-only Gateway tools.

The adapter is deliberately read-only: ``get_gateway_health`` calls only
``GET /health``, ``get_model_usage`` calls only
``GET /api/v1/usage/aggregates?group_by=model``, and ``get_agent_usage``
calls only ``GET /api/v1/usage/aggregates?group_by=agent``. No write/admin
or generic passthrough capability is exposed and no per-AFK-run model
attribution is performed.
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

MODEL_USAGE_TOOL_DESCRIPTION = (
    "Return usage aggregates grouped by model for an explicit date range from "
    "GET /api/v1/usage/aggregates?group_by=model. Requires start_date and end_date "
    "(ISO-8601). Optional Gateway-supported filters (client_id, model, session_id) "
    "are passed through only where the base API supports them. Preserves "
    "group_by=model semantics, session/record counts, token and cache fields, "
    "provider breakdown, and estimated cost with nulls preserved. No per-AFK-run "
    "model attribution is performed."
)

AGENT_USAGE_TOOL_DESCRIPTION = (
    "Return usage aggregates grouped by agent for an explicit date range from "
    "GET /api/v1/usage/aggregates?group_by=agent. Requires start_date and end_date "
    "(ISO-8601). Optional Gateway-supported filters (client_id, model, session_id) "
    "are passed through only where the base API supports them. Preserves "
    "group_by=agent semantics, session/record counts, token and cache fields, "
    "provider breakdown, and estimated cost with nulls preserved. No per-AFK-run "
    "model attribution is performed."
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


class UsageAggregateRow(BaseModel):
    """One usage aggregate row preserved from GET /api/v1/usage/aggregates.

    Mirrors ``app.core.schemas.usage.AggregateRow`` but preserves nulls and
    unknown future fields via ``extra="allow"``. Token, cache, session/record,
    provider breakdown, and cost fields are returned exactly as the Gateway
    reports them; ``None`` stays ``None``.
    """

    model_config = ConfigDict(extra="allow")

    group_value: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cost_usd: Any = None
    record_count: int = 0
    session_count: int = 0
    model_count: int = 0
    cache_hit_ratio: float | None = None
    provider_breakdown: dict[str, int] = {}
    project_label: str | None = None
    agent: str | None = None


def create_server(
    config: GatewayConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Create the MCP server with the read-only health and usage tools registered.

    ``http_client`` lets callers (tests) supply a controlled HTTP transport;
    when omitted the client owns its own ``httpx.AsyncClient``.
    """
    gateway = GatewayClient(config, http_client=http_client)
    server: MCPServer = MCPServer(SERVER_NAME, version=SERVER_VERSION)

    @server.tool(description=HEALTH_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
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

    @server.tool(description=MODEL_USAGE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_model_usage(
        start_date: str,
        end_date: str,
        client_id: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> list[UsageAggregateRow]:
        """Return usage aggregates grouped by model for the explicit date range."""
        try:
            rows = await gateway.get_usage_aggregates(
                start_date=start_date,
                end_date=end_date,
                group_by="model",
                client_id=client_id,
                model=model,
                session_id=session_id,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return [UsageAggregateRow.model_validate(r) for r in rows]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
            ) from None

    @server.tool(description=AGENT_USAGE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_agent_usage(
        start_date: str,
        end_date: str,
        client_id: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> list[UsageAggregateRow]:
        """Return usage aggregates grouped by agent for the explicit date range."""
        try:
            rows = await gateway.get_usage_aggregates(
                start_date=start_date,
                end_date=end_date,
                group_by="agent",
                client_id=client_id,
                model=model,
                session_id=session_id,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return [UsageAggregateRow.model_validate(r) for r in rows]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
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
