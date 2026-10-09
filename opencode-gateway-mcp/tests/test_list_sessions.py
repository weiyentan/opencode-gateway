"""Public MCP-surface behavior tests for the list_sessions tool.

Tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Gateway responses are shaped exactly like the published
GET /api/v1/usage/agent-runs contract; no Gateway implementation module
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

_SAMPLE_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_SAMPLE_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_SAMPLE_SOURCE_DB = "33333333-3333-3333-3333-333333333333"


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_list_sessions(
    server: MCPServer, arguments: dict[str, Any]
) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("list_sessions", arguments)


def _sample_agent_run(
    *,
    status: str = "running",
    external_session_id: str = "ses_abc123456789",
    model: str | None = "claude-sonnet-4-20250514",
    cost: Any = "1.23",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _SAMPLE_SESSION_ID,
        "external_session_id": external_session_id,
        "client_id": _SAMPLE_CLIENT_ID,
        "source_database_id": _SAMPLE_SOURCE_DB,
        "title": "coder — ses_abc123456",
        "status": status,
        "currentStatus": status,
        "agent": "coder",
        "project_id": "proj-123",
        "project_label": "acme/proj",
        "workspace_id": "ws-1",
        "todo_total": 5,
        "todo_completed": 3,
        "todo_blocked": 0,
        "code_changes_total": 2,
        "code_change_count": 2,
        "code_change_additions": 10,
        "code_change_deletions": 2,
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_cached_tokens": 10,
        "total_cache_read_tokens": 200,
        "total_cache_write_tokens": 100,
        "total_reasoning_tokens": 5,
        "primary_provider": "anthropic",
        "total_estimated_cost_usd": cost,
        "message_count": 10,
        "last_updated_at": "2026-09-01T10:00:00Z",
        "child_run_count": 1,
        "session_title": "Fix issue #42",
        "model": model,
    }
    if extra:
        base.update(extra)
    return base


# ── Tool exposure ────────────────────────────────────────────────────────


async def test_list_sessions_tool_is_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = [t.name for t in tools.tools]
    assert "list_sessions" in names
    assert "get_gateway_health" in names
    # description must clarify computed status vs proven live process
    tool = next(t for t in tools.tools if t.name == "list_sessions")
    desc = (tool.description or "").lower()
    assert "gateway-computed" in desc or "computed" in desc
    assert "not a proven" in desc or "not a proven live" in desc or "proven live" in desc
    assert "os" in desc or "tmux" in desc


async def test_list_sessions_happy_path_parity() -> None:
    gateway_body = {
        "items": [_sample_agent_run(status="running")],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": gateway_body})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 1
    assert structured["limit"] == 50
    assert structured["offset"] == 0
    assert len(structured["items"]) == 1
    item = structured["items"][0]
    assert item["id"] == _SAMPLE_SESSION_ID
    assert item["external_session_id"] == "ses_abc123456789"
    assert item["status"] == "running"
    assert item["currentStatus"] == "running"
    assert item["model"] == "claude-sonnet-4-20250514"
    assert item["total_input_tokens"] == 1000
    assert item["total_output_tokens"] == 500
    assert item["total_estimated_cost_usd"] == "1.23"


async def test_list_sessions_unwrapped_envelope_also_supported() -> None:
    gateway_body = {"items": [], "total": 0, "limit": 50, "offset": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=gateway_body)

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 0
    assert structured["items"] == []


async def test_list_sessions_forwards_supported_filters() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        result = await _call_list_sessions(
            server,
            {
                "client_id": _SAMPLE_CLIENT_ID,
                "from_date": "2026-09-01T00:00:00Z",
                "to_date": "2026-09-30T23:59:59Z",
                "agent": "coder",
                "external_project_id": "proj-123",
                "status": "completed",
            },
        )

    assert result.is_error is False
    assert seen["path"] == "/api/v1/usage/agent-runs"
    assert seen["params"]["client_id"] == _SAMPLE_CLIENT_ID
    assert seen["params"]["from_date"] == "2026-09-01T00:00:00Z"
    assert seen["params"]["to_date"] == "2026-09-30T23:59:59Z"
    assert seen["params"]["agent"] == "coder"
    assert seen["params"]["external_project_id"] == "proj-123"
    assert seen["params"]["status"] == "completed"
    assert seen["auth"] == f"Bearer {GATEWAY_API_KEY}"
    assert "repository" not in seen["params"]


async def test_list_sessions_all_status_values_forwarded() -> None:
    for status in ["running", "stale", "completed", "blocked", "unknown"]:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request, _status=status) -> httpx.Response:  # type: ignore[no-untyped-def]
            seen["params"] = dict(request.url.params)
            body = {"items": [], "total": 0, "limit": 50, "offset": 0}
            return httpx.Response(200, json=body)

        async with _server_with(handler) as server:
            result = await _call_list_sessions(server, {"status": status})

        assert result.is_error is False
        assert seen["params"]["status"] == status


async def test_list_sessions_supports_explicit_limit_offset_and_does_not_crawl() -> None:
    call_count = 0
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        seen["params"] = dict(request.url.params)
        body = {
            "items": [_sample_agent_run(status="completed")],
            "total": 100,
            "limit": 1,
            "offset": 10,
        }
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {"limit": 1, "offset": 10})

    assert result.is_error is False
    assert call_count == 1
    assert seen["params"]["limit"] == "1"
    assert seen["params"]["offset"] == "10"
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 100
    assert structured["limit"] == 1
    assert structured["offset"] == 10


async def test_list_sessions_preserves_nulls_and_extra_fields() -> None:
    gateway_body = {
        "items": [
            _sample_agent_run(
                status="unknown",
                model=None,
                cost=None,
                extra={
                    "project_label": None,
                    "session_title": None,
                    "primary_provider": None,
                    "total_cached_tokens": 0,
                    "total_cache_write_tokens": 0,
                    "future_field": {"nested": [1, None]},
                },
            )
        ],
        "total": 1,
        "limit": 50,
        "offset": 0,
        "future_top_level": {"x": None},
    }
    # override None values that sample sets
    gateway_body["items"][0]["model"] = None
    gateway_body["items"][0]["total_estimated_cost_usd"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": gateway_body})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    item = structured["items"][0]
    assert item["model"] is None
    assert item["total_estimated_cost_usd"] is None
    assert item["project_label"] is None
    assert item["future_field"] == {"nested": [1, None]}
    assert structured["future_top_level"] == {"x": None}


async def test_list_sessions_preserves_token_and_activity_fields() -> None:
    run = _sample_agent_run(
        status="stale",
        extra={
            "total_input_tokens": 2000,
            "total_output_tokens": 800,
            "total_cache_read_tokens": 300,
            "total_cache_write_tokens": 50,
            "total_reasoning_tokens": 7,
            "message_count": 15,
            "last_updated_at": "2026-09-02T11:00:00Z",
            "child_run_count": 2,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"items": [run], "total": 1, "limit": 50, "offset": 0}
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is False
    item = result.structured_content["items"][0]  # type: ignore[index]
    assert item["total_input_tokens"] == 2000
    assert item["total_output_tokens"] == 800
    assert item["total_cache_read_tokens"] == 300
    assert item["total_cache_write_tokens"] == 50
    assert item["total_reasoning_tokens"] == 7
    assert item["message_count"] == 15
    assert item["last_updated_at"] == "2026-09-02T11:00:00Z"
    assert item["child_run_count"] == 2


async def test_list_sessions_invalid_status_is_surfaced_without_gateway_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {"status": "bogus"})

    assert result.is_error is True
    assert "Invalid status" in result.content[0].text
    assert "bogus" in result.content[0].text
    assert called is False
    # valid values must be listed
    assert "running" in result.content[0].text


async def test_list_sessions_invalid_limit_is_surfaced_without_gateway_call() -> None:
    for bad_limit in [0, 1001, -5]:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

        async with _server_with(handler) as server:
            result = await _call_list_sessions(server, {"limit": bad_limit})

        assert result.is_error is True, f"limit {bad_limit} should be rejected"
        assert "Invalid limit" in result.content[0].text
        assert called is False


async def test_list_sessions_invalid_offset_is_surfaced_without_gateway_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {"offset": -1})

    assert result.is_error is True
    assert "Invalid offset" in result.content[0].text
    assert called is False


async def test_list_sessions_gateway_4xx_is_surfaced_without_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": {
                    "code": "BAD_REQUEST",
                    "message": f"bad status {GATEWAY_API_KEY}",
                },
            },
        )

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {"status": "running"})

    assert result.is_error is True
    text = result.content[0].text
    assert "400" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_list_sessions_gateway_date_validation_error_is_surfaced_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": {
                    "code": "BAD_REQUEST",
                    "message": f"bad date {GATEWAY_API_KEY}",
                },
            },
        )

    async with _server_with(handler) as server:
        result = await _call_list_sessions(
            server, {"from_date": "2026-09-30T00:00:00Z", "to_date": "2026-09-01T00:00:00Z"}
        )

    assert result.is_error is True
    assert "400" in result.content[0].text
    assert GATEWAY_API_KEY not in result.content[0].text


async def test_list_sessions_gateway_5xx_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_list_sessions_unreachable_gateway_is_surfaced_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is True
    text = result.content[0].text
    assert "Could not reach" in text
    assert GATEWAY_API_KEY not in text


async def test_list_sessions_bad_shape_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # missing items list
        return httpx.Response(
            200, json={"status": "ok", "data": {"total": 0, "limit": 50, "offset": 0}}
        )

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is True
    assert "unexpected" in result.content[0].text.lower()


async def test_list_sessions_non_json_is_surfaced_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    async with _server_with(handler) as server:
        result = await _call_list_sessions(server, {})

    assert result.is_error is True
    assert GATEWAY_API_KEY not in result.content[0].text


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))

    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []


async def test_list_sessions_rejects_repository_filter() -> None:
    # The MCP tool schema must not expose a repository filter; the Gateway
    # uses external_project_id instead. This test proves the tool does not
    # forward a repository param under any spelling.
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        body = {"items": [], "total": 0, "limit": 50, "offset": 0}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        # Call with only supported filters; repository must never appear
        result = await _call_list_sessions(server, {"external_project_id": "proj-999"})

    assert result.is_error is False
    assert "repository" not in seen["params"]
    assert seen["params"]["external_project_id"] == "proj-999"
    # Verify input schema does not expose repository
    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()
    tool = next(t for t in tools.tools if t.name == "list_sessions")
    assert "repository" not in (tool.input_schema.get("properties") or {})
