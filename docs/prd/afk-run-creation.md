# PRD: AFK Run Creation from AWX Execution Binding Callback

> Destination document for issue #581.  The design decisions below are
> **implemented**: the data model (migrations 0037/0038), the API contract
> (`POST /api/v1/afk/executions`), the transactional repository operation
> (`AsyncpgOutcomeRepository.create_or_replay_afk_execution_binding`), and
> the readback compatibility landed through the execution-binding and
> provisional-lifecycle slices (#582–#586, #589, #595).

## Problem Statement

When an AWX execution completes and posts its result to the Gateway's
execution-binding endpoint, no Gateway-owned AFK run is created. The Gateway
only persists an `execution_bindings` row keyed by `awx_job_id`. Downstream
consumers and the Aurora Glass dashboard have no stable Gateway-generated
run identity to correlate AWX executions with observed engineering outcomes,
provider events, or OpenCode sessions. This makes it impossible to traverse
the identity chain:

```text
trigger event → awx_job_id → execution binding → AFK run → session → PR/MR → later events
```

without an externally generated identifier.

## Solution

Extend the execution-binding POST endpoint so that each accepted callback
transactionally creates a provisional AFK run in `afk_runs` and links it to
the execution binding. The Gateway generates and returns a stable 26-character
ULID-format `afk_run_id`. Identical retries return the same ID. Conflicting
payloads for the same AWX job ID are rejected. The provisional run persists
as `pending` until later AFK correlation enriches it with deterministic
evidence.

## User Stories

1. As the AWX execution-binding callback, I want a single POST to return both `awx_job_id` and a Gateway-generated `afk_run_id`, so that I have a stable run identity from the moment execution completes.
2. As an AWX operator, I want identical retries of the same execution-binding POST to return the same `afk_run_id`, so that idempotency is preserved.
3. As an AWX operator, I want a conflicting POST (different payload for the same `awx_job_id`) to return `409 Conflict`, so that I know the original binding was not overwritten.
4. As the Aurora Glass dashboard, I want each execution binding to carry an `afk_run_id`, so that I can display a Gateway-owned run identity independent of the AWX job ID.
5. As a downstream consumer, I want the AFK run to use the existing 26-character ULID format, so that it is compatible with existing AFK outcome tables and correlation workflows.
6. As the Gateway, I want a provisional AFK run to be created transactionally alongside the execution binding, so that both identifiers are consistent and atomic.
7. As the Gateway, I want the provisional run to have a `pending` status until correlation enriches it, so that it does not falsely claim a derived outcome before evidence arrives.
8. As a provider event consumer, I want later AFK/provider events to be correlated to the provisional run only when deterministic evidence exists, so that ambiguous resource-level events are not misattributed to the wrong execution.
9. As an AWX operator performing a manual or scheduled launch, I want `source_event_id` to be optional, so that non-event-driven executions can still create a valid run.
10. As the Gateway, I want historical execution bindings without `afk_run_id` to remain readable without backfill, so that backward compatibility is preserved.
11. As the Gateway, I want execution bindings to link to AFK runs via a nullable foreign key with `ON DELETE SET NULL`, so that execution evidence survives any future AFK-run cleanup.
12. As a monitoring operator, I want unmatched provisional runs to remain `pending` with full execution evidence, so that they can be audited before a later reconciliation process classifies them.
13. As the Gateway, I want the AFK run creation and execution binding persistence to live in the repository layer, so that the operation is transactional and idempotent at the database level.
14. As the AWX playbook, I want to send `trigger_type` and `source_event_id` in the execution-binding POST, so that the Gateway can preserve trigger provenance.
15. As the Gateway, I want to reject event-driven callbacks that are missing `source_event_id`, so that trigger provenance is never silently lost.
16. As the Gateway, I want the new fields (`afk_run_id`, `trigger_type`, `source_event_id`) to be nullable on read responses for legacy rows, so that existing data remains accessible without backfill.

## Implementation Decisions

### Data Model

- Add nullable `afk_run_id` (`VARCHAR(26)`) to `execution_bindings`,
  referencing `afk_runs.afk_run_id` with `ON DELETE SET NULL` (migration
  0038; `trigger_type` lands there too, while `source_event_id` has existed
  on the table since migration 0037).
- Reuse the existing `afk_runs` schema without modifying that table.
- Create provisional runs with `status = 'pending'`, provider derived from
  the binding payload, and null outcome fields — written by
  `create_or_replay_afk_execution_binding` in the same transaction as the
  binding INSERT.
- Historical bindings may remain without an AFK run link (both columns are
  nullable; readback maps `NULL` to `None`).

### API Contract

`POST /api/v1/afk/executions` gains:

```yaml
trigger_type: eda | manual | scheduled | backfill | recovery
source_event_id: string | null
```

Validation:

- `trigger_type` is required for new AFK-run-producing POSTs.
- `source_event_id` is required when `trigger_type = eda`, optional otherwise.
- `source_event_id` is not unique.
- Recovery may preserve the original `source_event_id`.

Responses include:

```yaml
afk_run_id: string  # 26-character ULID
binding_id: string
```

Idempotency:

- Same `awx_job_id` and identical payload returns `200` with the original IDs.
- Same `awx_job_id` with different payload returns `409 Conflict` without
  mutation.
- First creation returns `201 Created`.

GET execution-binding responses (`GET /executions/{awx_job_id}` and the
resource-history `GET /executions`) expose nullable `afk_run_id`,
`trigger_type`, and `source_event_id` for legacy compatibility — a row that
predates the columns reads back as `null`, never an error.

### Repository and Correlation

- One repository-layer transactional operation
  (`AsyncpgOutcomeRepository.create_or_replay_afk_execution_binding`)
  creates/replays the provisional run and binding atomically; the caller
  owns the transaction boundary and the operation uses savepoints so a
  failure never leaves an orphaned `afk_runs` row.
- ULIDs are generated through the existing injectable `ULIDSource`
  abstraction (`MonotonicULID` in production; deterministic sources in
  tests).
- Only deterministic evidence, such as matching `source_event_id` or an
  explicit AWX-to-event mapping, may enrich an execution-specific
  provisional run.
- Resource-level provider events must not be merged into a specific run
  based only on resource or session matching.
- Unmatched runs remain `pending`; timeout-based reconciliation and
  `unresolved` classification are separate future work.

### Trigger Types

- `eda`: event-driven EDA callback; `source_event_id` required.
- `manual`: manual AWX launch; `source_event_id` optional.
- `scheduled`: scheduled AWX launch; `source_event_id` optional.
- `backfill`: historical reconstruction; `source_event_id` optional.
- `recovery`: rerun of a previous execution; `source_event_id` optional and
  may preserve original lineage.

### Modules

- Extend the execution-binding domain model (`afk_outcomes.models`) and the
  request/response schemas (`app/core/schemas/execution_binding.py`).
- Add the Alembic migrations (0037/0038) and the nullable ORM foreign key
  (`app/db/models/afk.py`).
- Add the repository transaction and response mapping
  (`afk_outcomes/repository.py`).
- Extend the execution-binding API (`app/api/afk_executions.py`).
- Update the AWX smoke playbook (`playbooks/afk_execution_binding_smoke.yml`)
  to send trigger metadata and assert the returned `afk_run_id`.

## Testing Decisions

- Test trigger validation and response shaping (required `trigger_type`;
  `trigger_type=eda` without `source_event_id` rejected; unknown trigger
  types rejected).
- Test first creation (`201`), identical replay (`200`, same IDs), conflict
  (`409`, no mutation), legacy rows (nullable readback), rollback (no
  orphaned `afk_runs`), and pending-run persistence.
- Test API create/read behavior and the `{status, data, error}` envelope.
- Preserve existing collector authentication coverage (the write path
  requires the dedicated `awx-execution-bindings` collector credential).
- Run the live AWX smoke test and verify POST creation plus GET readback
  return the same `afk_run_id`.
- Keep all existing test suites passing.

## Out of Scope

- Timeout-based reconciliation or automatic `unresolved` classification.
- Historical backfill of AFK run links.
- Separate start/live-status endpoints.
- Changes to the existing `afk_runs` table or correlation engine in this
  initial slice.
- Changes to `opencode-collector`, credentials, API-key rotation, Aurora
  Glass, Kafka consumers, CLI/backfill scripts, or legacy repository callers.

## Further Notes

- The implementation preserves the identity distinction:

  ```text
  awx_job_id          = execution identity
  source_event_id     = trigger provenance
  afk_run_id          = Gateway-owned AFK run identity
  external_session_id = OpenCode session identity
  ```

- `CONTEXT.md` defines the **Execution Binding** vocabulary; ADR 0024
  (`docs/adr/0024-awx-execution-binding-history.md`) governs the
  execution-binding write semantics and ADR 0026
  (`docs/adr/0026-afk-run-id-database-relationships.md`) documents the
  `afk_run_id` database relationships, including the `ON DELETE SET NULL`
  link that lets execution evidence survive AFK run cleanup.
- The dedicated collector credential must be provisioned through the
  existing Gateway client/credential mechanism before AWX can call the
  write endpoint.
- The two-phase pre-bind/finalize contract (issue #590) extends this flow
  rather than creating a separate binding model.
