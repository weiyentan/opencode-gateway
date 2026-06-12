"""Job lifecycle — centralized state transition rules for Job state machine.

This module defines the single source of truth for which Job state
transitions are allowed.  API handlers and other callers use
:func:`can_transition` to validate before performing state changes.
"""

from __future__ import annotations

from app.core.models.job import JobStatus

# ---------------------------------------------------------------------------
# Centralised transition table
# ---------------------------------------------------------------------------
# Every tuple ``(from_status, to_status)`` represents an allowed transition.
# Any transition *not* listed here is rejected.

VALID_TRANSITIONS: frozenset[tuple[JobStatus, JobStatus]] = frozenset(
    {
        # Normal lifecycle
        (JobStatus.PENDING, JobStatus.RUNNING),
        (JobStatus.PENDING, JobStatus.NEEDS_APPROVAL),
        (JobStatus.PENDING, JobStatus.ABORTING),
        # Running phase
        (JobStatus.RUNNING, JobStatus.COMPLETED),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.ABORTING),
        # Approval flow
        (JobStatus.NEEDS_APPROVAL, JobStatus.RUNNING),
        (JobStatus.NEEDS_APPROVAL, JobStatus.REJECTED),
        # Abort flow
        (JobStatus.ABORTING, JobStatus.ABORTED),
    },
)


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    """Return ``True`` if transitioning from *current* to *target* is allowed.

    This is the **single entry-point** for transition validation.  Every
    state-changing code path in the application should consult this function
    before issuing a status-update query.

    Self-transitions (e.g. ``pending → pending``) are always rejected.
    """
    return (current, target) in VALID_TRANSITIONS
