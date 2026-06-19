"""Observation-based pre-flight policy — disk and memory pressure guardrails.

This module provides :class:`ObservationBasedPolicy`, the default
pre-flight policy for the Gateway.  It inspects runner telemetry
(disk and memory usage) and rejects jobs when a runner exceeds its
configured thresholds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.core.config import Settings
from app.policy.base import (
    Alert,
    AlertHandler,
    LoggingAlertHandler,
    PolicyViolation,
)

logger = logging.getLogger(__name__)

# Runner status constants — observation-derived (system) statuses
RUNNER_STATUS_HEALTHY = "HEALTHY"
RUNNER_STATUS_BLOCKED_DISK = "BLOCKED_DISK_PRESSURE"
RUNNER_STATUS_BLOCKED_MEMORY = "BLOCKED_MEMORY_PRESSURE"
RUNNER_STATUS_UNKNOWN = "UNKNOWN"

# Runner status constants — manually-set (operator) statuses
RUNNER_STATUS_OFFLINE = "offline"
RUNNER_STATUS_ONLINE = "online"
RUNNER_STATUS_MAINTENANCE = "maintenance"

# Set of statuses that are under operator control and should not be
# overwritten by the observation-based policy engine.
_MANUAL_STATUSES: frozenset[str] = frozenset({
    RUNNER_STATUS_OFFLINE,
    RUNNER_STATUS_ONLINE,
    RUNNER_STATUS_MAINTENANCE,
})


class ObservationBasedPolicy:
    """Pre-flight policy driven by runner observability thresholds.

    The policy reads its thresholds from :class:`Settings`:

    * ``disk_threshold_percent`` — max disk usage percentage (default 80 %)
    * ``memory_threshold_percent`` — max memory usage percentage (default 85 %)
    * ``staleness_seconds`` — max age of last telemetry (default 600 s)

    When :meth:`check` is called with a database connection, it queries
    the latest ``runner_observations`` for the given *runner_id* and
    raises :class:`PolicyViolation` (HTTP 503) if any threshold is
    breached.  The runner's status is also updated in the database to
    reflect the pressure condition.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        alert_handlers: list[AlertHandler] | None = None,
    ) -> None:
        """Initialise the policy with threshold values from *settings*.

        Parameters
        ----------
        settings:
            A :class:`Settings` instance.  When ``None`` a default
            :class:`Settings` object is created.
        alert_handlers:
            A list of :class:`AlertHandler` callables.  When ``None``
            the default :class:`LoggingAlertHandler` is used, ensuring
            alerts are always logged at WARNING level.
        """
        cfg = settings if settings is not None else Settings()
        self.disk_threshold_percent: float = cfg.disk_threshold_percent
        self.memory_threshold_percent: float = cfg.memory_threshold_percent
        self.staleness_seconds: int = cfg.staleness_seconds
        self._alert_handlers: list[AlertHandler] = (
            alert_handlers if alert_handlers is not None else [LoggingAlertHandler()]
        )

    async def check(
        self,
        runner_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        """Inspect *runner_id* against the configured thresholds.

        Queries the ``runners`` and ``runner_observations`` tables via
        *conn*.  When *conn* is ``None`` or no observations exist for
        the runner, a warning is logged and ``None`` is returned (the
        runner is not blocked).

        Parameters
        ----------
        runner_id:
            The text identifier of the runner VM to inspect (the
            ``runners.runner_id`` column value).
        conn:
            An ``asyncpg`` database connection.  When ``None`` the
            check is skipped.

        Returns
        -------
        None
            The runner is healthy — all resource metrics are within
            their configured thresholds.

        Raises
        ------
        PolicyViolation
            A disk or memory threshold has been breached (HTTP 503).
        """
        if conn is None:
            logger.warning(
                "ObservationBasedPolicy.check(%r) — no DB connection, "
                "skipping enforcement",
                runner_id,
            )
            return None

        # Resolve the text runner_id to the internal runner UUID.
        runner_row = await conn.fetchrow(
            "SELECT id, admin_status, health_status, status FROM runners WHERE runner_id = $1",
            runner_id,
        )
        if runner_row is None:
            logger.warning(
                "ObservationBasedPolicy.check(%r) — runner not found in DB",
                runner_id,
            )
            return None

        runner_uuid: UUID = runner_row["id"]
        runner_admin_status: str | None = runner_row.get("admin_status")
        runner_health_status: str | None = runner_row.get("health_status")
        runner_status: str = runner_row["status"]

        # ------------------------------------------------------------------
        # Admin status guard — operator-controlled statuses take priority
        # over observation-derived thresholds.
        # ------------------------------------------------------------------
        if runner_admin_status in (RUNNER_STATUS_OFFLINE, RUNNER_STATUS_MAINTENANCE):
            logger.warning(
                "policy_reject runner_id=%s reason=admin_status status=%s",
                runner_id,
                runner_admin_status,
            )
            raise PolicyViolation(
                resource="manual_status",
                current_value=1.0,
                threshold=0.0,
                runner_id=runner_id,
                message=(
                    f"Runner {runner_id} is {runner_admin_status}. "
                    f"No jobs can be dispatched to this runner."
                ),
            )
        # ------------------------------------------------------------------
        # Health status guard — when the operator has not set an admin_status
        # override, a previously-observed unhealthy condition (disk pressure,
        # memory pressure, staleness) blocks dispatch.  Runners with
        # admin_status=online fall through to fresh observation checks below.
        # ------------------------------------------------------------------
        if runner_admin_status is None and runner_health_status is not None:
            _UNHEALTHY_HEALTH_STATUSES: frozenset[str] = frozenset({
                RUNNER_STATUS_BLOCKED_DISK,
                RUNNER_STATUS_BLOCKED_MEMORY,
                RUNNER_STATUS_UNKNOWN,
            })
            if runner_health_status in _UNHEALTHY_HEALTH_STATUSES:
                logger.warning(
                    "policy_reject runner_id=%s reason=health_status status=%s",
                    runner_id,
                    runner_health_status,
                )
                raise PolicyViolation(
                    resource="health_status",
                    current_value=1.0,
                    threshold=0.0,
                    runner_id=runner_id,
                    message=(
                        f"Runner {runner_id} has unhealthy health_status: "
                        f"{runner_health_status}. "
                        f"No jobs can be dispatched to this runner."
                    ),
                )

        # Fetch the latest runner observation.
        obs_row = await conn.fetchrow(
            "SELECT disk_used_percent, memory_used_percent, observed_at "
            "FROM runner_observations "
            "WHERE runner_id = $1 "
            "ORDER BY observed_at DESC "
            "LIMIT 1",
            runner_uuid,
        )

        if obs_row is None:
            logger.warning(
                "policy_no_data runner_id=%s reason=no_observations",
                runner_id,
            )
            return None

        disk_used: float | None = obs_row["disk_used_percent"]
        memory_used: float | None = obs_row["memory_used_percent"]
        observed_at: datetime = obs_row["observed_at"]

        # --- Staleness check ---
        now = datetime.now(timezone.utc)
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds > self.staleness_seconds:
            await self._emit_alert(
                runner_id, "staleness", float(age_seconds), float(self.staleness_seconds)
            )
            logger.warning(
                "policy_reject runner_id=%s reason=staleness resource=staleness "
                "current_value=%.0f threshold=%d",
                runner_id,
                age_seconds,
                self.staleness_seconds,
            )
            await self._set_runner_status(conn, runner_uuid, RUNNER_STATUS_UNKNOWN)
            raise PolicyViolation(
                resource="staleness",
                current_value=age_seconds,
                threshold=self.staleness_seconds,
                runner_id=runner_id,
                last_seen_at=observed_at.isoformat(),
                message=(
                    f"Runner {runner_id} observation is stale. "
                    f"Last seen at {observed_at.isoformat()}. "
                    f"Current staleness threshold is {self.staleness_seconds}s."
                ),
            )

        # --- Disk pressure check ---
        if disk_used is not None and disk_used > self.disk_threshold_percent:
            await self._emit_alert(runner_id, "disk", disk_used, self.disk_threshold_percent)
            logger.warning(
                "policy_reject runner_id=%s reason=disk_pressure resource=disk "
                "current_value=%.1f threshold=%.0f",
                runner_id,
                disk_used,
                self.disk_threshold_percent,
            )
            await self._set_runner_status(conn, runner_uuid, RUNNER_STATUS_BLOCKED_DISK)
            raise PolicyViolation(
                resource="disk",
                current_value=disk_used,
                threshold=self.disk_threshold_percent,
                runner_id=runner_id,
            )

        # --- Memory pressure check ---
        if memory_used is not None and memory_used > self.memory_threshold_percent:
            await self._emit_alert(runner_id, "memory", memory_used, self.memory_threshold_percent)
            logger.warning(
                "policy_reject runner_id=%s reason=memory_pressure resource=memory "
                "current_value=%.1f threshold=%.0f",
                runner_id,
                memory_used,
                self.memory_threshold_percent,
            )
            await self._set_runner_status(conn, runner_uuid, RUNNER_STATUS_BLOCKED_MEMORY)
            raise PolicyViolation(
                resource="memory",
                current_value=memory_used,
                threshold=self.memory_threshold_percent,
                runner_id=runner_id,
            )

        logger.info(
            "policy_accept runner_id=%s reason=healthy",
            runner_id,
        )
        return None

    @staticmethod
    async def _set_runner_status(
        conn: asyncpg.Connection,
        runner_uuid: UUID,
        status: str,
    ) -> None:
        """Update the runner's health_status and legacy status in the database.

        Only updates health_status (observation-derived). The admin_status
        column is never touched by the policy engine.
        """
        await conn.execute(
            "UPDATE runners SET health_status = $1, status = $1, updated_at = $2 WHERE id = $3",
            status,
            datetime.now(timezone.utc),
            runner_uuid,
        )
        logger.info(
            "Runner %s health_status updated to %s",
            runner_uuid,
            status,
        )

    async def _emit_alert(
        self,
        runner_id: str,
        metric: str,
        current_value: float,
        threshold: float,
    ) -> None:
        """Emit a structured alert to all registered alert handlers.

        Each handler in :attr:`_alert_handlers` is called sequentially.
        Failures in a single handler are logged and do not prevent
        subsequent handlers from executing.
        """
        alert = Alert(
            runner_id=runner_id,
            metric=metric,
            current_value=current_value,
            threshold=threshold,
        )
        for handler in self._alert_handlers:
            try:
                await handler(alert)
            except Exception:
                logger.exception(
                    "Alert handler %s failed for runner=%s metric=%s",
                    handler,
                    runner_id,
                    metric,
                )
