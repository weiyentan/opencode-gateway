"""Core reconciliation: canonical replay-merge delta computation and application.

The deepest module of the Replay-Safe Usage Accounting epic (#383).  It
implements the canonical-event counterpart of the legacy Replay Merge
(ADR 0011) for the ``usage_events`` table (migration 0021): a losing
replay delivery is reconciled against the stored canonical event by
computing a per-field delta and applying that delta to both the event
row and the owning session aggregate, all inside the caller's
transaction.

Semantics (canonical-event model, per issue #385):

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
- **Concurrent deliveries of the same event are serialised** with a
  transaction-scoped advisory lock (``pg_advisory_xact_lock``) keyed on
  the event id, acquired inside the caller's transaction so the lock
  spans the read-compute-write sequence and is released exactly at
  commit/rollback.

The module deliberately performs no DDL and opens no transaction of its
own — it consumes the canonical event schema inside the caller's
transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

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
# Advisory lock key — per-event serialisation namespace
# ---------------------------------------------------------------------------

REPLAY_LOCK_CLASS = 47_003
"""High 32 bits of the two-arg per-event replay lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002): the low 32 bits are derived from the
event UUID (``event_id.int & 0xFFFFFFFF``).
"""


def _replay_lock_key(event_id: uuid.UUID) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for a canonical event id.

    The low 32 bits of ``event_id.int`` are interpreted as a SIGNED int32
    so the key always binds to the ``int4`` arguments of
    ``pg_advisory_xact_lock(int, int)`` — an unsigned interpretation can
    exceed ``INT32_MAX`` and asyncpg raises ``OverflowError: value out of
    int32 range`` at bind time.  This is the same fix applied to
    :func:`_canonical_event_lock_key` in issue #395.
    """
    unsigned = event_id.int & 0xFFFFFFFF
    key = unsigned if unsigned <= 0x7FFFFFFF else unsigned - 0x100000000
    return (REPLAY_LOCK_CLASS, key)


# ---------------------------------------------------------------------------
# Advisory lock key — per-canonical-event first-delivery serialisation
# (issue #395)
# ---------------------------------------------------------------------------

CANONICAL_EVENT_LOCK_CLASS = 47_005
"""High 32 bits of the two-arg per-canonical-event insertion lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002, ``REPLAY_LOCK_CLASS`` = 47_003,
``RECONCILE_LOCK_CLASS`` = 47_004).  The low 32 bits are derived from
``hashtext(canonical_source_identity_id || source_record_id)`` so that
concurrent first-delivery attempts for the same canonical identity and
source record are serialised — the second blocks until the first commits,
then re-reads and finds the event already present.
"""


def _canonical_event_lock_key(
    canonical_source_identity_id: uuid.UUID,
    source_record_id: str,
) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for a canonical event insertion.

    Uses a deterministic hash of ``(canonical_source_identity_id || source_record_id)``
    consistent with PostgreSQL's ``hashtext()`` semantics: the concatenated
    text is MD5-hashed and the first 32 bits become the low-order key.
    The 32-bit value is interpreted as a SIGNED int32 (the same range
    ``hashtext()`` returns) so the key always binds to the ``int4``
    arguments of ``pg_advisory_xact_lock(int, int)`` — an unsigned
    interpretation can exceed ``INT32_MAX`` and asyncpg raises
    ``OverflowError: value out of int32 range`` at bind time.
    """
    import hashlib

    text = f"{canonical_source_identity_id}||{source_record_id}"
    hash_bytes = hashlib.md5(text.encode()).digest()[:4]
    key = int.from_bytes(hash_bytes, byteorder="big", signed=True)
    return (CANONICAL_EVENT_LOCK_CLASS, key)


async def acquire_canonical_event_lock(
    conn: asyncpg.Connection,
    canonical_source_identity_id: uuid.UUID,
    source_record_id: str,
) -> None:
    """Acquire a per-transaction advisory lock for canonical event insertion.

    Serialises concurrent first-delivery attempts for the same
    ``(canonical_source_identity_id, source_record_id)``.  The lock is
    transaction-scoped (``pg_advisory_xact_lock``) — released on commit
    or rollback — so the caller MUST wrap the lock acquisition and the
    subsequent SELECT+INSERT critical section in an explicit transaction.

    The second concurrent delivery blocks here until the first commits;
    after lock acquisition it re-reads ``usage_events`` and finds the
    event already present (re-read-after-commit pattern).
    """
    lock_class, lock_key = _canonical_event_lock_key(
        canonical_source_identity_id, source_record_id,
    )
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1, $2)",
        lock_class,
        lock_key,
    )


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
            delta = int(incoming) - old_tokens
            deltas[field_name] = delta
            effective_new[field_name] = int(incoming)
            if field_name in SESSION_TOKEN_FIELDS:
                token_adjustment += delta

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


# ---------------------------------------------------------------------------
# Replay merge application
# ---------------------------------------------------------------------------


async def apply_replay_merge(
    conn: asyncpg.Connection,
    event_id: uuid.UUID,
    new_values: Mapping[str, Any],
) -> IngestOutcome:
    """Apply the canonical Replay Merge within the caller's transaction.

    Serialises concurrent deliveries of the same canonical event with a
    transaction-scoped advisory lock derived from ``event_id``, reads the
    stored ``usage_events`` row, computes the delta against ``new_values``
    via :func:`compute_delta`, writes the authoritative (non-erasing)
    event values, and adjusts the owning ``sessions`` aggregate by the
    delta — clamping any total that would go negative to zero.

    No transaction is opened here: the caller owns the transaction, so
    the advisory lock and the ``FOR UPDATE`` row reads span the entire
    read-compute-write sequence and are released exactly at the caller's
    commit/rollback.  Returns:

    - :attr:`IngestOutcome.DUPLICATE` when the replay changes nothing
      (all deltas zero — no UPDATE issued);
    - :attr:`IngestOutcome.UPDATED` when the event and/or session
      aggregate were adjusted;
    - :attr:`IngestOutcome.REJECTED` when no ``usage_events`` row exists
      for ``event_id``.
    """
    if not isinstance(event_id, uuid.UUID):
        event_id = uuid.UUID(str(event_id))

    # ── 1. Serialise concurrent deliveries of the same event ──────────
    lock_class, lock_key = _replay_lock_key(event_id)
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1, $2)",
        lock_class,
        lock_key,
    )

    # ── 2. Read the canonical event under the lock ────────────────────
    current = await conn.fetchrow(
        """SELECT input_tokens, output_tokens, cached_tokens,
                  reasoning_tokens, cache_read_tokens, cache_write_tokens,
                  estimated_cost_usd, session_id
           FROM usage_events
           WHERE id = $1
           FOR UPDATE""",
        event_id,
    )
    if current is None:
        return IngestOutcome.REJECTED

    delta = compute_delta(current, new_values)
    if not any(delta.deltas.values()):
        return IngestOutcome.DUPLICATE

    # ── 3. Update the canonical event fields ──────────────────────────
    # Only fields with a non-zero delta are written, and the effective
    # new value already encodes the non-erasing merge (null/omitted
    # incoming keeps the stored value), so a null replay can never erase
    # a populated value.
    set_clauses: list[str] = []
    params: list[Any] = []
    for field_name in DELTA_FIELDS:
        if delta.deltas[field_name] == 0:
            continue
        set_clauses.append(f"{field_name} = ${len(params) + 1}")
        params.append(delta.new_values[field_name])
    params.append(event_id)
    await conn.execute(
        f"UPDATE usage_events SET {', '.join(set_clauses)} WHERE id = ${len(params)}",
        *params,
    )

    # ── 4. Adjust session aggregates by the delta, clamped to zero ────
    event_session_id = current["session_id"]
    if event_session_id is not None:
        session_row = await conn.fetchrow(
            """SELECT total_input_tokens, total_output_tokens, total_cached_tokens,
                      total_cache_read_tokens, total_cache_write_tokens,
                      total_estimated_cost_usd
               FROM sessions
               WHERE id = $1
               FOR UPDATE""",
            event_session_id,
        )
        if session_row is None:
            logger.warning(
                "Replay merge: session %s not found for event %s; aggregate not adjusted",
                event_session_id,
                event_id,
            )
        else:
            adjusted: dict[str, int | Decimal] = {}
            for field_name, column in SESSION_FIELD_MAP.items():
                field_delta = delta.deltas[field_name]
                if field_delta == 0:
                    continue
                current_total = session_row[column]
                adjusted[column] = (current_total or 0) + field_delta
            if adjusted and validate_no_negative_totals(event_session_id, adjusted):
                set_clauses = []
                params = []
                for column, value in adjusted.items():
                    set_clauses.append(f"{column} = ${len(params) + 1}")
                    params.append(value)
                params.append(event_session_id)
                await conn.execute(
                    f"UPDATE sessions SET {', '.join(set_clauses)} WHERE id = ${len(params)}",
                    *params,
                )

    return IngestOutcome.UPDATED


# ---------------------------------------------------------------------------
# Advisory lock key — per-endpoint serialisation for reconciliation
# ---------------------------------------------------------------------------

RECONCILE_LOCK_CLASS = 47_004
"""High 32 bits of the two-arg reconciliation serialisation lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002, ``REPLAY_LOCK_CLASS`` = 47_003).
The low 32 bits are derived from ``client_id.int & 0xFFFFFFFF`` to
serialise concurrent reconciliation runs per client.
"""


def _reconcile_lock_key(client_id: uuid.UUID) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for a reconciliation client.

    The low 32 bits of ``client_id.int`` are interpreted as a SIGNED int32
    so the key always binds to the ``int4`` arguments of
    ``pg_advisory_xact_lock(int, int)`` — an unsigned interpretation can
    exceed ``INT32_MAX`` and asyncpg raises ``OverflowError: value out of
    int32 range`` at bind time.  This is the same signed-int32 mapping used
    by :func:`_replay_lock_key`.
    """
    unsigned = client_id.int & 0xFFFFFFFF
    key = unsigned if unsigned <= 0x7FFFFFFF else unsigned - 0x100000000
    return (RECONCILE_LOCK_CLASS, key)


# ---------------------------------------------------------------------------
# Historical usage reconciliation — duplicate scan, canonical selection,
# session aggregate rebuild
# ---------------------------------------------------------------------------


def _select_canonical(
    group: list[asyncpg.Record],
) -> tuple[asyncpg.Record, list[asyncpg.Record]]:
    """Select the canonical row from a duplicate group.

    Canonical = earliest ``first_ingested_at``, lowest ``id`` as tiebreaker.
    Returns ``(canonical, non_canonicals)``.
    """
    # Sort by first_ingested_at ASC, then id ASC
    sorted_group = sorted(
        group, key=lambda r: (r["first_ingested_at"], r["id"])
    )
    return sorted_group[0], sorted_group[1:]


@dataclass
class ReconcilePreview:
    """Summary of a reconciliation scan — what would change.

    Attributes:
        events_to_merge: Number of non-canonical duplicate events to remove.
        aggregates_affected: Number of distinct sessions that would be rebuilt.
        token_adjustment: Net token change (sum of ``input_tokens + output_tokens``
            from non-canonical rows, negative — those tokens are removed).
        cost_adjustment: Net cost change (sum of ``estimated_cost_usd`` from
            non-canonical rows, negative — that cost is removed).
    """

    events_to_merge: int = 0
    aggregates_affected: int = 0
    token_adjustment: int = 0
    cost_adjustment: Decimal = Decimal("0")

    def to_response(self, dry_run: bool) -> dict[str, object]:
        return {
            "dry_run": dry_run,
            "events_to_merge": self.events_to_merge,
            "aggregates_affected": self.aggregates_affected,
            "token_adjustment": self.token_adjustment,
            "cost_adjustment_usd": str(self.cost_adjustment),
        }


async def scan_duplicate_groups(
    conn: asyncpg.Connection,
    *,
    client_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[list[asyncpg.Record]]:
    """Scan usage_events for duplicate (canonical_source_identity_id, source_record_id) groups.

    Returns a list of groups — each group is a list of ``usage_events``
    rows sharing the same ``(canonical_source_identity_id, source_record_id)``
    within the filter window.  Only groups with >1 row are returned.

    Groups are scoped per canonical source identity (the table's unique
    key), so an unfiltered scan never merges events across different
    clients/tenants.  ``source_record_id`` is collector-furnished and not
    globally unique; without the identity key, two clients that happen to
    share a record ID would have their events merged and one client's
    events deleted — a cross-tenant data-integrity issue.

    Filtering:
    - ``client_id``: optional — when provided, scans only that client's events.
    - ``date_from`` / ``date_to``: optional — filter on ``reported_at``.
    """
    conditions: list[str] = []
    params: list[object] = []
    idx = 1

    if client_id is not None:
        conditions.append(f"ue.client_id = ${idx}")
        params.append(client_id)
        idx += 1

    if date_from is not None:
        conditions.append(f"ue.reported_at >= ${idx}")
        params.append(date_from)
        idx += 1

    if date_to is not None:
        # Include the full day — date_to is a date, so add one day
        conditions.append(f"ue.reported_at < ${idx}")
        params.append(date_to)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    rows = await conn.fetch(
        f"""
        SELECT ue.*
        FROM usage_events ue
        JOIN (
            SELECT ue2.canonical_source_identity_id, ue2.source_record_id
            FROM usage_events ue2
            WHERE {where_clause.replace('ue.', 'ue2.')}
            GROUP BY ue2.canonical_source_identity_id, ue2.source_record_id
            HAVING COUNT(*) > 1
        ) dup ON ue.canonical_source_identity_id = dup.canonical_source_identity_id
              AND ue.source_record_id = dup.source_record_id
        WHERE {where_clause}
        ORDER BY ue.canonical_source_identity_id, ue.source_record_id,
                 ue.first_ingested_at, ue.id
        """,
        *params,
    )

    # Group rows by (canonical_source_identity_id, source_record_id) —
    # scoped per identity so a client_id-less scan NEVER merges events
    # across tenants.
    groups: dict[tuple[uuid.UUID, str], list[asyncpg.Record]] = {}
    for row in rows:
        groups.setdefault(
            (row["canonical_source_identity_id"], row["source_record_id"]), []
        ).append(row)

    return list(groups.values())


def compute_reconcile_preview(
    groups: list[list[asyncpg.Record]],
) -> ReconcilePreview:
    """Compute a dry-run preview from duplicate groups.

    For each group, selects the canonical row (earliest ``first_ingested_at``,
    lowest ``id`` as tiebreaker), then sums token and cost adjustments from
    the non-canonical rows.
    """
    preview = ReconcilePreview()
    affected_sessions: set[uuid.UUID] = set()

    for group in groups:
        canonical, non_canonicals = _select_canonical(group)
        if not non_canonicals:
            continue

        for nc in non_canonicals:
            preview.events_to_merge += 1
            # Token adjustment: input + output tokens from non-canonical
            preview.token_adjustment -= int(nc["input_tokens"] or 0)
            preview.token_adjustment -= int(nc["output_tokens"] or 0)
            # Cost adjustment
            nc_cost = nc["estimated_cost_usd"]
            if nc_cost is not None:
                if isinstance(nc_cost, Decimal):
                    preview.cost_adjustment -= nc_cost
                else:
                    preview.cost_adjustment -= Decimal(str(nc_cost))

            if nc["session_id"] is not None:
                affected_sessions.add(nc["session_id"])

    preview.aggregates_affected = len(affected_sessions)
    return preview


async def perform_reconciliation(
    conn: asyncpg.Connection,
    groups: list[list[asyncpg.Record]],
) -> ReconcilePreview:
    """Execute reconciliation and rebuild affected session aggregates.

    Assumes the caller has already acquired the advisory lock and opened
    a transaction.  For each duplicate group:
    1. Selects the canonical row.
    2. NULLs out ``usage_event_id`` on referencing ``usage_ingest_attempts``.
    3. Deletes non-canonical ``usage_events`` rows.
    4. Rebuilds the session aggregate from remaining canonical events.

    Returns a :class:`ReconcilePreview` with the actual counts.
    """
    preview = ReconcilePreview()
    affected_sessions: set[uuid.UUID] = set()
    non_canonical_ids: list[uuid.UUID] = []

    for group in groups:
        canonical, non_canonicals = _select_canonical(group)
        if not non_canonicals:
            continue

        for nc in non_canonicals:
            nc_id: uuid.UUID = nc["id"]
            non_canonical_ids.append(nc_id)
            preview.events_to_merge += 1
            preview.token_adjustment -= int(nc["input_tokens"] or 0)
            preview.token_adjustment -= int(nc["output_tokens"] or 0)
            nc_cost = nc["estimated_cost_usd"]
            if nc_cost is not None:
                if isinstance(nc_cost, Decimal):
                    preview.cost_adjustment -= nc_cost
                else:
                    preview.cost_adjustment -= Decimal(str(nc_cost))
            if nc["session_id"] is not None:
                affected_sessions.add(nc["session_id"])

    preview.aggregates_affected = len(affected_sessions)

    if not non_canonical_ids:
        return preview

    # ── 1. NULL out usage_event_id on referencing ingest attempts ─────
    await conn.execute(
        """
        UPDATE usage_ingest_attempts
        SET usage_event_id = NULL
        WHERE usage_event_id = ANY($1)
        """,
        non_canonical_ids,
    )

    # ── 2. Delete non-canonical usage_events ──────────────────────────
    await conn.execute(
        "DELETE FROM usage_events WHERE id = ANY($1)",
        non_canonical_ids,
    )

    # ── 3. Rebuild session aggregates from remaining canonical events ─
    if affected_sessions:
        session_list = list(affected_sessions)
        await _rebuild_session_aggregates(conn, session_list)

    return preview


async def _rebuild_session_aggregates(
    conn: asyncpg.Connection,
    session_ids: list[uuid.UUID],
) -> None:
    """Rebuild session aggregate totals from remaining canonical usage_events.

    Recomputes ``total_input_tokens``, ``total_output_tokens``,
    ``total_cached_tokens``, ``total_cache_read_tokens``,
    ``total_cache_write_tokens``, and ``total_estimated_cost_usd`` for
    each affected session directly from the canonical event rows that
    remain in ``usage_events``.
    """
    if not session_ids:
        return

    # Recompute from canonical events
    rows = await conn.fetch(
        """
        SELECT
            session_id,
            COALESCE(SUM(input_tokens), 0)::int AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0)::int AS total_output_tokens,
            COALESCE(SUM(cached_tokens), 0)::int AS total_cached_tokens,
            COALESCE(SUM(cache_read_tokens), 0)::int AS total_cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0)::int AS total_cache_write_tokens,
            SUM(estimated_cost_usd) AS total_estimated_cost_usd
        FROM usage_events
        WHERE session_id = ANY($1)
        GROUP BY session_id
        """,
        session_ids,
    )

    for row in rows:
        sid: uuid.UUID = row["session_id"]
        await conn.execute(
            """
            UPDATE sessions
            SET total_input_tokens = $2,
                total_output_tokens = $3,
                total_cached_tokens = $4,
                total_cache_read_tokens = $5,
                total_cache_write_tokens = $6,
                total_estimated_cost_usd = $7
            WHERE id = $1
            """,
            sid,
            row["total_input_tokens"],
            row["total_output_tokens"],
            row["total_cached_tokens"],
            row["total_cache_read_tokens"],
            row["total_cache_write_tokens"],
            row["total_estimated_cost_usd"],
        )
