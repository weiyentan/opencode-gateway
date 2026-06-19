"""Fake AWX API client for integration tests.

Provides a deterministic, configurable implementation of the AWX API
client interface.  Returns pre-configured responses instead of making
real HTTP calls.  Supports success, failure, and malformed response
modes with caller-tracking for test assertions.
"""

from __future__ import annotations

from typing import Any

from app.executors.awx.client import AWXJobResult, AWXJobSummary
from app.executors.awx.exceptions import (
    AWXHTTPError,
    AWXJobError,
)

# ── Constants ──────────────────────────────────────────────────────────────

MODE_SUCCESS = "success"
MODE_FAILURE = "failure"
MODE_MALFORMED = "malformed"

# ── Default response data ──────────────────────────────────────────────────

_DEFAULT_LAUNCH_SUMMARY: dict[str, Any] = {
    "job_id": 42,
    "status": "pending",
    "started": None,
    "finished": None,
}

_DEFAULT_JOB_RESULT: dict[str, Any] = {
    "job_id": 42,
    "status": "successful",
    "elapsed_seconds": 300.0,
    "artifacts": {},
}

_DEFAULT_CANCEL_RESULT: dict[str, Any] = {
    "job_id": 42,
    "status": "canceled",
    "elapsed_seconds": None,
    "artifacts": {},
}


class FakeAWXClient:
    """Fake AWX API client with configurable deterministic responses.

    Implements the same public API as :class:`app.executors.awx.client.AWXApiClient`
    but returns pre-configured data instead of making real HTTP calls.

    Args:
        mode: Response mode — one of ``"success"``, ``"failure"``,
            or ``"malformed"``.  Defaults to ``"success"``.
        launch_summary: Dict to construct the :class:`AWXJobSummary`
            returned by :meth:`launch_job_template`.  Overrides the
            default of ``{"job_id": 42, "status": "pending"}``.
        get_job_statuses: Dict mapping job IDs to status strings used
            by :meth:`get_job`.  When a job ID is not in this mapping,
            the default status ``"running"`` is used.
        job_result: Dict to use for :meth:`wait_for_job` and
            :meth:`cancel_job` artifact responses.  Overrides the
            default of ``{"job_id": 42, "status": "successful",
            "elapsed_seconds": 300.0, "artifacts": {}}``.
        cancel_result: Dict to use for :meth:`cancel_job` responses.
            Overrides the default.
        failure_message: Custom message for failure-mode exceptions.
        poll_interval_seconds: Simulated poll interval (default 0 for
            instant completion in tests).

    Caller-tracking attributes (populated during calls):
        * ``launch_calls``: List of ``(template_id, extra_vars)`` tuples.
        * ``get_job_calls``: List of job IDs queried.
        * ``wait_for_job_calls``: List of job IDs waited on.
        * ``cancel_calls``: List of job IDs cancelled.
        * ``close_called``: ``True`` if :meth:`close` was called.
    """

    def __init__(
        self,
        mode: str = MODE_SUCCESS,
        *,
        launch_summary: dict[str, Any] | None = None,
        get_job_statuses: dict[int, str] | None = None,
        job_result: dict[str, Any] | None = None,
        cancel_result: dict[str, Any] | None = None,
        failure_message: str = "Simulated AWX failure",
        poll_interval_seconds: int = 0,
    ) -> None:
        _validate_mode(mode)
        self._mode = mode
        self._launch_summary = launch_summary or dict(_DEFAULT_LAUNCH_SUMMARY)
        self._get_job_statuses = get_job_statuses or {}
        self._job_result = job_result or dict(_DEFAULT_JOB_RESULT)
        self._cancel_result = cancel_result or dict(_DEFAULT_CANCEL_RESULT)
        self._failure_message = failure_message
        self._poll_interval = poll_interval_seconds

        # Caller-tracking
        self.launch_calls: list[tuple[int, dict[str, Any] | None]] = []
        self.get_job_calls: list[int] = []
        self.wait_for_job_calls: list[int] = []
        self.cancel_calls: list[int] = []
        self.close_called: bool = False

    @property
    def mode(self) -> str:
        """The current response mode."""
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        """Change the response mode at runtime.

        Useful for tests that need to switch between success/failure
        mid-scenario (e.g. ``fake_awx.mode = "failure"``).
        """
        _validate_mode(value)
        self._mode = value

    # ── Public API (mirrors AWXApiClient) ───────────────────────────────

    async def launch_job_template(
        self,
        template_id: int,
        extra_vars: dict[str, Any] | None = None,
    ) -> AWXJobSummary:
        """Launch an AWX job template (fake).

        Records the call in ``launch_calls`` and returns the configured
        ``launch_summary`` as an :class:`AWXJobSummary`.
        """
        self.launch_calls.append((template_id, extra_vars))

        if self._mode == MODE_FAILURE:
            raise AWXJobError(self._failure_message, job_id=-1)

        if self._mode == MODE_MALFORMED:
            # Simulate a response missing the ``id`` field — the real
            # client would raise AWXJobError for this.
            raise AWXJobError(
                f"AWX launch response missing job ID for template {template_id}",
                job_id=-1,
            )

        return AWXJobSummary(**self._launch_summary)

    async def get_job(self, job_id: int) -> AWXJobSummary:
        """Get the current status of an AWX job (fake).

        Records the call in ``get_job_calls`` and returns a summary
        with the configured status for this job ID.
        """
        self.get_job_calls.append(job_id)

        if self._mode == MODE_FAILURE:
            raise AWXHTTPError(
                f"Simulated HTTP error for job {job_id}",
                status_code=500,
            )

        if self._mode == MODE_MALFORMED:
            raise AWXHTTPError(
                f"Simulated malformed response for job {job_id}",
                status_code=500,
            )

        status = self._get_job_statuses.get(job_id, "running")
        return AWXJobSummary(job_id=job_id, status=status)

    async def wait_for_job(
        self,
        job_id: int,
        max_seconds: int | None = None,
    ) -> AWXJobResult:
        """Poll an AWX job until it completes (fake, returns immediately).

        Records the call in ``wait_for_job_calls`` and returns the
        configured ``job_result`` as an :class:`AWXJobResult`.

        In ``"failure"`` mode raises :class:`AWXJobError` (simulating
        a failed/canceled/error job).  In ``"malformed"`` mode raises
        :class:`AWXJobError` to simulate an invalid response.
        """
        self.wait_for_job_calls.append(job_id)

        if self._mode == MODE_FAILURE:
            raise AWXJobError(
                f"AWX job {job_id} finished with status 'failed'",
                job_id=job_id,
            )

        if self._mode == MODE_MALFORMED:
            raise AWXJobError(
                f"AWX job {job_id} returned malformed artifacts",
                job_id=job_id,
            )

        return AWXJobResult(**self._job_result)

    async def cancel_job(self, job_id: int) -> AWXJobResult:
        """Cancel a running AWX job (fake).

        Records the call in ``cancel_calls`` and returns the configured
        ``cancel_result`` as an :class:`AWXJobResult`.
        """
        self.cancel_calls.append(job_id)

        if self._mode == MODE_FAILURE:
            raise AWXHTTPError(
                f"Simulated cancel failure for job {job_id}",
                status_code=500,
            )

        if self._mode == MODE_MALFORMED:
            raise AWXHTTPError(
                f"Simulated malformed cancel response for job {job_id}",
                status_code=500,
            )

        return AWXJobResult(**self._cancel_result)

    async def close(self) -> None:
        """Close the fake client (no-op, records the call)."""
        self.close_called = True

    async def __aenter__(self) -> FakeAWXClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


def _validate_mode(mode: str) -> None:
    """Validate the mode is one of the expected constants."""
    if mode not in (MODE_SUCCESS, MODE_FAILURE, MODE_MALFORMED):
        raise ValueError(
            f"Invalid mode: {mode!r}. "
            f"Expected one of: {MODE_SUCCESS!r}, {MODE_FAILURE!r}, {MODE_MALFORMED!r}"
        )
