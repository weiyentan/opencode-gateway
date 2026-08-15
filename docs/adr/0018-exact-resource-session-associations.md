# ADR 0018: Exact resource↔session associations — deterministic links from explicit stable resource references

## Status

Accepted (2026-08-16)

## Context

The AFK Outcomes read-model (ADR 0015's sibling in the AFK domain, migrations
0026–0028) reconstructs AFK Runs from engineering activity and correlates them
against Gateway sessions. Every derived link today is produced by the
CorrelationEngine's confidence-ordered rules
(`explicit_run_id` → `issue_reference` → `branch_issue_reference` →
`commit_issue_reference` → `temporal_inference`), which always terminate in a
heuristic: even a strong rule is ultimately a *match with confidence*, and the
weakest links fall back to temporal inference. Run↔session attachments are
explicitly provisional (`inferred`, `method`), and every Correlation records
a confidence, evidence, and resolver version so its semantics stay auditable.

Issue #481 (cross-repo PRD #478, reporting ingestion and capability layer)
needs a different, stricter kind of link: an **exact**, many-to-many
resource↔session association that holds **only because the session's own
metadata explicitly carries the resource's stable identity** — never because a
heuristic or a time window made it plausible. Where a Correlation is a
deterministic rule with a confidence score, an exact association is a
provable, reproducible fact: re-reading the session field that carried the
reference yields the same resource every time.

Two properties drive the design:

1. **Exactness excludes temporal/heuristic inference by construction.** The
   association path accepts only explicit stable resource references
   (`provider`, `repository`, `resource_type`, `resource_number`) carried in
   session metadata. The reference carries no timestamps, windows, or scores,
   so the resolver *structurally cannot* temporally or heuristically infer a
   link from it. The five-rule correlation engine — down to
   `temporal_inference` — is deliberately not reused here (issue #481 forbids
   it).
2. **Determinism and idempotency under replay.** Identical session metadata
   must produce identical associations regardless of delivery order, and the
   same explicit reference converging on the same association must never
   duplicate a row. The association path therefore keys on the stable resource
   identity plus the session identity, and writes with a conflict update that
   advances `last_seen_at` (recency) without re-merging `source_reference`.

## Decision

### 1. A new read-model table, `resource_session_associations` (migration 0032)

A many-to-many table between one engineering resource and one OpenCode
session. One resource may link to many sessions and one session may link to
many resources. It is a derived convenience read-model of the exact-association
capability (like the Client Project Rollup is to `usage_events`, ADR 0015), not
a source of truth: rows exist only because an explicit reference named the
pair, and the `afk_outcomes.repository` `AsyncpgOutcomeRepository` is the only
writer.

Schema highlights:

* Keyed by `UNIQUE (provider, repository, resource_type, resource_number,
  external_session_id)` (named `uq_resource_session_associations_resource_session`).
  The **external session id** is the deterministic session anchor (`NOT NULL`),
  because a reference can name a session before its internal Gateway UUID is
  resolved.
* `session_id` (internal Gateway session UUID) is a nullable enrichment with
  **no FK**, mirroring `afk_run_sessions`' loose reference, so this schema
  never couples to the exact shape of the `sessions` table.
* `source_reference` JSONB (server-default `[]`) holds the per-link
  provenance.
* Secondary index `ix_resource_session_associations_session`
  (`external_session_id`) for session-scoped lookups.
* No status/outcome/finished columns: associations deliberately carry no
  completion/finished claim (PRD Implementation Decision 13).

### 2. Derivation only from explicit stable resource references

`afk_outcomes.associations.derive_exact_associations(references)` accepts only
`SessionResourceReference` inputs — an explicit reference already carrying the
full stable resource identity, the session identity, and the session field that
carried it (`source_field`). Because a reference carries no timestamps,
windows, or scores, the resolver structurally cannot temporally or heuristically
infer a link. A link exists only because an explicit reference named it.

Derivation is deterministic and order-independent: references are grouped by
`(provider, repository, resource_type, resource_number, external_session_id)`
and output is sorted by resource identity then session identity. Repeated
references to the same pair from the same session merge into a single
association.

### 3. Source-reference provenance on every link

Every association records its **Reference Source**: the session metadata field
that carried the reference (`ReferenceSource.field`) and the value found there
(`ReferenceSource.detail`). The merged provenance is the deduped, sorted union
of source fields across the references that produced the link, stored in
`source_reference`. This makes each link provable and reproducible — re-reading
the named field yields the same resource — and is the association-path
counterpart of the correlation engine's `correlation_method`/`evidence`,
recording *where the link came from in the session* rather than a scoring rule.

### 4. Idempotency via `UNIQUE` + conflict-update recency

`AsyncpgOutcomeRepository.save_associations` inserts with
`ON CONFLICT (provider, repository, resource_type, resource_number,
external_session_id) DO UPDATE SET last_seen_at = now()`. The same explicit
reference converging on the same association never duplicates a row, but
re-observation advances `last_seen_at` to track recency (consistent with the
AFK enrich-only upsert convention). `source_reference` provenance is written
once with the first insert and never re-merged. The single-statement write
needs no advisory lock (there is still no read-modify-write).

### 5. Independent resolver version

`ASSOCIATION_RESOLVER_VERSION = "1"` is recorded on every association and is
independent of the correlation engine's `RESOLVER_VERSION = "2"`: the two paths
share no rule semantics. Bump `ASSOCIATION_RESOLVER_VERSION` when the
reference-extraction or dedup logic changes, so downstream consumers can detect
rule-semantics changes exactly as they can for correlation links.

### 6. No API surface

The association capability is read-model + domain logic only: no REST
endpoint, no UI, no ingest-surface change in this slice. The
`afk_outcomes.repository` protocol gains `save_associations` as the single
write surface; API/UI exposure is a later slice.

## Alternatives Considered

**Reuse the CorrelationEngine's five rules (down to `temporal_inference`).**
Rejected: issue #481 explicitly forbids temporal/heuristic inference for this
path, and correlation links are confidence-scored matches by design. Reusing
the engine would make every "exact" link as weak as its weakest rule and blur
the boundary between "provably referenced" and "probably related".

**Fold associations into `afk_run_sessions` (run-keyed session links).**
Rejected: `afk_run_sessions` links are run-scoped, provisional/inferred
(`inferred`, `method`), and carry temporal fields (`started_at`,
`finished_at`). An exact association is resource-scoped, deterministic, and
inference-free; keying on the resource identity (not a run) keeps the two link
families semantically distinct.

**Extend the correlation ruleset with a new rule (e.g.
`explicit_resource_reference`).** Rejected: the correlation engine's output is
a confidence-scored, resolver-versioned link whose weakest output is
heuristic. The exact path is a separate, stronger link family with no
confidence — it needs its own read-model and its own resolver version rather
than a rule appended to the heuristic chain.

**Temporal/heuristic scoring of references to break ties or "enrich".**
Rejected: no scoring, no guessing, no time windows anywhere in the
association path. Ambiguity is simply not created (unlike the correlation
engine, which must fall through to `temporal_inference`).

## Consequences

### Positive

- Exact resource↔session links exist with **proof**: every link records which
  session field carried the reference, and re-reading that field reproduces the
  link.
- Deterministic and idempotent: identical metadata → identical associations,
  replay converges to a single row (`UNIQUE` + conflict update advancing
  `last_seen_at`), no double-counting.
- The exact path structurally cannot produce a heuristic link — the input
  model excludes timestamps/windows/scores, so inference is impossible by
  construction, not by discipline.
- The correlation engine's semantics are untouched: `RESOLVER_VERSION = "2"`
  and the five rules (including `temporal_inference`) remain exactly as they
  were; the new path uses its own `ASSOCIATION_RESOLVER_VERSION = "1"`.
- Additive and backwards-compatible: new table only, no API or ingest-surface
  contract changes.

### Negative

- A second link family (exact associations vs correlation links) must stay
  semantically distinct in vocabulary and code; the CONTEXT.md terms and
  `_Avoid_` lines exist to prevent conflation.
- No API/UI in this slice, so the capability is not yet directly consumable —
  it waits on a later reporting surface.
- `source_reference` provenance is written once (first insert wins) and is not
  re-merged on later references; a session that gains a new reference field
  after the row exists will not update the stored provenance until a
  re-derivation/re-write path is added. `last_seen_at`, by contrast, *is*
  advanced on re-observation, so activity recency is tracked even though
  provenance is not re-merged.
