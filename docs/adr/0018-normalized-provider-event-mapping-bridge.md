# ADR 0018: Normalized provider-event mapping bridge (Stage 2)

## Status

Accepted (2026-08-16)

## Context

The AFK Outcome Consumer (`app/consumer/afk_consumer.py`, consumer group
`opencode-outcomes`) reads the external provider-events topic (`afk.events`).
Historically the topic carried one locked message shape — the ten canonical
event types (`issue.opened` … `pipeline.succeeded`) — parsed by
`ProviderEventMessage` and mapped 1:1 by `map_provider_event`.

The producer (`fast-api-eda-gateway`, issues #97–#102) now also emits a
**normalized, schema-versioned event** that is provider-agnostic: it carries
the producer's native resource vocabulary (`issue`, `pull_request`,
`merge_request`) rather than the outcome layer's canonical vocabulary
(`change_request`, CONTEXT.md).  The two vocabularies must not be conflated:
`pull_request` (GitHub) and `merge_request` (GitLab) are the *same*
outcome-layer concept — a `change_request` — while `issue` is unchanged.

The consumer must keep ingesting the legacy ten-type shape unchanged (a
non-breaking requirement) and additionally bridge the normalized shape into
the canonical vocabulary before persisting.

## Decision

Extend `map_provider_event` into a **dispatcher** that routes by message
shape, keeping the legacy mapping byte-for-byte intact:

* **Legacy shape** (`ProviderEventMessage`, discriminated by its `type` and
  `number` fields) — mapped by `_map_legacy_event`, the unchanged ten-type
  mapping.
* **Normalized shape** (`NormalizedProviderEvent`, discriminated by its
  `resource_type` field) — mapped by `map_normalized_event`, the Stage-2
  bridge.

The normalized event carries: `schema_version`, `provider`, forwarded
`delivery_id`, `resource_type`, `resource_id` (the stable provider-scoped
resource identity), `repository`, `action`, `occurred_at`, `ingested_at`,
`actor`, and `payload_ref` (a *reference* to the redacted payload — never the
payload itself).

The bridge maps:

| `resource_type`  | outcome-layer `entity_type` |
|------------------|-----------------------------|
| `issue`          | `issue`                     |
| `pull_request`   | `change_request`            |
| `merge_request`  | `change_request`            |

`action` becomes the canonical event-type suffix
(`change_request.merged`, `issue.opened`, …).  The resulting `event_type` is
validated against the locked canonical vocabulary (`_MAPPED_EVENT_TYPES`);
an unknown `resource_type` or an `action` that does not resolve to a canonical
event type returns `None`, which the consumer routes to `afk.events-dlq` as
an unmappable poison message (never persisted, never conflated with a legacy
type).  `payload_ref` is forwarded into the event `payload` under the
`payload_ref` key.

## Consequences

* The legacy ten-type mapping is preserved verbatim; existing producers and
  the provider adapters are unaffected.
* `pull_request`/`merge_request` deliveries persist as `change_request`
  entities/events — the same vocabulary the backfill adapters emit — so
  live-ingested and backfill-fetched events dedup on the shared
  `engineering_events` identity key.
* An unknown normalized action is a permanent (poison) failure → DLQ, matching
  the existing unmappable-type contract; the pipeline never stalls.
* The bridge is additive and does not change `delivery_log` /
  `engineering_events` write semantics, dedup, or the offset-commit frontier.
