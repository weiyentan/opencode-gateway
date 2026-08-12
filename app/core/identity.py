"""Token generation and hashing utilities for collector credentials, plus
canonical source identity resolution and quarantine management.

The canonical source identity functions back the replay-safe usage
accounting model (issue #383): they map collector source IDs to canonical
identity UUIDs, detect record overlap between a candidate identity and
existing identities, quarantine overlapping identities, and record every
resolution in ``source_identity_resolutions``.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg


def generate_collector_token() -> tuple[str, str, str]:
    """Generate a new collector bearer token.

    Returns a 3-tuple of ``(raw_token, token_hash, token_prefix)``:

    * **raw_token** — 64-char URL-safe string (``secrets.token_urlsafe(48)``).
      This is the value returned to the caller **once** and then discarded.
    * **token_hash** — SHA-256 hex digest of the raw token.  Stored in the
      ``collector_credentials.token_hash`` column for lookup.
    * **token_prefix** — First 8 characters of the raw token.  Stored for
      human identification in admin UIs.
    """
    raw = secrets.token_urlsafe(48)  # 48 bytes → 64 URL-safe chars
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:8]
    return raw, hashed, prefix


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of *raw_token*.

    Convenience for the auth middleware path — avoids needing to know
    the exact hashing algorithm at every call site.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ── Canonical source identity resolution ───────────────────────────────────


@dataclass
class OverlapEvidence:
    """Evidence that a candidate identity's records overlap an existing one.

    * **overlapping_identity_id** — the identity whose records the
      candidate replays.
    * **overlap_count** — number of source record IDs shared between the
      candidate's deliveries and the overlapping identity's deliveries.
    """

    overlapping_identity_id: uuid.UUID
    overlap_count: int


@dataclass
class QuarantineRow:
    """A row from ``source_identity_quarantine``."""

    id: uuid.UUID
    source_identity_id: uuid.UUID
    overlapping_identity_id: uuid.UUID
    overlap_count: int
    quarantined_at: datetime
    cleared_at: datetime | None
    resolution_id: uuid.UUID | None


async def resolve_canonical_identity(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
    collector_source_id: str,
) -> uuid.UUID:
    """Resolve a collector source ID to its canonical identity UUID.

    Returns the identity's own ``id`` when it is canonical, or its
    ``canonical_parent_id`` when the identity has been resolved into a
    parent.  Creates a new ``source_identities`` row when the source ID
    is unknown for the client, using an atomic ``INSERT … ON CONFLICT …
    DO NOTHING`` so concurrent ingests cannot double-create.
    """
    row = await conn.fetchrow(
        "SELECT id, canonical_parent_id FROM source_identities "
        "WHERE client_id = $1 AND collector_source_id = $2",
        client_id,
        collector_source_id,
    )
    if row is None:
        row = await conn.fetchrow(
            "INSERT INTO source_identities (client_id, collector_source_id) "
            "VALUES ($1, $2) "
            "ON CONFLICT (client_id, collector_source_id) DO NOTHING "
            "RETURNING id",
            client_id,
            collector_source_id,
        )
    if row is None:
        # Lost the race with a concurrent writer — read their committed row.
        row = await conn.fetchrow(
            "SELECT id, canonical_parent_id FROM source_identities "
            "WHERE client_id = $1 AND collector_source_id = $2",
            client_id,
            collector_source_id,
        )
    return (
        row["canonical_parent_id"]
        if row["canonical_parent_id"] is not None
        else row["id"]
    )


async def check_quarantine_overlap(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
    candidate_source_id: str,
) -> list[OverlapEvidence]:
    """Check whether a candidate source identity's records overlap existing ones.

    .. deprecated:: 0.1.0 (issue #416)
       Superseded by :func:`check_batch_overlap` — the ``POST /ingest``
       pipeline now performs one set-based overlap query per ingest batch
       instead of this per-record full-history self-join over
       ``usage_ingest_attempts``.  Retained for reference and tests only;
       do not wire this back into the ingest hot path.

    Compares the candidate's ``usage_ingest_attempts`` deliveries against
    every other identity's deliveries for the same client, returning one
    :class:`OverlapEvidence` per overlapping identity with the count of
    shared ``original_source_record_id`` values.  Identities that have
    already been resolved into a canonical parent are not overlap targets —
    they are no longer independent accounting identities.
    """
    rows = await conn.fetch(
        """SELECT e2.source_identity_id AS overlapping_identity_id,
                  COUNT(*)::int AS overlap_count
           FROM usage_ingest_attempts e1
           JOIN usage_ingest_attempts e2
             ON e2.original_source_record_id = e1.original_source_record_id
            AND e2.source_identity_id <> e1.source_identity_id
           JOIN source_identities si1 ON si1.id = e1.source_identity_id
           JOIN source_identities si2 ON si2.id = e2.source_identity_id
           WHERE si1.client_id = $1 AND si1.collector_source_id = $2
             AND si2.client_id = $1
             AND e2.source_identity_id NOT IN (
                 SELECT id FROM source_identities WHERE canonical_parent_id IS NOT NULL
             )
           GROUP BY e2.source_identity_id
           ORDER BY overlap_count DESC""",
        client_id,
        candidate_source_id,
    )
    return [
        OverlapEvidence(
            overlapping_identity_id=row["overlapping_identity_id"],
            overlap_count=row["overlap_count"],
        )
        for row in rows
    ]


async def check_batch_overlap(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
    canonical_identity_id: uuid.UUID,
    source_record_ids: list[str],
) -> list[OverlapEvidence]:
    """Check whether incoming source record IDs overlap existing canonical events.

    Performs ONE set-based query over ``usage_events`` instead of a
    per-record self-join over ``usage_ingest_attempts``.  The batch's
    ``source_record_ids`` are compared against records owned by other
    unresolved identities — when an overlap is found the entire batch
    should be quarantined before any accounting side effects are
    written.

    This is the batch-level replacement for :func:`check_quarantine_overlap`
    in the ``POST /ingest`` pipeline (issue #416).
    """
    if not source_record_ids:
        return []

    rows = await conn.fetch(
        """SELECT ue.canonical_source_identity_id AS overlapping_identity_id,
                  COUNT(*)::int AS overlap_count
           FROM usage_events ue
           JOIN source_identities si ON si.id = ue.canonical_source_identity_id
           WHERE si.client_id = $1
             AND ue.source_record_id = ANY($2::text[])
             AND ue.canonical_source_identity_id <> $3
             AND ue.canonical_source_identity_id NOT IN (
                 SELECT id FROM source_identities
                 WHERE canonical_parent_id IS NOT NULL
             )
           GROUP BY ue.canonical_source_identity_id
           ORDER BY overlap_count DESC""",
        client_id,
        source_record_ids,
        canonical_identity_id,
    )
    return [
        OverlapEvidence(
            overlapping_identity_id=row["overlapping_identity_id"],
            overlap_count=row["overlap_count"],
        )
        for row in rows
    ]


async def quarantine_identity(
    conn: asyncpg.Connection,
    source_identity_id: uuid.UUID,
    overlapping_identity_id: uuid.UUID,
    overlap_count: int,
) -> uuid.UUID:
    """Quarantine an overlapping identity.

    Creates a ``source_identity_quarantine`` row linking the candidate
    identity to the existing identity it overlaps, and returns the new
    quarantine's UUID.
    """
    row = await conn.fetchrow(
        """INSERT INTO source_identity_quarantine
           (source_identity_id, overlapping_identity_id, overlap_count, quarantined_at)
           VALUES ($1, $2, $3, now())
           RETURNING id""",
        source_identity_id,
        overlapping_identity_id,
        overlap_count,
    )
    return row["id"]


async def get_active_quarantines(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
) -> list[QuarantineRow]:
    """List unresolved (uncleared) quarantines for a client."""
    rows = await conn.fetch(
        """SELECT q.id, q.source_identity_id, q.overlapping_identity_id,
                  q.overlap_count, q.quarantined_at, q.cleared_at, q.resolution_id
           FROM source_identity_quarantine q
           JOIN source_identities si ON si.id = q.source_identity_id
           WHERE si.client_id = $1 AND q.cleared_at IS NULL
           ORDER BY q.quarantined_at""",
        client_id,
    )
    return [
        QuarantineRow(
            id=row["id"],
            source_identity_id=row["source_identity_id"],
            overlapping_identity_id=row["overlapping_identity_id"],
            overlap_count=row["overlap_count"],
            quarantined_at=row["quarantined_at"],
            cleared_at=row["cleared_at"],
            resolution_id=row["resolution_id"],
        )
        for row in rows
    ]


async def is_quarantined(
    conn: asyncpg.Connection,
    source_identity_id: uuid.UUID,
) -> bool:
    """Return whether the identity has an active (uncleared) quarantine."""
    return await conn.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM source_identity_quarantine
               WHERE source_identity_id = $1 AND cleared_at IS NULL
           )""",
        source_identity_id,
    )


async def resolve_identity(
    conn: asyncpg.Connection,
    quarantine_id: uuid.UUID,
    resolving_identity_id: uuid.UUID,
    reason: str | None,
    resolved_by: str | None,
) -> None:
    """Resolve a quarantined identity into its canonical parent.

    Records the decision in ``source_identity_resolutions``, clears the
    quarantine (linking it to the resolution row), and marks the
    quarantined identity as resolved: ``is_canonical = false``,
    ``canonical_parent_id`` set to *resolving_identity_id*, and
    ``resolved_at`` stamped.  All three statements run in one transaction
    so a resolution is all-or-nothing.
    """
    async with conn.transaction():
        resolution = await conn.fetchrow(
            """INSERT INTO source_identity_resolutions
               (quarantine_id, resolving_identity_id, resolved_by_user_id, reason,
                resolved_at)
               VALUES ($1, $2, $3, $4, now())
               RETURNING id""",
            quarantine_id,
            resolving_identity_id,
            resolved_by,
            reason,
        )
        await conn.execute(
            """UPDATE source_identity_quarantine
               SET cleared_at = now(), resolution_id = $2
               WHERE id = $1""",
            quarantine_id,
            resolution["id"],
        )
        await conn.execute(
            """UPDATE source_identities
               SET is_canonical = false, canonical_parent_id = $2, resolved_at = now()
               WHERE id = (
                   SELECT source_identity_id FROM source_identity_quarantine
                   WHERE id = $1
               )""",
            quarantine_id,
            resolving_identity_id,
        )
