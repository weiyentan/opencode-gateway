"""Runtime transport selection tests for opencode-gateway-mcp."""

from __future__ import annotations

from typing import Any

import pytest

from opencode_gateway_mcp import server as server_module


class DummyServer:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("OPENCODE_GATEWAY_API_KEY", "test-key")


def test_main_defaults_to_stdio(
    monkeypatch: pytest.MonkeyPatch,
    gateway_env: None,
) -> None:
    dummy = DummyServer()
    monkeypatch.delenv("OPENCODE_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server_module, "create_server", lambda config: dummy)

    server_module.main()

    assert dummy.calls == [((), {})]


def test_main_runs_streamable_http_for_tunnel_deployments(
    monkeypatch: pytest.MonkeyPatch,
    gateway_env: None,
) -> None:
    dummy = DummyServer()
    monkeypatch.setenv("OPENCODE_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("OPENCODE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OPENCODE_MCP_PORT", "8080")
    monkeypatch.setattr(server_module, "create_server", lambda config: dummy)

    server_module.main()

    assert dummy.calls == [
        (
            (),
            {
                "transport": "streamable-http",
                "host": "0.0.0.0",
                "port": 8080,
                "streamable_http_path": "/mcp",
                "stateless_http": True,
                "json_response": True,
            },
        )
    ]


@pytest.mark.parametrize("value", ["invalid", "123"])
def test_main_rejects_unknown_transport(
    monkeypatch: pytest.MonkeyPatch,
    gateway_env: None,
    value: str,
) -> None:
    dummy = DummyServer()
    monkeypatch.setenv("OPENCODE_MCP_TRANSPORT", value)
    monkeypatch.setattr(server_module, "create_server", lambda config: dummy)

    with pytest.raises(SystemExit):
        server_module.main()

    assert dummy.calls == []


@pytest.mark.parametrize("value", ["abc", "0", "65536"])
def test_main_rejects_invalid_http_port(
    monkeypatch: pytest.MonkeyPatch,
    gateway_env: None,
    value: str,
) -> None:
    dummy = DummyServer()
    monkeypatch.setenv("OPENCODE_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("OPENCODE_MCP_PORT", value)
    monkeypatch.setattr(server_module, "create_server", lambda config: dummy)

    with pytest.raises(SystemExit):
        server_module.main()

    assert dummy.calls == []
