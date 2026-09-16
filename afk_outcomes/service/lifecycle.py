"""AFK Run lifecycle service (issue #685).

A thin service seam for AFK Run lifecycle status resolution.  It separates
the policy domain (the pure, deterministic projection in
:mod:`afk_outcomes.run_status`) from persistence orchestration: callers
that need a run's lifecycle status go through this module instead of
importing the policy directly, so future orchestration (batching,
persistence-aware lookups) can be added behind a stable interface without
touching the policy or every call site.

This module is a pure delegation — it adds no policy of its own, consults
no :class:`~afk_outcomes.models.EngineeringOutcome`, touches no database,
and imports nothing from the application package (``app``).  Behavioral
identity with :func:`afk_outcomes.run_status.resolve_afk_run_status` is
enforced by a test under ``tests/``.
"""

from __future__ import annotations

from afk_outcomes.models import ExecutionOutcome
from afk_outcomes.run_status import resolve_afk_run_status

__all__ = ["get_run_status"]


def get_run_status(execution_outcomes: list[ExecutionOutcome | str]) -> str:
    """Derive AFK Run status from AWX Execution Binding outcomes.

    Thin delegation to the pure-domain policy
    :func:`afk_outcomes.run_status.resolve_afk_run_status`; see that
    function for the full contract (accepted input shapes, the five-status
    vocabulary, determinism, and unknown-value rejection).
    """
    return resolve_afk_run_status(execution_outcomes)
