"""HTTP client for the AWX REST API.

Implements a thin httpx-based client that authenticates with a Bearer
token and provides launch_job_template for launching AWX job templates.
Follows the same pattern as OpenCodeServeClient.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

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
        status: The job status (e.g. ``"pending"``, ``"running"``,
            ``"successful"``, ``"failed"``).
        started: ISO 8601 timestamp when the job started, if available.
        finished: ISO 8601 timestamp when the job finished, if available.
    """

    job_id: int
    status: str
    started: str | None = None
    finished: str | None = None


class AWXJobResult(BaseModel):
    """Final result of a completed AWX job.

    Attributes:
        job_id: The AWX job ID.
        status: The final job status (e.g. ``"successful"``).
        elapsed_seconds: Total job elapsed time in seconds, if available.
        artifacts: Arbitrary artifacts returned by the AWX job.
    """

    job_id: int
    status: str
    elapsed_seconds: float | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)


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

    async def get_job(self, job_id: int) -> AWXJobSummary:
        """Get the current status of an AWX job.

        Calls ``GET /api/v2/jobs/{job_id}/`` and returns a summary
        with the job's current status and timestamps.

        Args:
            job_id: The AWX job ID to query.

        Returns:
            AWXJobSummary with ``job_id``, ``status``, ``started``,
            and ``finished`` fields.

        Raises:
            AWXHTTPError: If the server returns a non-2xx status
                (e.g. 404 when the job is not found).
        """
        response = await self._request(
            "GET",
            f"/api/v2/jobs/{job_id}/",
        )
        data = response.json()

        return AWXJobSummary(
            job_id=data.get("id", job_id),
            status=data.get("status", "unknown"),
            started=data.get("started"),
            finished=data.get("finished"),
        )

    async def wait_for_job(
        self,
        job_id: int,
        max_seconds: int | None = None,
    ) -> AWXJobResult:
        """Poll an AWX job until it completes or times out.

        Calls :meth:`get_job` every ``poll_interval_seconds`` until the
        job reaches a terminal status (``"successful"``, ``"failed"``,
        ``"error"``, or ``"canceled"``).

        Does **not** auto-cancel the job on timeout — the job continues
        running on the AWX instance and must be cancelled separately if
        desired.

        Args:
            job_id: The AWX job ID to poll.
            max_seconds: Maximum seconds to wait before raising
                :class:`AWXTimeoutError`. Defaults to the client's
                ``timeout_seconds`` configuration.

        Returns:
            AWXJobResult with ``job_id``, ``status``, ``elapsed_seconds``,
            and ``artifacts`` when the job completes successfully.

        Raises:
            AWXTimeoutError: If the job does not reach a terminal
                status within ``max_seconds``.
            AWXJobError: If the job finishes with status ``"failed"``,
                ``"error"``, or ``"canceled"``.
        """
        timeout = max_seconds if max_seconds is not None else self._timeout
        deadline = time.monotonic() + timeout

        while True:
            job = await self.get_job(job_id)
            status = job.status

            if status in ("successful", "failed", "error", "canceled"):
                elapsed_seconds: float | None = None

                # Compute elapsed from started/finished if available.
                # Normalise trailing 'Z' (ISO 8601 UTC) to '+00:00' for
                # Python < 3.11 compatibility.
                if job.started and job.finished:
                    try:
                        from datetime import datetime as _dt

                        started_ts = job.started
                        finished_ts = job.finished
                        if started_ts.endswith("Z"):
                            started_ts = started_ts[:-1] + "+00:00"
                        if finished_ts.endswith("Z"):
                            finished_ts = finished_ts[:-1] + "+00:00"

                        started_dt = _dt.fromisoformat(started_ts)
                        finished_dt = _dt.fromisoformat(finished_ts)
                        elapsed_seconds = (
                            finished_dt - started_dt
                        ).total_seconds()
                    except (ValueError, TypeError):
                        elapsed_seconds = None

                if status == "successful":
                    # Fetch full detail for artifacts
                    response = await self._request(
                        "GET",
                        f"/api/v2/jobs/{job_id}/",
                    )
                    detail = response.json()
                    artifacts = detail.get("artifacts", {}) or {}
                    return AWXJobResult(
                        job_id=job.job_id,
                        status=status,
                        elapsed_seconds=elapsed_seconds,
                        artifacts=artifacts,
                    )

                # Non-successful terminal status
                raise AWXJobError(
                    f"AWX job {job_id} finished with status '{status}'",
                    job_id=job.job_id,
                )

            if time.monotonic() > deadline:
                raise AWXTimeoutError(
                    f"AWX job {job_id} did not complete within "
                    f"{timeout} seconds (current status: '{status}')",
                )

            await asyncio.sleep(self._poll_interval)

    async def cancel_job(self, job_id: int) -> AWXJobResult:
        """Cancel a running AWX job.

        Sends ``POST /api/v2/jobs/{job_id}/cancel/`` to request
        cancellation. Returns the final job status after cancellation.

        Args:
            job_id: The AWX job ID to cancel.

        Returns:
            AWXJobResult with ``job_id``, ``status``,
            ``elapsed_seconds``, and ``artifacts``.

        Raises:
            AWXHTTPError: If the server returns a non-2xx status.
        """
        response = await self._request(
            "POST",
            f"/api/v2/jobs/{job_id}/cancel/",
        )
        data = response.json()

        # AWX returns the job detail after cancellation
        started = data.get("started")
        finished = data.get("finished")
        elapsed_seconds: float | None = None

        if started and finished:
            try:
                from datetime import datetime as _dt

                started_ts = started
                finished_ts = finished
                if started_ts.endswith("Z"):
                    started_ts = started_ts[:-1] + "+00:00"
                if finished_ts.endswith("Z"):
                    finished_ts = finished_ts[:-1] + "+00:00"

                elapsed_seconds = (
                    _dt.fromisoformat(finished_ts) - _dt.fromisoformat(started_ts)
                ).total_seconds()
            except (ValueError, TypeError):
                elapsed_seconds = None

        return AWXJobResult(
            job_id=data.get("id", job_id),
            status=data.get("status", "unknown"),
            elapsed_seconds=elapsed_seconds,
            artifacts=data.get("artifacts", {}) or {},
        )
