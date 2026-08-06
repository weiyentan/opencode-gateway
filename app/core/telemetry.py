"""Shared timing infrastructure for the Gateway read path.

Provides request-level middleware, operation-level timing helpers, and
layered timeout context managers, all emitting structured timing log
entries.

Design rules:

* **Structured logging only.** Every event uses a stable event name as
  the log message and carries its data in structured ``extra`` fields —
  never via f-string interpolation of request data.
* **No sensitive data.** No raw SQL, session identifiers, tokens, or
  response bodies are ever logged.
* **Correlation propagation.** The request middleware generates a
  correlation ID per request and publishes it on a module-level
  :class:`contextvars.ContextVar`; operation and timeout helpers running
  anywhere within the request (including spawned tasks, which copy the
  current context) inherit it automatically.
* **Stdlib only.** Timing uses :func:`time.perf_counter`; deadline
  cancellation uses stdlib :func:`asyncio.timeout` on Python >= 3.11
  and a backport of it on 3.9/3.10; logging uses the :mod:`logging`
  module (reusing the project's ``RedactingFormatter`` configured in
  :mod:`app.core.logging`).

Usage::

    async with timed_operation("db.query.usage", "db"):
        rows = await conn.fetch(...)

    async with timeout_operation("status.compute", "compute"):
        await compute_status(...)
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Stable event names — do not rename; downstream log consumers depend on them.
EVENT_REQUEST_COMPLETED = "request.completed"
EVENT_OPERATION_COMPLETED = "operation.completed"
EVENT_OPERATION_TIMEOUT = "operation.timeout"

# Default operation timeout budget in milliseconds when
# GATEWAY_OPERATION_TIMEOUT_MS is unset or invalid.
DEFAULT_OPERATION_TIMEOUT_MS = 30_000
_OPERATION_TIMEOUT_ENV_VAR = "GATEWAY_OPERATION_TIMEOUT_MS"

# ── Correlation ID context ──────────────────────────────────────────────────

_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "telemetry_correlation_id", default=None
)


def get_correlation_id() -> str | None:
    """Return the correlation ID active in the current context, if any."""
    return _correlation_id_var.get()


def set_correlation_id(correlation_id: str | None) -> contextvars.Token[str | None]:
    """Set the correlation ID for the current context.

    Returns a :class:`contextvars.Token`; restore the previous value
    with :func:`reset_correlation_id`.
    """
    return _correlation_id_var.set(correlation_id)


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    """Restore the correlation ID to the value it had before a set.

    Pairs with :func:`set_correlation_id`::

        token = set_correlation_id("new-cid")
        try:
            ...
        finally:
            reset_correlation_id(token)
    """
    _correlation_id_var.reset(token)


def _new_correlation_id() -> str:
    """Generate a fresh per-request correlation ID (random hex string)."""
    return uuid.uuid4().hex


# ── Operation timing helper ─────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def timed_operation(
    event_name: str,
    operation_type: str,
    *,
    correlation_id: str | None = None,
) -> AsyncIterator[None]:
    """Time an async operation and emit one structured log entry on exit.

    Measures wall-clock duration with :func:`time.perf_counter` and logs
    a single ``operation.completed`` event carrying ``event_name``,
    ``operation_type``, ``duration_ms``, ``success`` and the active
    correlation ID.  When the wrapped block raises, the event is still
    emitted with ``success=False`` and the exception propagates
    unchanged.

    Args:
        event_name: Specific event name for this operation (e.g.
            ``"db.query.usage"``).
        operation_type: Coarse operation category (e.g. ``"db"``,
            ``"compute"``, ``"external"``).
        correlation_id: Explicit correlation ID; defaults to the one
            active in the current request context.
    """
    cid = correlation_id or get_correlation_id()
    start = time.perf_counter()
    success = True
    try:
        yield
    except BaseException:
        success = False
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            EVENT_OPERATION_COMPLETED,
            extra={
                "event_name": event_name,
                "operation_type": operation_type,
                "duration_ms": round(duration_ms, 3),
                "success": success,
                "correlation_id": cid,
            },
        )


# ── Layered timeout helpers ─────────────────────────────────────────────────


def get_operation_timeout_ms() -> float:
    """Return the default operation timeout budget in milliseconds.

    Reads ``GATEWAY_OPERATION_TIMEOUT_MS``; falls back to
    :data:`DEFAULT_OPERATION_TIMEOUT_MS` when the variable is unset,
    not numeric, or not a positive value.
    """
    raw = os.getenv(_OPERATION_TIMEOUT_ENV_VAR)
    if raw is None:
        return float(DEFAULT_OPERATION_TIMEOUT_MS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s value %r — using default budget",
            _OPERATION_TIMEOUT_ENV_VAR,
            raw,
        )
        return float(DEFAULT_OPERATION_TIMEOUT_MS)
    if value <= 0:
        logger.warning(
            "Non-positive %s value %r — using default budget",
            _OPERATION_TIMEOUT_ENV_VAR,
            raw,
        )
        return float(DEFAULT_OPERATION_TIMEOUT_MS)
    return value


# UP036: ruff considers this block redundant because requires-python is
# >=3.12 — but the local dev/test environment runs Python 3.9, so the
# backport branch is required here.
if sys.version_info >= (3, 11):  # noqa: UP036

    @contextlib.asynccontextmanager
    async def _cancel_after(seconds: float) -> AsyncIterator[None]:
        """Run a block with a deadline, cancelling the current task on expiry.

        Uses the stdlib :func:`asyncio.timeout` (Python >= 3.11), which
        re-checks its deadline on exit — so a block that swallows
        ``CancelledError`` still times out.  When the deadline passes,
        the current task is cancelled at its next suspension point and
        a :class:`TimeoutError` (builtin alias of
        :class:`asyncio.TimeoutError` on 3.11+) is raised.
        """
        async with asyncio.timeout(seconds):
            yield

else:

    @contextlib.asynccontextmanager
    async def _cancel_after(seconds: float) -> AsyncIterator[None]:
        """Run a block with a deadline, cancelling the current task on expiry.

        Backport of :func:`asyncio.timeout` for Python 3.9/3.10 (the
        local development environment).  When the deadline passes, the
        current task is cancelled at its next suspension point and a
        builtin :class:`TimeoutError` is raised.  Cancellations
        originating from other sources propagate unchanged.

        .. note::

            Unlike the stdlib version (used on Python >= 3.11), if the
            wrapped block *swallows* ``CancelledError`` the timeout is
            silently lost — the deadline is not re-checked on exit.
        """
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        if current is None:
            yield
            return

        expired = False

        def _on_deadline() -> None:
            nonlocal expired
            expired = True
            current.cancel()

        handle = loop.call_later(seconds, _on_deadline)
        try:
            try:
                yield
            except asyncio.CancelledError:
                if not expired:
                    raise
                raise TimeoutError from None
        finally:
            handle.cancel()


@contextlib.asynccontextmanager
async def timeout_operation(
    event_name: str,
    operation_type: str,
    *,
    budget_ms: float | None = None,
    correlation_id: str | None = None,
) -> AsyncIterator[None]:
    """Bound an operation with a timeout budget, cancelling it on expiry.

    Layers a deadline on the wrapped block (see :func:`_cancel_after`):
    when the block exceeds the budget, the current task is cancelled at
    its suspension point and a ``operation.timeout`` event is logged
    with ``event_name``, ``operation_type``, ``budget_ms``,
    ``duration_ms`` and the active correlation ID.  The
    :class:`TimeoutError` then propagates to the caller.

    Budget resolution order (layered): an explicit ``budget_ms`` wins;
    otherwise the value of ``GATEWAY_OPERATION_TIMEOUT_MS`` (see
    :func:`get_operation_timeout_ms`) is used.

    .. note::

        ``timeout_operation`` only emits an event when the budget
        expires; a wrapped block that fails with an ordinary exception
        emits nothing (the exception propagates unchanged).  For
        per-operation success/failure timing, compose with
        :func:`timed_operation`::

            async with timed_operation(event_name, operation_type):
                async with timeout_operation(event_name, operation_type):
                    ...

    Args:
        event_name: Specific event name for this operation (e.g.
            ``"db.query.usage"``).
        operation_type: Coarse operation category (e.g. ``"db"``,
            ``"compute"``, ``"external"``).
        budget_ms: Timeout budget in milliseconds.  Defaults to the
            configured ``GATEWAY_OPERATION_TIMEOUT_MS`` budget.
        correlation_id: Explicit correlation ID; defaults to the one
            active in the current request context.
    """
    cid = correlation_id or get_correlation_id()
    budget = budget_ms if budget_ms is not None else get_operation_timeout_ms()
    start = time.perf_counter()
    try:
        async with _cancel_after(budget / 1000):
            yield
    except TimeoutError:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.warning(
            EVENT_OPERATION_TIMEOUT,
            extra={
                "event_name": event_name,
                "operation_type": operation_type,
                "budget_ms": budget,
                "duration_ms": round(duration_ms, 3),
                "correlation_id": cid,
            },
        )
        raise


# ── Request middleware ──────────────────────────────────────────────────────


def _endpoint_name(request: Request) -> str:
    """Return a stable endpoint name for the request.

    Prefers the matched route's path format (e.g. ``/items/{item_id}``)
    so that requests are grouped by route rather than by raw URL; falls
    back to the raw path (query string excluded) when no route matched
    (e.g. 404s).
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str):
        return path_format
    return request.url.path


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Capture per-request timing and emit one structured log entry.

    For every request (including ones that fail with an unhandled
    exception) the middleware:

    * generates a per-request correlation ID — or reuses one supplied
      via the ``X-Correlation-ID`` request header — and exposes it via
      ``request.state.correlation_id``, the ``X-Correlation-ID``
      response header, and the module-level correlation context so
      operation helpers inherit it;
    * measures total wall-clock duration with :func:`time.perf_counter`;
    * emits exactly one ``request.completed`` event carrying
      ``endpoint``, ``duration_ms``, ``status_code`` and
      ``correlation_id``.

    Register this middleware outermost (added last) so the measured
    duration covers authentication, envelope wrapping, and routing.
    """

    #: Header used to accept and echo the request correlation ID.
    HEADER = "X-Correlation-ID"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        correlation_id = request.headers.get(self.HEADER) or _new_correlation_id()
        _correlation_id_var.set(correlation_id)
        request.state.correlation_id = correlation_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                EVENT_REQUEST_COMPLETED,
                extra={
                    "endpoint": _endpoint_name(request),
                    "duration_ms": round(duration_ms, 3),
                    "status_code": 500,
                    "correlation_id": correlation_id,
                },
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[self.HEADER] = correlation_id
        logger.info(
            EVENT_REQUEST_COMPLETED,
            extra={
                "endpoint": _endpoint_name(request),
                "duration_ms": round(duration_ms, 3),
                "status_code": response.status_code,
                "correlation_id": correlation_id,
            },
        )
        return response
