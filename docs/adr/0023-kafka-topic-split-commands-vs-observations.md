# ADR 0023 — Split Kafka topics: afk.events (actionable commands) vs engineering.events.normalized (observable lifecycle events)

## Status
Accepted (2026-08-18)

## Context
- ADR 0003 established `afk.events` as the cross-repository Kafka transport between fast-api-eda-gateway (producer) and this repo's AFK Outcome Consumer (consumer group `opencode-outcomes`).
- Today both AFK command messages and normalized lifecycle observations share the single topic `afk.events`. The outcomes consumer receives messages it does not own, must inspect message type before processing, and routes legitimate AFK command records to the DLQ as invalid outcome records (DLQ noise).
- A separate consumer group does NOT filter message types from one topic — every consumer group reading a topic receives every record. The topic split is the correct isolation boundary.
- The normalized-event v1 contract is producer-owned (FastAPI EDA Gateway ADR 0005 supersedes this repo's ADR 0020) and pinned in `docs/contracts/normalized-event-v1/` (schema.json, fixtures, checksums.sha256, consumer-policy.yaml).
- Live cluster facts: Strimzi Kafka, `auto.create.topics.enable: "false"` (topics must be explicitly provisioned), all topics RF=3, cleanup=delete, min ISR=2, 7-day retention (30-day for DLQs). The k8s_app/kafka repo declares `engineering.events.normalized`, `engineering.events.normalized.dlq`, and `afk.events.dlq` as KafkaTopic resources.

## Decision
1. **Two topics**: `afk.events` (actionable commands — "do something") and `engineering.events.normalized` (observable lifecycle events — "something happened").
2. **Fan-out model**: an AFK-labelled issue produces BOTH a command on `afk.events` and an observation on `engineering.events.normalized`. Opening a user-initiated, non-draft PR/MR also produces both a `pr_mr_opened` command and an observation, regardless of whether it has an `afk` label. Lifecycle events that match no automation trigger produce an observation only. The EDA command vocabulary remains unchanged (`label`, `review_request`, `developer_request`, `review_verdict`, `pr_mr_opened`, `container_upgrade_requested`), and `afk.events` carries derived commands only, never raw webhook payloads.
3. **Consumer**: the AFK Outcome Consumer subscribes to `engineering.events.normalized` instead of `afk.events`, keeping the same failure semantics (invalid → DLQ immediately; DB errors → retry ×5 → DLQ + commit; offset committed only after successful persistence; reconcile loop unchanged as repair path).
4. **Contract unchanged**: the producer emits provider-specific types in the normalized envelope (`pull_request.opened`, `merge_request.opened`, `issue.opened`, etc.); the canonical outcome vocabulary remains the consumer's internal mapping layer. `linked_issues` (extracted from PR/MR title/description) is included in `pull_request.opened` and `merge_request.opened` wire observations for issue-to-change-request correlation.
5. **DLQ strategy**: declare `engineering-events-normalized.yaml` (topic `engineering.events.normalized`, 6 partitions, 7-day retention) and `engineering-events-normalized-dlq.yaml` (topic `engineering.events.normalized.dlq`, 3 partitions, 30-day retention) in `kafka-instance/` of the k8s_app/kafka repo, mirroring the `opencode-usage-v1` pattern. Also declare `afk-events-dlq.yaml` (topic `afk.events.dlq`). Kubernetes resource filenames are hyphenated; their `spec.topicName` values are dot-separated Kafka topic names.
6. **Configuration**: topic names are configured rather than hardcoded. The producer uses `AFK_EVENTS_TOPIC` and `NORMALIZED_EVENTS_TOPIC`; the OpenCode consumer uses `GATEWAY_NORMALIZED_EVENTS_TOPIC` and `GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC`.
7. **Migration**: create topics → deploy consumer on new topic → validate consumer health (lag trending to zero, no errors) → switch producer → verify. The reconcile loop (1h cadence, 24h window) backfills the small cutover gap; no dual-write/dual-consume compatibility code. Rollback is idempotent via `delivery_log UNIQUE(provider, delivery_id)` dedup.

## Consequences
- Each consumer gets a clean contract: everything on `engineering.events.normalized` is a normalized lifecycle event; everything on `afk.events` is AFK orchestration.
- Legitimate AFK commands no longer appear in the outcomes consumer DLQ.
- Topics must be explicitly provisioned (auto-create disabled); the normalized source topic, normalized DLQ, and AFK command DLQ are declared in `kafka-instance/`.
- Existing AFK orchestration path (Rulebook/AWX) is behaviorally unchanged.

## Alternatives Considered
- Single topic + consumer-side filtering (rejected: consumer groups do not filter message types; DLQ noise persists).
- Dual-write / dual-consume during migration (rejected: reconcile loop already covers the cutover gap; avoids temporary compatibility code).
- Producer-side canonical type normalization (rejected in favor of the already-implemented provider-specific contract; canonical vocabulary remains the consumer's internal mapping layer).

## Test Matrix

| # | Check | Expectation |
|---|---|---|
| 1 | AFK-labelled issue | Command on `afk.events` (Rulebook/AWX unchanged) AND observation on `engineering.events.normalized` |
| 2 | User-initiated, non-draft PR/MR opened without `afk` label | Command (`pr_mr_opened`) AND observation |
| 3 | PR/MR edited or updated without another command trigger | Observation only; no command |
| 4 | Human issue (no label) | Observation only (`issue.opened`) |
| 5 | Unmatched non-lifecycle webhook or comment | Nothing produced on either topic |
| 6 | Observation persistence | Rows in `engineering_events` (Postgres) |
| 7 | Duplicate delivery | Same `delivery_id` → deduped |
| 8 | Malformed event | -> `engineering.events.normalized.dlq`, source offset committed, consumer healthy |
| 9 | Postgres outage | Retry ×5 → DLQ + commit |
| 10 | Cutover hygiene | No legitimate AFK commands in outcomes DLQ after migration |
| 11 | Rollback drill | Reverted to `afk.events` → idempotent re-processing |

Plus: topic names configurable via their producer and consumer environment variables, schema validation passes, and `linked_issues` is populated for develop-loop PRs/MRs.

## References
- ADR 0003 (kafka-events-cross-repository-transport)
- ADR 0020 (normalized-provider-event-mapping-bridge, superseded by FastAPI EDA Gateway ADR 0005)
- `docs/contracts/normalized-event-v1/` (producer-owned contract artifacts)
- Design doc: "Kafka Topic Split for AFK Commands and Engineering Observations"
