# ADR 0017 — Reconstructed AFK run identity

## Status

Accepted

## Context

opencode-gateway is observability-only (post-#207 refactor: no executor, no
job scheduler). AFK runs begin in the develop-loop runner (AWX on a
dedicated node) — the gateway only ever observes the aftermath: usage
records, sessions, and change_requests. The outcomes layer reconstructs
logical runs from evidence:

- PR title pattern "Develop-Loop: … — Implemented issues #N"
- PR body "afk-run:" annotation when present
- issue references, branch names, commit messages
- temporal overlap

## Decision

Every reconstructed run receives a newly-assigned ULID as `afk_run_id` —
never derived from evidence (no hash of PR number, no reuse of AWX job ID or
PR/MR number as primary key). External identifiers are mapped foreign
identifiers only. Backfill idempotency comes from entity-mapping uniqueness
`UNIQUE(provider, repository, entity_type, external_id, afk_run_id)`:
re-running backfill finds the existing run via the mapping rather than
minting a duplicate. Correlation evidence (`correlation_method`,
`correlation_confidence`, `evidence_json`) is preserved per link. Explicit
RUN_ID propagation (a later phase) is mapped as an external identifier, never
the PK.

## Consequences

- Run identity is stable across repeated backfills.
- Reconstruction is idempotent via mappings.
- The model is ready to receive propagated RUN_IDs without schema change.
- UI/API must always display correlation confidence alongside reconstructed
  links.
- The live consumer commits offsets only after a SINGLE DB transaction
  (delivery_log insert + engineering_events/run writes) succeeds, in the
  dedicated `opencode-outcomes` group (`auto_offset_reset=earliest`,
  `enable_auto_commit=False`); dedup is dual (`delivery_log` by delivery
  UUID, `engineering_events` by event-level UNIQUE), with scheduled
  reconciliation as the ultimate self-heal.
- Backfill and live ingest share one write path (same repository, same
  constraints), so replays and re-backfills converge idempotently; the
  outcomes consumer group `opencode-outcomes` never shares offsets with the
  usage consumer group `opencode-gateway`.
- Bounded-window reconciliation reruns are the repair primitive: they repair
  missed events and may reconsider derived correlations as the resolver
  evolves, stamping `resolver_version`; previously-unresolved correlations
  (see `unresolved_correlations`) may be resolved into real links by later
  reruns.

## Alternatives Considered

**Deterministic run IDs derived from evidence (e.g., hash of the
change_request number).** Rejected: bakes inference into identity,
misrepresents reconstructed provenance, and breaks when a run has no
change_request or when evidence changes.

**AWX job ID as primary identity.** Rejected: manual runs have no AWX job,
IDs are not portable across environments, and the plan explicitly requires
the run to outlive any trigger.
