"""Public MCP-surface behavior tests for the get_gateway_health tool.

The tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Gateway responses are shaped exactly like the published
GET /health contract; no Gateway implementation module is imported.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from opencode_gateway_mcp.client import GatewayConfig, GatewayConfigError
from opencode_gateway_mcp.server import create_server

GATEWAY_BASE_URL = "https://gateway.example"
GATEWAY_API_KEY = "gateway-api-key-for-tests"

Handler = Callable[[httpx.Request], httpx.Response]

HEALTHY_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "version": "0.4.2",
    "database": "connected",
    "last_ingest_timestamp": "2026-10-07T09:15:00+00:00",
    "collectors": [
        {
            "client_id": "11111111-1111-1111-1111-111111111111",
            "client_name": "remote-collector-runner-a",
            "last_heartbeat": "2026-10-07T09:14:30+00:00",
            "total_records_ingested": 42,
            "health": "healthy",
        }
    ],
    "source_databases": [
        {
            "source_database_id": "22222222-2222-2222-2222-222222222222",
            "client_name": "remote-collector-runner-a",
            "last_push": "2026-10-07T09:14:00+00:00",
            "record_count": 17,
            "health": "healthy",
        }
    ],
}

STALE_UNKNOWN_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "version": "0.4.2",
    "database": "disconnected",
    "last_ingest_timestamp": None,
    "collectors": [
        {
            "client_id": "33333333-3333-3333-3333-333333333333",
            "client_name": "remote-collector-runner-b",
            "last_heartbeat": None,
            "total_records_ingested": 0,
            "health": "unknown",
        },
        {
            "client_id": "44444444-4444-4444-4444-444444444444",
            "client_name": "remote-collector-runner-c",
            "last_heartbeat": "2026-10-05T09:00:00+00:00",
            "total_records_ingested": 7,
            "health": "stale",
        },
    ],
    "source_databases": [
        {
            "source_database_id": "55555555-5555-5555-5555-555555555555",
            "client_name": "remote-collector-runner-c",
            "last_push": None,
            "record_count": 0,
            "health": "unknown",
            "future_source_field": {"nested": [1, None]},
        }
    ],
    "future_top_level_field": {"nested": [1, None]},
}


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_health(server: MCPServer) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("get_gateway_health", {})


async def test_get_gateway_health_preserves_gateway_health_facts() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=HEALTHY_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_health(server)

    assert result.is_error is False
    assert seen == {
        "method": "GET",
        "path": "/health",
        "authorization": f"Bearer {GATEWAY_API_KEY}",
    }

    structured = result.structured_content
    assert structured is not None
    assert structured["status"] == "ok"
    assert structured["version"] == "0.4.2"
    assert structured["database"] == "connected"
    assert structured["last_ingest_timestamp"] == "2026-10-07T09:15:00+00:00"
    assert structured["collectors"] == HEALTHY_PAYLOAD["collectors"]
    assert structured["source_databases"] == HEALTHY_PAYLOAD["source_databases"]


async def test_health_values_are_preserved_without_reinterpretation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STALE_UNKNOWN_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_health(server)

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None

    assert structured["database"] == "disconnected"
    assert structured["last_ingest_timestamp"] is None
    assert structured["collectors"][0]["last_heartbeat"] is None
    assert structured["collectors"][0]["health"] == "unknown"
    assert structured["collectors"][1]["health"] == "stale"
    assert structured["source_databases"][0]["last_push"] is None
    assert structured["source_databases"][0]["health"] == "unknown"
    assert structured["source_databases"][0]["future_source_field"] == {"nested": [1, None]}
    assert structured["future_top_level_field"] == {"nested": [1, None]}


async def test_only_the_read_only_health_tool_is_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=HEALTHY_PAYLOAD)

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = sorted(tool.name for tool in tools.tools)
    # After issue #765 the adapter exposes both health and correlation-quality tools.
    assert "get_gateway_health" in names
    assert set(names).issubset({"get_gateway_health", "get_correlation_issues"})


async def test_gateway_4xx_is_surfaced_as_mcp_error_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Hostile Gateway: echoes the credential back in its error body.
        return httpx.Response(
            401,
            json={"detail": f"invalid bearer token {GATEWAY_API_KEY}"},
            headers={"www-authenticate": "Bearer"},
        )

    async with _server_with(handler) as server:
        result = await _call_health(server)

    assert result.is_error is True
    text = result.content[0].text
    assert "401" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_gateway_5xx_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_health(server)

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_unreachable_gateway_is_surfaced_as_safe_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_health(server)

    assert result.is_error is True
    text = result.content[0].text
    assert "Could not reach" in text
    assert GATEWAY_API_KEY not in text


def test_config_reads_required_environment_values() -> None:
    config = GatewayConfig.from_env(
        {
            "OPENCODE_GATEWAY_URL": "https://gateway.example/",
            "OPENCODE_GATEWAY_API_KEY": "value-never-logged",
        }
    )

    assert config.base_url == "https://gateway.example"
    assert config.api_key == "value-never-logged"


def test_config_failures_do_not_expose_api_key() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewayConfig.from_env({"OPENCODE_GATEWAY_API_KEY": "value-never-logged"})

    assert "OPENCODE_GATEWAY_URL" in str(excinfo.value)
    assert "value-never-logged" not in str(excinfo.value)


def test_config_repr_hides_api_key() -> None:
    config = GatewayConfig(base_url="https://gateway.example", api_key="value-never-logged")

    assert "value-never-logged" not in repr(config)


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))

    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []
