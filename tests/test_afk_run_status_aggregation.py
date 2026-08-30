"""Exhaustive table-driven tests for AFK Run status aggregation (issue #605).

Verifies the deterministic policy in :func:`afk_outcomes.run_status.resolve_afk_run_status`:

* No bindings → pending
* Any running → running (dominates regardless of other outcomes)
* When all terminal, any completed → completed
* When all terminal with no completion, any failed → failed
* When all terminal and all cancelled → cancelled
* Exhaustive mixed combinations of running / completed / failed / cancelled
* Policy does not consult PR/MR or EngineeringOutcome (signature + behavior)
* Deterministic, order-independent, handles enum/str/binding inputs
"""

from __future__ import annotations

import inspect

import pytest

from afk_outcomes.models import (
    ExecutionBinding,
    ExecutionOutcome,
    PROVISIONAL_RUN_STATUS,
)
from afk_outcomes.run_status import resolve_afk_run_status

# Shorthand aliases for readability in tables.
PENDING = PROVISIONAL_RUN_STATUS
RUNNING = ExecutionOutcome.RUNNING.value
COMPLETED = ExecutionOutcome.COMPLETED.value
FAILED = ExecutionOutcome.FAILED.value
CANCELLED = ExecutionOutcome.CANCELLED.value


# ── Core exhaustive table ───────────────────────────────────────────────────
# Each entry is (outcomes, expected_status).  ``outcomes`` may be enum members
# or plain strings — the policy normalises both.  The table is intentionally
# exhaustive across the precedence tiers described in CONTEXT.md / the contract.

_TABLE: list[tuple[list[ExecutionOutcome | str], str]] = [
    # No bindings → pending.
    ([], PENDING),
    # Single each outcome.
    ([ExecutionOutcome.RUNNING], RUNNING),
    ([ExecutionOutcome.COMPLETED], COMPLETED),
    ([ExecutionOutcome.FAILED], FAILED),
    ([ExecutionOutcome.CANCELLED], CANCELLED),
    (["running"], RUNNING),
    (["completed"], COMPLETED),
    (["failed"], FAILED),
    (["cancelled"], CANCELLED),
    # Any running dominates — regardless of other outcomes.
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.COMPLETED], RUNNING),
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.FAILED], RUNNING),
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.CANCELLED], RUNNING),
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED], RUNNING),
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.COMPLETED, ExecutionOutcome.CANCELLED], RUNNING),
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], RUNNING),
    (
        [
            ExecutionOutcome.RUNNING,
            ExecutionOutcome.COMPLETED,
            ExecutionOutcome.FAILED,
            ExecutionOutcome.CANCELLED,
        ],
        RUNNING,
    ),
    # String variants of running dominance.
    (["running", "completed"], RUNNING),
    (["running", "failed", "cancelled"], RUNNING),
    # Observed example from context digest: 9164/completed, 9165/completed, 9166/running → running.
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.COMPLETED, ExecutionOutcome.RUNNING], RUNNING),
    # Reversed order must still be running (order independence spot-check).
    ([ExecutionOutcome.RUNNING, ExecutionOutcome.COMPLETED, ExecutionOutcome.COMPLETED], RUNNING),
    # All terminal — completed dominates.
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED], COMPLETED),
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.CANCELLED], COMPLETED),
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], COMPLETED),
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.COMPLETED], COMPLETED),
    ([ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.FAILED], COMPLETED),
    (["completed", "failed"], COMPLETED),
    (["completed", "cancelled"], COMPLETED),
    (["completed", "completed", "cancelled"], COMPLETED),
    # All terminal, no completed — failed dominates.
    ([ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], FAILED),
    ([ExecutionOutcome.FAILED, ExecutionOutcome.FAILED], FAILED),
    ([ExecutionOutcome.FAILED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], FAILED),
    (["failed", "cancelled"], FAILED),
    (["failed", "cancelled", "cancelled"], FAILED),
    # All terminal, all cancelled → cancelled.
    ([ExecutionOutcome.CANCELLED], CANCELLED),
    ([ExecutionOutcome.CANCELLED, ExecutionOutcome.CANCELLED], CANCELLED),
    (
        [ExecutionOutcome.CANCELLED, ExecutionOutcome.CANCELLED, ExecutionOutcome.CANCELLED],
        CANCELLED,
    ),
    (["cancelled", "cancelled"], CANCELLED),
    # Mixed enum + string inputs (normalisation check).
    ([ExecutionOutcome.RUNNING, "completed"], RUNNING),
    ([ExecutionOutcome.COMPLETED, "failed"], COMPLETED),
    ([ExecutionOutcome.FAILED, "cancelled"], FAILED),
    (["completed", ExecutionOutcome.CANCELLED], COMPLETED),
]


@pytest.mark.parametrize("outcomes,expected", _TABLE, ids=[f"case-{i}" for i in range(len(_TABLE))])
def test_resolve_afk_run_status_table(outcomes: list[ExecutionOutcome | str], expected: str) -> None:
    """Exhaustive table-driven coverage of the precedence policy."""
    assert resolve_afk_run_status(outcomes) == expected


# ── Order independence ───────────────────────────────────────────────────────


def test_order_independence_all_terminal() -> None:
    """Permutations of the same multiset must yield the same result."""
    cases: list[tuple[list[ExecutionOutcome], str]] = [
        ([ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], COMPLETED),
        ([ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED], FAILED),
        ([ExecutionOutcome.CANCELLED, ExecutionOutcome.CANCELLED], CANCELLED),
    ]
    for outcomes, expected in cases:
        # Original order.
        assert resolve_afk_run_status(outcomes) == expected
        # Reversed order.
        assert resolve_afk_run_status(list(reversed(outcomes))) == expected
        # Sorted order.
        assert resolve_afk_run_status(sorted(outcomes, key=lambda o: o.value)) == expected


def test_order_independence_running_dominance() -> None:
    """Running dominance holds regardless of where the running entry appears."""
    base = [ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED]
    for position in range(len(base) + 1):
        outcomes = base[:position] + [ExecutionOutcome.RUNNING] + base[position:]
        assert resolve_afk_run_status(outcomes) == RUNNING


# ── Binding-object input ─────────────────────────────────────────────────────


def _binding(outcome: ExecutionOutcome | str) -> ExecutionBinding:
    """Create a minimal ExecutionBinding carrying ``outcome``."""
    return ExecutionBinding(
        binding_id="01JBINDING0000000000000001",
        awx_job={"job_id": "123", "job_template_id": 1},  # type: ignore[arg-type]
        outcome=ExecutionOutcome(outcome) if isinstance(outcome, str) else outcome,  # type: ignore[arg-type]
    )


def test_accepts_binding_objects() -> None:
    """The policy accepts binding-like objects via their ``outcome`` attribute."""
    bindings = [
        _binding(ExecutionOutcome.COMPLETED),
        _binding(ExecutionOutcome.RUNNING),
        _binding(ExecutionOutcome.FAILED),
    ]
    assert resolve_afk_run_status(bindings) == RUNNING  # running dominates

    terminal_bindings = [
        _binding(ExecutionOutcome.COMPLETED),
        _binding(ExecutionOutcome.FAILED),
    ]
    assert resolve_afk_run_status(terminal_bindings) == COMPLETED

    failed_bindings = [_binding(ExecutionOutcome.FAILED), _binding(ExecutionOutcome.CANCELLED)]
    assert resolve_afk_run_status(failed_bindings) == FAILED

    cancelled_bindings = [_binding(ExecutionOutcome.CANCELLED), _binding(ExecutionOutcome.CANCELLED)]
    assert resolve_afk_run_status(cancelled_bindings) == CANCELLED

    assert resolve_afk_run_status([]) == PENDING


def test_binding_objects_mixed_string_and_enum() -> None:
    """Binding objects may carry string or enum outcomes — both normalise."""
    bindings = [_binding("completed"), _binding(ExecutionOutcome.FAILED)]
    assert resolve_afk_run_status(bindings) == COMPLETED


# ── Determinism ──────────────────────────────────────────────────────────────


def test_deterministic_same_input_same_output() -> None:
    """Repeated calls with the same input always yield the same result."""
    outcomes = [ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED]
    first = resolve_afk_run_status(outcomes)
    for _ in range(5):
        assert resolve_afk_run_status(outcomes) == first
        assert resolve_afk_run_status(list(reversed(outcomes))) == first


# ── No PR/MR or EngineeringOutcome consultation ──────────────────────────────


def test_policy_signature_does_not_accept_engineering_outcome_or_resource() -> None:
    """The policy function's signature must not require PR/MR or outcome args."""
    sig = inspect.signature(resolve_afk_run_status)
    params = list(sig.parameters.keys())
    # The function takes a single positional argument — the outcomes sequence.
    assert params == ["outcomes"], f"unexpected signature: {params}"
    # The implementation must not import or depend on EngineeringOutcome /
    # PR/MR modules; its only dependencies are ExecutionOutcome and the
    # provisional pending constant.  Checking imports rather than docstring
    # mentions (the docstring legitimately documents the non-consultation).
    source = inspect.getsource(resolve_afk_run_status)
    # Strip the docstring before checking for forbidden implementation references.
    # The docstring is the first triple-quoted block after the def line.
    body = source.split('"""')[-1] if '"""' in source else source
    for forbidden in ("change_request", "repository_url"):
        assert forbidden not in body, f"policy must not consult {forbidden!r}"


def test_completed_without_pr_still_resolves() -> None:
    """A completed outcome without any PR/MR context still maps to completed."""
    # Pure outcome list — no resource, no engineering state.
    assert resolve_afk_run_status([ExecutionOutcome.COMPLETED]) == COMPLETED
    assert resolve_afk_run_status([ExecutionOutcome.COMPLETED, ExecutionOutcome.CANCELLED]) == COMPLETED


# ── String / enum interchangeability ─────────────────────────────────────────


def test_string_and_enum_interchangeable() -> None:
    """String values and enum members are interchangeable inputs."""
    assert resolve_afk_run_status([ExecutionOutcome.RUNNING]) == resolve_afk_run_status(["running"])
    assert resolve_afk_run_status([ExecutionOutcome.COMPLETED]) == resolve_afk_run_status(["completed"])
    assert resolve_afk_run_status([ExecutionOutcome.FAILED]) == resolve_afk_run_status(["failed"])
    assert resolve_afk_run_status([ExecutionOutcome.CANCELLED]) == resolve_afk_run_status(["cancelled"])
    # Mixed.
    assert resolve_afk_run_status([ExecutionOutcome.RUNNING, "failed"]) == RUNNING
    assert resolve_afk_run_status(["completed", ExecutionOutcome.CANCELLED]) == COMPLETED


# ── Validation ───────────────────────────────────────────────────────────────


def test_unknown_outcome_raises() -> None:
    """An unknown outcome string is rejected."""
    with pytest.raises(ValueError, match="unknown execution outcome"):
        resolve_afk_run_status(["unknown"])  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unknown execution outcome"):
        resolve_afk_run_status([ExecutionOutcome.RUNNING, "bogus"])  # type: ignore[arg-type]


# ── Exhaustive pairwise combos (generated) ───────────────────────────────────


def test_exhaustive_pairwise_combos() -> None:
    """Generate all 4*4=16 ordered pairs plus singletons and verify precedence."""
    outcomes = [ExecutionOutcome.RUNNING, ExecutionOutcome.COMPLETED, ExecutionOutcome.FAILED, ExecutionOutcome.CANCELLED]

    def expected_for_pair(a: ExecutionOutcome, b: ExecutionOutcome) -> str:
        s = {a.value, b.value}
        if RUNNING in s:
            return RUNNING
        if COMPLETED in s:
            return COMPLETED
        if FAILED in s:
            return FAILED
        return CANCELLED

    for a in outcomes:
        # Singleton already covered but re-check through helper.
        assert resolve_afk_run_status([a]) == a.value
        for b in outcomes:
            assert resolve_afk_run_status([a, b]) == expected_for_pair(a, b)
