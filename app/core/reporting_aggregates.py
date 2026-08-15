"""Forward-only current-aggregate layer over reporting deliveries (issue #480).

The deepest module of the reporting-ingestion slice: it maintains a single
*current* aggregate per stable resource identity, enriched forward-only by
``occurred_at`` ordering, over the immutable ``reporting_deliveries`` /
``delivery_state_trails`` tables built by #479.

Semantics (ADR 0018):

- **Stable identity** — an aggregate is keyed by the composite
  ``provider:repository_url:resource_type:resource_number``, sourced from a
  delivery's ``resource`` object.  ``repository_url`` is normalized
  (lowercased, trailing slash stripped) so the insert and query paths agree.
- **Forward-only merge (per-key provenance)** — a newer event's non-null
  values overwrite; a stale (late) event fills only keys absent from the
  stored payload and never regresses state already set by a newer event.
  Ordering is compared *per key*, not against a single global flag: the
  aggregate persists a per-key provenance map recording which event
  (``occurred_at``, ``delivery_id``) last wrote each key, and key ``k`` is
  overwritten only when the incoming event is newer than the writer of
  ``k`` — never merely newer than the aggregate's global last event.  This
  makes the merge order-independent when 3+ events write disjoint keys.
  Null/omitted incoming values never erase (ADR 0011 non-erasure).
  Numeric zero is a valid value, never treated as missing.
- **Equal-``occurred_at`` tie-break** — the lowest ``delivery_id`` wins
  (compared as strings), so the merge is deterministic.
- **Serialised read-modify-write** — a transaction-scoped advisory lock
  (``pg_advisory_xact_lock``, class ``47_006``, hashtext-style signed-int32
  key) serialises concurrent live/replay ingestion per resource.

The module deliberately performs no DDL and opens no transaction of its
own — it runs inside the caller's per-delivery transaction (the same one
that persists ``reporting_deliveries`` and ``delivery_state_trails``).
A malformed/absent ``resource`` never fails a delivery: the identity
extraction returns ``None`` and the caller simply skips enrichment.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.core.schemas.reporting import ReportingDeliveryIn
from app.core.secrets import redact_dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stable resource identity
# ---------------------------------------------------------------------------


def normalize_repository_url(repository_url: str) -> str:
    """Normalize a repository URL: lowercase and strip trailing slashes.

    Applied identically on both insert and query paths so a URL written as
    ``https://github.com/Acme/Backend/`` and queried as
    ``https://github.com/acme/backend`` resolve to the same aggregate.
    """
    return repository_url.strip().lower().rstrip("/")


@dataclass(frozen=True)
class ResourceIdentity:
    """Stable identity of one reporting resource.

    Mirrors the producer partition-key vocabulary
    (``provider:repository_url:type:number``).  ``repository_url`` is
    normalized (lowercase, trailing slash stripped) at construction time.
    """

    provider: str
    repository_url: str
    resource_type: str
    resource_number: str

    @property
    def composite_key(self) -> str:
        """The canonical aggregate key string for this identity."""
        return (
            f"{self.provider}:{self.repository_url}:"
            f"{self.resource_type}:{self.resource_number}"
        )


def resource_identity_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    provider: str,
) -> ResourceIdentity | None:
    """Extract a stable :class:`ResourceIdentity` from a delivery payload.

    Reads the ``resource`` object from ``payload["resource"]`` and
    normalizes ``repository_url``.  ``provider`` is the delivery's top-level
    provider (not present inside ``payload``).  Returns ``None`` — never
    raises — when the payload is malformed or the ``resource`` object is
    absent/incomplete, so the caller can skip enrichment without rejecting
    the delivery (immutable fact).
    """
    if not isinstance(payload, Mapping) or not provider:
        return None
    resource = payload.get("resource")
    if not isinstance(resource, Mapping):
        return None

    repository_url = resource.get("repository_url")
    if not isinstance(repository_url, str):
        return None
    repository_url = normalize_repository_url(repository_url)
    if not repository_url:
        return None

    resource_type = resource.get("resource_type")
    if not isinstance(resource_type, str) or not resource_type.strip():
        return None

    resource_number = resource.get("resource_number")
    if resource_number is None or isinstance(resource_number, bool):
        return None
    if isinstance(resource_number, int):
        resource_number = str(resource_number)
    if not isinstance(resource_number, str) or not resource_number.strip():
        return None

    return ResourceIdentity(
        provider=str(provider),
        repository_url=repository_url,
        resource_type=resource_type.strip(),
        resource_number=resource_number.strip(),
    )


# ---------------------------------------------------------------------------
# Advisory lock key — per-resource serialisation namespace
# ---------------------------------------------------------------------------

AGGREGATE_LOCK_CLASS = 47_006
"""High 32 bits of the two-arg per-resource aggregate lock.

Follows the ``app/db/lock.py`` convention (``PORT_LOCK_KEY`` = 47_001,
``CLEANUP_LOCK_CLASS`` = 47_002, ``REPLAY_LOCK_CLASS`` = 47_003,
``RECONCILE_LOCK_CLASS`` = 47_004, ``CANONICAL_EVENT_LOCK_CLASS`` =
47_005).  The low 32 bits are derived from a hashtext-style hash of the
composite identity key.
"""


def _aggregate_lock_key(identity: ResourceIdentity) -> tuple[int, int]:
    """Derive the two-arg advisory lock key for an aggregate identity.

    Mirrors :func:`app.core.reconciliation._canonical_event_lock_key`: the
    composite key text is MD5-hashed and the first 32 bits are interpreted
    as a SIGNED int32 (the range ``hashtext()`` returns) so the key always
    binds to the ``int4`` arguments of ``pg_advisory_xact_lock(int, int)``.
    An unsigned interpretation can exceed ``INT32_MAX`` and asyncpg raises
    ``OverflowError: value out of int32 range`` at bind time.
    """
    text = identity.composite_key
    hash_bytes = hashlib.md5(text.encode()).digest()[:4]
    key = int.from_bytes(hash_bytes, byteorder="big", signed=True)
    return (AGGREGATE_LOCK_CLASS, key)


# ---------------------------------------------------------------------------
# Forward-only merge helpers (pure)
# ---------------------------------------------------------------------------


def is_newer(
    incoming_occurred_at: datetime,
    incoming_delivery_id: str,
    stored_occurred_at: datetime,
    stored_delivery_id: str,
) -> bool:
    """Return ``True`` when the incoming event is newer than the stored one.

    Strictly-greater ``occurred_at`` wins; on equal ``occurred_at`` the
    lowest ``delivery_id`` (string comparison) wins — the deterministic
    tie-break of ADR 0018.
    """
    if incoming_occurred_at != stored_occurred_at:
        return incoming_occurred_at > stored_occurred_at
    return incoming_delivery_id < stored_delivery_id


def advance_last(
    stored_occurred_at: datetime,
    stored_delivery_id: str,
    incoming_occurred_at: datetime,
    incoming_delivery_id: str,
) -> tuple[datetime, str]:
    """Return the forward-advanced ``(last_occurred_at, last_delivery_id)``.

    The maximum ``occurred_at`` wins; on a tie the lowest ``delivery_id``
    is retained (so ``last_delivery_id`` is the tie-break among equal
    ``occurred_at``).
    """
    if incoming_occurred_at > stored_occurred_at:
        return incoming_occurred_at, incoming_delivery_id
    if incoming_occurred_at < stored_occurred_at:
        return stored_occurred_at, stored_delivery_id
    if incoming_delivery_id < stored_delivery_id:
        return incoming_occurred_at, incoming_delivery_id
    return stored_occurred_at, stored_delivery_id


def forward_merge(
    stored_payload: Mapping[str, Any],
    incoming_payload: Mapping[str, Any],
    *,
    stored_provenance: Mapping[str, tuple[datetime, str]] | None,
    incoming_occurred_at: datetime,
    incoming_delivery_id: str,
    fallback_writer: tuple[datetime, str] | None = None,
) -> tuple[dict[str, Any], dict[str, tuple[datetime, str]]]:
    """Merge two payload dicts forward-only, returning two new dicts.

    Returns ``(merged_payload, merged_provenance)`` — both new dicts; the
    inputs are never mutated.

    Per-key rule (ADR 0018 non-erasure + numeric-zero rules preserved):

    - A null/omitted incoming value is skipped and never erases a
      populated value; provenance is unchanged.
    - A key absent from the stored payload is set and its provenance
      recorded as ``(incoming_occurred_at, incoming_delivery_id)``.
    - A key present in the stored payload is overwritten only when the
      incoming event is newer than that key's *writer* — read from
      ``stored_provenance``, falling back to ``fallback_writer`` (the
      aggregate's global last event) for legacy rows written before
      per-key provenance existed.  On overwrite, the key's provenance is
      updated to the incoming event.

    Comparing per key (rather than against one global ``is_newer`` flag)
    makes the merge order-independent when 3+ events write disjoint keys:
    a globally-stale event can still upgrade a key it is newer than that
    key's current writer of.
    """
    merged = dict(stored_payload)
    provenance: dict[str, tuple[datetime, str]] = dict(stored_provenance or {})

    for key, value in incoming_payload.items():
        if value is None:
            continue
        if key not in merged:
            merged[key] = value
            provenance[key] = (incoming_occurred_at, incoming_delivery_id)
            continue
        writer = provenance.get(key)
        if writer is None:
            writer = fallback_writer
        if writer is None or is_newer(
            incoming_occurred_at, incoming_delivery_id, *writer
        ):
            merged[key] = value
            provenance[key] = (incoming_occurred_at, incoming_delivery_id)

    return merged, provenance


def _serialize_provenance(
    provenance: Mapping[str, tuple[datetime, str]],
) -> dict[str, dict[str, str]]:
    """Serialize per-key provenance to a JSONB-safe mapping.

    Each key maps to ``{"occurred_at": "<ISO8601 with tz>",
    "delivery_id": "<id>"}`` so it round-trips through the JSONB column.
    """
    return {
        key: {
            "occurred_at": occurred_at.isoformat(),
            "delivery_id": delivery_id,
        }
        for key, (occurred_at, delivery_id) in provenance.items()
    }


def _deserialize_provenance(
    raw: Any,
) -> dict[str, tuple[datetime, str]]:
    """Deserialize per-key provenance from a JSONB-decoded value.

    Returns ``{key: (occurred_at, delivery_id)}``.  Missing, non-dict, or
    malformed entries are skipped gracefully (an empty dict results), so a
    legacy aggregate row with no ``key_provenance`` entries yields no
    per-key writers and the caller falls back to the global last event.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}

    result: dict[str, tuple[datetime, str]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        occurred_at = entry.get("occurred_at")
        delivery_id = entry.get("delivery_id")
        if not isinstance(occurred_at, str) or not isinstance(delivery_id, str):
            continue
        try:
            parsed = datetime.fromisoformat(occurred_at)
        except ValueError:
            continue
        result[key] = (parsed, delivery_id)
    return result


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_AGGREGATE_SQL = """
    SELECT last_occurred_at, last_delivery_id, payload, key_provenance
    FROM reporting_resource_aggregates
    WHERE provider = $1 AND repository_url = $2
      AND resource_type = $3 AND resource_number = $4
    FOR UPDATE
"""

_INSERT_AGGREGATE_SQL = """
    INSERT INTO reporting_resource_aggregates
        (provider, repository_url, resource_type, resource_number,
         last_occurred_at, last_delivery_id, last_ingested_at, payload,
         key_provenance)
    VALUES ($1, $2, $3, $4, $5, $6, now(), $7::jsonb, $8::jsonb)
"""

_UPDATE_AGGREGATE_SQL = """
    UPDATE reporting_resource_aggregates
    SET payload = $5::jsonb,
        key_provenance = $6::jsonb,
        last_occurred_at = $7,
        last_delivery_id = $8,
        last_ingested_at = now(),
        updated_at = now()
    WHERE provider = $1 AND repository_url = $2
      AND resource_type = $3 AND resource_number = $4
"""

_GET_AGGREGATE_SQL = """
    SELECT provider, repository_url, resource_type, resource_number,
           last_occurred_at, last_delivery_id, last_ingested_at,
           payload, key_provenance, updated_at
    FROM reporting_resource_aggregates
    WHERE provider = $1 AND repository_url = $2
      AND resource_type = $3 AND resource_number = $4
"""


# ---------------------------------------------------------------------------
# Enrich (read-modify-write) + query
# ---------------------------------------------------------------------------


async def enrich_aggregate(
    conn: asyncpg.Connection,
    identity: ResourceIdentity,
    delivery: ReportingDeliveryIn,
) -> None:
    """Enrich the current aggregate for ``identity`` from ``delivery``.

    Runs inside the caller's per-delivery transaction: acquires the
    per-resource advisory lock, re-reads the aggregate (re-read-after-commit
    pattern), and applies the forward-only merge.  A first event INSERTs the
    aggregate; a later event UPDATEs it.

    The stored payload is the *redacted* delivery payload (secret-safe).
    No transaction is opened here — the caller owns it, so the advisory
    lock and the ``FOR UPDATE`` read span the entire read-modify-write
    sequence and are released exactly at the caller's commit/rollback.
    """
    incoming_payload = redact_dict(delivery.payload)

    lock_class, lock_key = _aggregate_lock_key(identity)
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1, $2)",
        lock_class,
        lock_key,
    )

    current = await conn.fetchrow(
        _SELECT_AGGREGATE_SQL,
        identity.provider,
        identity.repository_url,
        identity.resource_type,
        identity.resource_number,
    )

    if current is None:
        initial_provenance = {
            key: (delivery.occurred_at, delivery.delivery_id)
            for key, value in incoming_payload.items()
            if value is not None
        }
        await conn.execute(
            _INSERT_AGGREGATE_SQL,
            identity.provider,
            identity.repository_url,
            identity.resource_type,
            identity.resource_number,
            delivery.occurred_at,
            delivery.delivery_id,
            json.dumps(incoming_payload),
            json.dumps(_serialize_provenance(initial_provenance)),
        )
        return

    stored_occurred_at: datetime = current["last_occurred_at"]
    stored_delivery_id: str = current["last_delivery_id"]
    stored_payload: Mapping[str, Any] = current["payload"] or {}
    stored_provenance = _deserialize_provenance(current["key_provenance"])

    merged, merged_provenance = forward_merge(
        stored_payload,
        incoming_payload,
        stored_provenance=stored_provenance,
        incoming_occurred_at=delivery.occurred_at,
        incoming_delivery_id=delivery.delivery_id,
        fallback_writer=(stored_occurred_at, stored_delivery_id),
    )
    last_occurred_at, last_delivery_id = advance_last(
        stored_occurred_at,
        stored_delivery_id,
        delivery.occurred_at,
        delivery.delivery_id,
    )

    await conn.execute(
        _UPDATE_AGGREGATE_SQL,
        identity.provider,
        identity.repository_url,
        identity.resource_type,
        identity.resource_number,
        json.dumps(merged),
        json.dumps(_serialize_provenance(merged_provenance)),
        last_occurred_at,
        last_delivery_id,
    )


async def get_aggregate(
    conn: asyncpg.Connection,
    identity: ResourceIdentity,
) -> asyncpg.Record | None:
    """Return the current aggregate row for ``identity``, or ``None``.

    Minimal read surface — the full reporting API is deferred.  The caller
    must supply an already-normalized :class:`ResourceIdentity` (the same
    normalization applied on the insert path).
    """
    return await conn.fetchrow(
        _GET_AGGREGATE_SQL,
        identity.provider,
        identity.repository_url,
        identity.resource_type,
        identity.resource_number,
    )
