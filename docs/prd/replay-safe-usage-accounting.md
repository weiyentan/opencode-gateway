## Problem Statement

The Gateway's ingest pipeline stores every unique `(client_id, source_database_id, source_record_id)` tuple as a single row in `opencode_usage_records`, incrementing session aggregate counters on each insert. This first-write-wins idempotency model works for live collection but breaks under three real-world scenarios:

1. **Collector Replay** — When a collector re-reads an OpenCode SQLite Source over a date range and redelivers previously seen records, the Gateway either silently accepts identical duplicates (no accounting impact) or rejects divergent values with a `conflict` status. There is no mechanism to replay-correct a record whose token counts were wrong at first delivery without double-counting.

2. **Source Identity Shift** — When a Runner VM is rebuilt or its SQLite database path changes, the Collector assigns a new `source_database_id`. The same external session produces records under two identities, causing the Gateway to count tokens twice across two parallel session aggregates. There is no operator workflow to resolve this identity discontinuity without risking recounting.

3. **Historical Duplicate Repair** — Pre-existing duplicate data in production cannot be repaired because the existing dedup key `(client_id, source_database_id, source_record_id)` is immutable — changing the key would require rewriting every downstream reference.

The current `IngestRecordResult.status` supports only `accepted | rejected | conflict`. It has no way to express `duplicate`, `updated`, or `quarantined` outcomes, which are essential for replay-safe accounting.

Every delivery must remain auditable as an **Ingest Attempt**, even when it does not create or modify a **Canonical Usage Event**. Token and cost aggregates must adjust by delta rather than counting events again. Concurrent live and replay deliveries of the same event must produce one deterministic outcome.

## Solution

Introduce a canonical event layer above the raw ingest pipeline. Each **Ingest Attempt** carries a complete redacted normalized record as JSONB. The Gateway resolves a **Canonical Usage Event** by reconciling the attempt against any existing event using **Replay Merge** semantics. Non-null collector values are authoritative; omitted values never erase stored values. Differing values are updated and accounting counters adjust by delta.

New **Source Identities** that produce records overlapping those of an existing identity for the same client enter **Source Identity Quarantine** until an operator explicitly resolves them. After resolution, events are keyed by `canonical_source_identity + source_record_id`. Original collector source IDs remain attached to every attempt for audit.

All ingest operations — attempt creation, event change, delta computation, aggregate update, and outcome recording — execute within a single PostgreSQL transaction (**Atomic Ingest Reconciliation**). Concurrent deliveries of the same canonical event are serialized by a per-event advisory lock (**Concurrent Replay Reconciliation**).

The ingest API returns batch-level 2xx responses with per-record outcomes: `accepted`, `duplicate`, `updated`, `quarantined`, `conflict`. Existing collectors continue working unchanged — replay metadata fields are optional.

A separate controlled activity (**Historical Usage Reconciliation**) repairs previously stored duplicate data after the live reconciliation model is deployed.

## User Stories

### Ingest Pipeline & Deduplication

1. As a collector, I can POST a batch of usage records to `/ingest` and receive per-record outcomes (`accepted`, `duplicate`, `updated`, `quarantined`, `conflict`) in a batch-level 2xx response, so that I know exactly how each record was handled.

2. As the Gateway, I store every delivery as an **Ingest Attempt** with the full redacted normalized record as JSONB, so that operators can diagnose issues even when the attempt did not create or modify a Canonical Usage Event.

3. As the Gateway, when an Ingest Attempt arrives with a dedup key matching an existing attempt for the exact same field values, I return `duplicate` and do not modify any Canonical Usage Event, so that replayed batches are safe to re-send.

4. As the Gateway, when an Ingest Attempt arrives with a dedup key matching an existing Canonical Usage Event but with differing non-null field values, I apply **Replay Merge** semantics — updating only the changed fields and adjusting token/cost aggregates by delta, so that collector corrections propagate without double-counting.

5. As the Gateway, when an Ingest Attempt arrives with a dedup key matching an existing event and all field values are identical, I return `duplicate` without touching the database, so that replayed batches are cheap.

6. As the Gateway, I compute token and cost deltas between the old and new Canonical Usage Event values during Replay Merge, applying positive deltas as additions and negative deltas as subtractions to session aggregates, so that totals remain accurate regardless of how many times a record is corrected.

7. As the Gateway, I reject records with negative token values or other validation failures with a `rejected` outcome, so that malformed data never enters the accounting layer.

### Source Identity Resolution & Quarantine

8. As the Gateway, when a new Source Identity appears for a client and its records overlap with an existing Source Identity's records for the same client, I place the new identity in **Source Identity Quarantine**, so that potentially duplicate accounting is prevented automatically.

9. As an operator, I can view quarantined Source Identities via a management endpoint, including the overlapping records and the affected sessions, so that I can diagnose why quarantine was triggered.

10. As an operator, I can explicitly resolve a quarantined Source Identity by declaring it continuous with an existing identity, so that future events use the resolved canonical key and past quarantined attempts become eligible for reconciliation.

11. As the Gateway, after a Source Identity Resolution, I create a **Canonical Source Identity** that represents multiple collector-provided source IDs linked to one physical OpenCode SQLite Source, so that Canonical Usage Events are keyed correctly going forward.

12. As an operator, I can see the full audit trail of Source Identity Resolutions — who resolved what, when, and with what reasoning — so that identity continuity decisions are traceable.

13. As the Gateway, I attach original collector source IDs to every Ingest Attempt for audit, even after a Source Identity Resolution, so that the mapping history remains reconstructible.

### Atomicity & Concurrency

14. As the Gateway, I commit or roll back an Ingest Attempt, Canonical Usage Event change, accounting delta, session aggregate update, and Ingest Outcome as one PostgreSQL transaction, so that partial reconciliations are never visible.

15. As the Gateway, when two concurrent deliveries (one live, one replay) target the same Canonical Usage Event, I serialize them using a per-event advisory lock, so that both produce one deterministic event state and cannot increment accounting more than once.

16. As the Gateway, if a transaction fails mid-reconciliation (e.g., deadlock), I abort the entire operation and return an appropriate error, so that Kafka redelivery restores consistency.

### Historical Usage Reconciliation

17. As an operator, I can trigger a **Historical Usage Reconciliation** that scans for duplicate Canonical Usage Events in existing data, selects events deterministically, rebuilds affected session aggregates, and preserves the original delivery history for audit, so that pre-existing duplicates can be repaired without affecting live ingestion.

18. As the Gateway, Historical Usage Reconciliation operates as a separate controlled activity with its own API endpoint, so that it does not interfere with normal ingest throughput.

19. As an operator, I can preview the impact of a Historical Usage Reconciliation before committing — seeing which events would be merged, which aggregates would change, and by how much — so that I can validate the repair plan.

### Backward Compatibility

20. As an existing collector, I can continue posting records using the current schema version without adding any replay metadata fields, so that the Gateway upgrade does not break live collection.

21. As a collector performing a **Collector Replay**, I can optionally include replay metadata (replay ID, requested start date, delivery mode) in the ingest request, so that the Gateway can distinguish replay deliveries from live ones while still processing them through the same reconciliation logic.

22. As the Gateway, I accept optional replay metadata fields on ingest requests without rejecting requests that omit them, so that backward compatibility is preserved.

### Observability & Reporting

23. As an Aurora Glass user, I see the same deduplicated totals whether records arrived via live collection or replay, so that dashboard numbers are always consistent.

24. As an operator, I can query Ingest Attempts independently from Canonical Usage Events, so that I can inspect delivery history without being affected by replay merges.

25. As an operator, I can filter Ingest Attempts by outcome (`accepted`, `duplicate`, `updated`, `quarantined`, `conflict`), so that I can quickly identify problematic deliveries.

26. As the Gateway, I expose operational metrics on ingestion — attempts per second, outcome distribution, quarantine count, average reconciliation latency — so that SRE dashboards can monitor health.

### Consumer Integration

27. As the Usage Record Consumer, I handle `quarantined` and `conflict` outcomes as successful deliveries (not transport failures), so that I do not redeliver these records to Kafka unnecessarily.

28. As the Usage Record Consumer, I continue sending DLQ messages only for 4xx transport errors and Pydantic validation failures, so that the DLQ topic remains focused on unprocessable data.

29. As the Usage Record Consumer, I can optionally tag replay payloads with a header or envelope wrapper indicating they originate from a Collector Replay, so that the Gateway receives replay context even when the collector omits inline metadata.

### Edge Cases & Error Handling

30. As the Gateway, when a Replay Merge would produce zero or negative token totals on a session aggregate, I clamp to zero rather than allowing negative accounting, so that session summaries remain valid.

31. As the Gateway, when a Source Identity Resolution references a non-existent identity, I reject the resolution with a clear error message, so that operators cannot accidentally orphan events.

32. As the Gateway, when a replay batch contains a mix of accepted, duplicate, updated, and quarantined records, I process each record independently and return all outcomes in the batch response, so that partial success is supported.

33. As the Gateway, when a Canonical Usage Event spans a Source Identity Resolution boundary (the event existed before resolution and continues after), I ensure the event's accounting does not double-count during the transition, so that session totals remain stable across the resolution point.

34. As an operator, I can manually mark a quarantined identity as a false positive (no overlap with existing identity) without resolving it to another identity, so that quarantine can be cleared even when there is nothing to merge into.

35. As the Gateway, when a replay arrives for an event whose Canonical Usage Event was already deleted (e.g., due to data retention policy), I treat it as a fresh `accepted` insert rather than a merge, so that no dangling references occur.

## Implementation Decisions

### Database Schema

#### `usage_events` (canonical events table)

Replaces `opencode_usage_records` as the primary accounting table. Stores the single deduplicated representation of each usage event.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Gateway-internal UUID |
| `canonical_source_identity_id` | UUID FK → `source_identities.id` | Resolved canonical identity |
| `source_record_id` | TEXT | Original collector record ID |
| `session_id` | UUID FK → `sessions.id` | Resolved internal session |
| `model_id` | UUID FK → `observed_models.id` | Model reference |
| `input_tokens` | INTEGER | Latest input token count |
| `output_tokens` | INTEGER | Latest output token count |
| `cached_tokens` | INTEGER | Latest cached token count |
| `cache_read_tokens` | INTEGER | Cache read tokens |
| `cache_write_tokens` | INTEGER | Cache write tokens |
| `reasoning_tokens` | INTEGER | Reasoning tokens |
| `estimated_cost_usd` | NUMERIC | Estimated cost |
| `reported_at` | TIMESTAMPTZ | When the collector recorded this usage |
| `provider` | TEXT | LLM provider name |
| `mode` | TEXT | Execution mode |
| `finish_reason` | TEXT | LLM finish reason |
| `project_id` | TEXT | Project identifier |
| `workspace_id` | TEXT | Workspace identifier |
| `agent` | TEXT | Agent name |
| `parent_session_id` | TEXT | Parent session identifier |
| `first_ingested_at` | TIMESTAMPTZ | When first seen |
| `last_ingested_at` | TIMESTAMPTZ | Last time updated |
| `created_at` | TIMESTAMPTZ | Row creation time |
| `updated_at` | TIMESTAMPTZ | Last modification time |

Unique constraint: `(canonical_source_identity_id, source_record_id)`

Indexes:
- `(session_id, reported_at)` — session aggregation queries
- `(canonical_source_identity_id, first_ingested_at)` — chronological lookup
- `(session_id, model_id)` — model breakdown queries

#### `usage_ingest_attempts` (every delivery)

Stores every delivery of a usage record to the Gateway, including first delivery, retry, and replay.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Gateway-internal UUID |
| `usage_event_id` | UUID FK → `usage_events.id` NULLABLE | Resolved canonical event (NULL if quarantined/conflict) |
| `source_identity_id` | UUID FK → `source_identities.id` | Identity used for this attempt |
| `original_source_record_id` | TEXT | Original record ID from collector |
| `record_jsonb` | JSONB | Complete redacted normalized record |
| `ingest_batch_id` | UUID FK → `ingest_batches.id` | Batch membership |
| `outcome` | TEXT | `accepted` / `duplicate` / `updated` / `quarantined` / `conflict` |
| `replay_id` | UUID NULLABLE | Replay Operation ID if this is a replay delivery |
| `delivered_at` | TIMESTAMPTZ | When the attempt was processed |
| `created_at` | TIMESTAMPTZ | Row creation time |

Index: `(source_identity_id, original_source_record_id)` — dedup lookup
Index: `(usage_event_id)` — reverse lookup from event to attempts
Index: `(ingest_batch_id, record_index)` — batch audit ordering

#### `source_identities` (identity mapping)

Maps collector-provided source IDs to canonical identities.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Gateway-internal UUID |
| `client_id` | UUID FK → `opencode_clients.id` | Owning client |
| `collector_source_id` | TEXT | Source database ID from collector |
| `is_canonical` | BOOLEAN | Whether this is the resolved canonical identity |
| `canonical_parent_id` | UUID FK → `source_identities.id` NULLABLE | Parent identity after resolution |
| `resolved_at` | TIMESTAMPTZ NULLABLE | When resolution occurred |
| `created_at` | TIMESTAMPTZ | Row creation time |

Unique constraint: `(client_id, collector_source_id)` — prevents duplicate mappings

#### `source_identity_quarantine` (quarantined identities)

Holds identities awaiting operator resolution.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Gateway-internal UUID |
| `source_identity_id` | UUID FK → `source_identities.id` | The quarantined identity |
| `overlapping_identity_id` | UUID FK → `source_identities.id` | The identity it overlaps with |
| `overlap_count` | INTEGER | Number of conflicting records found |
| `quarantined_at` | TIMESTAMPTZ | When quarantine was applied |
| `cleared_at` | TIMESTAMPTZ NULLABLE | When quarantine was lifted |
| `resolution_id` | UUID FK → `source_identity_resolutions.id` NULLABLE | Associated resolution |

Index: `(source_identity_id, cleared_at)` — active quarantine lookup

#### `source_identity_resolutions` (audit trail)

Records operator decisions about identity continuity.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | Gateway-internal UUID |
| `quarantine_id` | UUID FK → `source_identity_quarantine.id` | Associated quarantine |
| `resolving_identity_id` | UUID FK → `source_identities.id` | The identity chosen as canonical |
| `resolved_by_user_id` | UUID FK → `auth_users.id` NULLABLE | Operator who made the decision |
| `reason` | TEXT NULLABLE | Human-readable justification |
| `resolved_at` | TIMESTAMPTZ | When resolution occurred |

### Module Changes

#### `app/db/models/usage_events.py` — NEW

SQLAlchemy ORM model for `usage_events`. Defines column types, constraints, and relationships to `sessions`, `observed_models`, and `source_identities`. Includes computed properties for total tokens and cost verification.

#### `app/db/models/ingest_attempts.py` — NEW

SQLAlchemy ORM model for `usage_ingest_attempts`. Defines the `record_jsonb` column, outcome enum, and relationships to `usage_events`, `source_identities`, and `ingest_batches`. Includes helper methods for extracting token values from the JSONB payload.

#### `app/db/models/source_identities.py` — NEW

SQLAlchemy ORM models for `source_identities`, `source_identity_quarantine`, and `source_identity_resolutions`. Defines the identity resolution graph and quarantine lifecycle states.

#### `app/core/identity.py` — NEW

Canonical source identity resolution logic. Exposes:

- `resolve_canonical_identity(client_id, collector_source_id) -> UUID` — Returns the canonical identity UUID, creating a new one if none exists.
- `check_quarantine(client_id, candidate_source_id, existing_source_id) -> bool` — Returns `True` if the candidate should be quarantined due to overlap with an existing identity.
- `resolve_identity(quarantine_id, resolving_identity_id, reason, user_id) -> None` — Performs a Source Identity Resolution, linking identities and clearing quarantine.
- `get_active_quarantines(client_id) -> list[QuarantineRow]` — Lists unresolved quarantines for a client.

#### `app/core/reconciliation.py` — NEW

Replay merge logic and delta computation. Exposes:

- `compute_delta(old_event, new_values) -> DeltaResult` — Computes the difference between existing and incoming field values. Returns per-field deltas and overall token/cost adjustment.
- `apply_replay_merge(conn, event_id, new_values) -> IngestOutcome` — Applies Replay Merge semantics within the caller's transaction. Updates the canonical event, adjusts session aggregates by delta, and returns the outcome.
- `validate_no_negative_totals(session_id, adjusted_values) -> bool` — Ensures adjustments would not produce negative token totals.

#### `app/api/ingest.py` — MODIFY

Rewrite `_process_one_record()` to use the canonical event layer:

1. Resolve the canonical source identity for the incoming `source_database_id`.
2. Check quarantine status — if quarantined, record as `quarantined` outcome and skip event creation.
3. Look up the canonical event by `(canonical_source_identity_id, source_record_id)`.
4. If no existing event → create new event, outcome = `accepted`.
5. If existing event with identical values → outcome = `duplicate`.
6. If existing event with differing values → apply Replay Merge, outcome = `updated`.
7. If existing event with conflicting values (e.g., different session_id) → outcome = `conflict`.
8. Insert the Ingest Attempt row with full JSONB record and outcome.
9. All steps run within the caller's transaction.

Update `IngestRecordResult` to support the new outcome values: `accepted`, `duplicate`, `updated`, `quarantined`, `conflict`, `rejected`.

Add optional replay metadata fields to `IngestRequest`: `replay_id` (UUID), `replay_requested_start` (date), `replay_delivery_mode` (string).

#### `app/api/usage.py` — MODIFY

The existing aggregate, records, and sessions endpoints continue querying from `usage_events` instead of `opencode_usage_records`. The SQL structure is largely unchanged — just the table name and join paths differ slightly. No API contract changes are required.

#### Alembic Migration — NEW (migration 0013)

Creates the five new tables (`usage_events`, `usage_ingest_attempts`, `source_identities`, `source_identity_quarantine`, `source_identity_resolutions`) with all indexes and foreign keys. Does NOT drop `opencode_usage_records` — the migration is additive. A future migration will migrate data and swap the table role.

### API Contracts

#### `POST /ingest` — Response Schema Update

```python
class IngestRecordResult(BaseModel):
    index: int
    status: str  # "accepted" | "duplicate" | "updated" | "quarantined" | "conflict" | "rejected"
    reason: str | None
    event_id: uuid.UUID | None  # Present when outcome is accepted/updated/duplicate
    attempt_id: uuid.UUID       # Always present — the Ingest Attempt identifier
```

All outcomes return HTTP 200 with batch-level results. The consumer treats all 2xx responses as success.

#### `GET /admin/quarantined-identities` — NEW ENDPOINT

Returns a paginated list of active Source Identity Quarantines for a given client.

Response:
```json
{
  "items": [
    {
      "quarantine_id": "uuid",
      "source_identity_id": "uuid",
      "collector_source_id": "src-db-abc123",
      "overlapping_identity_id": "uuid",
      "overlapping_collector_source_id": "src-db-def456",
      "overlap_count": 42,
      "quarantined_at": "2026-07-15T10:30:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

Requires Admin API Key authentication.

#### `POST /admin/resolve-source-identity` — NEW ENDPOINT

Performs a Source Identity Resolution.

Request:
```json
{
  "quarantine_id": "uuid",
  "resolving_identity_id": "uuid",
  "reason": "Runner VM rebuilt, same physical machine"
}
```

Response: 200 with resolution details. Returns 400 if the quarantine is already cleared or the resolving identity does not exist.

#### `POST /admin/reconcile-historical-duplicates` — NEW ENDPOINT

Triggers Historical Usage Reconciliation.

Request (optional):
```json
{
  "dry_run": true,
  "client_id": "uuid",
  "date_from": "2026-01-01",
  "date_to": "2026-01-31"
}
```

Response:
```json
{
  "dry_run": true,
  "events_to_merge": 150,
  "aggregates_affected": 30,
  "token_adjustment": -50000,
  "cost_adjustment_usd": "-0.12"
}
```

When `dry_run: false`, performs the actual reconciliation and returns counts of merged events and updated aggregates.

### Alembic Migration Strategy

Migration `0013_observability_domain_tables_v2` creates all five new tables and indexes. It does NOT modify `opencode_usage_records`. The migration includes:

1. Table definitions with proper foreign keys and constraints
2. Indexes for dedup lookup, session aggregation, and quarantine queries
3. A placeholder function `migrate_usage_records()` left empty — data migration is a separate concern for a future migration

The migration is fully reversible via `downgrade()`.

## Testing Decisions

### Unit Tests

- **`tests/unit/test_reconciliation.py`** — Test `compute_delta` and `apply_replay_merge` with fixture data covering: identical values (no-op), single field change, multi-field change, null vs non-null handling (omitted values preserved), negative delta clamping.
- **`tests/unit/test_identity.py`** — Test `resolve_canonical_identity` with mock database connections covering: new identity creation, existing identity reuse, quarantine detection, resolution graph traversal.
- **`tests/unit/test_ingest_outcomes.py`** — Test `_process_one_record` outcome determination for each of the six statuses: accepted, duplicate, updated, quarantined, conflict, rejected. Use mocked asyncpg connections.

### Integration Tests

- **`tests/integration/test_ingest_reconciliation.py`** — End-to-end ingest flow against a test Postgres instance. Verifies: atomic transaction rollback on failure, correct outcome returned for each scenario, JSONB persistence, session aggregate accuracy after Replay Merge.
- **`tests/integration/test_quarantine_lifecycle.py`** — Verify quarantine creation, manual clearance, and resolution through to canonical identity assignment. Assert that quarantined attempts do not affect session aggregates.
- **`tests/integration/test_concurrent_replay.py`** — Launch two concurrent ingest requests targeting the same canonical event. Verify that only one event is created and both attempts are recorded.

### Regression Tests

- **`tests/regression/test_backward_compatibility.py`** — Send ingest payloads without replay metadata fields. Verify the Gateway processes them identically to pre-change behavior (same outcomes for identical inputs).
- **`tests/regression/test_existing_endpoints.py`** — Query the existing `/aggregates`, `/records`, and `/sessions` endpoints with mixed data from both `opencode_usage_records` and `usage_events`. Verify consistent results.

### Performance Tests

- **`tests/perf/test_ingest_throughput.py`** — Benchmark ingest throughput with 1000-record batches under concurrent load. Target: < 50ms median latency per batch with replay merge enabled.
- **`tests/perf/test_advisory_lock_contention.py`** — Measure serialization overhead when 10 concurrent requests target the same canonical event. Target: < 100ms additional latency for serialized requests.

### Out of Scope for Testing

- UI tests for admin endpoints (Aurora Glass integration is a separate effort).
- Load testing the Historical Usage Reconciliation endpoint (deferred to post-deployment monitoring).
- Testing the Kafka consumer's handling of new outcome codes (consumer changes are minimal — it already treats all 2xx as success).

## Out of Scope

- **Migration of existing `opencode_usage_records` data** — The new tables are created alongside the existing table. Data migration is a separate task that runs after deployment, during a maintenance window. Live ingestion continues on `opencode_usage_records` until the migration completes.

- **Automatic Source Identity resolution** — Resolution requires explicit operator action. No heuristic merging is implemented.

- **Real-time alerting on quarantine events** — Quarantine visibility is provided through the admin API; alerting integrations (PagerDuty, Slack) are outside this feature's scope.

- **Multi-tenant isolation** — The system assumes a single-tenant Gateway. Cross-client quarantine checks are not implemented.

- **Collector-side replay orchestration** — The Gateway accepts replay metadata but does not manage replay scheduling, checkpointing, or cursor management. Those belong to the collector or an external orchestrator.

- **Data retention policies** — Retention and archival of Ingest Attempts and Canonical Usage Events is not part of this feature.

- **Aurora Glass UI changes** — Dashboard rendering of usage data is unaffected by the backend changes; Aurora Glass consumes the same API contracts.

- **EDA (Event-Driven Automation) triggers** — No EDA rules are defined for quarantine events or reconciliation outcomes.

## Further Notes

### Deployment Order

Per the agreed design decision, Gateway work (this feature) must be completed and deployed before any Collector Replay deployment. Issue #48 depends on this feature. The deployment sequence is:

1. Deploy migration 0013 (additive — no downtime)
2. Deploy Gateway code changes (backward compatible — existing collectors work unchanged)
3. Deploy collector replay capability (issue #48)
4. Run Historical Usage Reconciliation (post-deployment cleanup)

### Relationship to Existing Tables

The `usage_events` table coexists with `opencode_usage_records` during a transition period. All new writes go to `usage_events`. Read queries for aggregates and records are updated to query `usage_events` exclusively. A future migration handles the cutover.

The `ingest_batches` and `ingest_audit` tables from migration 0012 remain in use. New ingest attempts reference `ingest_batches` via `ingest_batch_id`. The `usage_ingest_attempts` table provides richer per-attempt detail (JSONB record, outcome, replay metadata) alongside the lightweight audit trail.

### Glossary Alignment

This PRD uses the domain vocabulary defined in `CONTEXT.md`. Key terms:

- **Canonical Usage Event** — The single accounting representation of one OpenCode usage message. Counted once regardless of delivery count.
- **Ingest Attempt** — One delivery of a Usage Record to the Gateway. Observable even when it does not create or modify a Canonical Usage Event.
- **Ingest Outcome** — Per-record result: `accepted`, `duplicate`, `updated`, `quarantined`, or `conflict`. Returned inside a successful batch response.
- **Replay Merge** — Field-level reconciliation where non-null collector values are authoritative; missing values preserve existing data; accounting adjusts by delta.
- **Source Identity Quarantine** — Protective state for newly observed Source Database identities whose records overlap an existing identity.
- **Source Identity Resolution** — Explicit operator decision establishing identity continuity.
- **Canonical Source Identity** — The Gateway identity used for accounting after resolution.
- **Atomic Ingest Reconciliation** — Single PostgreSQL transaction encompassing attempt, event change, delta, and outcome.
- **Concurrent Replay Reconciliation** — Serialization of live and replay deliveries by canonical event identity.
- **Historical Usage Reconciliation** — Separate controlled activity for repairing previously stored duplicate data.

### Risk Considerations

- **Schema migration complexity** — Adding five new tables with interdependent foreign keys requires careful ordering in the Alembic migration. Foreign keys to `source_identities` cannot be added until the identity table exists.
- **Transaction contention** — Under high ingest throughput, the per-event advisory lock may become a bottleneck. Monitoring should track lock wait times during the initial deployment window.
- **JSONB storage growth** — Every Ingest Attempt stores the full record as JSONB. For high-volume collectors, this could significantly increase storage compared to the current approach. Consider compression or periodic archival in a future iteration.
- **Backward compatibility risk** — While the API contract is backward compatible, any change to `_process_one_record()` behavior introduces regression risk. Comprehensive unit and integration tests are essential.
