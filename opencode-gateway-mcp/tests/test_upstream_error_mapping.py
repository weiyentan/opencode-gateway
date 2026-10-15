"""Contract tests for MCP upstream error mapping (issue #780).

Verifies the MCP tool boundary translates upstream Gateway HTTP failures
and timeouts into distinguishable upstream-failure categories without
misclassifying them as invalid local arguments, and that successful
payloads remain compatible with no secret leakage.
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
SESSION_ID = "11111111-1111-1111-1111-111111111111"

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> GatewayConfig:
    return GatewayConfig(base_url=GATEWAY_BASE_URL, api_key=GATEWAY_API_KEY)


@asynccontextmanager
async def _server_with(handler: Handler) -> AsyncIterator[MCPServer]:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        yield create_server(_config(), http_client=http_client)


async def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as mcp_client:
        return await mcp_client.call_tool(name, arguments)


# ── HTTP 500 surfaces explicit upstream-failure code ───────────────────────


async def test_http_500_surfaces_explicit_upstream_failure_code() -> None:
    """Upstream HTTP 500 must surface an explicit safe upstream-failure code
    in the MCP tool error, even if outer status cannot be altered."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Hostile gateway tries to echo secret and raw body
        return httpx.Response(
            500,
            text="internal server error: leaked " + GATEWAY_API_KEY,
        )

    async with _server_with(handler) as server:
        result = await _call(server, "get_gateway_health", {})

    assert result.is_error is True
    text = result.content[0].text
    # Must contain explicit upstream failure marker and HTTP 500, never "Invalid"
    assert "500" in text
    assert "UPSTREAM" in text.upper() or "upstream" in text.lower()
    # Explicit code check: should contain UPSTREAM_ERROR or similar
    assert "UPSTREAM_ERROR" in text or "upstream failure" in text.lower()
    assert "Invalid" not in text
    assert "invalid" not in text.lower() or "invalid" not in text.lower().split()  # no blame
    assert GATEWAY_API_KEY not in text
    # Raw body contains "leaked" marker plus api key; reason phrase is "Internal Server Error"
    # but raw body must not be echoed. Check leaked marker not present.
    assert "leaked" not in text.lower()
    assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_http_404_distinguishable_from_500_and_invalid() -> None:
    """HTTP 404 must be distinguishable from HTTP 500 and from invalid local arguments."""

    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "error", "error": {"code": "NOT_FOUND"}})

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    # 404 case
    async with _server_with(handler_404) as server:
        result_404 = await _call(server, "get_gateway_health", {})
    assert result_404.is_error is True
    text_404 = result_404.content[0].text
    assert "404" in text_404
    assert GATEWAY_API_KEY not in text_404
    # Must contain distinct upstream not-found marker, not generic 500 marker
    assert "NOT_FOUND" in text_404 or "not found" in text_404.lower()
    # Must be upstream, not Invalid
    assert "Invalid" not in text_404

    # 500 case for contrast
    async with _server_with(handler_500) as server:
        result_500 = await _call(server, "get_gateway_health", {})
    assert result_500.is_error is True
    text_500 = result_500.content[0].text
    assert "500" in text_500
    # The two messages must be distinguishable (different codes)
    assert text_404 != text_500
    # 404 category must not be the same as 500's generic upstream error
    # At least one of them contains its own status; they must not collide
    assert "404" not in text_500
    assert "500" not in text_404

    # Invalid argument case - distinct from both upstream errors
    async with _server_with(handler_404) as server:
        # list_change_requests with invalid provider triggers local validation before gateway call
        result_invalid = await _call(
            server, "list_change_requests", {"provider": "invalid_provider"}
        )
    assert result_invalid.is_error is True
    text_invalid = result_invalid.content[0].text
    assert "Invalid" in text_invalid
    assert "UPSTREAM" not in text_invalid.upper()
    assert text_invalid != text_404
    assert text_invalid != text_500


async def test_timeout_distinguishable_from_500_and_invalid() -> None:
    """Read/timeout errors must be distinguishable from HTTP 500 and invalid arguments."""

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    async with _server_with(handler_timeout) as server:
        result_timeout = await _call(server, "get_gateway_health", {})

    assert result_timeout.is_error is True
    text_timeout = result_timeout.content[0].text
    assert GATEWAY_API_KEY not in text_timeout
    # Must surface upstream timeout/connection category, not Invalid
    assert "Invalid" not in text_timeout
    assert "UPSTREAM" in text_timeout.upper() or "upstream" in text_timeout.lower()
    # Timeout-specific marker
    assert (
        "TIMEOUT" in text_timeout.upper()
        or "timeout" in text_timeout.lower()
        or "CONNECTION" in text_timeout.upper()
    )

    async with _server_with(handler_500) as server:
        result_500 = await _call(server, "get_gateway_health", {})
    text_500 = result_500.content[0].text
    assert text_timeout != text_500
    assert "500" not in text_timeout

    # Invalid argument still distinct
    async with _server_with(handler_timeout) as server:
        result_invalid = await _call(server, "list_sessions", {"status": "bogus"})
    assert result_invalid.is_error is True
    assert "Invalid" in result_invalid.content[0].text
    assert result_invalid.content[0].text != text_timeout


async def test_connection_error_distinguishable_from_invalid() -> None:
    """Generic connection errors must not be blamed as invalid input."""

    def handler_conn(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _server_with(handler_conn) as server:
        result = await _call(server, "get_gateway_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert "Invalid" not in text
    assert "UPSTREAM" in text.upper() or "upstream" in text.lower() or "Could not reach" in text
    assert GATEWAY_API_KEY not in text


async def test_successful_payload_remains_compatible() -> None:
    """Successful tool payloads must remain compatible (same shape)."""
    healthy = {"status": "ok", "version": "0.4.2", "database": "connected"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=healthy)

    async with _server_with(handler) as server:
        result = await _call(server, "get_gateway_health", {})

    assert result.is_error is False
    sc = result.structured_content
    assert sc is not None
    assert sc["status"] == "ok"
    assert sc["version"] == "0.4.2"
    assert sc["database"] == "connected"


async def test_no_secret_or_body_leak_on_upstream_errors() -> None:
    """No API keys, headers, or raw bodies leak into upstream error messages."""
    leaked = f"Bearer {GATEWAY_API_KEY} and raw body secret"

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": leaked})

    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": leaked})

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(leaked, request=request)

    for handler in (handler_500, handler_404, handler_timeout):
        async with _server_with(handler) as server:
            result = await _call(server, "get_gateway_health", {})
        assert result.is_error is True
        text = result.content[0].text
        assert GATEWAY_API_KEY not in text
        assert leaked not in text
        assert "Bearer" not in text
        assert GATEWAY_API_KEY not in str(result.structured_content)


async def test_upstream_errors_across_multiple_tools_are_consistent() -> None:
    """Upstream mapping must be consistent across tools (not only health)."""

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    # get_model_usage
    async with _server_with(handler_500) as server:
        r1 = await _call(
            server,
            "get_model_usage",
            {"start_date": "2026-09-01", "end_date": "2026-09-30"},
        )
    assert r1.is_error is True
    assert "500" in r1.content[0].text
    assert "Invalid" not in r1.content[0].text

    # list_sessions
    async with _server_with(handler_500) as server:
        r2 = await _call(server, "list_sessions", {})
    assert r2.is_error is True
    assert "500" in r2.content[0].text

    # get_session_detail with valid UUID but upstream 500
    async with _server_with(handler_500) as server:
        r3 = await _call(server, "get_session_detail", {"session_id": SESSION_ID})
    assert r3.is_error is True
    assert "500" in r3.content[0].text
    assert "Invalid" not in r3.content[0].text
