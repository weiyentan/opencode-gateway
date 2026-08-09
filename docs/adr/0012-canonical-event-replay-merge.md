# ADR 0012: Canonical Event Replay Merge — delta-based reconciliation of `usage_events`

## Status

Accepted

## Context

The Replay-Safe Usage Accounting epic (#383, issues #384–#395) introduced a
canonical accounting event table (`usage_events`, migration 0021) as the read
source for the usage query endpoints. Each canonical event is keyed by
`(canonical_source_identity_id, source_record_id)` — one row per logical
collector record per canonical source identity — and every delivery of a
record is audited in `usage_ingest_attempts`.

The legacy Replay Merge (ADR 0011) governs the `opencode_usage_records` table:
a losing replay that passes the dedup identity check is an identical duplicate
and only *fills* currently-NULL nullable enrichment fields; a divergent replay
goes to `conflict` and is not merged. That rule cannot be applied unchanged to
the canonical event layer for two reasons:

1. **Divergent replays must be reconcilable, not dropped.** A collector that
   redelivers a record with corrected token counts (e.g. a later observation
   with authoritative non-null values) must be able to correct the stored
   canonical event. Routing every divergence to `conflict` would leave wrong
   values frozen on the accounting source of truth.
2. **Correction must not double-count.** A naive "write the incoming value and
   re-increment the session aggregate" would over-count when the first delivery
   already incremented the aggregate. The aggregate must be *moved* by the
   difference between the corrected value and the stored value, never
   re-incremented.

Without a recorded decision, two failure modes were possible: replaying a
corrected record would either overwrite the event and double-count the session
aggregate, or be rejected as a conflict and leave the canonical event stale.

## Decision

The canonical-event Replay Merge (`app/core/reconciliation.py`) reconciles a
losing replay against the stored canonical event with a **per-field delta**:

### Non-null collector values are authoritative

A replay carrying a non-null value different from the stored event value
corrects the event toward the collector's latest observation
(`event field = incoming value`) and moves the owning `sessions` aggregate by
the per-field delta (`new − old`, with `old` treated as zero when the stored
value is NULL). The aggregate is **delta-adjusted, never re-incremented**, so
a replay of the same logical record can never double-count.

### Omitted/null collector values produce a zero delta (no erasure)

A replay that lacks a field can never erase a populated value: the effective
new value stays the stored value and no UPDATE clause is generated for that
field. Numeric zero is a valid observed value and is never treated as missing.
Text enrichment (`provider`, `mode`, `finish_reason`) is COALESCE-filled
without erasing.

### Session totals are clamped to zero

A negative delta that would drive a session token total below zero is clamped
in place, so no negative token total is ever written. `reasoning_tokens`
deltas correct the event but are not applied to the session — the `sessions`
table carries no reasoning aggregate.

### Concurrent deliveries are serialised

Concurrent deliveries of the same canonical event — live and replay — are
serialised with a transaction-scoped advisory lock (`pg_advisory_xact_lock`)
keyed on the event id (first-delivery insertion uses a lock keyed on
`hashtext(canonical_source_identity_id || source_record_id)`). The lock spans
the read-compute-write sequence inside the caller's explicit transaction. A
second delivery blocks until the first commits, then re-reads the event
(re-read-after-commit) and resolves to `duplicate` (all deltas zero, no
UPDATE) or `updated` (event and/or session aggregate adjusted).

### Outcome vocabulary and routing

- `accepted` — a genuinely-new canonical event was created.
- `duplicate` — idempotent replay; no event modification.
- `updated` — a Replay Merge corrected the event and/or adjusted the session.
- `quarantined` — the source identity has an active quarantine, or was newly
  quarantined because its records overlap an existing identity; no canonical
  event or session aggregate change is made.
- `conflict` — the canonical event is owned by a different, unresolved
  identity (cross-identity conflict); no merge is attempted.
- `rejected` — validation failure or internal error.

All outcomes are 2xx at batch level so the Usage Record Consumer commits Kafka
offsets; only invalid payloads and 4xx/5xx responses route to the DLQ.

The canonical row of a duplicate group (historical reconciliation) is selected
deterministically as the earliest `first_ingested_at`, with the lowest `id` as
tiebreaker.

## Consequences

### Positive

- Replays can correct canonical events toward the collector's latest
  non-null observation, so wrong values on the accounting source of truth can
  be fixed without manual intervention.
- Correction never double-counts: the session aggregate moves by the delta
  (`new − old`), never by a re-increment.
- Omitted/null replay fields never erase populated values — the non-erasing
  principle of ADR 0011 is preserved.
- No negative session token totals can be written.
- Concurrent live and replay deliveries of the same event produce one
  deterministic outcome, and lock wait time is instrumented
  (`lock.acquired` telemetry event).
- The usage query endpoints read from `usage_events`; the legacy
  `opencode_usage_records` path keeps the ADR 0011 fill-absent rule unchanged,
  so existing consumers and collectors remain compatible (issue #394).

### Negative

- `usage_ingest_attempts` grows by one row per delivery (including
  `duplicate`, `quarantined`, `conflict`, and `rejected` outcomes) — a new
  audit surface that did not exist for the legacy path.
- Reconciliation adds a read+compute+write sequence on the replay path with an
  advisory lock; first-delivery and replay deliveries now contend on the
  per-event lock, adding bounded latency under concurrent delivery of the same
  event.

## Alternatives Considered

**Rejecting every divergent replay as `conflict` (ADR 0011 semantics applied
verbatim).** Rejected: it freezes potentially-wrong values on the canonical
source of truth and contradicts the epic's goal of correcting historical
usage through replay.

**Overwrite-on-replay plus aggregate re-increment.** Rejected: double-counts
session totals whenever a corrected replay follows an accepted first delivery.

**Session aggregate rebuild from events on every replay.** Rejected: too
expensive on the hot ingest path; the delta adjustment covers the common
single-event correction, and full rebuilds are reserved for the historical
reconciliation endpoint (`POST /admin/reconcile-historical-duplicates`).
