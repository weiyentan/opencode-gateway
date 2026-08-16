"""Shared per-query and total-request timeout budget helpers.

These async-context-manager helpers wrap
:func:`app.core.telemetry.timeout_operation` with the fixed budget layering
used across the API routers:

* :func:`db_timeout` — the configured per-query database budget.
* :func:`request_timeout` — the whole-handler total request budget.

Each router imports them under its existing private aliases
(``_db_timeout`` / ``_request_timeout``) so call sites and event names are
unchanged while the definitions live in exactly one place.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from app.core.telemetry import timeout_operation


@contextlib.asynccontextmanager
async def db_timeout(
    event_name: str, db_timeout_seconds: int
) -> AsyncIterator[None]:
    """Wrap a database query with the configured per-query timeout budget."""
    async with timeout_operation(
        event_name, "db", budget_ms=db_timeout_seconds * 1000
    ):
        yield


@contextlib.asynccontextmanager
async def request_timeout(
    total_request_timeout_seconds: int,
) -> AsyncIterator[None]:
    """Wrap an endpoint handler body with the total request timeout budget."""
    async with timeout_operation(
        "request.total", "request",
        budget_ms=total_request_timeout_seconds * 1000,
    ):
        yield
