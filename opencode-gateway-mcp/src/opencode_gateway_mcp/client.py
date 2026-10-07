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
from urllib.parse import quote, urlparse

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
                    response = await http_client.get(
                        url, headers=self._headers(), timeout=self._timeout
                    )
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
                        url, params=params, headers=self._headers(), timeout=self._timeout
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
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise GatewayResponseError(
                "OpenCode Gateway returned an error envelope for "
                "/api/v1/afk-outcomes/runs"
            )
        # Unwrap the paginated {status: "ok", data: {items, ...}} envelope when
        # present; a data object without an items list is returned unchanged.
        if (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and isinstance(payload.get("data"), dict)
        ):
            inner = payload["data"]
            if isinstance(inner.get("items"), list):
                payload = inner
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk-outcomes/runs payload shape"
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
                        url, params=params, headers=self._headers(), timeout=self._timeout
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
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise GatewayResponseError(
                "OpenCode Gateway returned an error envelope for "
                "/api/v1/afk/dashboard/summary"
            )
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
                        url, headers=self._headers(), params=params, timeout=self._timeout
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
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise GatewayResponseError(
                "OpenCode Gateway returned an error envelope for /api/v1/usage/aggregates"
            )
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

    async def get_afk_run_detail(self, afk_run_id: str) -> dict[str, Any]:
        """Fetch ``GET /api/v1/afk-outcomes/runs/{afk_run_id}``.

        Returns the canonical run-detail payload (run, outcome, issues,
        change_requests, reviews, commits, merge_events, sessions, agents,
        usage) as reported by the Gateway. ``null`` values are preserved
        exactly as returned.

        Raises a :class:`GatewayError` for transport failures, non-2xx
        responses, and unparseable bodies.
        """
        encoded_id = quote(afk_run_id, safe="")
        path = f"/api/v1/afk-outcomes/runs/{encoded_id}"
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
        encoded_id = quote(afk_run_id, safe="")
        path = f"/api/v1/afk/executions/runs/{encoded_id}"
        result = await self._get_json(path)
        if not isinstance(result, list):
            raise GatewayResponseError(
                f"OpenCode Gateway returned an unexpected {path} payload shape"
            )
        return result

    async def list_change_requests(
        self,
        *,
        provider: str | None = None,
        repository: str | None = None,
        provider_state: str | None = None,
        activity_from: str | None = None,
        activity_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """Fetch the frontend change-request summary list contract.

        Calls ``GET /api/v1/afk-outcomes/change-requests`` with the same
        filters used by the Gateway frontend. The response preserves the
        per-change-request ``total_estimated_cost_usd``, provider lifecycle
        state, latest linked activity, execution counts, and explicit
        pagination. Only one page is fetched; no silent crawling or local
        cost reconstruction is performed.
        """
        url = f"{self._config.base_url}/api/v1/afk-outcomes/change-requests"
        params: dict[str, str] = {}
        if provider is not None:
            params["provider"] = provider
        if repository is not None:
            params["repository"] = repository
        if provider_state is not None:
            params["provider_state"] = provider_state
        if activity_from is not None:
            params["activity_from"] = activity_from
        if activity_to is not None:
            params["activity_to"] = activity_to
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
                        url, params=params, headers=self._headers(), timeout=self._timeout
                    )
        except httpx.HTTPError as exc:
            raise GatewayConnectionError(
                f"Could not reach the OpenCode Gateway at {self._config.base_url} "
                f"({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise GatewayHTTPError(
                "/api/v1/afk-outcomes/change-requests",
                response.status_code,
                response.reason_phrase,
            )
        try:
            payload = response.json()
        except ValueError:
            raise GatewayResponseError(
                "OpenCode Gateway returned a non-JSON "
                "/api/v1/afk-outcomes/change-requests response"
            ) from None
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise GatewayResponseError(
                "OpenCode Gateway returned an error envelope for "
                "/api/v1/afk-outcomes/change-requests"
            )
        if (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and isinstance(payload.get("data"), dict)
        ):
            inner = payload["data"]
            if isinstance(inner.get("items"), list):
                payload = inner
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk-outcomes/change-requests payload shape"
            )
        return payload

    async def get_change_request_detail(
        self, provider: str, repository: str, external_number: str
    ) -> dict[str, Any]:
        """Fetch change-request detail via the published Gateway API.

        Calls ``GET /api/v1/afk-outcomes/change-requests/``
        ``{provider}/{repository}/{external_number}`` and returns the JSON
        object unchanged (envelope-unwrapped). Raises a :class:`GatewayError`
        with a credential-free message for transport failures, non-2xx
        responses, and unparseable bodies. Only this read-only path is ever
        called; no issue reverse lookup or generic passthrough is performed.
        """
        encoded_provider = quote(provider, safe="")
        encoded_repository = quote(repository, safe="/")
        encoded_number = quote(external_number, safe="")
        path = (
            f"/api/v1/afk-outcomes/change-requests/"
            f"{encoded_provider}/{encoded_repository}/{encoded_number}"
        )
        url = f"{self._config.base_url}{path}"
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers=self._headers(), timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(
                        url, headers=self._headers(), timeout=self._timeout
                    )
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
                "OpenCode Gateway returned a non-JSON change-request response"
            ) from None
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise GatewayResponseError(
                f"OpenCode Gateway returned an error envelope for {path}"
            )
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected change-request payload shape"
            )
        if payload.get("status") == "ok" and "data" in payload:
            inner = payload["data"]
            if isinstance(inner, dict):
                return inner
        return payload

    async def get_correlation_issues(
        self,
        *,
        reason: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch ``GET /api/v1/afk-outcomes/correlations`` and return the paginated payload.

        Only one Gateway request is ever issued — no silent crawl. ``reason``
        is ``ambiguous`` or ``unmatched`` when supplied; ``limit``/``offset``
        are forwarded exactly as provided.

        Raises a :class:`GatewayError` with a credential-free message for
        transport failures, non-2xx responses, and unparseable bodies.
        """
        path = "/api/v1/afk-outcomes/correlations"
        url = f"{self._config.base_url}{path}"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if reason is not None:
            params["reason"] = reason
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers=self._headers(), params=params, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http_client:
                    response = await http_client.get(
                        url, headers=self._headers(), params=params, timeout=self._timeout
                    )
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
                "OpenCode Gateway returned a non-JSON correlations response"
            ) from None
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "OpenCode Gateway returned an unexpected correlations payload shape"
            )
        if payload.get("status") == "error":
            raise GatewayResponseError(
                f"OpenCode Gateway returned an error envelope for {path}"
            )
        # Unwrap the standard {status: "ok", data: ...} envelope when present so
        # callers see the paginated {items, total, limit, offset} shape directly.
        if payload.get("status") == "ok" and isinstance(payload.get("data"), dict):
            inner: Any = payload["data"]
            if isinstance(inner.get("items"), list):
                return inner  # type: ignore[no-any-return]
        return payload  # type: ignore[no-any-return]
