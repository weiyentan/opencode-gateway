# ADR 0018 — Outcome layer event identity and write semantics

## Status

Accepted

## Context

The AFK outcome/provenance layer ingests the same logical engineering events
from two paths: the live Kafka consumer on the `afk.events` topic
(`opencode-outcomes` group) and windowed reconciliation/backfill reruns of
the backfill engine. Replays, redeliveries, and reruns must converge on
identical rows, and derived correlation links must remain explainable and
corrigible without destroying provenance.

## Decision

**(A1) Deterministic event identity.** Two EngineeringEvents are the same
event iff `(provider, repository, entity_type, external_id, event_type,
occurred_at)` all match, enforced by `UNIQUE` + `ON CONFLICT DO NOTHING`.
When the provider emits an event ID (GitHub timeline event id, GitLab event
id), it is stored as `provider_event_id` and is the authoritative source of
`occurred_at` — identity is anchored on provider truth where available.

**(A2) Fact vs. state semantics.** `engineering_events` are immutable facts —
`INSERT ... ON CONFLICT DO NOTHING`; re-delivery never rewrites them. State
rows (`afk_runs`, `afk_run_entities`, `afk_run_sessions`) upsert enrich-only:
later passes may raise confidence, append evidence, correct the derived
outcome; confidence is never silently lowered; superseded links get an
explicit marker, never a hard delete — no destructive loss of provenance.

**(A4) Explainable links.** Every derived link records `correlation_method`,
`correlation_confidence`, `evidence_json` (including source identifiers), and
the `resolver_version` that produced the link, so rule-improvement reruns
stamp the new version and stale links remain identifiable.

**(A6) Durable unresolved state.** Ambiguous/unmatched correlations persist in
`unresolved_correlations` (`reason ∈ {ambiguous, unmatched}`,
`candidates_json` for ambiguous ties, `rule_used`, `resolver_version`,
`first_seen_at`, `last_seen_at`) and may be resolved by later reconciliation;
the canonical link model keeps `afk_run_id NOT NULL`, so unresolved rows
never contaminate it.

## Consequences

- Replay, redelivery, and rerun safety by construction: every path converges
  on the same rows.
- Explainable and corrigible links: method, confidence, evidence, and
  resolver version travel with every derived link.
- A durable path from unresolved → resolved as the resolver improves.
- Write semantics consistent with the existing replay-merge discipline (ADRs
  0011/0012).

## Alternatives Considered

**Report-only unresolved state (ambiguity logged, nothing persisted).**
Rejected: ambiguity must remain durable and auditable, not vanish from the
record.

**`DO UPDATE` on engineering events.** Rejected: events are facts; re-delivery
must not rewrite them; enrichment belongs to state rows.

**Event identity derived solely from `provider_event_id`.** Rejected: not all
providers/events expose stable event IDs, so the six-tuple is the invariant,
with `provider_event_id` as authoritative evidence where present.
