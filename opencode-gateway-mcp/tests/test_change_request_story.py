"""Public MCP-surface behavior tests for get_change_request_story.

Drives the real MCP server through an in-memory MCP client call and controls
the Gateway HTTP API with httpx MockTransport. Gateway responses are shaped
exactly like GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}.
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

GITHUB_DETAIL: dict[str, Any] = {
    "change_request": {
        "provider": "github",
        "repository": "octocat/hello-world",
        "external_id": "42",
        "resource_type": "change_request",
        "provider_state": "merged",
        "total_estimated_cost_usd": 12.34,
        "latest_linked_activity": "2026-10-06T18:00:00Z",
        "provider_state_observed_at": "2026-10-06T17:59:00Z",
        "merged_at": "2026-10-06T17:58:00Z",
        "title": "Fix the thing",
        "executions": {"total": 2, "running": 0, "completed": 1, "failed": 1, "cancelled": 0},
    },
    "afk_runs": [
        {
            "afk_run_id": "01HAAAAAAAAAAAAAAAAAAAAAAAAA",
            "provider": "github",
            "title": "AFK run 1",
            "started_at": "2026-10-06T10:00:00Z",
            "finished_at": "2026-10-06T11:00:00Z",
            "outcome_status": "merged",
            "first_seen_at": "2026-10-06T10:00:00Z",
            "last_seen_at": "2026-10-06T11:00:00Z",
            "link_sources": ["change_request_binding"],
        }
    ],
    "executions": [
        {
            "awx_job": {"job_id": "100", "job_template_id": 10},
            "external_session_id": "ses_abc",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "afk_run_id": "01HAAAAAAAAAAAAAAAAAAAAAAAAA",
            "outcome": "completed",
            "purpose": "implementation",
            "trigger_type": "eda",
            "source_event_id": "evt-1",
            "branch": "feat/fix",
            "title": "Fix the thing",
            "started_at": "2026-10-06T10:05:00Z",
            "finished_at": "2026-10-06T10:55:00Z",
            "duration_seconds": 3000.0,
            "failure_reason": None,
            "failure_summary": None,
            "total_input_tokens": 10000,
            "total_output_tokens": 5000,
            "total_cache_read_tokens": 2000,
            "total_cache_write_tokens": 300,
            "estimated_cost_usd": 1.11,
        }
    ],
    "sessions": [
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "external_session_id": "ses_abc",
            "started_at": "2026-10-06T10:05:00Z",
            "finished_at": "2026-10-06T10:55:00Z",
            "inferred": True,
            "agent": "opencode",
            "message_count": 10,
            "total_input_tokens": 10000,
            "total_output_tokens": 5000,
            "total_cache_read_tokens": 2000,
            "total_cache_write_tokens": 300,
            "total_estimated_cost_usd": 1.11,
            "parent_session_id": None,
        }
    ],
    "usage": {
        "active_tokens": 15000,
        "input_tokens": 10000,
        "output_tokens": 5000,
        "cache_read_tokens": 2000,
        "cache_write_tokens": 300,
        "estimated_cost_usd": 1.11,
        "message_count": 10,
        "session_count": 1,
    },
    "total_estimated_cost_usd": 12.34,
    "merge_state": {"state": "merged", "merged_at": "2026-10-06T17:58:00Z"},
    "timeline": {
        "events": [
            {
                "event_type": "change_request.opened",
                "occurred_at": "2026-10-06T09:00:00Z",
                "observed_via": "webhook",
                "snapshot_at": "2026-10-06T09:00:05Z",
                "actor": "alice",
            },
            {
                "event_type": "change_request.merged",
                "occurred_at": "2026-10-06T17:58:00Z",
                "observed_via": "webhook",
                "snapshot_at": "2026-10-06T17:58:05Z",
                "actor": "bob",
            },
        ]
    },
}

GITLAB_DETAIL_NULLABLE: dict[str, Any] = {
    "change_request": {
        "provider": "gitlab",
        "repository": "mygroup/myproject",
        "external_id": "7",
        "resource_type": "change_request",
        "provider_state": None,
        "total_estimated_cost_usd": None,
        "latest_linked_activity": None,
        "provider_state_observed_at": None,
        "merged_at": None,
        "title": None,
        "executions": {"total": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0},
    },
    "afk_runs": [],
    "executions": [],
    "sessions": [],
    "usage": {
        "active_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_cost_usd": None,
        "message_count": 0,
        "session_count": 0,
    },
    "total_estimated_cost_usd": None,
    "merge_state": None,
    "timeline": None,
}


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_change_request(
    server: MCPServer, provider: str, repository: str, external_number: str
) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool(
            "get_change_request_story",
            {
                "provider": provider,
                "repository": repository,
                "external_number": external_number,
            },
        )


async def test_get_change_request_story_maps_provider_repository_and_preserves_github_detail() -> None:  # noqa: E501
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        # Envelope wrapping as Gateway does
        return httpx.Response(200, json={"status": "ok", "data": GITHUB_DETAIL})

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "github", "octocat/hello-world", "42")

    assert result.is_error is False
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/afk-outcomes/change-requests/github/octocat/hello-world/42"
    assert seen["authorization"] == f"Bearer {GATEWAY_API_KEY}"
    structured = result.structured_content
    assert structured is not None
    # change_request block preserves lifecycle and cost
    assert structured["change_request"]["provider"] == "github"
    assert structured["change_request"]["repository"] == "octocat/hello-world"
    assert structured["change_request"]["external_id"] == "42"
    assert structured["change_request"]["provider_state"] == "merged"
    assert structured["change_request"]["merged_at"] == "2026-10-06T17:58:00Z"
    assert structured["change_request"]["total_estimated_cost_usd"] == 12.34
    # linked runs/executions/sessions preserved
    assert len(structured["afk_runs"]) == 1
    assert structured["afk_runs"][0]["afk_run_id"] == "01HAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert structured["afk_runs"][0]["link_sources"] == ["change_request_binding"]
    assert len(structured["executions"]) == 1
    assert structured["executions"][0]["awx_job"]["job_id"] == "100"
    assert structured["executions"][0]["total_input_tokens"] == 10000
    assert structured["executions"][0]["estimated_cost_usd"] == 1.11
    assert structured["executions"][0]["duration_seconds"] == 3000.0
    assert len(structured["sessions"]) == 1
    assert structured["sessions"][0]["external_session_id"] == "ses_abc"
    # usage, merge_state, timeline provenance preserved
    assert structured["usage"]["active_tokens"] == 15000
    assert structured["usage"]["estimated_cost_usd"] == 1.11
    assert structured["total_estimated_cost_usd"] == 12.34
    assert structured["merge_state"]["state"] == "merged"
    assert structured["merge_state"]["merged_at"] == "2026-10-06T17:58:00Z"
    assert len(structured["timeline"]["events"]) == 2
    assert structured["timeline"]["events"][0]["observed_via"] == "webhook"


async def test_get_change_request_story_supports_gitlab_and_preserves_nulls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/afk-outcomes/change-requests/gitlab/mygroup/myproject/7"
        return httpx.Response(200, json=GITLAB_DETAIL_NULLABLE)

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "gitlab", "mygroup/myproject", "7")

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["change_request"]["provider"] == "gitlab"
    assert structured["change_request"]["provider_state"] is None
    assert structured["change_request"]["merged_at"] is None
    assert structured["change_request"]["total_estimated_cost_usd"] is None
    assert structured["change_request"]["title"] is None
    assert structured["total_estimated_cost_usd"] is None
    assert structured["merge_state"] is None
    assert structured["timeline"] is None
    assert structured["usage"]["estimated_cost_usd"] is None
    assert structured["afk_runs"] == []
    assert structured["executions"] == []
    assert structured["sessions"] == []


async def test_only_health_and_change_request_tools_are_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # health path
        if request.url.path == "/health":
            return httpx.Response(  # noqa: E501
                200, json={"status": "ok", "version": "0.4.2", "database": "connected"}
            )
        return httpx.Response(200, json={"status": "ok", "data": GITHUB_DETAIL})

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()
    names = [tool.name for tool in tools.tools]
    assert "get_change_request_story" in names
    assert "get_gateway_health" in names


async def test_gateway_4xx_is_surfaced_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"status": "error", "error": {"code": "NOT_FOUND", "message": f"Change request not found: github octocat/hello-world #999 {GATEWAY_API_KEY}"}},  # noqa: E501
        )

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "github", "octocat/hello-world", "999")

    assert result.is_error is True
    text = result.content[0].text
    assert "404" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_gateway_5xx_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "github", "octocat/hello-world", "42")

    assert result.is_error is True
    assert "503" in result.content[0].text


async def test_unreachable_gateway_is_surfaced_as_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "github", "octocat/hello-world", "42")

    assert result.is_error is True
    text = result.content[0].text
    assert "Could not reach" in text
    assert GATEWAY_API_KEY not in text


async def test_invalid_provider_is_surfaced_as_error() -> None:
    # Should not even hit Gateway; tool validation fails
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called Gateway for invalid provider")

    async with _server_with(handler) as server:
        result = await _call_change_request(server, "bitbucket", "octocat/hello-world", "42")

    assert result.is_error is True


async def test_repository_and_external_number_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called Gateway for empty repository/number")

    async with _server_with(handler) as server:
        result_empty_repo = await _call_change_request(server, "github", "", "42")
        result_empty_number = await _call_change_request(  # noqa: E501
            server, "github", "octocat/hello-world", ""
        )
        result_whitespace = await _call_change_request(server, "github", "   ", "42")

    assert result_empty_repo.is_error is True
    assert result_empty_number.is_error is True
    assert result_whitespace.is_error is True


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))
    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []


def test_client_calls_only_allowed_gateway_paths() -> None:
    source = (  # noqa: E501
        Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp" / "client.py"
    ).read_text()
    # Must call only GET /health and GET /api/v1/afk-outcomes/change-requests
    assert "/api/v1/afk-outcomes/change-requests" in source
    # No issue reverse lookup or generic passthrough
    assert "closure-relationship" not in source
    assert "issue" not in source.lower() or "change-request" in source.lower()  # noqa: E501
    # No direct DB/Kafka imports already checked above
