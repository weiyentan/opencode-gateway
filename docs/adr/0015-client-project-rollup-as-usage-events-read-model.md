# ADR 0015: Client Project Rollup as a `usage_events` read-model

## Status

Accepted (2026-08-10)

> **Amends ADR 0014.** The rollup decisions in
> [ADR 0014](0014-canonical-client-name-and-rollup.md) predate the canonical
> accounting layer. This ADR revises three of them: the rollup is now a
> read-model of `usage_events` (not of legacy `opencode_usage_records`), it is
> keyed on the stable project ID (not the volatile project label), and backfill
> is in scope. ADR 0014's rollup column list is also revised — the rollup
> stores only the additive token and cost totals listed below (ADR 0014 also
> listed reasoning tokens). The canonical-client-name decisions in ADR 0014 are
> unchanged.

## Context

ADR 0014 defined the client-project rollup as an additive table keyed by
`(client_id, project_label, day)`, maintained inline at ingest, with raw usage
records treated as authoritative, and left backfill out of scope. Two things
have since changed:

1. **The canonical accounting layer exists.** The Replay-Safe Usage Accounting
   epic (#383) introduced `usage_events` (migration 0021) as the authoritative
   accounting store: the usage query endpoints read from it, and divergent
   replays are reconciled by a delta-based Replay Merge (ADR 0012) that moves
   session aggregates by `new − old` instead of re-incrementing. Issue #372's
   phrasing that "raw usage records are authoritative" predates this layer and
   must be superseded — the accounting truth is the canonical event, not the
   legacy record. A rollup derived from the legacy records would inherit
   neither the dedup nor the delta semantics and would re-introduce the
   double-counting bug the canonical layer exists to prevent.
2. **Project labels are not stable identity.** The friendly-name backfill
   (#329) already rewrote project labels once. Keying the rollup on the label
   string would fragment the table every time a label changes, splitting one
   logical project across many rollup rows.

The upcoming Client Project Rollup work (#371 rollup table and maintenance,
#372 rollup integration with the aggregate read path, #373 hybrid aggregates
and the frontend view merge) implements the rollup. The implementing agents
read `CONTEXT.md` and `docs/adr/` first, so these decisions are recorded before
implementation begins.

## Decision

1. **Read-model of `usage_events`, not legacy records.**
   `client_project_rollup` is a pre-aggregated read-model of the canonical
   `usage_events` table. It is not a source of truth: it inherits the dedup and
   delta semantics of the canonical layer, and a row is only ever moved by the
   same reconciled deltas the canonical Replay Merge computes — never by a
   blind re-increment.

2. **Keyed on the stable project ID, not the volatile label.**
   The rollup stores `(client_id, project_id, day)`. The human-readable
   project label is resolved at read time from source project metadata,
   mirroring the canonical-client-name COALESCE pattern
   (`COALESCE(canonical_name, name)`). Keying on the label string would
   fragment the table when labels change — the friendly-name backfill (#329)
   already rewrote labels once.

3. **Additive columns only.**
   Each row stores only additive token totals — input, output, cache read,
   cache write — plus estimated cost. No session counts and no model counts:
   those remain distinct-count queries over raw records (per issue #372),
   because distinct counts do not aggregate additively across days.

4. **Maintenance is the replay-merge delta path, in the same atomic ingest
   transaction as the `usage_event` write.**
   The rollup row moves exactly when its source moves:
   - **First insert** of a canonical event → full increment of the rollup row
     for `(client_id, project_id, day)`.
   - **Replay-merge with differing values** (ADR 0012 `updated`) → the rollup
     row adjusts by the same per-field delta (`new − old`) the reconciliation
     layer already computes for session aggregates.
   - **Exact duplicate** (`duplicate`, all deltas zero) → no-op.
   The rollup maintenance runs inside the same explicit transaction as the
   canonical event write, so the event and its aggregate can never diverge
   within a single ingest.

5. **Backfill.**
   `scripts/backfill_client_project_rollup.py` recomputes rollup rows from
   `usage_events` using the same additive math as live maintenance, plus a
   verification mode that flags disagreements with `SUM(usage_events)` per
   `(client_id, project_id, day)`. Backfill↔live-maintenance equivalence is a
   required test: recomputing a row that live maintenance already built must
   produce the identical value.

6. **Deployment is a straight switch — no feature flag.**
   The rollup migration deploys first; the backfill runs immediately after,
   while the table is still write-only (nothing reads it until #373). That
   gives the verification mode a window to catch disagreements before the read
   path depends on the table.

7. **Hybrid read (#373).**
   `group_by=client,project` reads the rollup table; every other aggregate
   dimension (model, session, day, week, month) keeps scanning raw
   `usage_events`. `COALESCE(canonical_name, name)` is applied at read time on
   both paths, so changing a canonical name never requires a table recompute.

## Alternatives Considered

1. **Keep legacy `opencode_usage_records` as the rollup source (ADR 0014
   status quo).** Rejected: the legacy records are no longer the usage query
   source and are reconciled by a fill-absent rule that does not model deltas;
   a rollup built on them would re-introduce the double-counting the canonical
   layer eliminates.
2. **Key on the project label string.** Rejected: labels are display values,
   not identity (#329 rewrote them once already); a label change would
   fragment one logical project across many rollup rows.
3. **Also store distinct counts (sessions, models) in the rollup.** Rejected
   (unchanged from ADR 0014): distinct counts do not aggregate additively
   across days; they stay distinct-count queries over raw records.
4. **Feature-flag the read switch.** Rejected: with backfill running in the
   write-only window between the migration deploy and #373, verification
   happens before any reader depends on the table — a flag adds deployment
   complexity without adding safety.
5. **Recompute the rollup from raw records on every read.** Rejected
   (unchanged from ADR 0014): the client-project dimension is the hottest,
   most fragmented view; the additive table exists to avoid that scan.

## Consequences

### Positive

- The rollup inherits the canonical dedup and delta semantics, so it cannot
  re-introduce the double-counting bug: it only ever moves by reconciled
  deltas in the same transaction as the canonical event.
- Renaming a project label or a canonical client name is a read-time change —
  no table recompute, no rollup fragmentation.
- The client-project view gets the hot-path scan savings of the additive
  table while every other aggregate dimension keeps the raw-record
  flexibility.
- The backfill verification mode makes the rollup auditable against
  `SUM(usage_events)` per `(client_id, project_id, day)`.

### Negative

- The rollup is a derived convenience: a bug in live maintenance or in the
  backfill is a bug in a second copy of the accounting data — which is why the
  backfill↔live-maintenance equivalence test and the verification mode are
  required rather than optional.
- Read-time project label resolution adds a join on the client-project read
  path that a label-keyed table would not need.
