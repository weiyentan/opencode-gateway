"""Public MCP-surface behavior tests for get_session_detail.

Drives the real MCP server through an in-memory MCP client call and controls
the Gateway HTTP API with httpx MockTransport. Gateway responses are shaped
exactly like GET /api/v1/usage/agent-runs/{session_id} returning
AgentRunDetail (app/core/schemas/usage.py).

Coverage:
* Happy-path contract parity: status/currentStatus, IDs, parent/child,
  context, todos, agent/model/project, tokens, cost null preservation,
  aggregated facts without raw transcript.
* Wrong identifier type handling (ses_*, non-UUID, empty).
* Unknown UUID 404 surfacing.
* Backend 5xx and bad-shape handling without credential leakage.
* No transcript/secret exposure.
* Existing tools still work (regression gate).

No Gateway implementation module is imported.
"""

from __future__ import annotations

import re
import uuid
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
SESSION_ID = "11111111-1111-1111-1111-111111111111"
PARENT_SESSION_ID = "ses_parent12345"
PARENT_INTERNAL_ID = "22222222-2222-2222-2222-222222222222"
CHILD_ID = "33333333-3333-3333-3333-333333333333"

Handler = Callable[[httpx.Request], httpx.Response]

DETAIL_PAYLOAD: dict[str, Any] = {
    "id": SESSION_ID,
    "external_session_id": "ses_abc123def456",
    "client_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "source_database_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "title": "code-editor — ses_abc123de",
    "status": "completed",
    "currentStatus": "completed",
    "agent": "code-editor",
    "project_id": "proj-123",
    "project_label": "my-project",
    "workspace_id": "ws-1",
    "parent_session_id": PARENT_SESSION_ID,
    "parent_internal_id": PARENT_INTERNAL_ID,
    "child_summaries": [
        {
            "id": CHILD_ID,
            "external_session_id": "ses_child999",
            "status": "running",
            "currentStatus": "running",
            "agent": "subagent",
            "message_count": 3,
        }
    ],
    "todo_rows": [
        {"content": "Write tests", "status": "completed", "priority": "high", "position": 1},
        {"content": "Implement feature", "status": "pending", "priority": None, "position": 2},
    ],
    "todo_total": 2,
    "todo_completed": 1,
    "todo_blocked": 0,
    "code_changes_total": 5,
    "code_change_count": 5,
    "code_change_additions": 100,
    "code_change_deletions": 20,
    "session_context": {
        "session_model": "claude-sonnet-4-20250514",
        "title": "Implement feature X",
        "source_directory": "/workspace/proj",
        "source_path": "/workspace/proj/file.py",
        "code_change_additions": 100,
        "code_change_deletions": 20,
    },
    "message_count": 10,
    "total_input_tokens": 5000,
    "total_output_tokens": 2500,
    "total_cached_tokens": 0,
    "total_cache_read_tokens": 1000,
    "total_cache_write_tokens": 200,
    "total_reasoning_tokens": 0,
    "primary_provider": "anthropic",
    "total_estimated_cost_usd": "0.01234",
    "first_message_at": "2026-10-07T10:00:00+00:00",
    "last_message_at": "2026-10-07T10:30:00+00:00",
    "loki_search_url": "https://grafana.example/explore?session=1111",
}

# Null-preserving payload: nullable parent/child/context/cost are null/empty.
NULL_DETAIL_PAYLOAD: dict[str, Any] = {
    "id": SESSION_ID,
    "external_session_id": "ses_null123",
    "client_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "source_database_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "title": None,
    "status": "unknown",
    "currentStatus": "unknown",
    "agent": None,
    "project_id": None,
    "project_label": "unknown",
    "workspace_id": None,
    "parent_session_id": None,
    "parent_internal_id": None,
    "child_summaries": [],
    "todo_rows": [],
    "todo_total": 0,
    "todo_completed": 0,
    "todo_blocked": 0,
    "code_changes_total": 0,
    "code_change_count": 0,
    "code_change_additions": 0,
    "code_change_deletions": 0,
    "session_context": None,
    "message_count": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_cached_tokens": 0,
    "total_cache_read_tokens": 0,
    "total_cache_write_tokens": 0,
    "total_reasoning_tokens": 0,
    "primary_provider": None,
    "total_estimated_cost_usd": None,
    "first_message_at": None,
    "last_message_at": None,
    "loki_search_url": None,
}


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_detail(server: MCPServer, session_id: str = SESSION_ID) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("get_session_detail", {"session_id": session_id})


def _envelope(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _not_found() -> httpx.Response:
    return httpx.Response(
        404,
        json={"status": "error", "error": {"code": "NOT_FOUND", "message": "not found"}},
    )


# ── Happy-path contract ───────────────────────────────────────────────────


async def test_get_session_detail_returns_parent_and_child_relationships() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers.get("authorization") == f"Bearer {GATEWAY_API_KEY}"
        assert request.method == "GET"
        if request.url.path == f"/api/v1/usage/agent-runs/{SESSION_ID}":
            return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is False
    assert seen == [f"/api/v1/usage/agent-runs/{SESSION_ID}"]
    sc = result.structured_content
    assert sc is not None
    # IDs
    assert sc["id"] == SESSION_ID
    assert sc["external_session_id"] == "ses_abc123def456"
    # status/currentStatus
    assert sc["status"] == "completed"
    assert sc["currentStatus"] == "completed"
    # Parent preservation (both external and internal)
    assert sc["parent_session_id"] == PARENT_SESSION_ID
    assert sc["parent_internal_id"] == PARENT_INTERNAL_ID
    # Child summaries
    assert len(sc["child_summaries"]) == 1
    child = sc["child_summaries"][0]
    assert child["id"] == CHILD_ID
    assert child["external_session_id"] == "ses_child999"
    assert child["status"] == "running"
    assert child["currentStatus"] == "running"
    assert child["agent"] == "subagent"
    assert child["message_count"] == 3
    # Context
    assert sc["session_context"] is not None
    assert sc["session_context"]["session_model"] == "claude-sonnet-4-20250514"
    assert sc["session_context"]["title"] == "Implement feature X"
    # Todos
    assert sc["todo_total"] == 2
    assert sc["todo_completed"] == 1
    assert sc["todo_blocked"] == 0
    assert len(sc["todo_rows"]) == 2
    assert sc["todo_rows"][0]["content"] == "Write tests"
    assert sc["todo_rows"][0]["status"] == "completed"
    # agent/model/project
    assert sc["agent"] == "code-editor"
    assert sc["project_label"] == "my-project"
    assert sc["workspace_id"] == "ws-1"
    # usage tokens
    assert sc["total_input_tokens"] == 5000
    assert sc["total_output_tokens"] == 2500
    assert sc["total_cache_read_tokens"] == 1000
    assert sc["total_cache_write_tokens"] == 200
    assert sc["primary_provider"] == "anthropic"
    # cost not null
    assert sc["total_estimated_cost_usd"] is not None
    assert str(sc["total_estimated_cost_usd"]) == "0.01234"
    # No raw transcript fields
    assert "transcript" not in str(sc).lower() or "transcript" not in sc
    for forbidden in ("prompt", "transcript", "message_parts", "parts", "stdout"):
        assert forbidden not in sc
        # also check child/context don't leak raw fields
        assert forbidden not in child
    # Ensure session_context doesn't contain raw prompt
    if sc["session_context"]:
        for forbidden in ("prompt", "transcript", "message_parts"):
            assert forbidden not in sc["session_context"]


async def test_get_session_detail_preserves_nulls_and_cost_null() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/usage/agent-runs/{SESSION_ID}":
            return httpx.Response(200, json=_envelope(NULL_DETAIL_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is False
    sc = result.structured_content
    assert sc is not None
    # Nullable parent/child preserved as null/empty, not inferred
    assert sc["parent_session_id"] is None
    assert sc["parent_internal_id"] is None
    assert sc["child_summaries"] == []
    assert sc["session_context"] is None
    assert sc["todo_rows"] == []
    assert sc["agent"] is None
    assert sc["primary_provider"] is None
    assert sc["total_estimated_cost_usd"] is None
    assert sc["first_message_at"] is None
    assert sc["last_message_at"] is None
    assert sc["loki_search_url"] is None
    # status still present (unknown when no messages)
    assert sc["status"] == "unknown"
    assert sc["currentStatus"] == "unknown"


async def test_get_session_detail_uses_bearer_auth_and_url_encoding() -> None:
    seen_headers: dict[str, str] = {}
    seen_path: str = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_path
        seen_path = request.url.path
        seen_headers["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is False
    assert seen_headers["authorization"] == f"Bearer {GATEWAY_API_KEY}"
    assert seen_path == f"/api/v1/usage/agent-runs/{SESSION_ID}"
    assert GATEWAY_API_KEY not in seen_path


# ── Wrong identifier type handling ────────────────────────────────────────


async def test_rejects_external_ses_identifier_without_calling_gateway() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "get_session_detail", {"session_id": "ses_abc123def456"}
            )

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "ses_*" in text or "ses_" in text or "external" in text.lower()
    assert "UUID" in text or "uuid" in text.lower()
    assert called is False


async def test_rejects_non_uuid_string_without_calling_gateway() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "get_session_detail", {"session_id": "not-a-uuid"}
            )

    assert result.is_error is True
    assert "UUID" in result.content[0].text  # type: ignore[union-attr]
    assert called is False


async def test_rejects_empty_session_id_without_calling_gateway() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            for bad in ("", "   "):
                result = await mcp_client.call_tool("get_session_detail", {"session_id": bad})
                assert result.is_error is True
                assert "session_id" in result.content[0].text.lower()  # type: ignore[union-attr]
    assert called is False


async def test_rejects_wrong_type_gracefully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            # Pass integer (Pydantic/MCP will surface as validation error)
            result = await mcp_client.call_tool("get_session_detail", {"session_id": 12345})  # type: ignore[dict-item]
    # Either ToolError or MCP validation error — but must be is_error True, not success.
    assert result.is_error is True


# ── 404 handling ──────────────────────────────────────────────────────────


async def test_unknown_uuid_404_is_surfaced_as_mcp_error() -> None:
    unknown = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/usage/agent-runs/{unknown}"
        return httpx.Response(
            404,
            json={
                "status": "error",
                "error": {"code": "NOT_FOUND", "message": "Agent run not found"},
            },
        )

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool("get_session_detail", {"session_id": unknown})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "404" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


# ── Backend failures ──────────────────────────────────────────────────────


async def test_backend_500_is_surfaced_without_credential_leakage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is True
    assert "500" in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]


async def test_backend_4xx_error_body_never_exposes_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "status": "error",
                "error": {"code": "UNAUTHORIZED", "message": f"invalid bearer {GATEWAY_API_KEY}"},
            },
        )

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is True
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_unreachable_gateway_is_surfaced_as_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is True
    assert "Could not reach" in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]


async def test_invalid_payload_shape_is_surfaced_as_tool_error() -> None:
    # Missing required fields (id, status, etc.) and wrong type
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope({"unexpected": "shape", "id": 123}))

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "unexpected" in text.lower() or "payload shape" in text.lower()
    assert GATEWAY_API_KEY not in text


async def test_non_dict_payload_is_surfaced_as_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Gateway returns a list instead of dict for detail
        return httpx.Response(200, json=_envelope([{"id": SESSION_ID}]))

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is True
    assert "payload shape" in result.content[0].text.lower()  # type: ignore[union-attr]


# ── No transcript/secret exposure ─────────────────────────────────────────


async def test_does_not_return_raw_transcript_or_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        result = await _call_detail(server)

    assert result.is_error is False
    sc = result.structured_content
    assert sc is not None
    serialized = str(sc)
    for forbidden in ("prompt", "transcript", "message_parts", "parts", "stdout", "extra_vars"):
        # Only allow these words if they appear as part of allowed field names? But none should.
        # Check no top-level key named forbidden
        assert forbidden not in sc
    # Ensure no secret token leaked even if Gateway were hostile — error path already tested
    assert GATEWAY_API_KEY not in serialized


# ── Adapter boundary ──────────────────────────────────────────────────────


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
    client_path = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp" / "client.py"
    source = client_path.read_text()
    assert "/api/v1/usage/agent-runs/" in source
    # No direct DB/Kafka
    assert "asyncpg" not in source
    assert "aiokafka" not in source
    # No transcript table access
    assert "observed_messages" not in source
    assert "observed_parts" not in source


async def test_existing_tools_still_work_after_adding_get_session_detail() -> None:
    """Regression gate: health and correlation tools still function."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"status": "ok", "version": "0.4.2", "database": "connected"}
            )
        if request.url.path == "/api/v1/afk-outcomes/correlations":
            return httpx.Response(
                200,
                json=_envelope({"items": [], "total": 0, "limit": 50, "offset": 0}),
            )
        return _not_found()

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()
            names = [t.name for t in tools.tools]
            # All prior tools plus new one
            assert "get_gateway_health" in names
            assert "get_correlation_issues" in names
            assert "get_session_detail" in names
            assert "get_afk_run_story" in names
            # Call health to prove it still works
            health = await mcp_client.call_tool("get_gateway_health", {})
            assert health.is_error is False

            corr = await mcp_client.call_tool("get_correlation_issues", {})
            assert corr.is_error is False
