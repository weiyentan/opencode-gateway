"""HTTP client for the AWX REST API.

Implements a thin httpx-based client that authenticates with a Bearer
token and provides launch_job_template for launching AWX job templates.
Follows the same pattern as OpenCodeServeClient.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

from app.executors.awx.exceptions import (
    AWXClientError,
    AWXConnectionError,
    AWXHTTPError,
    AWXJobError,
    AWXTimeoutError,
)

logger = logging.getLogger(__name__)


# ── Response models ──────────────────────────────────────────────────────


class AWXJobSummary(BaseModel):
    """Summary of a launched AWX job.

    Attributes:
        job_id: The AWX job ID returned by the launch endpoint.
        status: The initial job status (e.g. ``"pending"``, ``"running"``).
    """

    job_id: int
    status: str


# ── Client implementation ────────────────────────────────────────────────


class AWXApiClient:
    """HTTP client for the AWX REST API.

    Authenticates with a Bearer token and communicates with an AWX
    instance to manage job templates and workflow runs.

    Args:
        base_url: Base URL of the AWX instance
            (e.g. ``https://awx.example.com``).
        token: AWX API Bearer token for authentication.
        timeout_seconds: Timeout in seconds for HTTP requests (default 300).
        poll_interval_seconds: Seconds between poll retries when waiting
            for job completion (default 5).

    Usage::

        client = AWXApiClient(
            base_url="https://awx.example.com",
            token="abc123",
        )
        result = await client.launch_job_template(42, {"repo_url": "..."})
        await client.close()
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: int = 300,
        poll_interval_seconds: int = 5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {token}"},
        )

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and free resources."""
        await self._client.aclose()

    async def __aenter__(self) -> AWXApiClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Internal helpers ─────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform an HTTP request and handle errors transparently.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: URL path relative to the base URL
                (e.g. ``/api/v2/job_templates/42/launch/``).
            **kwargs: Additional arguments passed to
                ``httpx.AsyncClient.request``.

        Returns:
            The HTTP response on success (2xx).

        Raises:
            AWXTimeoutError: If the request times out.
            AWXConnectionError: If the connection fails.
            AWXHTTPError: If the server returns a non-2xx status.
            AWXClientError: For any other httpx error.
        """
        url = f"{self._base_url}{path}"
        logger.debug("Sending %s request to %s", method, url)

        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            logger.debug("Request to %s timed out: %s", url, exc)
            raise AWXTimeoutError(str(exc)) from exc
        except httpx.ConnectError as exc:
            logger.debug("Connection to %s failed: %s", url, exc)
            raise AWXConnectionError(str(exc)) from exc
        except httpx.HTTPError as exc:
            logger.debug("HTTP error during request to %s: %s", url, exc)
            raise AWXClientError(str(exc)) from exc

        logger.debug("Received response %s from %s", response.status_code, url)

        if response.status_code >= 400:
            raise AWXHTTPError(
                f"AWX returned status {response.status_code} "
                f"for {method} {url}",
                status_code=response.status_code,
            )

        return response

    # ── Public API ───────────────────────────────────────────────────

    async def launch_job_template(
        self,
        template_id: int,
        extra_vars: dict[str, Any] | None = None,
    ) -> AWXJobSummary:
        """Launch an AWX job template and return the job summary.

        Calls ``POST /api/v2/job_templates/{template_id}/launch/`` with
        optional ``extra_vars`` in the request body.

        Args:
            template_id: The AWX job template ID to launch.
            extra_vars: Optional dictionary of extra variables to pass
                to the job template.

        Returns:
            AWXJobSummary with ``job_id`` and initial ``status``.

        Raises:
            AWXJobError: If the AWX job launch response is missing
                required fields.
        """
        payload: dict[str, Any] = {}
        if extra_vars is not None:
            payload["extra_vars"] = extra_vars

        response = await self._request(
            "POST",
            f"/api/v2/job_templates/{template_id}/launch/",
            json=payload,
        )

        data = response.json()

        # AWX returns the launched job object with an ``id`` field.
        job_id = data.get("id")
        if job_id is None:
            raise AWXJobError(
                f"AWX launch response missing job ID for template {template_id}",
                job_id=-1,
            )

        status = data.get("status", "unknown")
        return AWXJobSummary(job_id=job_id, status=status)
