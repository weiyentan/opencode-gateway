"""Behavioral-identity tests for the AFK Run lifecycle service (issue #685).

Verifies that the thin service seam
:func:`afk_outcomes.service.lifecycle.get_run_status` is behaviorally
identical to the pure-domain policy
:func:`afk_outcomes.run_status.resolve_afk_run_status` for every input
shape the policy accepts — and that the package re-exports the service
function.  Backward compatibility (the policy import itself still works)
is covered by ``tests/test_afk_run_status_aggregation.py``.
"""

from __future__ import annotations

import pytest

from afk_outcomes.models import ExecutionBinding, ExecutionOutcome
from afk_outcomes.run_status import resolve_afk_run_status
from afk_outcomes.service.lifecycle import get_run_status

PENDING = "pending"
RUNNING = ExecutionOutcome.RUNNING.value
COMPLETED = ExecutionOutcome.COMPLETED.value
FAILED = ExecutionOutcome.FAILED.value
CANCELLED = ExecutionOutcome.CANCELLED.value


def _binding(outcome: ExecutionOutcome | str) -> ExecutionBinding:
    """Build a minimal binding-like object carrying ``outcome``."""
    return ExecutionBinding(
        binding_id="01JBINDING0000000000000001",
        awx_job={"job_id": "123", "job_template_id": 1},  # type: ignore[arg-type]
        outcome=ExecutionOutcome(outcome) if isinstance(outcome, str) else outcome,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([], PENDING),
        ([RUNNING], RUNNING),
        ([COMPLETED], COMPLETED),
        ([FAILED], FAILED),
        ([CANCELLED], CANCELLED),
        ([RUNNING, COMPLETED], RUNNING),
        ([RUNNING, FAILED, CANCELLED], RUNNING),
        ([COMPLETED, FAILED], COMPLETED),
        ([COMPLETED, CANCELLED], COMPLETED),
        ([FAILED, CANCELLED], FAILED),
        ([CANCELLED, FAILED], FAILED),
        (
            [ExecutionOutcome.RUNNING, "failed", ExecutionOutcome.COMPLETED],
            RUNNING,
        ),
    ],
)
def test_get_run_status_identity_with_policy(
    outcomes: list[ExecutionOutcome | str], expected: str
) -> None:
    """``get_run_status`` returns exactly what the policy returns."""
    assert get_run_status(outcomes) == resolve_afk_run_status(outcomes) == expected


def test_get_run_status_identity_binding_like_inputs() -> None:
    """Identity holds for binding-like objects with an ``outcome``."""
    bindings = [_binding(ExecutionOutcome.COMPLETED), _binding(FAILED)]
    assert get_run_status(bindings) == resolve_afk_run_status(bindings) == COMPLETED


def test_get_run_status_identity_unknown_value_raises() -> None:
    """Identity holds for rejection behavior: both reject unknown values."""
    with pytest.raises(ValueError):
        get_run_status(["unknown"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        resolve_afk_run_status(["unknown"])  # type: ignore[list-item]


def test_package_reexports_get_run_status() -> None:
    """``afk_outcomes.get_run_status`` re-exports the service function."""
    from afk_outcomes import get_run_status as reexported

    assert reexported is get_run_status
