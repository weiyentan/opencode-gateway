"""Public MCP-surface behavior tests for the get_correlation_issues tool.

The tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Gateway responses are shaped exactly like the published
GET /api/v1/afk-outcomes/correlations contract; no Gateway implementation module
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

# Two unresolved correlations: one ambiguous (candidates present) and one
# unmatched (candidates empty). Shapes mirror app/core/schemas/afk.py
# UnresolvedCorrelationRow.
AMBIGUOUS_ITEM: dict[str, Any] = {
    "entity_id": "issue:42",
    "entity_type": "issue",
    "external_id": "42",
    "provider": "github",
    "repository": "acme/app",
    "afk_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "method": "temporal_inference",
    "reason": "ambiguous",
    "correlation_confidence": 0.0,
    "candidates": ["issue:42", "issue:43"],
    "evidence": [
        {"kind": "branch_name", "source_entity_id": "issue:42", "detail": "afk-42", "weight": 1.0}
    ],
    "resolver_version": "2",
    "created_at": "2026-10-07T09:00:00+00:00",
    "provisional": True,
}

UNMATCHED_ITEM: dict[str, Any] = {
    "entity_id": "afk_run:01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "entity_type": "afk_run",
    "external_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "provider": "gitlab",
    "repository": "acme/other",
    "afk_run_id": None,
    "method": "issue_reference",
    "reason": "unmatched",
    "correlation_confidence": 0.0,
    "candidates": [],
    "evidence": [],
    "resolver_version": "2",
    "created_at": "2026-10-07T09:01:00+00:00",
    "provisional": True,
}


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_correlations(server: MCPServer, arguments: dict[str, Any]) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("get_correlation_issues", arguments)


async def test_get_correlation_issues_preserves_ambiguous_and_unmatched_exactly() -> None:
    """Ambiguous and unmatched reasons, candidates, and identities are preserved verbatim
    without random tie-breaking or manufacturing a resolved link."""

    payload = {
        "items": [AMBIGUOUS_ITEM, UNMATCHED_ITEM],
        "total": 2,
        "limit": 50,
        "offset": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/afk-outcomes/correlations"
        return httpx.Response(200, json=payload)

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["total"] == 2
    assert len(structured["items"]) == 2

    first = structured["items"][0]
    assert first["reason"] == "ambiguous"
    assert first["candidates"] == ["issue:42", "issue:43"]
    assert first["entity_id"] == "issue:42"
    assert first["afk_run_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert first["provisional"] is True

    second = structured["items"][1]
    assert second["reason"] == "unmatched"
    assert second["candidates"] == []
    assert second["entity_type"] == "afk_run"
    assert second["afk_run_id"] is None
    assert second["provisional"] is True


async def test_reason_filter_is_forwarded_as_single_query_param() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        seen["path"] = request.url.path
        # Return only ambiguous items when filtered
        filtered = {
            "items": [AMBIGUOUS_ITEM],
            "total": 1,
            "limit": 50,
            "offset": 0,
        }
        return httpx.Response(200, json=filtered)

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {"reason": "ambiguous"})

    assert result.is_error is False
    assert seen["path"] == "/api/v1/afk-outcomes/correlations"
    assert seen["query"].get("reason") == "ambiguous"
    structured = result.structured_content
    assert structured is not None
    assert structured["items"][0]["reason"] == "ambiguous"


async def test_unmatched_reason_filter_is_forwarded() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        payload = {"items": [UNMATCHED_ITEM], "total": 1, "limit": 50, "offset": 0}
        return httpx.Response(200, json=payload)

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {"reason": "unmatched"})

    assert result.is_error is False
    assert seen["query"].get("reason") == "unmatched"


async def test_pagination_limit_offset_forwarded_without_silent_crawl() -> None:
    call_count = {"n": 0}
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={"items": [AMBIGUOUS_ITEM], "total": 100, "limit": 1, "offset": 5},
        )

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {"limit": 1, "offset": 5})

    assert result.is_error is False
    # Exactly one Gateway request — no silent crawl
    assert call_count["n"] == 1
    assert seen["query"].get("limit") == "1"
    assert seen["query"].get("offset") == "5"
    structured = result.structured_content
    assert structured is not None
    assert structured["limit"] == 1
    assert structured["offset"] == 5
    assert structured["total"] == 100


async def test_null_values_are_preserved() -> None:
    """Null afk_run_id, evidence, etc. are preserved, not coerced to empty strings."""
    null_item: dict[str, Any] = {
        "entity_id": "issue:99",
        "entity_type": "issue",
        "external_id": "99",
        "provider": "github",
        "repository": "acme/app",
        "afk_run_id": None,
        "method": "issue_reference",
        "reason": "unmatched",
        "correlation_confidence": 0.0,
        "candidates": [],
        "evidence": [],
        "resolver_version": None,
        "created_at": None,
        "provisional": True,
    }
    payload = {"items": [null_item], "total": 1, "limit": 50, "offset": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {})

    assert result.is_error is False
    item = result.structured_content["items"][0]  # type: ignore[index]
    assert item["afk_run_id"] is None
    assert item["resolver_version"] is None
    assert item["created_at"] is None


async def test_gateway_4xx_is_surfaced_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": f"invalid reason with secret {GATEWAY_API_KEY}"},
        )

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {"reason": "ambiguous"})

    assert result.is_error is True
    text = result.content[0].text
    assert "400" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_gateway_5xx_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_correlations(server, {})

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_tool_is_registered_alongside_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = sorted(tool.name for tool in tools.tools)
    assert "get_correlation_issues" in names
    assert "get_gateway_health" in names


def test_adapter_has_no_direct_storage_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))
    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []
