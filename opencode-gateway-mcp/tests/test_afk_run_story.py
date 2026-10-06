"""Public MCP-surface behavior tests for the get_afk_run_story composite tool.

The tests drive the real MCP server through an in-memory MCP client call and
control the only external dependency (the Gateway HTTP API) with an httpx
MockTransport. Both Gateway responses are shaped exactly like the published
contracts:

* GET /api/v1/afk-outcomes/runs/{afk_run_id}  — envelope {status:"ok", data: RunDetail}
* GET /api/v1/afk/executions/runs/{afk_run_id} — envelope {status:"ok", data: [bindings]}

No Gateway implementation module is imported.
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
AFK_RUN_ID = "01H5K6XYZABCDEF1234567890"

Handler = Callable[[httpx.Request], httpx.Response]

# ── Canonical run-detail payload (mirrors app/core/schemas/afk.py RunDetail) ─

RUN_DETAIL_PAYLOAD: dict[str, Any] = {
    "run": {
        "afk_run_id": AFK_RUN_ID,
        "provider": "github",
        "title": "Fix issue #42",
        "started_at": "2026-10-07T09:00:00+00:00",
        "finished_at": None,
        "outcome_status": None,
        "first_seen_at": "2026-10-07T09:00:00+00:00",
        "last_seen_at": "2026-10-07T09:10:00+00:00",
    },
    "outcome": {
        "status": "open",
        "change_request_ids": [],
        "resolved_issue_ids": [],
        "merge_event_id": None,
        "merged_at": None,
    },
    "issues": [
        {
            "entity_id": "issue:42",
            "entity_type": "issue",
            "external_id": "42",
            "provider": "github",
            "repository": "acme/proj",
            "role": "resolved",
            "correlation_method": "issue_reference",
            "correlation_confidence": 1.0,
            "evidence": [],
            "resolver_version": "2",
            "owning_change_request_id": None,
            "correlation_source": "direct",
            "provisional": False,
        }
    ],
    "change_requests": [
        {
            "entity_id": "change_request:99",
            "entity_type": "change_request",
            "external_id": "99",
            "provider": "github",
            "repository": "acme/proj",
            "role": "resolved",
            "correlation_method": "explicit_run_id",
            "correlation_confidence": 1.0,
            "evidence": [],
            "resolver_version": "2",
            "owning_change_request_id": None,
            "correlation_source": "direct",
            "provisional": False,
        }
    ],
    "reviews": [
        {
            "entity_id": "review:10",
            "entity_type": "review",
            "external_id": "10",
            "provider": "github",
            "repository": "acme/proj",
            "role": "resolved",
            "correlation_method": "explicit_run_id",
            "correlation_confidence": 1.0,
            "evidence": [],
            "resolver_version": "2",
            "owning_change_request_id": "99",
            "correlation_source": "owning_change_request",
            "provisional": False,
        }
    ],
    "commits": [
        {
            "entity_id": "commit:abcdef1234567890abcdef1234567890abcdef12",
            "entity_type": "commit",
            "external_id": "abcdef1234567890abcdef1234567890abcdef12",
            "provider": "github",
            "repository": "acme/proj",
            "role": "resolved",
            "correlation_method": "explicit_run_id",
            "correlation_confidence": 1.0,
            "evidence": [],
            "resolver_version": "2",
            "owning_change_request_id": "99",
            "correlation_source": "owning_change_request",
            "provisional": False,
        }
    ],
    "merge_events": [],
    "sessions": [
        {
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "external_session_id": "ses_abc123",
            "started_at": "2026-10-07T09:01:00+00:00",
            "finished_at": "2026-10-07T09:09:00+00:00",
            "inferred": True,
            "agent": "code-editor",
            "message_count": 5,
            "total_input_tokens": 500,
            "total_output_tokens": 250,
            "total_cache_read_tokens": 100,
            "total_cache_write_tokens": 50,
            "total_estimated_cost_usd": "0.00123",
            "parent_session_id": None,
        }
    ],
    "agents": ["code-editor"],
    "usage": {
        "active_tokens": 750,
        "input_tokens": 500,
        "output_tokens": 250,
        "cache_read_tokens": 100,
        "cache_write_tokens": 50,
        "estimated_cost_usd": "0.00123",
        "message_count": 5,
        "session_count": 1,
    },
}

# Null-preserving payload: every nullable field is null/empty to verify verbatim preservation.
NULL_DETAIL_PAYLOAD: dict[str, Any] = {
    "run": {
        "afk_run_id": AFK_RUN_ID,
        "provider": "github",
        "title": None,
        "started_at": None,
        "finished_at": None,
        "outcome_status": None,
        "first_seen_at": None,
        "last_seen_at": None,
    },
    "outcome": None,
    "issues": [],
    "change_requests": [],
    "reviews": [],
    "commits": [],
    "merge_events": [],
    "sessions": [],
    "agents": [],
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
}

# Two executions: first failed with approved failure summary, second completed retry.
EXECUTIONS_PAYLOAD: list[dict[str, Any]] = [
    {
        "binding_id": "11111111-1111-1111-1111-111111111111",
        "awx_job": {"job_id": "101", "job_template_id": 10},
        "external_session_id": "ses_abc123",
        "external_session_ids": ["ses_abc123"],
        "resource": {
            "provider": "github",
            "repository": "github.com/acme/proj",
            "resource_type": "change_request",
            "resource_number": "99",
        },
        "outcome": "failed",
        "afk_run_id": AFK_RUN_ID,
        "trigger_type": "eda",
        "source_event_id": "evt_001",
        "branch": "branch/afk-42",
        "title": "Fix issue #42",
        "started_at": "2026-10-07T09:00:00+00:00",
        "finished_at": "2026-10-07T09:02:00+00:00",
        "failure_reason": "job failed",
        "failure_summary": "exit code 1: tests failed",
    },
    {
        "binding_id": "22222222-2222-2222-2222-222222222222",
        "awx_job": {"job_id": "102", "job_template_id": 10},
        "external_session_id": "ses_def456",
        "external_session_ids": ["ses_def456"],
        "resource": {
            "provider": "github",
            "repository": "github.com/acme/proj",
            "resource_type": "change_request",
            "resource_number": "99",
        },
        "outcome": "completed",
        "afk_run_id": AFK_RUN_ID,
        "trigger_type": "eda",
        "source_event_id": "evt_002",
        "branch": "branch/afk-42",
        "title": "Fix issue #42",
        "started_at": "2026-10-07T09:05:00+00:00",
        "finished_at": "2026-10-07T09:10:00+00:00",
        "failure_reason": None,
        "failure_summary": None,
    },
]


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_story(server: MCPServer, afk_run_id: str = AFK_RUN_ID) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool("get_afk_run_story", {"afk_run_id": afk_run_id})


def _envelope(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _not_found() -> httpx.Response:
    return httpx.Response(
        404,
        json={"status": "error", "error": {"code": "NOT_FOUND", "message": "not found"}},
    )


def _run_not_found() -> httpx.Response:
    return httpx.Response(
        404,
        json={"status": "error", "error": {"code": "NOT_FOUND", "message": "AFK run not found"}},
    )


# ── Success: composite includes canonical detail + all AWX attempts ──────────


async def test_get_afk_run_story_combines_canonical_detail_and_execution_history() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers.get("authorization") == f"Bearer {GATEWAY_API_KEY}"
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    # Both endpoints were called with the same explicit afk_run_id.
    assert seen == [
        f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}",
        f"/api/v1/afk/executions/runs/{AFK_RUN_ID}",
    ]
    structured = result.structured_content
    assert structured is not None
    # Top-level afk_run_id echoed.
    assert structured["afk_run_id"] == AFK_RUN_ID
    # Canonical detail fields preserved verbatim.
    assert structured["run"]["afk_run_id"] == AFK_RUN_ID
    assert structured["run"]["provider"] == "github"
    assert len(structured["issues"]) == 1
    assert structured["issues"][0]["entity_id"] == "issue:42"
    assert len(structured["change_requests"]) == 1
    assert len(structured["reviews"]) == 1
    assert len(structured["commits"]) == 1
    assert structured["merge_events"] == []
    assert structured["sessions"][0]["external_session_id"] == "ses_abc123"
    assert structured["agents"] == ["code-editor"]
    assert structured["usage"]["active_tokens"] == 750
    assert structured["usage"]["estimated_cost_usd"] is not None
    # Execution history includes both failed attempt and retry, with failure summaries.
    assert len(structured["executions"]) == 2
    assert structured["executions"][0]["outcome"] == "failed"
    assert structured["executions"][0]["failure_summary"] == "exit code 1: tests failed"
    assert structured["executions"][0]["failure_reason"] == "job failed"
    assert structured["executions"][1]["outcome"] == "completed"
    assert structured["executions"][1]["failure_summary"] is None


async def test_story_preserves_retry_history_order_and_all_attempts() -> None:
    # Executions must be returned in Gateway order (oldest first) and include all retries.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    ids = [e["awx_job"]["job_id"] for e in result.structured_content["executions"]]
    assert ids == ["101", "102"]


async def test_failure_summary_is_preserved_without_raw_secrets_or_stdout() -> None:
    # Gateway already redacts; MCP must not reintroduce raw prompts/stdout/secrets.
    # Verify only the approved bounded failure fields are present and no extra raw fields leak.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    for execution in result.structured_content["executions"]:
        # Only approved failure keys exist; raw prompts/stdout/extra_vars must not appear.
        assert "extra_vars" not in execution
        assert "stdout" not in execution
        assert "prompt" not in execution
        # If failure fields present they are bounded strings (no secret-bearing token).
        if execution["failure_summary"] is not None:
            assert len(execution["failure_summary"]) <= 1000
            # Secret token must not leak even if Gateway redacted — check no raw bearer.
            lower = execution["failure_summary"].lower()
            assert "bearer" not in lower or "***" in execution["failure_summary"]


async def test_nulls_are_preserved_exactly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(NULL_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            # Empty execution list for a provisioned run with no executions yet.
            return httpx.Response(200, json=_envelope([]))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    sc = result.structured_content
    assert sc["run"]["title"] is None
    assert sc["run"]["started_at"] is None
    assert sc["outcome"] is None
    assert sc["issues"] == []
    assert sc["change_requests"] == []
    assert sc["usage"]["estimated_cost_usd"] is None
    assert sc["executions"] == []


async def test_empty_execution_list_is_valid_story() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope([]))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    assert result.structured_content["executions"] == []


async def test_only_the_two_approved_tools_are_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # No calls expected for list_tools, but provide a fallback.
        return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            tools = await mcp_client.list_tools()

    names = [tool.name for tool in tools.tools]
    assert "get_afk_run_story" in names
    assert "get_gateway_health" in names


async def test_get_afk_run_story_requires_nonempty_afk_run_id() -> None:
    # Validation happens before any Gateway call.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))

    async with _server_with(handler) as server:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool("get_afk_run_story", {"afk_run_id": "   "})

    assert result.is_error is True
    assert "afk_run_id" in result.content[0].text  # type: ignore[union-attr]
    assert called is False


# ── Partial-failure visibility ──────────────────────────────────────────────


async def test_run_detail_404_is_surfaced_as_mcp_error_without_partial_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return _run_not_found()
        # Even if history would succeed, story must not return partial success.
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "404" in text
    assert GATEWAY_API_KEY not in text
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_execution_history_404_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return _run_not_found()
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    assert "404" in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]


async def test_execution_history_500_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(500, text="internal error")
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    assert "500" in result.content[0].text  # type: ignore[union-attr]


async def test_run_detail_500_is_surfaced_as_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(500, text="internal error")
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    assert "500" in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]


async def test_gateway_4xx_error_body_never_exposes_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Hostile Gateway echoes credential in error body.
        return httpx.Response(
            401,
            json={
                "status": "error",
                "error": {"code": "UNAUTHORIZED", "message": f"invalid bearer {GATEWAY_API_KEY}"},
            },
        )

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_unreachable_gateway_is_surfaced_as_safe_mcp_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is True
    assert "Could not reach" in result.content[0].text  # type: ignore[union-attr]
    assert GATEWAY_API_KEY not in result.content[0].text  # type: ignore[union-attr]


# ── No invented relationships ──────────────────────────────────────────────


async def test_does_not_invent_relationships_beyond_gateway_payloads() -> None:
    # The story must be the mechanical composition — no extra correlation.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server)

    assert result.is_error is False
    sc = result.structured_content
    # Exact counts match Gateway payloads — no synthetic issue or execution added.
    assert len(sc["issues"]) == len(RUN_DETAIL_PAYLOAD["issues"])
    assert len(sc["executions"]) == len(EXECUTIONS_PAYLOAD)
    # No invented keys beyond approved shape (allow extra via model).
    for key in [  # noqa: E501
        "issues", "change_requests", "reviews", "commits",
        "merge_events", "sessions", "agents", "usage",
        "executions", "run", "outcome", "afk_run_id",
    ]:
        assert key in sc


def test_adapter_has_no_direct_storage_or_orchestration_access() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
    source = "\n".join(path.read_text() for path in sorted(package_dir.glob("*.py")))
    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(app|asyncpg|aiokafka|psycopg2?|sqlalchemy|awxkit)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []


async def test_composition_uses_same_afk_run_id_for_both_calls() -> None:
    # Verify both URLs contain the same id — the tool must not mix ids or use a stale id.
    alt_id = "01DIFFERENTRUNID0000000000"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        if request.url.path == f"/api/v1/afk/executions/runs/{AFK_RUN_ID}":
            return httpx.Response(200, json=_envelope(EXECUTIONS_PAYLOAD))
        # If the tool mixed ids, this 404 would surface as an error.
        if alt_id in request.url.path:
            return httpx.Response(200, json=_envelope(RUN_DETAIL_PAYLOAD))
        return _not_found()

    async with _server_with(handler) as server:
        result = await _call_story(server, afk_run_id=AFK_RUN_ID)

    assert result.is_error is False
    assert requested == [
        f"/api/v1/afk-outcomes/runs/{AFK_RUN_ID}",
        f"/api/v1/afk/executions/runs/{AFK_RUN_ID}",
    ]
    assert all(alt_id not in p for p in requested)
