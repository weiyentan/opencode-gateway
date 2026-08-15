# ADR 0019: Reporting Read API — ingested resources, state trails, session links (no completion claims)

## Status

Accepted

## Context

PRD #478 (cross-repository PR/MR/Issue Ingestion and Reporting Capability)
defines, in Implementation Decision 13, a read-only reporting API — "the
first report" — exposing what the Gateway has ingested: resources, their
per-delivery state trail, and the sessions provably linked to them.

Issue #479 delivered the write path (`app/api/reporting_ingest.py`,
migration 0031): an immutable `reporting_deliveries` row per delivery plus
an append-only `delivery_state_trails` entry per lifecycle state. Issue
#484 adds the read surface over that data.

Two upstream layers are **not yet merged** at the time of this decision:

- **#480 — immutable deliveries + current aggregates.** Supplies a
  normalized `reporting_resource_aggregates` table keyed by
  `(provider, repository_url, resource_type, resource_number)` and a
  forward-only per-key merge. Until it lands, the read path derives each
  resource's current aggregate at read time from the immutable
  `reporting_deliveries` table.
- **#481 — exact many-to-many session correlation.** Supplies deterministic
  session↔resource links from explicit stable resource references. Until it
  lands, no exact link exists; the read path must degrade explicitly rather
  than invent links.

## Decision

Add a strictly read-only reporting router (`app/api/reporting.py`,
prefix `/api/v1/reporting`) with three `GET` endpoints:

1. **`GET /resources`** — list ingested resources, paginated, filterable by
   any subset of the stable resource identity (`provider`,
   `repository_url`, `resource_type`, `resource_number`). Each item carries
   the current aggregate: verbatim payload, `delivery_count`,
   `last_delivery_id`, `last_ingested_at`, plus the composite
   `resource_id` key.
2. **`GET /resources/detail`** — full detail for one resource addressed by
   all four identity components (required query params). Returns the
   aggregate plus the per-delivery state trail (`delivery_state_trails`,
   chronological) plus `session_links`.
3. **`GET /session-links`** — the session links that currently exist
   (`afk_run_sessions`), surfaced as **provisional** (`provisional=True`)
   with an empty `source_references` list.

The surface follows the `app/api/afk_outcomes.py` conventions: raw asyncpg
via `Depends(get_session)`, the `{status, data, error}` envelope, and the
`_db_timeout` / `_request_timeout` helpers.

### Operator-token gating (no broad read)

Delivery payload and the state trail are **operator-only** data (issue
#483, ADR 0020). All three reporting `GET` endpoints therefore require the
dedicated operator token (`GATEWAY_OPERATOR_TOKEN`) via the
`require_operator_token` dependency — an **additional** gate on top of the
global `ApiKeyMiddleware` (`GATEWAY_API_KEY`). The operator token is read
from the dedicated `X-Operator-Token` header (never `Authorization`, which
carries the Admin API Key), so a client presents both credentials on the
same request and both gates are satisfiable. An empty operator token
fails closed (403), and the Admin API Key never satisfies the operator
gate. The reporting read API is the *sanctioned* read path for delivery
payload and the state trail; no other route reads those tables back out.

### Stable resource identity

A resource is addressed by `provider + repository_url + resource_type +
resource_number` — the producer's partition-key vocabulary. At this layer
`repository_url` is matched verbatim against the delivery payload's
`resource.repository_url`; URL normalization (lowercase, trailing-slash
strip) is owned by #480's aggregate layer and will apply uniformly to the
read path once that table exists.

### No completion claims

The report must never imply a "finished"/completion/outcome state for a
resource ("Definition of finished" is explicitly future discussion — PRD
Implementation Decision 13). The read path therefore:

- surfaces the resource's current payload **verbatim** and never derives a
  completed/finished/outcome state;
- exposes the delivery lifecycle states (`received`, `normalized`,
  `published`, `persisted`, `rejected`, `failed`) as *pipeline*
  observations, not as resource completion;
- carries no `completed` / `finished` / `outcome` / `done` field anywhere
  in a resource response shape.

### Session links degrade explicitly

`session_links` on resource detail is empty until #481 exists — the Gateway
never fabricates a resource↔session link it cannot prove. The standalone
`/session-links` endpoint surfaces the inferred `afk_run_sessions` links
that do exist, each marked `provisional=True` with an empty
`source_references` list. When #481 lands, exact links populate
`source_references` and set `provisional=False`; the shape is already
forward-compatible.

### Read-only

The read model is strictly read-only. The router exposes only `GET`; the
write path remains `app/api/reporting_ingest.py` (issue #479).

## Consequences

- Aurora Glass consumes the new `GET /api/v1/reporting/*` endpoints
  (frontend views are a later phase — out of scope for this slice) and must
  present the operator token (`GATEWAY_OPERATOR_TOKEN`) on those requests,
  via the dedicated `X-Operator-Token` header.
- Delivery payload and the state trail are readable **only** through this
  operator-gated surface; every other route that touches
  `reporting_deliveries` / `delivery_state_trails` is write-only.
- When #480 merges, the resource list/detail read queries can switch from
  delivery-derived aggregation to `reporting_resource_aggregates` without
  changing the `ResourceSummary` shape (`payload`, `last_delivery_id`,
  `last_ingested_at` already align; `last_occurred_at` and per-key
  provenance slot in).
- When #481 merges, `source_references` populates and `provisional` flips,
  with no response-shape change.
- No completion/outcome derivation is introduced; any future "finished"
  semantics remain a separate, explicitly-scoped decision.
