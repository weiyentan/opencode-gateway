"""Observation-based pre-flight policy — skeleton with configurable thresholds.

This module provides :class:`ObservationBasedPolicy`, the default
pre-flight policy for the Gateway.  In future iterations it will
inspect runner telemetry (disk, memory, staleness) and reject jobs
when a runner exceeds its configured thresholds.

Currently the policy is a **skeleton only** — :meth:`check` always
returns ``None``.  No enforcement logic is implemented yet.
"""

from __future__ import annotations

import logging

from app.core.config import Settings

logger = logging.getLogger(__name__)


class ObservationBasedPolicy:
    """Pre-flight policy driven by runner observability thresholds.

    The policy reads its thresholds from :class:`Settings`:

    * ``disk_threshold_percent`` — max disk usage percentage (default 80 %)
    * ``memory_threshold_percent`` — max memory usage percentage (default 85 %)
    * ``staleness_seconds`` — max age of last telemetry (default 600 s)

    **Skeleton notice:** :meth:`check` currently returns ``None`` in
    every case.  Enforcement logic will be added in a follow-up issue.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise the policy with threshold values from *settings*.

        Parameters
        ----------
        settings:
            A :class:`Settings` instance.  When ``None`` a default
            :class:`Settings` object is created.
        """
        cfg = settings if settings is not None else Settings()
        self.disk_threshold_percent: float = cfg.disk_threshold_percent
        self.memory_threshold_percent: float = cfg.memory_threshold_percent
        self.staleness_seconds: int = cfg.staleness_seconds

    async def check(self, runner_id: str) -> None:  # pragma: no cover
        """Inspect *runner_id* against the configured thresholds.

        **Skeleton implementation** — always returns ``None``.
        No telemetry lookup or threshold comparison is performed yet.

        Parameters
        ----------
        runner_id:
            The identifier of the runner VM to inspect.

        Returns
        -------
        None
            The runner is accepted unconditionally in this skeleton
            version.
        """
        logger.debug(
            "ObservationBasedPolicy.check(%r) — skeleton pass (disk≤%.0f%%, "
            "mem≤%.0f%%, stale≤%ds)",
            runner_id,
            self.disk_threshold_percent,
            self.memory_threshold_percent,
            self.staleness_seconds,
        )
        return None
