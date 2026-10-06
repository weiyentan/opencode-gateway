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

    async def get_health(self) -> dict[str, Any]:
        """Fetch ``GET /health`` and return the JSON object unchanged.

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        url = f"{self._config.base_url}/health"
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
            raise GatewayHTTPError("/health", response.status_code, response.reason_phrase)
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                "OpenCode Gateway returned a non-JSON /health response"
            ) from None
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected /health payload shape"
            )
        return payload

    async def get_usage_aggregates(
        self,
        start_date: str,
        end_date: str,
        group_by: str,
        client_id: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch ``GET /api/v1/usage/aggregates`` with the given grouping.

        ``group_by`` is always supplied by the caller (``model`` or ``agent`` for
        the MCP usage tools) and never taken from untrusted MCP input.  Optional
        filters are forwarded only when explicitly provided and only where the
        base API supports them (``client_id``, ``model``, ``session_id``).

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        url = f"{self._config.base_url}/api/v1/usage/aggregates"
        params: dict[str, str] = {
            "start_date": start_date,
            "end_date": end_date,
            "group_by": group_by,
        }
        if client_id is not None:
            params["client_id"] = client_id
        if model is not None:
            params["model"] = model
        if session_id is not None:
            params["session_id"] = session_id
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(
                        url, headers=self._headers(), params=params
                    )
        except httpx.HTTPError as exc:
            raise GatewayConnectionError(
                f"Could not reach the OpenCode Gateway at {self._config.base_url} "
                f"({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise GatewayHTTPError(
                "/api/v1/usage/aggregates", response.status_code, response.reason_phrase
            )
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                "OpenCode Gateway returned a non-JSON /api/v1/usage/aggregates response"
            ) from None
        # The aggregates endpoint returns a bare JSON array (not the envelope's
        # ``data`` wrapper) when accessed via the raw client; the FastAPI envelope
        # is stripped by httpx when the test mock returns the inner list directly.
        # Normalize both: if the payload is a dict with ``data``, unwrap it.
        if isinstance(payload, dict) and "data" in payload:
            inner = payload["data"]
            if not isinstance(inner, list):
                raise GatewayResponseError(
                    "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
                )
            return inner  # type: ignore[return-value]
        if not isinstance(payload, list):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
            )
        return payload  # type: ignore[return-value]
