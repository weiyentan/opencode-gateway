"""Job lifecycle — centralized state transition rules for Job state machine.

This module defines the single source of truth for which Job state
transitions are allowed.  API handlers and other callers use
:func:`can_transition` to validate before performing state changes.

Two-tier completion model
-------------------------
The Gateway supports two completion paths that coexist side by side:

* **Review-gate path** (HITL): ``RUNNING → AWAITING_REVIEW → COMPLETED``.
  For human-in-the-loop workflows where an operator must approve the
  diff before the job is final.

* **Direct-completion path** (external terminal callback): ``RUNNING →
  COMPLETED``.  For automated / orchestrator-driven workflows where the
  caller (e.g. Paperclip) has its own review mechanism and calls
  ``POST /jobs/{id}/complete`` directly with ``target_status="completed"``.
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
        # Normal lifecycle — granular provisioning
        (JobStatus.PENDING, JobStatus.PROVISIONING_WORKSPACE),
        (JobStatus.PROVISIONING_WORKSPACE, JobStatus.STARTING_OPENCODE),
        (JobStatus.STARTING_OPENCODE, JobStatus.RUNNING),
        # Post-execution — review gate
        (JobStatus.RUNNING, JobStatus.AWAITING_REVIEW),
        (JobStatus.AWAITING_REVIEW, JobStatus.COMPLETED),
        (JobStatus.AWAITING_REVIEW, JobStatus.FAILED),
        # Approval flow
        (JobStatus.PENDING, JobStatus.NEEDS_APPROVAL),
        (JobStatus.NEEDS_APPROVAL, JobStatus.RUNNING),
        (JobStatus.NEEDS_APPROVAL, JobStatus.REJECTED),
        # Failure / abort during provisioning stages
        (JobStatus.PROVISIONING_WORKSPACE, JobStatus.FAILED),
        (JobStatus.STARTING_OPENCODE, JobStatus.FAILED),
        (JobStatus.PROVISIONING_WORKSPACE, JobStatus.ABORTING),
        (JobStatus.STARTING_OPENCODE, JobStatus.ABORTING),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.ABORTING),
        # Abort from pending and review
        (JobStatus.PENDING, JobStatus.ABORTING),
        (JobStatus.AWAITING_REVIEW, JobStatus.ABORTING),
        # Abort finalisation
        (JobStatus.ABORTING, JobStatus.ABORTED),
        # Direct completion paths (external terminal callback model)
        # ``PENDING -> RUNNING`` supports older DB records that predate the
        # granular provisioning stages (provisioning_workspace, starting_opencode).
        #
        # ``RUNNING -> COMPLETED`` supports the external-terminal-callback model
        # where callers (e.g. Paperclip orchestration) call POST /jobs/{id}/complete
        # directly with target_status="completed". This bypasses the human-in-the-loop
        # review gate (RUNNING -> AWAITING_REVIEW -> COMPLETED). Both paths coexist:
        # the review gate is for HITL workflows, the direct path is for automated
        # or orchestrator-driven completion.
        (JobStatus.PENDING, JobStatus.RUNNING),
        (JobStatus.RUNNING, JobStatus.COMPLETED),
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
