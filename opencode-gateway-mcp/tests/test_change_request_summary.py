"""Public MCP-surface behavior tests for list_change_requests validation.

Drives the real MCP server through an in-memory MCP client call and controls
the Gateway HTTP API with httpx MockTransport. Verifies the provider /
provider_state validation branches (Finding 2 / PR 770) mirror the
get_change_request_story::test_invalid_provider_is_surfaced_as_error pattern:
invalid values surface as an MCP error and do NOT issue a Gateway request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from opencode_gateway_mcp.client import GatewayConfig
from opencode_gateway_mcp.server import create_server

GATEWAY_BASE_URL = "https://gateway.example"
GATEWAY_API_KEY = "gateway-api-key-for-tests"

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_list_change_requests(
    server: MCPServer, arguments: dict[str, Any] | None = None
) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("list_change_requests", arguments or {})


async def test_invalid_provider_is_surfaced_as_error() -> None:
    # Should not even hit Gateway; tool validation fails before gateway call.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called Gateway for invalid provider")

    async with _server_with(handler) as server:
        result = await _call_list_change_requests(server, {"provider": "bitbucket"})

    assert result.is_error is True
    # Optional: confirm the validation message mentions the invalid value and valid set
    text = result.content[0].text if result.content else ""
    assert "Invalid provider" in text
    assert "bitbucket" in text


async def test_invalid_provider_state_is_surfaced_as_error() -> None:
    # Should not even hit Gateway; tool validation fails before gateway call.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called Gateway for invalid provider_state")

    async with _server_with(handler) as server:
        result = await _call_list_change_requests(server, {"provider_state": "pending"})

    assert result.is_error is True
    text = result.content[0].text if result.content else ""
    assert "Invalid provider_state" in text
    assert "pending" in text
