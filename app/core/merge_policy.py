"""Pure-domain replay merge policy for canonical usage events (issue #684).

The policy half of the Replay-Safe Usage Accounting epic (#383): the
delta-computation rules of the canonical-event Replay Merge (ADR 0012),
extracted from :mod:`app.core.reconciliation` so they can be reasoned
about and tested without a database.  This module is **pure domain** —
it imports no ``asyncpg``, opens no transaction, and performs no I/O.
The DB/transaction orchestration (advisory locks, ``usage_events`` and
``sessions`` writes, rollup maintenance) remains in
:mod:`app.core.reconciliation`, which imports and re-exports everything
defined here for backward compatibility.

Semantics (canonical-event model, per issue #385 / ADR 0012):

- **Non-null collector values are authoritative.**  A replay carrying a
  non-null value different from the stored event value corrects the
  event (``event field = incoming``) and adjusts the session aggregate
  by the difference (``new - old``).  This is the *reconciliation*
  counterpart of the usage-record path: the canonical event is corrected
  toward the collector's latest observation, and the session aggregate
  never double-counts because it is moved by the delta, not re-applied.
- **Omitted/null collector values produce a zero delta (no erasure).**
  A replay that lacks a field can never erase a populated value: the
  effective new value stays the stored value and no UPDATE clause is
  generated for it.  Numeric zero is a valid observed value and is never
  treated as missing.
- **Session totals are clamped to zero.**  A negative delta that would
  drive a session token total below zero is clamped, so no negative
  token totals are ever written.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Field sets — the seven delta-computable fields of a canonical usage event
# ---------------------------------------------------------------------------

DELTA_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
)
"""Every canonical-event field that participates in delta computation."""

ROLLUP_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
)
"""The subset of DELTA_FIELDS with corresponding client_project_rollup columns.

``cached_tokens`` and ``reasoning_tokens`` are in DELTA_FIELDS but have
no rollup column — they map to sessions only.  The rollup stores only
additive token/cost totals (ADR 0015 decision 3).
"""

SESSION_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
"""Token fields with a ``sessions`` aggregate column.

``reasoning_tokens`` is deliberately absent: the ``sessions`` table has
no reasoning-token aggregate, so reasoning deltas are written to the
event but never adjusted onto the session.
"""

COST_FIELD: str = "estimated_cost_usd"
"""The cost field — deltas are ``Decimal`` arithmetic."""

# Canonical event field -> sessions aggregate column.  These are the real
# column names of the ``sessions`` table (see app/db/models/ingest.py).
SESSION_FIELD_MAP: dict[str, str] = {
    "input_tokens": "total_input_tokens",
    "output_tokens": "total_output_tokens",
    "cached_tokens": "total_cached_tokens",
    "cache_read_tokens": "total_cache_read_tokens",
    "cache_write_tokens": "total_cache_write_tokens",
    "estimated_cost_usd": "total_estimated_cost_usd",
}

# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class IngestOutcome(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Outcome of a replay delivery, compatible with the ingest layer.

    Values are the plain strings used by the ingest response layer
    (``accepted``/``rejected``/``conflict``) plus the replay-specific
    outcomes of this module (``duplicate``/``updated``/``quarantined``).
    """

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UPDATED = "updated"
    QUARANTINED = "quarantined"
    CONFLICT = "conflict"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# DeltaResult
# ---------------------------------------------------------------------------


@dataclass
class DeltaResult:
    """The difference between a stored canonical event and incoming values.

    Attributes:
        old_values: Per-field stored values of the canonical event.
        new_values: Per-field *effective* values after the non-erasing
            merge — the incoming value when the collector sent a non-null
            value, the stored value otherwise (null/omitted incoming never
            erases).
        deltas: Per-field deltas (``effective new - old``); always zero
            for null/omitted incoming values.
        token_adjustment: Overall token adjustment for the session — the
            sum of the per-field deltas of :data:`SESSION_TOKEN_FIELDS`
            (the token fields with a session aggregate column).
            ``reasoning_tokens`` is excluded because ``sessions`` carries
            no reasoning aggregate.
        cost_adjustment: The ``estimated_cost_usd`` delta.
    """

    old_values: dict[str, int | Decimal | None]
    new_values: dict[str, int | Decimal | None]
    deltas: dict[str, int | Decimal]
    token_adjustment: int
    cost_adjustment: Decimal


# ---------------------------------------------------------------------------
# Value coercion helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce a numeric value to ``Decimal``, passing ``None`` through."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


def compute_delta(
    old_event: Mapping[str, Any],
    new_values: Mapping[str, Any],
) -> DeltaResult:
    """Compute the per-field delta between a stored canonical event and incoming values.

    ``old_event`` is the stored ``usage_events`` row (an ``asyncpg``
    ``Record``, a ``dict``, or any mapping exposing ``.get``);
    ``new_values`` maps the :data:`DELTA_FIELDS` names to the collector's
    incoming values — a ``None`` (or absent) value means the collector
    did not observe the field and produces a zero delta.

    Non-null incoming values are authoritative: the effective new value
    becomes the incoming value and the delta is ``new - old`` (``old``
    treated as zero when the stored value is NULL).  Numeric zero is a
    valid observed value and never treated as missing.
    """
    old_values: dict[str, int | Decimal | None] = {}
    effective_new: dict[str, int | Decimal | None] = {}
    deltas: dict[str, int | Decimal] = {}
    token_adjustment = 0
    cost_adjustment = Decimal("0")

    for field_name in DELTA_FIELDS:
        old = old_event.get(field_name)
        incoming = new_values.get(field_name)
        old_values[field_name] = old

        if incoming is None:
            # Null/omitted collector value → zero delta, no erasure.
            deltas[field_name] = 0
            effective_new[field_name] = old
            continue

        if field_name == COST_FIELD:
            old_cost = _to_decimal(old) or Decimal("0")
            new_cost = _to_decimal(incoming)
            assert new_cost is not None  # incoming is non-None here
            delta = new_cost - old_cost
            deltas[field_name] = delta
            effective_new[field_name] = new_cost
            cost_adjustment = delta
        else:
            old_tokens = int(old) if old is not None else 0
            token_delta = int(incoming) - old_tokens
            deltas[field_name] = token_delta
            effective_new[field_name] = int(incoming)
            if field_name in SESSION_TOKEN_FIELDS:
                token_adjustment += token_delta

    return DeltaResult(
        old_values=old_values,
        new_values=effective_new,
        deltas=deltas,
        token_adjustment=token_adjustment,
        cost_adjustment=cost_adjustment,
    )


# ---------------------------------------------------------------------------
# Session-total validation
# ---------------------------------------------------------------------------


def validate_no_negative_totals(
    session_id: uuid.UUID | None,
    adjusted_values: dict[str, int | Decimal],
) -> bool:
    """Check and clamp adjusted session totals so no negative total is written.

    Inspects the proposed post-adjustment session totals in
    ``adjusted_values`` (keyed by ``sessions`` aggregate column names).
    Any total that would go negative is clamped to zero **in place**, so
    the caller can write the dict contents safely.  Returns ``True`` when
    the adjusted totals are safe to write after clamping.

    Returns ``False`` when ``session_id`` is ``None`` — there is no
    session to protect, and the caller should skip the aggregate write.
    """
    if session_id is None:
        return False
    for column, value in adjusted_values.items():
        if value is not None and value < 0:
            adjusted_values[column] = Decimal("0") if isinstance(value, Decimal) else 0
    return True
