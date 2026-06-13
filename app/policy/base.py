"""Pre-flight policy protocol — the pluggable interface for pre-job checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import asyncpg
from fastapi import HTTPException

logger = logging.getLogger(__name__)


class PolicyViolation(HTTPException):
    """Raised by a pre-flight policy when a runner exceeds a resource threshold.

    This is an HTTP exception with status code 503 so the Gateway can
    propagate it directly to callers.
    """

    def __init__(
        self,
        resource: str,
        current_value: float,
        threshold: float,
        runner_id: str,
        last_seen_at: str | None = None,
        message: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "resource": resource,
            "current_value": current_value,
            "threshold": threshold,
            "runner_id": runner_id,
            "message": message or _default_message(resource, threshold),
        }
        if last_seen_at is not None:
            detail["last_seen_at"] = last_seen_at
        super().__init__(status_code=503, detail=detail)


def _default_message(resource: str, threshold: float) -> str:
    """Return a human-readable message for a PolicyViolation."""
    return f"Runner exceeds {resource} threshold of {threshold:.0f}%"


@runtime_checkable
class PreflightPolicy(Protocol):
    """Protocol for pre-flight check implementations.

    A *PreflightPolicy* is called by the Gateway before it accepts a job.
    The check inspects the runner targeted by the job and returns ``None``
    when the runner is healthy enough to accept work.

    Implementations that detect an unhealthy runner should raise an
    exception (e.g. :class:`PolicyViolation`) — see
    :mod:`app.policy.observation` for the observation-based implementation.
    """

    async def check(
        self, runner_id: str, conn: asyncpg.Connection | None = None
    ) -> None:  # pragma: no cover
        """Inspect *runner_id* and return ``None`` if the runner is healthy.

        Parameters
        ----------
        runner_id:
            The identifier of the runner (VM) that will host the job's
            workspace.  The concrete implementation determines what
            operations are performed against this identifier.
        conn:
            An optional asyncpg database connection.  Concrete
            implementations may use this to query runner state.

        Returns
        -------
        None
            The runner is healthy.  The Gateway may proceed with job
            creation.
        """
        ...


# ── Alert infrastructure ────────────────────────────────────────────────


@dataclass
class Alert:
    """A structured alert emitted when a policy threshold is breached.

    Carries all contextual information needed by downstream alert
    handlers (logging, webhooks, Slack, email, etc.).  The ``level``
    defaults to ``"WARNING"`` but can be overridden for higher-severity
    conditions.
    """

    runner_id: str
    metric: str
    current_value: float
    threshold: float
    level: str = "WARNING"


@runtime_checkable
class AlertHandler(Protocol):
    """Protocol for pluggable alert handlers.

    Alert handlers receive structured :class:`Alert` instances and can
    route them to any destination: logs, webhooks, Slack, email, etc.
    The protocol requires only an async ``__call__``, making it
    compatible with simple callables, classes, and dependency-injected
    services.
    """

    async def __call__(self, alert: Alert) -> None: ...


class LoggingAlertHandler:
    """Default alert handler — logs alerts at WARNING level.

    Produces structured log entries (``policy_alert`` key=value format)
    that can be ingested by log aggregation systems.  This handler is
    always enabled unless explicitly replaced.
    """

    async def __call__(self, alert: Alert) -> None:
        logger.warning(
            "policy_alert runner_id=%s metric=%s current_value=%.1f "
            "threshold=%.0f level=%s",
            alert.runner_id,
            alert.metric,
            alert.current_value,
            alert.threshold,
            alert.level,
        )
