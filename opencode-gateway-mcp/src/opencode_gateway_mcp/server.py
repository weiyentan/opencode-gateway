"""MCP server bootstrap and the read-only semantic tools.

The adapter is deliberately read-only: it calls only ``GET /health``,
``GET /api/v1/afk/dashboard/summary`` and
``GET /api/v1/afk-outcomes/runs`` on the published Gateway API, and no
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

AFK_ACTIVITY_TOOL_DESCRIPTION = (
    "Return AFK rollup activity for an explicit date range via "
    "GET /api/v1/afk/dashboard/summary, optionally scoped to provider and "
    "repository. Supports interval daily/monthly. Rollup is UTC-calendar based "
    "(daily buckets or monthly sums of UTC daily buckets); no implicit today "
    "is substituted and no timezone-local reconstruction is claimed. Values "
    "including nulls and estimated cost are preserved as the Gateway reports them."
)

AFK_RUNS_TOOL_DESCRIPTION = (
    "Find AFK runs via GET /api/v1/afk-outcomes/runs using the Gateway's "
    "existing run filters. Supports repository, provider (translated to Gateway "
    "origin), outcome, explicit started/finished/seen time windows, and explicit "
    "limit/offset pagination without silent crawling. Unsupported filters are "
    "rejected rather than emulated. Null/unavailable values are preserved "
    "verbatim and Gateway 4xx/5xx are surfaced as MCP errors without exposing "
    "credentials."
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


class AFKActivityBucket(BaseModel):
    """One UTC-calendar bucket from the AFK dashboard rollup.

    Fields preserve the Gateway's ``afk_dashboard_daily`` additive metrics
    verbatim. Strings stay strings so dates and ``derived_at`` timestamps are
    preserved exactly as the Gateway reports them (UTC-calendar). ``None``
    stays ``None`` and unknown future fields are allowed.
    """

    model_config = ConfigDict(extra="allow")

    period_start: str
    provider: str
    repository: str
    runs_started: int = 0
    change_requests_opened: int = 0
    change_requests_merged: int = 0
    change_requests_closed: int = 0
    execution_count: int = 0
    successful_execution_count: int = 0
    failed_execution_count: int = 0
    cancelled_execution_count: int = 0
    session_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Any = None
    derived_at: str | None = None
    oldest_derived_at: str | None = None


class AFKActivitySummary(BaseModel):
    """Structured AFK dashboard summary returned by ``get_afk_activity_summary``.

    Mirrors ``GET /api/v1/afk/dashboard/summary`` with explicit UTC-calendar
    date range, optional provider/repository scoping, and daily/monthly
    interval. Values are preserved as reported; the rollup is UTC-calendar
    based with no implicit today.
    """

    model_config = ConfigDict(extra="allow")

    interval: str
    from_date: str
    to_date: str
    provider: str | None = None
    repository: str | None = None
    buckets: list[AFKActivityBucket] = []
    derived_at: str | None = None
    oldest_derived_at: str | None = None


class AFKRun(BaseModel):
    """One AFK run row, passed through verbatim from the Gateway.

    Timestamps are preserved as the Gateway's own string representation
    with ``None`` staying ``None``; ``extra="allow"`` keeps unknown future
    Gateway fields.
    """

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class ListAFKRunsResult(BaseModel):
    """Paginated AFK runs result returned by ``list_afk_runs``.

    Mirrors ``GET /api/v1/afk-outcomes/runs`` with explicit pagination and
    ``total``. Null/unavailable values are preserved without coercion.
    """

    model_config = ConfigDict(extra="allow")

    items: list[AFKRun] = []
    total: int = 0
    limit: int = 0
    offset: int = 0


def create_server(
    config: GatewayConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Create the MCP server with the read-only semantic tools registered.

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

    @server.tool(description=AFK_ACTIVITY_TOOL_DESCRIPTION)
    async def get_afk_activity_summary(
        from_date: str,
        to_date: str,
        provider: str | None = None,
        repository: str | None = None,
        interval: str = "daily",
    ) -> AFKActivitySummary:
        """Return AFK activity for an explicit UTC-calendar window.

        Calls only ``GET /api/v1/afk/dashboard/summary`` with the provided
        ``from_date``/``to_date``, optional ``provider``/``repository`` scoping,
        and ``interval`` (``daily`` or ``monthly``). No implicit today is
        substituted; UTC-calendar rollup semantics are preserved without
        timezone-local invention. Gateway 4xx/5xx and transport failures are
        surfaced as MCP-visible errors without exposing credentials.
        """
        try:
            payload: dict[str, Any] = await gateway.get_afk_dashboard_summary(
                from_date=from_date,
                to_date=to_date,
                provider=provider,
                repository=repository,
                interval=interval,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return AFKActivitySummary.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk/dashboard/summary payload shape"
            ) from None

    @server.tool(description=AFK_RUNS_TOOL_DESCRIPTION)
    async def list_afk_runs(
        repository: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
        started_from: str | None = None,
        started_to: str | None = None,
        finished_from: str | None = None,
        finished_to: str | None = None,
        seen_from: str | None = None,
        seen_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ListAFKRunsResult:
        """Find AFK runs using the Gateway's existing run filters.

        Calls only ``GET /api/v1/afk-outcomes/runs`` via the authenticated
        Gateway HTTP client. ``provider`` is translated to the Gateway's
        ``origin`` filter. All supported filters, time windows, and explicit
        ``limit``/``offset`` pagination are forwarded verbatim without silent
        crawling. Unsupported filters are rejected (the tool schema only
        exposes supported filters). Gateway 4xx/5xx and transport failures are
        surfaced as MCP-visible errors without exposing credentials.
        """
        try:
            payload: dict[str, Any] = await gateway.list_afk_runs(
                repository=repository,
                provider=provider,
                outcome=outcome,
                started_from=started_from,
                started_to=started_to,
                finished_from=finished_from,
                finished_to=finished_to,
                seen_from=seen_from,
                seen_to=seen_to,
                limit=limit,
                offset=offset,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return ListAFKRunsResult.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk-outcomes/runs payload shape"
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
