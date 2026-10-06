"""Gateway configuration and HTTP client for the published OpenCode Gateway API.

This module owns the adapter's only transport concern: authenticated, read-only
HTTP calls to the published OpenCode Gateway API. It never imports Gateway
implementation modules, never touches Postgres/Kafka/collector databases, and
never exposes the configured API key in errors or representations.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

GATEWAY_URL_ENV_VAR = "OPENCODE_GATEWAY_URL"
GATEWAY_API_KEY_ENV_VAR = "OPENCODE_GATEWAY_API_KEY"


class GatewayConfigError(Exception):
    """Required Gateway configuration is missing or invalid."""


class GatewayError(Exception):
    """Base class for safe, MCP-surfaced Gateway transport failures.

    Messages are safe to return to MCP callers: they never contain the API
    key, request headers, or raw Gateway response bodies.
    """


class GatewayHTTPError(GatewayError):
    """The Gateway returned a 4xx/5xx response."""

    def __init__(self, path: str, status_code: int, reason_phrase: str = "") -> None:
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        detail = f"HTTP {status_code}"
        if reason_phrase:
            detail = f"{detail} {reason_phrase}"
        super().__init__(f"Gateway request to {path} failed with {detail}")


class GatewayConnectionError(GatewayError):
    """The Gateway could not be reached."""


class GatewayResponseError(GatewayError):
    """The Gateway returned a response that could not be interpreted."""


@dataclass(frozen=True)
class GatewayConfig:
    """Runtime configuration for the Gateway HTTP client.

    ``api_key`` is excluded from the dataclass ``repr`` so credentials never
    leak through logs or debugging output.
    """

    base_url: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        """Build configuration from ``OPENCODE_GATEWAY_URL``/``OPENCODE_GATEWAY_API_KEY``."""
        values = os.environ if env is None else env
        raw_url = (values.get(GATEWAY_URL_ENV_VAR) or "").strip()
        raw_key = (values.get(GATEWAY_API_KEY_ENV_VAR) or "").strip()
        if not raw_url:
            raise GatewayConfigError(f"{GATEWAY_URL_ENV_VAR} is not set")
        if not raw_key:
            raise GatewayConfigError(f"{GATEWAY_API_KEY_ENV_VAR} is not set")
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GatewayConfigError(
                f"{GATEWAY_URL_ENV_VAR} must be an absolute http(s) URL"
            )
        return cls(base_url=raw_url.rstrip("/"), api_key=raw_key)


class GatewayClient:
    """Read-only HTTP client for the published OpenCode Gateway API."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._timeout = timeout

    @property
    def config(self) -> GatewayConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": "application/json",
        }

    async def _get_json(self, path: str) -> Any:
        """Fetch ``GET {path}`` and return the JSON-decoded body.

        - Raises :class:`GatewayConnectionError` for transport failures.
        - Raises :class:`GatewayHTTPError` for 4xx/5xx with credential-free message.
        - Raises :class:`GatewayResponseError` for non-JSON or non-object/list bodies.
        - Unwraps the Gateway envelope ``{status: "ok", data: ...}`` when present;
          otherwise returns the payload unchanged (so controlled mocks may supply
          either shape).
        """
        url = f"{self._config.base_url}{path}"
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers=self._headers(), timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise GatewayConnectionError(
                f"Could not reach the OpenCode Gateway at {self._config.base_url} "
                f"({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise GatewayHTTPError(path, response.status_code, response.reason_phrase)
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                f"OpenCode Gateway returned a non-JSON {path} response"
            ) from None
        # Unwrap the standard Gateway envelope when present.
        if isinstance(payload, dict) and payload.get("status") == "ok" and "data" in payload:
            return payload["data"]
        if isinstance(payload, dict) and payload.get("status") == "error":
            # Should have been surfaced as GatewayHTTPError via status code, but
            # some handlers return 200 with error envelope — surface as response error.
            raise GatewayResponseError(
                f"OpenCode Gateway returned an error envelope for {path}"
            )
        return payload

    async def get_health(self) -> dict[str, Any]:
        """Fetch ``GET /health`` and return the JSON object unchanged.

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        result = await self._get_json("/health")
        if not isinstance(result, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected /health payload shape"
            )
        return result

    async def get_afk_run_detail(self, afk_run_id: str) -> dict[str, Any]:
        """Fetch ``GET /api/v1/afk-outcomes/runs/{afk_run_id}``.

        Returns the canonical run-detail payload (run, outcome, issues,
        change_requests, reviews, commits, merge_events, sessions, agents,
        usage) as reported by the Gateway. ``null`` values are preserved
        exactly as returned.

        Raises a :class:`GatewayError` for transport failures, non-2xx
        responses, and unparseable bodies.
        """
        path = f"/api/v1/afk-outcomes/runs/{afk_run_id}"
        result = await self._get_json(path)
        if not isinstance(result, dict):
            raise GatewayResponseError(
                f"OpenCode Gateway returned an unexpected {path} payload shape"
            )
        return result

    async def get_afk_executions_for_run(self, afk_run_id: str) -> list[dict[str, Any]]:
        """Fetch ``GET /api/v1/afk/executions/runs/{afk_run_id}``.

        Returns the complete execution-attempt history for the run as a list
        of execution binding read payloads, including failed attempts and
        retries, in the order returned by the Gateway. ``null`` / ``None``
        values are preserved.

        Raises a :class:`GatewayError` for transport failures, non-2xx
        responses, and unparseable bodies.
        """
        path = f"/api/v1/afk/executions/runs/{afk_run_id}"
        result = await self._get_json(path)
        if not isinstance(result, list):
            raise GatewayResponseError(
                f"OpenCode Gateway returned an unexpected {path} payload shape"
            )
        return result
