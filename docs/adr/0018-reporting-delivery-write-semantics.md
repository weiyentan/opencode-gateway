# ADR 0018: Reporting-delivery write semantics — immutable deliveries + forward-only current aggregates

## Status

Accepted (2026-08-15)

## Context

The reporting-ingestion surface (#479, migration 0031) persists normalized
deliveries from the producer (`fast-api-eda-gateway`, emitting
`afk.events` with `event_type: "normalized"`).  Each delivery is an
immutable fact: a `reporting_deliveries` row keyed by
`UNIQUE (provider, delivery_id)` written with
`INSERT ... ON CONFLICT (provider, delivery_id) DO NOTHING`, plus an
append-only `delivery_state_trails` entry.

Consumers need a *current* view of each reporting resource (e.g. the latest
state of a GitHub issue or a merge request) without replaying the full
delivery history.  That view must be derived from the immutable delivery
facts, and it must behave deterministically when deliveries arrive out of
order — a *late* event (a delivery whose `occurred_at` is older than an
already-observed event for the same resource) must enrich state forward but
must never regress state that a newer event already set.

Two facts were previously unrecorded and therefore underspecified:

1. The **stable identity** of a resource — the key under which a "current
   state" should be aggregated — was not defined in code or docs.
2. The **write semantics** of that aggregate (what "forward-only" means,
   and how equal-`occurred_at` deliveries are ordered) were not defined.

This ADR records those decisions so the aggregate layer is unambiguous.

## Decision

### Aggregate key

A resource is identified by the composite key

```
provider : repository_url : resource_type : resource_number
```

sourced from a delivery's `resource` object and the delivery's top-level
`provider`.  `repository_url` is normalized on both the insert and query
paths — lowercased, trailing slash stripped — so
`https://github.com/Acme/Backend/` and `https://github.com/acme/backend`
resolve to the same aggregate.  This mirrors the producer partition-key
vocabulary (`provider:repository_url:type:number`); the producer is not
changed.

`resource_number` is stored as a string (a numeric value in the payload is
coerced).  A malformed or absent `resource` object never rejects a
delivery: identity extraction returns `None` and enrichment is skipped,
leaving the immutable delivery fact intact.

### Delivery-row immutability

`reporting_deliveries` remains immutable: re-inserting the same
`(provider, delivery_id)` is a no-op (`ON CONFLICT ... DO NOTHING`), and the
aggregate is never written on the duplicate path.  Migration 0032 records
both `occurred_at` (provider time, backfilled from the trail when rows
exist, falling back to `received_at`) and `ingested_at` (gateway time,
`now()`) on every event row; `received_at`/`created_at` are not silently
reused for `ingested_at`.

### Forward-only merge rule

The aggregate for identity `A`, upon receiving event `E`, after acquiring a
per-resource advisory lock and re-reading `A`:

- `A` absent → insert an aggregate with `E`'s redacted payload,
  `last_occurred_at = E.occurred_at`, `last_delivery_id = E.delivery_id`,
  and a per-key provenance map recording `E` as the writer of every
  non-null payload key.  Only non-`None` payload keys are persisted on
  INSERT: a `None`-valued key is stored as *absent* (filled forward by the
  first real value) so the payload and the per-key provenance map stay
  symmetric — a `None` value never records a writer.
- `A` present → per-key merge driven by provenance.  The aggregate stores,
  alongside the merged payload, a per-key provenance map recording which
  event (`occurred_at`, `delivery_id`) last wrote each key.  Each non-null
  key `k` of `E.payload` is applied when:
  - `k` is absent from `A.payload`, or present with a `None` value (a
    legacy row written before the INSERT filtering) — fill-absent-enrich
    forward; or
  - `E` is newer than the *writer of `k`* — i.e.
    `E.occurred_at > writer.occurred_at`, or equal `occurred_at` with
    `E.delivery_id < writer.delivery_id`.  The writer is read from the
    per-key provenance map, falling back to `A`'s global last event for
    legacy rows written before provenance existed.

  Null/omitted incoming values never erase a populated value (ADR 0011
  non-erasure; numeric zero is a valid observation).  Comparing per key —
  never against a single global "newer than the aggregate" flag — makes
  the merge order-independent when 3+ events write disjoint keys: a
  globally-stale event can still upgrade a key it is newer than that key's
  current writer of.

Then `last_occurred_at = max(...)`, with `last_delivery_id = min(...)` on a
tie, `last_ingested_at = now()`, and `updated_at = now()` on update.  This
satisfies both halves of the contract: a late event may fill-absent-enrich
forward, but never regresses state already set by a newer event.

### Equal-`occurred_at` tie-break

The lowest `delivery_id` (compared as strings) wins.  This is stable and
deterministic, so live-then-replay and replay-then-live ingestion converge
on the same aggregate state.

### Serialisation

The aggregate read-modify-write is serialised per resource with a
transaction-scoped advisory lock
(`pg_advisory_xact_lock`, class `47_006`, hashtext-style signed-int32 key
derived from the composite key, mirroring
`_canonical_event_lock_key`), acquired inside the caller's per-delivery
transaction.  The afk "only writer" contract and existing migrations remain
untouched.

## Consequences

### Positive

- A current view of each resource is queryable via a minimal read surface
  (`get_aggregate`) without replaying history; the full reporting API is
  deferred.
- Late events never corrupt current state; replay and live ingestion are
  deterministic and convergent.
- The `reporting_*` family remains distinct from `delivery_log` /
  `engineering_events` / afk tables, preserving the "only writer" contract.

### Negative

- The aggregate is a denormalized read model: it trades storage and a
  serialised write for O(1) current-state reads.  It is derived data and
  can be rebuilt from the immutable delivery facts if ever needed.
- Per-key provenance is a second JSONB map stored alongside the payload,
  so the aggregate carries additional storage proportional to the number of
  distinct payload keys written by distinct events; it is derived data and
  can be rebuilt from the immutable delivery facts if ever needed.
- Equal-`occurred_at` ordering depends on string comparison of
  `delivery_id`, which is deterministic but not chronological.

## Alternatives Considered

**Overwrite-last-write-wins (no ordering guard).** Rejected: a late event
with an older `occurred_at` could regress state already set by a newer
event, violating the non-regression requirement.

**Full history replay per query.** Rejected: reconstructing current state
from all deliveries on every read is more expensive than a maintained
aggregate and duplicates the replay logic at read time.

**Ordering by `ingested_at` instead of `occurred_at`.** Rejected:
`ingested_at` is gateway wall-clock time and depends on delivery order; the
provider's `occurred_at` is the correct ordering signal for resource state.
