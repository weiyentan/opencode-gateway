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

    # Superset: all 8 read-only semantic tools (health + 7 domain tools)
    tool_names = {tool.name for tool in tools.tools}
    assert "get_gateway_health" in tool_names
    assert "get_afk_activity_summary" in tool_names
    assert "list_afk_runs" in tool_names
    assert "get_model_usage" in tool_names
    assert "get_agent_usage" in tool_names
    assert "get_afk_run_story" in tool_names
    assert "get_change_request_story" in tool_names
    assert "get_correlation_issues" in tool_names
    # No write/admin or generic passthrough.
    assert tool_names.isdisjoint({"write", "admin", "passthrough"})


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


# ── Model and agent usage tests ────────────────────────────────────────────

MODEL_PAYLOAD: list[dict[str, Any]] = [
    {
        "group_value": "claude-sonnet-4-20250514",
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_cached_tokens": 10,
        "total_reasoning_tokens": 5,
        "total_cache_read_tokens": 200,
        "total_cache_write_tokens": 100,
        "total_estimated_cost_usd": "1.23",
        "record_count": 10,
        "session_count": 3,
        "model_count": 1,
        "cache_hit_ratio": 0.1667,
        "provider_breakdown": {"anthropic": 10},
        "project_label": None,
        "agent": None,
    },
    {
        "group_value": "gpt-4o",
        "total_input_tokens": 2000,
        "total_output_tokens": 800,
        "total_cached_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_estimated_cost_usd": "2.50",
        "record_count": 5,
        "session_count": 2,
        "model_count": 1,
        "cache_hit_ratio": None,
        "provider_breakdown": {"openai": 5},
        "project_label": None,
        "agent": None,
        "future_model_field": {"nested": [1, None]},
    },
]

AGENT_PAYLOAD: list[dict[str, Any]] = [
    {
        "group_value": "coder",
        "total_input_tokens": 1500,
        "total_output_tokens": 600,
        "total_cached_tokens": 5,
        "total_reasoning_tokens": 3,
        "total_cache_read_tokens": 150,
        "total_cache_write_tokens": 80,
        "total_estimated_cost_usd": "1.80",
        "record_count": 7,
        "session_count": 4,
        "model_count": 2,
        "cache_hit_ratio": 0.09,
        "provider_breakdown": {"anthropic": 7},
        "project_label": None,
        "agent": "coder",
    },
    {
        "group_value": "unknown",
        "total_input_tokens": 300,
        "total_output_tokens": 100,
        "total_cached_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_estimated_cost_usd": None,
        "record_count": 2,
        "session_count": 2,
        "model_count": 1,
        "cache_hit_ratio": None,
        "provider_breakdown": {},
        "project_label": None,
        "agent": "unknown",
    },
]


async def _call_tool(
    server: MCPServer, name: str, arguments: dict[str, Any]
) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool(name, arguments)


async def test_get_model_usage_calls_aggregates_with_group_by_model() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        assert set(request.url.params.keys()) == {"start_date", "end_date", "group_by"}
        assert request.url.params["group_by"] == "model"
        return httpx.Response(200, json=MODEL_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is False
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/usage/aggregates"
    assert seen["auth"] == f"Bearer {GATEWAY_API_KEY}"
    assert seen["query"]["start_date"] == "2025-07-01T00:00:00Z"
    assert seen["query"]["end_date"] == "2025-07-31T23:59:59Z"
    assert seen["query"]["group_by"] == "model"

    structured = result.structured_content
    assert structured is not None
    import json

    rows: Any = structured
    if isinstance(structured, dict) and "result" in structured:
        rows = structured["result"]
    if not isinstance(rows, list):
        text = result.content[0].text if result.content else ""
        try:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else parsed.get("result", parsed)
        except Exception:
            pass
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["group_value"] == "claude-sonnet-4-20250514"
    assert rows[0]["total_input_tokens"] == 1000
    assert rows[0]["total_cache_read_tokens"] == 200
    assert rows[0]["record_count"] == 10
    assert rows[0]["session_count"] == 3
    assert rows[0]["provider_breakdown"] == {"anthropic": 10}
    assert rows[0]["total_estimated_cost_usd"] == "1.23"
    assert rows[1]["future_model_field"] == {"nested": [1, None]}


async def test_get_agent_usage_calls_aggregates_with_group_by_agent() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        assert request.url.params["group_by"] == "agent"
        return httpx.Response(200, json=AGENT_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_agent_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is False
    assert seen["query"]["group_by"] == "agent"
    import json

    structured = result.structured_content
    rows: Any = structured
    if isinstance(structured, dict) and "result" in structured:
        rows = structured["result"]
    if not isinstance(rows, list):
        text = result.content[0].text if result.content else ""
        try:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else parsed.get("result", parsed)
        except Exception:
            pass
    assert isinstance(rows, list)
    assert rows[0]["group_value"] == "coder"
    assert rows[0]["agent"] == "coder"
    assert rows[1]["agent"] == "unknown"


async def test_model_usage_preserves_token_cache_session_cost_and_provider_breakdown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MODEL_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is False
    import json

    structured = result.structured_content
    rows: Any = structured
    if isinstance(structured, dict) and "result" in structured:
        rows = structured["result"]
    if not isinstance(rows, list):
        text = result.content[0].text if result.content else ""
        try:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else parsed.get("result", parsed)
        except Exception:
            pass
    assert isinstance(rows, list)
    first = rows[0]
    assert first["total_input_tokens"] == 1000
    assert first["total_output_tokens"] == 500
    assert first["total_cached_tokens"] == 10
    assert first["total_reasoning_tokens"] == 5
    assert first["total_cache_read_tokens"] == 200
    assert first["total_cache_write_tokens"] == 100
    assert first["record_count"] == 10
    assert first["session_count"] == 3
    assert first["provider_breakdown"] == {"anthropic": 10}
    assert first["total_estimated_cost_usd"] == "1.23"
    assert first["cache_hit_ratio"] == 0.1667


async def test_agent_usage_preserves_nulls() -> None:
    null_payload: list[dict[str, Any]] = [
        {
            "group_value": "claude-sonnet-4-20250514",
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_reasoning_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_estimated_cost_usd": None,
            "record_count": 0,
            "session_count": 0,
            "model_count": 0,
            "cache_hit_ratio": None,
            "provider_breakdown": {},
            "project_label": None,
            "agent": None,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=null_payload)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_agent_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is False
    import json

    structured = result.structured_content
    rows: Any = structured
    if isinstance(structured, dict) and "result" in structured:
        rows = structured["result"]
    if not isinstance(rows, list):
        text = result.content[0].text if result.content else ""
        try:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else parsed.get("result", parsed)
        except Exception:
            pass
    assert isinstance(rows, list)
    assert rows[0]["total_estimated_cost_usd"] is None
    assert rows[0]["cache_hit_ratio"] is None
    assert rows[0]["provider_breakdown"] == {}


async def test_optional_filters_are_passed_through_when_provided() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=MODEL_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {
                "start_date": "2025-07-01T00:00:00Z",
                "end_date": "2025-07-31T23:59:59Z",
                "client_id": "11111111-1111-1111-1111-111111111111",
                "model": "gpt-4o",
            },
        )

    assert result.is_error is False
    assert seen["params"]["client_id"] == "11111111-1111-1111-1111-111111111111"
    assert seen["params"]["model"] == "gpt-4o"
    assert seen["params"]["group_by"] == "model"
    assert "session_id" not in seen["params"]


async def test_agent_usage_optional_session_id_passthrough() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=AGENT_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_agent_usage",
            {
                "start_date": "2025-07-01T00:00:00Z",
                "end_date": "2025-07-31T23:59:59Z",
                "session_id": "22222222-2222-2222-2222-222222222222",
            },
        )

    assert result.is_error is False
    assert seen["params"]["session_id"] == "22222222-2222-2222-2222-222222222222"
    assert seen["params"]["group_by"] == "agent"


async def test_no_per_afk_run_filter_is_sent() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        assert "afk_run_id" not in seen["params"]
        assert "run_id" not in seen["params"]
        return httpx.Response(200, json=MODEL_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is False


async def test_model_usage_gateway_4xx_is_surfaced_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": f"invalid bearer token {GATEWAY_API_KEY}"},
        )

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is True
    text = result.content[0].text if result.content else ""
    assert "400" in text
    assert GATEWAY_API_KEY not in text


async def test_agent_usage_gateway_5xx_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_agent_usage",
            {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
        )

    assert result.is_error is True
    assert "503" in result.content[0].text
