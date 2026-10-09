"""v1 acceptance gate: the complete opencode-gateway-mcp contract (ADR 0031).

This module proves the finished MCP surface together: exactly the eleven approved
read-only tools, public MCP-boundary behavior for every tool against a controlled
Gateway HTTP mock, boundary invariants (no writes, no passthrough, no direct
Postgres/Kafka/AWX access, no secret leakage, no silent crawling, no natural
language time resolution, UTC limitations preserved, nulls preserved), the
compatibility fixtures that detect Gateway response-shape drift, and the
published container/CI shape that starts the completed server.

The tests drive the real ``opencode_gateway_mcp.server`` through the public MCP
client surface (``Client``) and control the only external dependency with an
httpx MockTransport. No Gateway implementation module is imported.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from opencode_gateway_mcp.client import GatewayConfig, GatewayConfigError
from opencode_gateway_mcp.server import create_server

GATEWAY_BASE_URL = "https://gateway.example"
GATEWAY_API_KEY = "v1-contract-super-secret-key"

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "opencode_gateway_mcp"
FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "v1_gateway_responses.json"
DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mcp-publish.yml"

EXPECTED_TOOL_NAMES = frozenset(
    {
        "get_afk_activity_summary",
        "list_afk_runs",
        "list_change_requests",
        "get_afk_run_story",
        "get_model_usage",
        "get_agent_usage",
        "get_change_request_story",
        "get_gateway_health",
        "get_correlation_issues",
        "list_sessions",
        "get_session_detail",
    }
)

# The exact input-schema parameter set of each approved v1 tool (drift guard).
EXPECTED_PARAMETERS: dict[str, frozenset[str]] = {
    "get_gateway_health": frozenset(),
    "get_afk_activity_summary": frozenset(
        {"from_date", "to_date", "provider", "repository", "interval"}
    ),
    "list_afk_runs": frozenset(
        {
            "repository",
            "provider",
            "outcome",
            "started_from",
            "started_to",
            "finished_from",
            "finished_to",
            "seen_from",
            "seen_to",
            "limit",
            "offset",
        }
    ),
    "list_change_requests": frozenset(
        {
            "provider",
            "repository",
            "provider_state",
            "activity_from",
            "activity_to",
            "limit",
            "offset",
        }
    ),
    "get_model_usage": frozenset({"start_date", "end_date", "client_id", "model", "session_id"}),
    "get_agent_usage": frozenset({"start_date", "end_date", "client_id", "model", "session_id"}),
    "get_afk_run_story": frozenset({"afk_run_id"}),
    "get_change_request_story": frozenset({"provider", "repository", "external_number"}),
    "get_correlation_issues": frozenset({"reason", "limit", "offset"}),
    "list_sessions": frozenset(
        {
            "client_id",
            "from_date",
            "to_date",
            "agent",
            "external_project_id",
            "status",
            "limit",
            "offset",
        }
    ),
    "get_session_detail": frozenset({"session_id"}),
}

EXPECTED_REQUIRED: dict[str, frozenset[str]] = {
    "get_gateway_health": frozenset(),
    "get_afk_activity_summary": frozenset({"from_date", "to_date"}),
    "list_afk_runs": frozenset(),
    "list_change_requests": frozenset(),
    "get_model_usage": frozenset({"start_date", "end_date"}),
    "get_agent_usage": frozenset({"start_date", "end_date"}),
    "get_afk_run_story": frozenset({"afk_run_id"}),
    "get_change_request_story": frozenset({"provider", "repository", "external_number"}),
    "get_correlation_issues": frozenset(),
    "list_sessions": frozenset(),
    "get_session_detail": frozenset({"session_id"}),
}

# Minimal valid arguments per tool (used for negative/error matrix tests).
TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_gateway_health": {},
    "get_afk_activity_summary": {"from_date": "2026-09-01", "to_date": "2026-09-30"},
    "list_afk_runs": {},
    "list_change_requests": {},
    "get_model_usage": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
    "get_agent_usage": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
    "get_afk_run_story": {"afk_run_id": "01H5K6XYZABCDEF1234567890"},
    "get_change_request_story": {
        "provider": "github",
        "repository": "acme/proj",
        "external_number": "42",
    },
    "get_correlation_issues": {},
    "list_sessions": {},
    "get_session_detail": {"session_id": "11111111-1111-1111-1111-111111111111"},
}

# The only Gateway paths the v1 adapter is allowed to call, as anchor patterns.
APPROVED_PATH_PATTERNS = (
    re.compile(r"^/health$"),
    re.compile(r"^/api/v1/afk/dashboard/summary$"),
    re.compile(r"^/api/v1/afk-outcomes/runs$"),
    re.compile(r"^/api/v1/afk-outcomes/runs/[^/]+$"),
    re.compile(r"^/api/v1/afk/executions/runs/[^/]+$"),
    re.compile(r"^/api/v1/afk-outcomes/change-requests$"),
    re.compile(r"^/api/v1/afk-outcomes/change-requests/[^/]+/.+$"),
    re.compile(r"^/api/v1/usage/aggregates$"),
    re.compile(r"^/api/v1/afk-outcomes/correlations$"),
    re.compile(r"^/api/v1/usage/agent-runs$"),
    re.compile(r"^/api/v1/usage/agent-runs/[^/]+$"),
)

# Parameters that would indicate a generic arbitrary HTTP passthrough tool.
FORBIDDEN_PASSTHROUGH_PARAMETERS = frozenset(
    {"path", "url", "endpoint", "method", "query", "body", "headers", "http_method"}
)

FIXTURES: Any = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call_tool(server: MCPServer, name: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool(name, arguments)


def _payload(result: Any) -> Any:
    """Normalize ``structured_content``: list-returning tools wrap as ``{"result": [...]}``."""
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def _walk(value: Any, path: list[Any]) -> Any:
    for step in path:
        value = value[step]
    return value


def _fixture_handler(entry: dict[str, Any], seen: list[httpx.Request]) -> Handler:
    responses: Any = entry["responses"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        index = len(seen) - 1
        if index >= len(responses):
            pytest.fail(f"unexpected extra Gateway request: {request.method} {request.url}")
        expected: Any = responses[index]
        assert request.url.path == expected["path"], (
            f"tool called {request.url.path}, expected {expected['path']}"
        )
        return httpx.Response(200, json=expected["body"])

    return handler


async def _list_tool_descriptions(server: MCPServer) -> dict[str, str | None]:
    async with Client(server) as mcp_client:
        tools = await mcp_client.list_tools()
    return {tool.name: tool.description for tool in tools.tools}


# ── Tool surface: exactly eleven approved read-only tools ─────────────────────


async def test_exactly_the_nine_approved_read_only_tools_are_exposed() -> None:
    async with Client(create_server(_config())) as mcp_client:
        tools = await mcp_client.list_tools()

    names = {tool.name for tool in tools.tools}
    assert names == set(EXPECTED_TOOL_NAMES), (
        f"tool surface drifted: expected {sorted(EXPECTED_TOOL_NAMES)}, got {sorted(names)}"
    )

    write_markers = (
        "write",
        "admin",
        "reconcile",
        "ingest",
        "provision",
        "delete",
        "patch",
        "create",
        "update",
        "call",
        "passthrough",
        "raw",
    )
    assert not any(
        marker in name.lower() for name in names for marker in write_markers
    ), "a tool name suggests a write/admin/passthrough capability"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
async def test_every_tool_schema_exposes_only_approved_parameters(tool_name: str) -> None:
    async with Client(create_server(_config())) as mcp_client:
        tools = await mcp_client.list_tools()

    tool = next(t for t in tools.tools if t.name == tool_name)
    schema: Any = tool.input_schema
    properties = set(schema.get("properties", {}))
    required = set(schema.get("required") or [])

    assert properties == set(EXPECTED_PARAMETERS[tool_name]), (
        f"{tool_name} parameter surface drifted: {sorted(properties)}"
    )
    assert required == set(EXPECTED_REQUIRED[tool_name]), (
        f"{tool_name} required-parameter surface drifted: {sorted(required)}"
    )
    assert properties.isdisjoint(FORBIDDEN_PASSTHROUGH_PARAMETERS), (
        f"{tool_name} exposes a generic passthrough parameter"
    )
    assert (tool.description or "").strip(), f"{tool_name} must carry a description"


# ── Public MCP-boundary behavior for all eleven tools ─────────────────────────


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
async def test_every_tool_replays_fixture_through_public_mcp_boundary(tool_name: str) -> None:
    """Each tool call through the public MCP surface returns its fixture facts,
    preserves nulls, and calls exactly the approved read-only endpoints."""
    if tool_name not in FIXTURES:
        pytest.skip(f"no v1 fixture yet for {tool_name} — covered by dedicated tests")
    entry: Any = FIXTURES[tool_name]
    seen: list[httpx.Request] = []

    async with _server_with(_fixture_handler(entry, seen)) as server:
        result = await _call_tool(server, tool_name, entry["arguments"])

    assert result.is_error is False, result.content
    assert result.structured_content is not None

    # Every request is a GET to an approved Gateway path with the bearer key only
    # in the Authorization header (never in URL or query).
    assert len(seen) == len(entry["responses"]), "adapter issued an unexpected request"
    for request in seen:
        assert request.method == "GET", "adapter issued a non-GET (write) request"
        assert any(
            pattern.match(request.url.path) for pattern in APPROVED_PATH_PATTERNS
        ), f"request to unapproved path {request.url.path}"
        assert request.headers.get("authorization") == f"Bearer {GATEWAY_API_KEY}"
        assert GATEWAY_API_KEY not in str(request.url)

    payload = _payload(result)
    for path in entry["sentinel_paths"]:
        assert _walk(payload, path) is not None, f"{tool_name} lost sentinel {path}"
    for path in entry["null_paths"]:
        assert _walk(payload, path) is None, f"{tool_name} coerced null at {path}"


@pytest.mark.parametrize("status_code", [400, 404, 500, 503])
@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
async def test_gateway_errors_are_surfaced_as_mcp_errors_never_empty_success(
    tool_name: str, status_code: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Hostile Gateway: tries to echo the credential back in the error body.
        return httpx.Response(status_code, json={"detail": f"leaked {GATEWAY_API_KEY}"})

    async with _server_with(handler) as server:
        result = await _call_tool(server, tool_name, TOOL_ARGUMENTS[tool_name])

    assert result.is_error is True, "a Gateway 4xx/5xx must never become a successful result"
    text = result.content[0].text if result.content else ""
    assert str(status_code) in text, "MCP error must surface the HTTP status"
    assert GATEWAY_API_KEY not in text, "API key leaked into the MCP error"
    assert GATEWAY_API_KEY not in str(result.structured_content)


# ── Pagination, time, UTC, and null invariants ──────────────────────────────


async def test_pagination_is_explicit_and_never_silently_crawls() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        params = dict(request.url.params)
        assert params["limit"] == "1"
        assert params["offset"] == "5"
        body = {
            "items": [{"afk_run_id": "01H5K6XYZABCDEF1234567890"}],
            "total": 100,
            "limit": 1,
            "offset": 5,
        }
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        result = await _call_tool(server, "list_afk_runs", {"limit": 1, "offset": 5})

    assert result.is_error is False
    assert request_count == 1, "list_afk_runs must return one explicit page, never crawl"
    assert _payload(result)["total"] == 100


async def test_change_request_summary_matches_frontend_contract_and_preserves_cost() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = dict(request.url.params)
        assert params == {
            "provider": "gitlab",
            "repository": "group/project",
            "provider_state": "merged",
            "activity_from": "2026-10-07T00:00:00+13:00",
            "activity_to": "2026-10-08T00:15:00+13:00",
            "limit": "20",
            "offset": "0",
        }
        body = {
            "items": [
                {
                    "provider": "gitlab",
                    "repository": "group/project",
                    "external_id": "11",
                    "provider_state": "merged",
                    "total_estimated_cost_usd": "0.42",
                    "latest_linked_activity": "2026-10-07T01:04:36+00:00",
                    "provider_state_observed_at": "2026-10-07T01:04:36+00:00",
                    "executions": {
                        "total": 1,
                        "running": 0,
                        "completed": 1,
                        "failed": 0,
                        "cancelled": 0,
                    },
                }
            ],
            "total": 1,
            "limit": 20,
            "offset": 0,
        }
        return httpx.Response(200, json={"status": "ok", "data": body})

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "list_change_requests",
            {
                "provider": "gitlab",
                "repository": "group/project",
                "provider_state": "merged",
                "activity_from": "2026-10-07T00:00:00+13:00",
                "activity_to": "2026-10-08T00:15:00+13:00",
                "limit": 20,
                "offset": 0,
            },
        )

    assert result.is_error is False
    payload = _payload(result)
    assert payload["items"][0]["total_estimated_cost_usd"] == "0.42"
    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/afk-outcomes/change-requests"


async def test_correlation_pagination_is_explicit_and_never_silently_crawls() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        params = dict(request.url.params)
        assert params["limit"] == "2"
        assert params["offset"] == "4"
        body = {"items": [], "total": 50, "limit": 2, "offset": 4}
        return httpx.Response(200, json=body)

    async with _server_with(handler) as server:
        result = await _call_tool(server, "get_correlation_issues", {"limit": 2, "offset": 4})

    assert result.is_error is False
    assert request_count == 1, "get_correlation_issues must return one explicit page, never crawl"
    assert _payload(result)["total"] == 50


async def test_natural_language_dates_are_forwarded_verbatim_and_never_resolved() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "data": []})

    async with _server_with(handler) as server:
        result = await _call_tool(
            server,
            "get_model_usage",
            {"start_date": "last week", "end_date": "yesterday"},
        )

    assert result.is_error is False
    assert dict(seen[0].url.params)["start_date"] == "last week"
    assert dict(seen[0].url.params)["end_date"] == "yesterday"


async def test_no_implicit_today_is_substituted_for_missing_dates() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with _server_with(handler) as server:
        result = await _call_tool(
            server, "get_afk_activity_summary", {"from_date": "2026-09-01"}
        )

    assert result.is_error is True  # missing required to_date → MCP-level validation error
    assert seen == [], "no Gateway request may be issued when explicit dates are missing"


async def test_utc_calendar_rollup_limitations_are_preserved() -> None:
    entry: Any = FIXTURES["get_afk_activity_summary"]
    seen: list[httpx.Request] = []

    async with _server_with(_fixture_handler(entry, seen)) as server:
        descriptions = await _list_tool_descriptions(server)
        result = await _call_tool(server, "get_afk_activity_summary", entry["arguments"])

    assert result.is_error is False
    payload = _payload(result)
    # UTC-calendar values and timestamps are preserved verbatim — no timezone
    # reconstruction, no implicit today added.
    assert payload["from_date"] == "2026-09-01"
    assert payload["to_date"] == "2026-09-30"
    assert payload["buckets"][0]["period_start"] == "2026-09-01"
    assert payload["buckets"][0]["derived_at"] == "2026-09-30T03:00:00+00:00"
    assert payload["oldest_derived_at"] == "2026-09-01T03:00:00+00:00"
    assert "UTC" in (descriptions["get_afk_activity_summary"] or ""), (
        "the UTC-calendar limitation must be communicated in the tool description"
    )


# ── Configuration, fail-closed, and secret hygiene ──────────────────────────


def test_environment_config_failures_are_clear_and_secret_free() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewayConfig.from_env({})
    message = str(excinfo.value)
    assert "OPENCODE_GATEWAY_URL" in message
    assert "value-never-logged" not in message

    with pytest.raises(GatewayConfigError) as excinfo:
        GatewayConfig.from_env(
            {"OPENCODE_GATEWAY_URL": "https://gateway.example", "OPENCODE_GATEWAY_API_KEY": ""}
        )
    message = str(excinfo.value)
    assert "OPENCODE_GATEWAY_API_KEY" in message

    with pytest.raises(GatewayConfigError) as excinfo:
        GatewayConfig.from_env(
            {"OPENCODE_GATEWAY_URL": "gateway.example:8000", "OPENCODE_GATEWAY_API_KEY": "k"}
        )
    assert "http(s)" in str(excinfo.value)

    config = GatewayConfig(base_url="https://gateway.example", api_key="value-never-logged")
    assert "value-never-logged" not in repr(config)


def test_server_fails_closed_without_environment_configuration() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(PACKAGE_DIR.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        [sys.executable, "-m", "opencode_gateway_mcp.server"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        stdin=subprocess.DEVNULL,
        cwd=str(PACKAGE_DIR),
    )

    assert process.returncode == 1, "server must fail closed without configuration"
    assert "OPENCODE_GATEWAY_URL" in process.stderr
    assert process.stdout == "", "stdout is the MCP protocol channel and must stay clean"


# ── Boundary prohibitions (ADR 0031): no storage, no writes ─────────────────


def test_adapter_has_no_direct_storage_orchestration_imports_and_no_write_verbs() -> None:
    sources = [path.read_text(encoding="utf-8") for path in sorted(PACKAGE_DIR.glob("*.py"))]
    combined = "\n".join(sources)

    forbidden_imports = re.findall(
        r"^\s*(?:import|from)\s+"
        r"(?:app|asyncpg|aiokafka|kafka|confluent_kafka|psycopg|psycopg2|sqlalchemy|"
        r"awx|awxkit|redis|motor|boto3|pymongo)(?:\.|\b)",
        combined,
        flags=re.MULTILINE,
    )
    assert forbidden_imports == [], (
        "the adapter must not import Gateway implementation modules or "
        "Postgres/Kafka/AWX/collector libraries"
    )

    write_verbs = re.findall(
        r"\.(?:post|put|delete|patch)\s*\(|method\s*=\s*['\"](?:POST|PUT|DELETE|PATCH)",
        combined,
    )
    assert write_verbs == [], "the adapter must only ever issue GET requests"


# ── Compatibility fixtures detect Gateway response-shape drift ──────────────


async def test_fixture_drift_is_detected_when_a_required_field_disappears() -> None:
    # Health fixture with the required `version` field removed → MCP error,
    # never an empty successful result.
    broken = deepcopy(FIXTURES["get_gateway_health"]["responses"][0]["body"])
    del broken["version"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broken)

    async with _server_with(handler) as server:
        result = await _call_tool(server, "get_gateway_health", {})

    assert result.is_error is True
    assert "unexpected" in (result.content[0].text if result.content else "")


async def test_fixture_drift_is_detected_when_a_required_block_disappears() -> None:
    # Change-request fixture without the required `change_request` block.
    broken = deepcopy(FIXTURES["get_change_request_story"]["responses"][0]["body"])
    del broken["data"]["change_request"]

    async with _server_with(handler_broken_change_request(broken)) as server:
        result = await _call_tool(
            server,
            "get_change_request_story",
            {"provider": "github", "repository": "acme/proj", "external_number": "42"},
        )

    assert result.is_error is True
    assert "unexpected" in (result.content[0].text if result.content else "")


def handler_broken_change_request(broken: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broken)

    return handler


# ── Published container and CI shape (issue #766 gate) ──────────────────────


def test_dockerfile_runs_the_completed_mcp_server_not_a_stub() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "stub" not in dockerfile.lower(), "Dockerfile must no longer ship the container stub"
    assert "opencode-gateway-mcp/src/opencode_gateway_mcp" in dockerfile, (
        "Dockerfile must COPY the real MCP package from the repo"
    )
    assert "opencode_gateway_mcp.server" in dockerfile, (
        "Dockerfile must start the completed MCP server"
    )
    assert "USER mcp" in dockerfile, "container must run as the non-root mcp user"
    assert "asyncpg" not in dockerfile and "aiokafka" not in dockerfile
    assert "psycopg" not in dockerfile and "sqlalchemy" not in dockerfile

    for line in dockerfile.splitlines():
        if line.strip().startswith("ENV") and "OPENCODE_GATEWAY_API_KEY" in line:
            pytest.fail("Dockerfile must never bake OPENCODE_GATEWAY_API_KEY as ENV")


def test_publish_workflow_smoke_validates_the_full_v1_tool_surface() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tools/list" in workflow, "container smoke must drive the MCP tools/list handshake"
    assert "initialize" in workflow, "container smoke must perform the MCP initialize handshake"
    for name in sorted(EXPECTED_TOOL_NAMES):
        assert name in workflow, f"container smoke check must pin the {name} tool"
    assert "OPENCODE_GATEWAY_API_KEY" in workflow, (
        "container smoke must exercise the runtime API key env"
    )
    assert "pytest" in workflow, "the v1 contract suite must run in CI before publish"