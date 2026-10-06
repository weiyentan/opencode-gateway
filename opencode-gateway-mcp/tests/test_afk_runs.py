"""Public MCP-surface behavior tests for the list_afk_runs tool.

Tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Gateway responses are shaped exactly like the published
GET /api/v1/afk-outcomes/runs contract; no Gateway implementation module
is imported.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
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


async def _call_list_afk_runs(
    server: MCPServer, arguments: dict[str, Any]
) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("list_afk_runs", arguments)


async def test_list_afk_runs_tool_is_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = [t.name for t in tools.tools]
    assert "list_afk_runs" in names
    assert "get_gateway_health" in names
    # superset across integrated layers — health + domain tools, no generic passthrough
    assert "get_gateway_health" in names and "list_afk_runs" in names


async def test_list_afk_runs_maps_repository_and_provider_to_origin_and_outcome() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(
            server,
            {
                "repository": "my-org/my-repo",
                "provider": "github",
                "outcome": "merged",
            },
        )

    assert result.is_error is False
    assert seen["path"] == "/api/v1/afk-outcomes/runs"
    assert seen["params"]["repository"] == "my-org/my-repo"
    # provider is translated to origin
    assert seen["params"]["origin"] == "github"
    assert "provider" not in seen["params"]
    assert seen["params"]["outcome"] == "merged"
    assert seen["auth"] == f"Bearer {GATEWAY_API_KEY}"


async def test_list_afk_runs_provider_gitlab_translates_to_origin() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {"provider": "gitlab"})

    assert result.is_error is False
    assert seen["params"]["origin"] == "gitlab"
    assert "provider" not in seen["params"]


async def test_list_afk_runs_maps_explicit_time_windows() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(
            server,
            {
                "started_from": "2026-09-01T00:00:00Z",
                "started_to": "2026-09-30T23:59:59Z",
                "finished_from": "2026-09-10T00:00:00Z",
                "finished_to": "2026-09-11T00:00:00Z",
                "seen_from": "2026-09-09T00:00:00Z",
                "seen_to": "2026-09-10T00:00:00Z",
            },
        )

    assert result.is_error is False
    assert seen["params"]["started_from"] == "2026-09-01T00:00:00Z"
    assert seen["params"]["started_to"] == "2026-09-30T23:59:59Z"
    assert seen["params"]["finished_from"] == "2026-09-10T00:00:00Z"
    assert seen["params"]["finished_to"] == "2026-09-11T00:00:00Z"
    assert seen["params"]["seen_from"] == "2026-09-09T00:00:00Z"
    assert seen["params"]["seen_to"] == "2026-09-10T00:00:00Z"


async def test_list_afk_runs_supports_explicit_limit_offset_and_does_not_crawl() -> None:
    call_count = 0
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        seen["params"] = dict(request.url.params)
        body = {
            "items": [
                {
                    "afk_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "provider": "github",
                    "title": "run one",
                    "started_at": "2026-09-01T10:00:00Z",
                    "finished_at": None,
                    "outcome_status": "open",
                    "first_seen_at": "2026-09-01T10:00:00Z",
                    "last_seen_at": "2026-09-01T11:00:00Z",
                }
            ],
            "total": 100,
            "limit": 1,
            "offset": 10,
        }
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {"limit": 1, "offset": 10})

    assert result.is_error is False
    # exactly one Gateway request — no silent page crawling
    assert call_count == 1
    assert seen["params"]["limit"] == "1"
    assert seen["params"]["offset"] == "10"
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 100
    assert structured["limit"] == 1
    assert structured["offset"] == 10
    assert len(structured["items"]) == 1
    assert structured["items"][0]["afk_run_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def test_list_afk_runs_preserves_nulls_and_extra_fields() -> None:
    gateway_body = {
        "items": [
            {
                "afk_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "provider": "gitlab",
                "title": None,
                "started_at": None,
                "finished_at": None,
                "outcome_status": None,
                "first_seen_at": None,
                "last_seen_at": None,
                "future_field": {"nested": [1, None]},
            }
        ],
        "total": 1,
        "limit": 50,
        "offset": 0,
        "future_top_level": {"x": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": gateway_body})

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    item = structured["items"][0]
    assert item["title"] is None
    assert item["started_at"] is None
    assert item["finished_at"] is None
    assert item["outcome_status"] is None
    assert item["first_seen_at"] is None
    assert item["last_seen_at"] is None
    assert item["future_field"] == {"nested": [1, None]}
    assert structured["future_top_level"] == {"x": None}


async def test_list_afk_runs_unwrapped_envelope_also_supported() -> None:
    # Some MockTransports return the inner payload directly; both shapes are valid.
    gateway_body = {"items": [], "total": 0, "limit": 50, "offset": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=gateway_body)

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 0
    assert structured["items"] == []


async def test_list_afk_runs_gateway_4xx_is_surfaced_without_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": {
                    "code": "BAD_REQUEST",
                    "message": f"bad origin {GATEWAY_API_KEY}",
                },
            },
        )

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {"provider": "github"})

    assert result.is_error is True
    text = result.content[0].text
    assert "400" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_list_afk_runs_gateway_5xx_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {})

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_list_afk_runs_unreachable_gateway_is_surfaced_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_list_afk_runs(server, {})

    assert result.is_error is True
    text = result.content[0].text
    assert "Could not reach" in text
    assert GATEWAY_API_KEY not in text


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))

    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []
