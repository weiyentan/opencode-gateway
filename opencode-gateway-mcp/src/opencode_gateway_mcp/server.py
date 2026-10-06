"""MCP server bootstrap and the read-only correlation quality tools.

The adapter is deliberately read-only: tools call only published Gateway GET
endpoints and no write/admin or generic passthrough capability is exposed.
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

CORRELATIONS_TOOL_DESCRIPTION = (
    "Return unresolved AFK correlation quality problems from "
    "GET /api/v1/afk-outcomes/correlations: unresolved correlation identity, "
    "AFK run identity, entity identity, reason (ambiguous or unmatched), "
    "candidates, and provenance. Supports optional reason filtering "
    "(ambiguous/unmatched) and explicit limit/offset pagination without "
    "silent crawl. Nulls are preserved and candidates are never tie-broken."
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


class CorrelationIssue(BaseModel):
    """One unresolved correlation row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    entity_id: str
    entity_type: str
    external_id: str
    provider: str
    repository: str
    afk_run_id: str | None = None
    method: str
    reason: str | None = None
    correlation_confidence: float = 0.0
    candidates: list[str] = []
    evidence: list[Any] = []
    resolver_version: str | None = None
    created_at: str | None = None
    provisional: bool = True


class CorrelationIssuesResult(BaseModel):
    """Paginated unresolved correlation quality problems."""

    model_config = ConfigDict(extra="allow")

    items: list[CorrelationIssue]
    total: int
    limit: int
    offset: int


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

    @server.tool(description=CORRELATIONS_TOOL_DESCRIPTION)
    async def get_correlation_issues(
        reason: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CorrelationIssuesResult:
        try:
            payload: dict[str, Any] = await gateway.get_correlation_issues(
                reason=reason, limit=limit, offset=offset
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return CorrelationIssuesResult.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected correlations payload shape"
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
