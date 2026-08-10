# ADR 0011: Replay Merge semantics — fill absent enrichment fields without erasing populated values

## Status

Accepted

> **Scope note:** This ADR governs the legacy `opencode_usage_records` path,
> which is still written at ingest but is no longer the usage query source.
> The canonical event layer (`usage_events`, migration 0021) applies a
> different reconciliation rule — delta-based correction where non-null
> collector values are authoritative — documented in
> [ADR 0012](0012-canonical-event-replay-merge.md). The non-erasing principle
> (populated values are never erased; numeric zero is a valid observation)
> carries over to the canonical path.

## Context

Usage Record ingestion is replay-safe by design: collector deliveries can be
redelivered (duplicate batches, retries, Kafka at-least-once semantics), so the
Gateway keeps normal ingest idempotent to prevent double-counting. An atomic
`INSERT … ON CONFLICT … DO NOTHING` on
`(client_id, source_database_id, source_record_id)` determines a single winner
under concurrent replay; the winner runs all first-time side effects (session
resolution with aggregate increments, source-database record-count bump). A
losing replay is discriminated as either an identical duplicate (idempotent
accept) or a divergent duplicate (conflict).

A replay delivery may carry additional nullable enrichment that was absent when
the record was first stored — for example, `provider`, `mode`, `finish_reason`,
`reasoning_tokens`, `cache_read_tokens`, or `cache_write_tokens` that the
collector only populated in a later redelivery. The stored Usage Record is the
authoritative copy: replay deliveries must be able to *enrich* it, but must
never *erase* values already populated by the original write or by a prior
replay.

Without a recorded decision, two failure modes were possible:

1. A naive merge that overwrites stored fields with incoming values would let a
   late, less-complete replay destroy enrichment that a prior replay had already
   filled.
2. A naive append that increments session aggregates on every replay would
   double-count base totals (`message_count`, `total_input_tokens`,
   `total_output_tokens`, `total_cached_tokens`) across duplicate deliveries.

This ADR records the Replay Merge semantics: a non-erasing fill-absent rule for
Usage Records, its extension to Session Context and Project projections, and the
exactly-once aggregate repair that backfilled cache enrichment triggers.

## Decision

Replay Merge is the rule applied when a losing replay is an identical duplicate
of the stored Usage Record: **fill only currently-NULL nullable enrichment
fields; never erase a populated value.**

### Merge gate

Replay Merge applies only after the dedup identity check passes — the incoming
replay's required/accounting values must match the stored row: `input_tokens`,
`output_tokens`, `cached_tokens`, and `estimated_cost_usd` (compared via
`_decimal_equal`). A divergent replay goes to conflict and is not merged.

### Fillable nullable enrichment fields

Nullable enrichment fields subject to Replay Merge on the Usage Record:

- Text: `provider`, `mode`, `finish_reason`
- Numeric: `reasoning_tokens`, `cache_read_tokens`, `cache_write_tokens`

`estimated_cost_usd` is **excluded** as a fill candidate because it is part of
the dedup identity comparison. If the stored cost is NULL and the incoming cost
is populated, the identity check fails and the replay goes to conflict (a
cost-bearing replay cannot silently alter accounting); if both are populated the
values must already be equal. A cost SET clause would therefore be unreachable
dead code on the merge path.

### Normalization

- Null and whitespace-only optional text values are treated as **missing**
  (fillable). Whitespace-only strings are normalised to `None` before the SET
  clause is built, so the column is simply not included in the UPDATE.
- Numeric zero is a **valid observed value** and is never treated as missing —
  a replay carrying `cache_read_tokens = 0` is a real observation, not an
  absence.

### Atomicity and non-erasure

Each SET clause reads `col = COALESCE(col, $n)`. Postgres row-level locking
serialises concurrent UPDATEs on the same row and EvalPlanQual re-evaluates the
COALESCE against the committed tuple, so a populated value is never overwritten
regardless of concurrent replays carrying differing enrichment values.

The entire read+repair sequence — `SELECT … FOR UPDATE`, the enrichment
COALESCE UPDATE, and the session aggregate repair — runs inside one explicit
transaction. Under auto-commit, the `FOR UPDATE` lock would be released at
statement end, allowing two concurrent replays to both read NULL, both compute a
delta, and both apply it (double-count). The explicit transaction holds the row
lock across all statements so exactly one replay fills the column and applies
the session-aggregate delta.

### Session Context and Project projections

The same non-erasing principle applies to the Session Context and Project
projections while preserving their snapshot semantics. Their upserts already use
`COALESCE(EXCLUDED.x, table.x)` fill-absent for every projected column, so a
replay carrying additional descriptive metadata fills absent fields without
overwriting populated ones, and the whitespace-only-as-missing normalisation is
applied to their optional text fields as well. First-seen timestamps are
preserved; last-seen timestamps are touched.

## Consequences

### Positive

- Replays enrich stored records: a later delivery carrying `provider` (or any
  other previously-NULL nullable enrichment field) fills it on the stored row.
- Populated enrichment values are never erased: a late, less-complete replay
  cannot destroy what a prior replay already filled.
- Base session aggregate totals (`total_input_tokens`, `total_output_tokens`,
  `total_cached_tokens`, `message_count`) are never incremented by a replay
  delivery — only the winning first write increments them, so duplicate delivery
  cannot double-count base accounting.
- Derived session aggregate enrichment totals (`total_cache_read_tokens`,
  `total_cache_write_tokens`) are repaired exactly once when Replay Merge
  backfills the corresponding cache token columns, and the explicit transaction
  with `SELECT … FOR UPDATE` prevents double-counting under concurrent replays.
- Session Context and Project projections follow the same non-erasing rule
  without losing their snapshot semantics.

### Negative

- A response `reason` string may report "enrichment applied" whenever the
  incoming record carries a non-NULL enrichment value, even if COALESCE
  preserved an already-populated stored value (no actual change occurred). This
  is a cosmetic wording consequence of the atomic design — the merge path cannot
  know stored state without re-introducing a read-then-write race. Status stays
  `accepted`.
- An UPDATE is issued on every idempotent replay that carries non-NULL
  enrichment values, even when all stored columns are already populated
  (previously no UPDATE on a pure duplicate). Harmless (COALESCE no-ops and the
  same row the dedup path would lock is locked) but slightly noisier on the hot
  dedup path.
- `estimated_cost_usd` can never be backfilled by a replay: a stored-NULL +
  populated-incoming cost fails the identity check and goes to conflict rather
  than being filled. This is the deliberate consequence of making cost part of
  the dedup identity.

## Alternatives Considered

**SELECT-then-UPDATE merge (read stored enrichment state, then write).**
Rejected: a TOCTOU race — a stale read of enrichment state could let a
concurrent replay's bare `col = $n` UPDATE overwrite a value filled by another
replay. Eliminated by removing the enrichment SELECT entirely and expressing
every SET clause as `col = COALESCE(col, $n)` in a single atomic UPDATE, with
the row lock held across the whole repair sequence in an explicit transaction.

**Treating numeric zero as missing (fillable/erased).** Rejected: zero is a
valid observed value for token counts (a request may genuinely use no cache
tokens); treating it as absent would let a replay corrupt a legitimate zero
observation.

**Including `estimated_cost_usd` in the merge.** Rejected as unreachable: it is
part of the dedup identity via `_decimal_equal`, so a fillable stored-NULL +
populated-incoming cost never reaches the merge path — it goes to conflict.
