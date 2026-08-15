# ADR 0019: Retention defaults + access controls (PRD #478 decisions 15–16)

## Status

Accepted (2026-08-16)

## Context

The AFK outcome + reporting read-model accumulates several distinct kinds of
data with very different longevity and sensitivity:

* **Aggregates** (`afk_runs`, `afk_run_sessions`) — the reconstructed run
  read-model, which is indefinite.
* **Metadata** (`engineering_events`, `delivery_log`, `delivery_state_trails`,
  `afk_run_entities`, `unresolved_correlations`) — events, delivery/state
  trails, and correlation links.
* **Redacted payload storage** (`reporting_deliveries.payload` and the
  `engineering_events.payload` redacted projection) — higher-volume,
  lower-longevity verbatim (redacted) payloads.
* **DLQ** (`afk.events-dlq`) — poison messages retained until resolved, but
  with no bound on growth.

Prior to this ADR the Gateway had retention settings only for the execution
transcript (ADR 0016); the AFK/reporting tables persisted indefinitely, the
DLQ had no operational maximum, and there was no operator role separating
delivery-payload / DLQ reads from the collector and admin credential layers.

## Decision

### Retention defaults (configurable via settings/env)

Four retention tiers are declared on `app.core.config.Settings`, all
env-driven (`GATEWAY_RETENTION_*`) so operators adjust them with no code
change:

| Tier                     | Setting                             | Default  |
|--------------------------|-------------------------------------|----------|
| Aggregates               | `retention_afk_aggregates_days`     | `0` (indefinite) |
| Metadata                 | `retention_afk_metadata_days`       | `365` (12 months) |
| Redacted payload storage | `retention_afk_payload_days`        | `90`    |
| DLQ operational max      | `retention_dlq_max_age_days`        | `30`    |

`0` means "never swept" (indefinite).  The metadata/payload/aggregate tiers
are **configurable defaults** — the sweep job that enforces the 12-month and
90-day windows over the Postgres tables is a subsequent slice; this ADR locks
the tier vocabulary and the settings so the future sweep has a stable,
config-driven source of truth.

### DLQ operational max (escalation/expiry, never unbounded)

The DLQ is a Kafka topic, so its operational maximum is enforced in two
cooperating parts:

1. **Producer-path stamping** — every DLQ record is written with
   `dead_lettered_at` (UTC) and `max_age_days` (the operational max in effect
   at DLQ time) by `build_dlq_payload`, so a record's age is self-describing
   and measurable.
2. **Escalation sweep** — `sweep_dlq` (CLI
   `python -m app.consumer.afk_consumer --dlq-sweep`) consumes the DLQ in
   bounded batches (`--batch-size`, `--limit`), classifies each record, and
   **escalates** records strictly older than the operational max by publishing
   an escalation record (original payload + reason + `escalated_at` +
   `escalation_reason`) to `afk.events-dlq-expired`.  A `--dry-run` reports
   the would-be-escalated records without publishing.  Physical removal from
   the DLQ is enforced by the topic's Kafka retention configured to the same
   max age; the escalation topic is the durable operator record, so nothing is
   ever silently lost.

Boundary semantics mirror the transcript retention job (ADR 0016): a record
exactly at the max-age edge is retained (strict `>`); only strictly older
records expire; a record without a usable `dead_lettered_at` has unknown age
and is retained (never prematurely expired).

### Access controls (operator-only delivery-payload / DLQ access)

Three distinct credentials replace the previously ambiguous "one master
token":

| Credential          | Source                    | Scope                                   |
|---------------------|---------------------------|-----------------------------------------|
| Admin API Key       | `GATEWAY_API_KEY`         | dashboard / admin surfaces              |
| Collector Credential| `collector_credentials`   | ingestion path (`/ingest`, reporting)   |
| Operator Token      | `GATEWAY_OPERATOR_TOKEN`  | operator-only read surfaces             |

* **No broad read** — delivery payload and DLQ data have **no** read surface:
  the `/ingest` and reporting-ingestion endpoints are write-only, and no route
  reads `reporting_deliveries` / `delivery_state_trails` / `delivery_log` /
  `engineering_events.payload` back out.  Broad read is therefore impossible.
* **Operator-only gate** — `require_operator_token` (`app/api/ingest.py`) is
  the enforcement primitive for any future operator-only read surface.  It
  validates `GATEWAY_OPERATOR_TOKEN` and **fails closed** (403) when no
  operator token is configured.
* **No shared tokens** — the operator token is distinct from the Admin API Key
  and from collector credentials; the Admin API Key does not satisfy the
  operator gate.  The ingestion path continues to rely on the dedicated
  collector credential (Two-Layer Auth, ADR 0007) — never the Admin API Key
  alone.
* **Private ingestion** — the ingestion endpoint remains behind the
  ApiKeyMiddleware + collector credential layers; it is not exposed to the
  public internet, and the producer webhook ingress on the EDA gateway side is
  unchanged.

## Consequences

* Operators can tune every retention window (including disabling the aggregate
  sweep or tightening the DLQ max) via environment variables only.
* The DLQ can no longer grow unbounded: expired records are escalated to a
  durable operator queue and the active DLQ is bounded by Kafka retention.
* Delivery payload and DLQ data have no broad read path; operator access is
  gated by a dedicated token that fails closed when unprovisioned.
* The existing Two-Layer Auth semantics (ADR 0007) and the
  `afk.events-dlq` producer path (#482) are preserved — the DLQ record shape
  gains two additive fields (`dead_lettered_at`, `max_age_days`) and is
  otherwise unchanged.
