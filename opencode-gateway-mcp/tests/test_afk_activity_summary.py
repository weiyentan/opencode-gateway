"""Public MCP-surface behavior tests for the get_afk_activity_summary tool.

The tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Gateway responses are shaped like the published
GET /api/v1/afk/dashboard/summary contract; no Gateway implementation module
is imported.

UTC-calendar rollup semantics, explicit date requirements, provider/repository
scoping, daily/monthly intervals, null preservation, and error surfacing are
all exercised through the public MCP tool call.
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

SUMMARY_BUCKET: dict[str, Any] = {
    "period_start": "2026-09-01",
    "provider": "github",
    "repository": "acme/proj",
    "runs_started": 2,
    "change_requests_opened": 1,
    "change_requests_merged": 1,
    "change_requests_closed": 0,
    "execution_count": 3,
    "successful_execution_count": 2,
    "failed_execution_count": 1,
    "cancelled_execution_count": 0,
    "session_count": 4,
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_read_tokens": 200,
    "cache_write_tokens": 100,
    "estimated_cost_usd": "0.25",
    "derived_at": "2026-09-30T03:00:00+00:00",
    "oldest_derived_at": "2026-09-01T03:00:00+00:00",
}

SUMMARY_PAYLOAD: dict[str, Any] = {
    "interval": "daily",
    "from_date": "2026-09-01",
    "to_date": "2026-09-30",
    "provider": None,
    "repository": None,
    "buckets": [SUMMARY_BUCKET],
    "derived_at": "2026-09-30T03:00:00+00:00",
    "oldest_derived_at": "2026-09-01T03:00:00+00:00",
    "future_top_level_field": {"nested": [1, None]},
}

NULL_PAYLOAD: dict[str, Any] = {
    "interval": "daily",
    "from_date": "2026-01-01",
    "to_date": "2026-01-02",
    "provider": None,
    "repository": None,
    "buckets": [
        {
            "period_start": "2026-01-01",
            "provider": "gitlab",
            "repository": "team/repo",
            "runs_started": 0,
            "change_requests_opened": 0,
            "change_requests_merged": 0,
            "change_requests_closed": 0,
            "execution_count": 0,
            "successful_execution_count": 0,
            "failed_execution_count": 0,
            "cancelled_execution_count": 0,
            "session_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": None,
            "derived_at": None,
            "oldest_derived_at": None,
            "future_bucket_field": None,
        }
    ],
    "derived_at": None,
    "oldest_derived_at": None,
}


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_summary(server: MCPServer, arguments: dict[str, Any]) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("get_afk_activity_summary", arguments)


# ── Explicit date / interval / scoping ──────────────────────────────────────


async def test_get_afk_activity_summary_preserves_gateway_facts_with_explicit_dates() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=SUMMARY_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server,
            {
                "from_date": "2026-09-01",
                "to_date": "2026-09-30",
                "provider": "github",
                "repository": "acme/proj",
                "interval": "daily",
            },
        )

    assert result.is_error is False
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/afk/dashboard/summary"
    assert seen["authorization"] == f"Bearer {GATEWAY_API_KEY}"
    # Explicit dates and scoping are forwarded verbatim; no implicit today.
    assert seen["query"]["from_date"] == "2026-09-01"
    assert seen["query"]["to_date"] == "2026-09-30"
    assert seen["query"]["provider"] == "github"
    assert seen["query"]["repository"] == "acme/proj"
    assert seen["query"]["interval"] == "daily"

    structured = result.structured_content
    assert structured is not None
    assert structured["interval"] == "daily"
    assert structured["from_date"] == "2026-09-01"
    assert structured["to_date"] == "2026-09-30"
    assert len(structured["buckets"]) == 1
    bucket = structured["buckets"][0]
    # runs started, execution outcomes, sessions, tokens, cost preserved
    assert bucket["runs_started"] == 2
    assert bucket["execution_count"] == 3
    assert bucket["successful_execution_count"] == 2
    assert bucket["failed_execution_count"] == 1
    assert bucket["cancelled_execution_count"] == 0
    assert bucket["session_count"] == 4
    assert bucket["input_tokens"] == 1000
    assert bucket["output_tokens"] == 500
    assert bucket["cache_read_tokens"] == 200
    assert bucket["cache_write_tokens"] == 100
    assert bucket["estimated_cost_usd"] == "0.25"
    assert bucket["period_start"] == "2026-09-01"
    assert structured["derived_at"] == "2026-09-30T03:00:00+00:00"
    # unknown future field preserved via extra="allow"
    assert structured["future_top_level_field"] == {"nested": [1, None]}


async def test_monthly_interval_is_forwarded_and_bucket_preserved() -> None:
    seen: dict[str, str] = {}
    monthly_payload: dict[str, Any] = {
        "interval": "monthly",
        "from_date": "2026-08-01",
        "to_date": "2026-08-31",
        "provider": None,
        "repository": None,
        "buckets": [
            {
                **SUMMARY_BUCKET,
                "period_start": "2026-08-01",
                "provider": "gitlab",
                "repository": "team/repo",
                "runs_started": 7,
            }
        ],
        "derived_at": "2026-08-31T03:00:00+00:00",
        "oldest_derived_at": "2026-08-01T03:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=monthly_payload)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server,
            {"from_date": "2026-08-01", "to_date": "2026-08-31", "interval": "monthly"},
        )

    assert result.is_error is False
    assert seen["interval"] == "monthly"
    assert seen["from_date"] == "2026-08-01"
    assert seen["to_date"] == "2026-08-31"
    structured = result.structured_content
    assert structured is not None
    assert structured["interval"] == "monthly"
    assert structured["buckets"][0]["period_start"] == "2026-08-01"
    assert structured["buckets"][0]["runs_started"] == 7


async def test_provider_repository_scoping_optional() -> None:
    # No provider/repository → query params absent.
    seen_no_scope: dict[str, str] = {}

    def handler_no_scope(request: httpx.Request) -> httpx.Response:
        seen_no_scope.update(dict(request.url.params))
        return httpx.Response(200, json=SUMMARY_PAYLOAD)

    async with _server_with(handler_no_scope) as server:
        result = await _call_summary(
            server, {"from_date": "2026-09-01", "to_date": "2026-09-30"}
        )
    assert result.is_error is False
    assert "provider" not in seen_no_scope
    assert "repository" not in seen_no_scope

    # Provider only
    seen_provider: dict[str, str] = {}

    def handler_provider(request: httpx.Request) -> httpx.Response:
        seen_provider.update(dict(request.url.params))
        return httpx.Response(200, json={**SUMMARY_PAYLOAD, "provider": "github"})

    async with _server_with(handler_provider) as server:
        result = await _call_summary(
            server,
            {"from_date": "2026-09-01", "to_date": "2026-09-30", "provider": "github"},
        )
    assert result.is_error is False
    assert seen_provider["provider"] == "github"
    assert "repository" not in seen_provider

    # Repository only
    seen_repo: dict[str, str] = {}

    def handler_repo(request: httpx.Request) -> httpx.Response:
        seen_repo.update(dict(request.url.params))
        return httpx.Response(200, json={**SUMMARY_PAYLOAD, "repository": "acme/proj"})

    async with _server_with(handler_repo) as server:
        result = await _call_summary(
            server,
            {
                "from_date": "2026-09-01",
                "to_date": "2026-09-30",
                "repository": "acme/proj",
            },
        )
    assert result.is_error is False
    assert seen_repo["repository"] == "acme/proj"
    assert "provider" not in seen_repo


async def test_null_handling_preserved_without_coercion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=NULL_PAYLOAD)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "2026-01-01", "to_date": "2026-01-02"}
        )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["derived_at"] is None
    assert structured["oldest_derived_at"] is None
    bucket = structured["buckets"][0]
    assert bucket["estimated_cost_usd"] is None
    assert bucket["derived_at"] is None
    assert bucket["oldest_derived_at"] is None
    assert bucket["runs_started"] == 0
    # future bucket field with None preserved, not coerced to 0 or ""
    assert bucket["future_bucket_field"] is None


async def test_utc_calendar_rollup_preserved_without_timezone_invention() -> None:
    # Gateway rollup is UTC-calendar based; MCP must not claim timezone-local precision.
    # The payload's period_start is a calendar date string, not a timezone-converted timestamp.
    utc_payload: dict[str, Any] = {
        "interval": "daily",
        "from_date": "2026-09-15",
        "to_date": "2026-09-15",
        "provider": None,
        "repository": None,
        "buckets": [
            {
                **SUMMARY_BUCKET,
                "period_start": "2026-09-15",
                "derived_at": "2026-09-15T03:00:00+00:00",
                "oldest_derived_at": "2026-09-15T03:00:00+00:00",
            }
        ],
        "derived_at": "2026-09-15T03:00:00+00:00",
        "oldest_derived_at": "2026-09-15T03:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=utc_payload)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "2026-09-15", "to_date": "2026-09-15"}
        )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    # Dates stay as UTC calendar strings, not converted to local offsets
    assert structured["from_date"] == "2026-09-15"
    assert structured["to_date"] == "2026-09-15"
    assert structured["buckets"][0]["period_start"] == "2026-09-15"
    assert structured["buckets"][0]["derived_at"] == "2026-09-15T03:00:00+00:00"
    assert structured["interval"] == "daily"


async def test_envelope_wrapped_response_is_unwrapped() -> None:
    # Production Gateway wraps success payloads in {status: "ok", data: ...}.
    envelope = {"status": "ok", "data": SUMMARY_PAYLOAD}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "2026-09-01", "to_date": "2026-09-30"}
        )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["from_date"] == "2026-09-01"
    assert structured["buckets"][0]["runs_started"] == 2


async def test_only_read_only_activity_and_health_tools_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Return valid health for health tool, summary for activity tool
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "version": "0.4.2",
                    "database": "connected",
                    "last_ingest_timestamp": None,
                    "collectors": [],
                    "source_databases": [],
                },
            )
        return httpx.Response(200, json=SUMMARY_PAYLOAD)

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = sorted(t.name for t in tools.tools)
    assert names == ["get_afk_activity_summary", "get_gateway_health"]


async def test_gateway_4xx_is_surfaced_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": f"invalid bearer {GATEWAY_API_KEY}"},
        )

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "bad-date", "to_date": "2026-09-30"}
        )

    assert result.is_error is True
    text = result.content[0].text
    assert "400" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_gateway_5xx_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "2026-09-01", "to_date": "2026-09-30"}
        )

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_unreachable_gateway_is_surfaced_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_summary(
            server, {"from_date": "2026-09-01", "to_date": "2026-09-30"}
        )

    assert result.is_error is True
    assert "Could not reach" in result.content[0].text
    assert GATEWAY_API_KEY not in result.content[0].text


async def test_invalid_interval_400_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Invalid interval"})

    async with _server_with(handler) as server:
        result = await _call_summary(
            server,
            {"from_date": "2026-09-01", "to_date": "2026-09-30", "interval": "weekly"},
        )

    assert result.is_error is True
    assert "400" in result.content[0].text


async def test_missing_required_date_is_mcp_validation_error_without_http_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=SUMMARY_PAYLOAD)

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            # Omit to_date — MCP schema requires it; should be a validation error
            # before any HTTP request.
            result = await mcp_client.call_tool(
                "get_afk_activity_summary", {"from_date": "2026-09-01"}
            )

    assert result.is_error is True
    assert called is False


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))

    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []
