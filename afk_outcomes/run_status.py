"""Pure domain policy for AFK Run status aggregation (issue #605).

Projects an AFK Run's execution status deterministically from its AWX
Execution Binding outcomes.

Policy (CONTEXT.md — AFK Run, RunStatus, Execution Outcome):

* No bindings → ``pending`` (the provisional status, ``PROVISIONAL_RUN_STATUS``).
* Any ``running`` binding → ``running``, regardless of other outcomes.
* When all bindings are terminal:
  * any ``completed`` → ``completed``
  * otherwise any ``failed`` → ``failed``
  * otherwise (all ``cancelled``) → ``cancelled``

The policy is:

* **pure domain** — no DB, no provider state, no PR/MR state, no
  :class:`EngineeringOutcome` consultation.
* **deterministic and order-independent** — identical outcome multisets
  always yield the identical result regardless of input order; no clock
  dependence, no randomness, idempotent.
* **exhaustive** — every combination of ``running`` / ``completed`` /
  ``failed`` / ``cancelled`` maps to exactly one result via the precedence
  above.

This module deliberately imports nothing from ``app``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from afk_outcomes.models import ExecutionOutcome, PROVISIONAL_RUN_STATUS

# The four execution outcomes that are valid inputs.  Kept as plain strings for
# fast membership checks after normalisation.
_VALID_OUTCOMES: frozenset[str] = frozenset(
    {
        ExecutionOutcome.RUNNING.value,
        ExecutionOutcome.COMPLETED.value,
        ExecutionOutcome.FAILED.value,
        ExecutionOutcome.CANCELLED.value,
    }
)


def _coerce_outcome_value(item: Any) -> str:
    """Normalize one outcome item to its canonical string value.

    Accepts:

    * :class:`ExecutionOutcome` enum members,
    * plain strings (the enum values),
    * objects carrying an ``outcome`` attribute (e.g. :class:`ExecutionBinding`
      or any duck-typed binding-like object) whose ``outcome`` is either of the
      above.

    The indirection via ``outcome`` keeps the policy usable directly on binding
    objects without the caller having to map first.
    """
    # Duck-typed binding / wrapper: extract ``outcome`` first.  An
    # ``ExecutionOutcome`` itself has no ``outcome`` attribute, so this does
    # not collide with plain enum inputs.
    if hasattr(item, "outcome") and not isinstance(item, ExecutionOutcome):
        # ``ExecutionBinding`` and similar carry ``outcome`` as enum-or-string.
        # Re-enter normalisation on the extracted value.
        candidate = getattr(item, "outcome")
        # ``candidate`` may itself be an enum with ``.value``.
        if isinstance(candidate, ExecutionOutcome):
            return candidate.value
        if isinstance(candidate, str):
            return candidate
        if hasattr(candidate, "value"):
            return str(candidate.value)  # type: ignore[no-any-return]
        return str(candidate)

    if isinstance(item, ExecutionOutcome):
        return item.value
    if isinstance(item, str):
        return item
    if hasattr(item, "value"):
        return str(item.value)  # type: ignore[no-any-return]
    return str(item)


def resolve_afk_run_status(
    outcomes: Sequence[ExecutionOutcome | str | Any],
) -> str:
    """Project an AFK Run's status from its AWX Execution Binding outcomes.

    Parameters
    ----------
    outcomes:
        The execution outcomes of the run's bindings.  Each entry may be an
        :class:`ExecutionOutcome`, its string value, or an object with an
        ``outcome`` attribute (e.g. :class:`ExecutionBinding`).  Order is
        irrelevant.

    Returns
    -------
    str
        One of ``"pending"`` (no bindings), ``"running"``, ``"completed"``,
        ``"failed"``, ``"cancelled"``.  Terminal values coincide with
        :class:`RunStatus` members; ``"pending"`` is the provisional status
        ``PROVISIONAL_RUN_STATUS`` and is intentionally not a :class:`RunStatus`
        member.

    Notes
    -----
    * The policy does **not** consult PR/MR state or :class:`EngineeringOutcome`
      — execution status is purely a function of binding outcomes.
    * Deterministic: identical multisets (regardless of order) always yield the
      same result; no clock or random dependence.
    """
    if not outcomes:
        return PROVISIONAL_RUN_STATUS

    # Normalize to a set for order-independence and fast membership checks.
    normalized: set[str] = set()
    for item in outcomes:
        value = _coerce_outcome_value(item)
        if value not in _VALID_OUTCOMES:
            raise ValueError(f"unknown execution outcome: {value!r}")
        normalized.add(value)

    # Non-terminal dominates: any running keeps the run non-terminal.
    if ExecutionOutcome.RUNNING.value in normalized:
        return ExecutionOutcome.RUNNING.value

    # All terminal from here — success-aware precedence.
    if ExecutionOutcome.COMPLETED.value in normalized:
        return ExecutionOutcome.COMPLETED.value

    if ExecutionOutcome.FAILED.value in normalized:
        return ExecutionOutcome.FAILED.value

    # Only remaining terminal value is cancelled, and ``normalized`` is non-empty
    # so the "all cancelled" case falls through here.
    return ExecutionOutcome.CANCELLED.value
