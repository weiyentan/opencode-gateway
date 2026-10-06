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

    async def list_afk_runs(
        self,
        *,
        repository: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
        started_from: str | None = None,
        started_to: str | None = None,
        finished_from: str | None = None,
        finished_to: str | None = None,
        seen_from: str | None = None,
        seen_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """Fetch ``GET /api/v1/afk-outcomes/runs`` with explicit filters.

        The MCP tool exposes ``provider``; the Gateway's query parameter is
        ``origin`` — this method translates ``provider`` to ``origin`` and
        forwards all other supported filters verbatim. Only one page is fetched;
        ``limit``/``offset`` are forwarded as-is without silent crawling.
        Null/unavailable values from the Gateway are preserved unchanged.

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        url = f"{self._config.base_url}/api/v1/afk-outcomes/runs"
        params: dict[str, str] = {}
        if repository is not None:
            params["repository"] = repository
        if provider is not None:
            params["origin"] = provider
        if outcome is not None:
            params["outcome"] = outcome
        if started_from is not None:
            params["started_from"] = started_from
        if started_to is not None:
            params["started_to"] = started_to
        if finished_from is not None:
            params["finished_from"] = finished_from
        if finished_to is not None:
            params["finished_to"] = finished_to
        if seen_from is not None:
            params["seen_from"] = seen_from
        if seen_to is not None:
            params["seen_to"] = seen_to
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, params=params, headers=self._headers(), timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(
                        url, params=params, headers=self._headers()
                    )
        except httpx.HTTPError as exc:
            raise GatewayConnectionError(
                f"Could not reach the OpenCode Gateway at {self._config.base_url} "
                f"({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise GatewayHTTPError(
                "/api/v1/afk-outcomes/runs",
                response.status_code,
                response.reason_phrase,
            )
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                "OpenCode Gateway returned a non-JSON "
                "/api/v1/afk-outcomes/runs response"
            ) from None
        if isinstance(payload, dict) and payload.get("status") == "ok" and "data" in payload:
            data = payload["data"]
            if isinstance(data, dict):
                payload = data
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk-outcomes/runs payload shape"
            )
        return payload

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

    async def get_afk_dashboard_summary(
        self,
        *,
        from_date: str,
        to_date: str,
        provider: str | None = None,
        repository: str | None = None,
        interval: str = "daily",
    ) -> dict[str, Any]:
        """Fetch ``GET /api/v1/afk/dashboard/summary`` with explicit UTC-calendar semantics.

        The caller must supply explicit ``from_date``/``to_date`` (ISO-8601 calendar
        dates, UTC); no implicit "today" is substituted. ``interval`` is ``daily``
        or ``monthly`` per the Gateway contract. ``provider``/``repository`` are
        optional scopes forwarded verbatim. The rollup is UTC-calendar based; the
        method preserves Gateway string representations and nulls without coercion.

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        url = f"{self._config.base_url}/api/v1/afk/dashboard/summary"
        params: dict[str, str] = {
            "from_date": from_date,
            "to_date": to_date,
            "interval": interval,
        }
        if provider is not None:
            params["provider"] = provider
        if repository is not None:
            params["repository"] = repository
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, params=params, headers=self._headers(), timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(
                        url, params=params, headers=self._headers()
                    )
        except httpx.HTTPError as exc:
            raise GatewayConnectionError(
                f"Could not reach the OpenCode Gateway at {self._config.base_url} "
                f"({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise GatewayHTTPError(
                "/api/v1/afk/dashboard/summary",
                response.status_code,
                response.reason_phrase,
            )
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                "OpenCode Gateway returned a non-JSON "
                "/api/v1/afk/dashboard/summary response"
            ) from None
        # Unwrap the ``{status: "ok", data: ...}`` envelope when the Gateway
        # is running with the response-envelope middleware; preserve the inner
        # payload verbatim otherwise (MockTransport tests return unwrapped inner).
        if (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and "data" in payload
            and isinstance(payload["data"], dict)
        ):
            payload = payload["data"]
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk/dashboard/summary payload shape"
            )
        return payload
