# ADR 0026: AFK Run ID Database Relationships

## Status

Accepted (2026-08-24)

## Context

`afk_run_id` is the stable correlation identifier for one logical AFK lifecycle. It ties orchestration executions, OpenCode sessions, and engineering entities together.

It is not an AWX job ID, OpenCode session ID, issue ID, PR/MR ID, commit SHA, or review ID. A lifecycle can be provisioned before AWX starts and before a PR/MR exists. Multiple issue webhooks can arrive in one AFK batching group and contribute to the same lifecycle.

## Decision

### Aggregate root

`afk_runs` is the lifecycle aggregate root:

```text
afk_runs
----------------------------------
PK  afk_run_id
    provider
    repository
    trigger_type
    host
    source_event_id
    status
    title
    outcome
    started_at
    finished_at
    first_seen_at
    last_seen_at

    change_request_provider
    change_request_repository
    change_request_external_id
    recovered_from_afk_run_id
```

`afk_run_id` is the natural primary key of `afk_runs` — there is no surrogate `id` column. This differs from ADR 0024's `execution_bindings` table, which uses a surrogate `id` PK alongside `awx_job_id` as the idempotency key. The rationale is timing: `afk_run_id` is a ULID assigned at provisioning time before any execution exists, so it serves as both the logical identity and the physical PK from the moment the row is created. `execution_bindings`, by contrast, uses a surrogate `id` because `awx_job_id` is the idempotency key but is assigned by AWX after the binding is created — the binding row must exist before the job runs, so the identity cannot be supplied by AWX up front.

`afk_runs.source_event_id` stores the first/triggering provider delivery ID that caused the lifecycle to be provisioned — the webhook delivery that triggered the batch. It is NOT the lifecycle grouping key; it is provenance for the trigger event only. The binding-level `execution_bindings.source_event_id` is different: it stores the per-execution EDA source event ID — the specific webhook delivery that triggered that particular AWX job. The two serve different purposes: the root-level field is trigger provenance for the lifecycle, while the binding-level field is per-execution provenance.

The lifecycle exists independently of AWX and the eventual change request:

```text
webhook batch accepted -> afk_run_id provisioned -> AWX launch
        -> OpenCode session(s) -> engineering work -> PR/MR binding
```

### Cardinalities

```text
1 afk_run_id -> N execution_bindings
1 afk_run_id -> N afk_run_sessions
1 afk_run_id -> N afk_run_entities
1 afk_run_id -> 0..1 change request
1 PR/MR       -> 0..1 afk_run_id
```

A successful develop lifecycle may therefore contain many AWX jobs, sessions, issues, commits, and reviews, plus exactly one PR/MR. A failed or incomplete lifecycle may contain no PR/MR.

One lifecycle must never own two PRs/MRs. One PR/MR must not be assigned to two lifecycle rows. The change-request identity is the tuple `(change_request_provider, change_request_repository, change_request_external_id)`; a partial unique constraint enforces reverse ownership once all three values are present.

### AWX execution bindings

The execution table is `execution_bindings`. There is no `afk_run_executions` table. Each row represents one AWX job:

```text
execution_bindings
----------------------------------
PK  id
    awx_job_id
    job_template_id
    external_session_id
    provider
    repository_url
    entity_type
    entity_number
    outcome
    source_event_id
    branch
    title
    failure_reason
    failure_summary
    started_at
    finished_at
    created_at
    updated_at

FK  afk_run_id
    trigger_type
```

`afk_run_id` is the logical lifecycle identity. `awx_job_id` is one execution-attempt identity. AWX idempotency is bounded by `awx_job_id`:

```text
same awx_job_id + same binding        = replay / no-op
same awx_job_id + conflicting binding = reject
new awx_job_id + same afk_run_id      = valid retry
```

A retry creates a new binding while retaining the same run:

```text
afk_run_id: run-001
    +-- awx_job_id: 18432  outcome=failed
    +-- awx_job_id: 18447  outcome=completed
```

The provider resource identity is not unique because multiple AWX jobs may execute against the same PR/MR.

`failure_reason` (short label) and `failure_summary` (truncated text) are
bounded, redacted failure metadata. Redaction is applied once at the API
schema layer before persistence (the schema is the sole redaction
enforcement point — direct repository callers are responsible for their
own redaction), and `failure_summary` truncation is a Python character
slice (`[:1000]`) applied after redaction. A `completed` execution
carries no failure metadata: non-null `failure_reason` or
`failure_summary` alongside `outcome == completed` is rejected with
`422` on both POST and PATCH, and the repository's after-merge check
rejects a completed PATCH transition whose merged state still carries a
stored `failure_summary`.

### OpenCode sessions

`afk_run_sessions` records the sessions belonging to a lifecycle:

```text
afk_run_sessions
----------------------------------
PK  id
FK  afk_run_id
    session_id
    external_session_id
    started_at
    finished_at
    first_seen_at
    last_seen_at
```

One lifecycle may contain a parent session and multiple agent sessions. There is no `execution_id` foreign key on this table. The AWX-job-to-session relationship is provided by `execution_bindings.external_session_id`.

### Engineering entities and events

`afk_run_entities` records issues, change requests, commits, and reviews correlated to a lifecycle:

```text
afk_run_entities
----------------------------------
PK  id
FK  afk_run_id
    provider
    repository
    entity_type
    external_id
    owning_change_request_id
    role
    correlation_method
    correlation_source
    correlation_confidence
    evidence
    resolver_version
    superseded_at
    first_seen_at
    last_seen_at
```

Its uniqueness boundary is:

```text
UNIQUE(provider, repository, entity_type, external_id, afk_run_id)
```

Deduplication follows a first-writer-wins rule per `(provider, repository, entity_type, external_id, afk_run_id)`: the first correlation method to persist the entity wins, and subsequent correlations for the same entity + run pair are no-ops — the existing row is not updated. This prevents churn from competing correlation methods (for example, an explicit issue mention firing alongside a body-text parse) while preserving the audit trail of which method first established the link. If a correlation method needs to supersede a previous one, the `superseded_at` column is used: it is set on the old row, and a new row is inserted with the different method.

`engineering_events` stores immutable observations about those entities:

```text
engineering_events
----------------------------------
PK  id
    afk_run_id          FK REFERENCES afk_runs(afk_run_id), nullable
    provider
    repository
    entity_type
    external_id
    event_type
    occurred_at
    provider_event_id
    actor
    payload
    observation_key
    observed_via
    snapshot_at
    first_ingested_at
```

The `afk_run_id` foreign key is optional (nullable) because events may arrive before the lifecycle is provisioned. When set, it provides a direct query path for lifecycle-scoped event aggregation, avoiding a join through `afk_run_entities` by `(provider, repository, entity_type, external_id)`. The join path through `afk_run_entities` remains available as a secondary strategy for events that were ingested before lifecycle association.

Examples include `issue.opened`, `issue.closed`, `change_request.opened`, `change_request.review_requested`, `change_request.changes_requested`, `change_request.approved`, and `change_request.merged`.

### Change-request binding

The change request is stored directly on `afk_runs` using:

```text
change_request_provider
change_request_repository
change_request_external_id
```

The run is provisioned first and bound explicitly later. Repeating the same binding is idempotent. Binding a different change request to an already-bound run is rejected. Binding a change request already owned by another run is rejected.

### Recovery

`recovered_from_afk_run_id` is a self-referential foreign key:

```text
AFK Run A -- recovery needed --> AFK Run B
                                  recovered_from_afk_run_id = Run A
```

Recovery creates a new lifecycle and does not mutate the predecessor or its change-request relationship.

### Webhook batching and source provenance

The lifecycle boundary for batched issue webhooks is:

```text
one provider + one host + one repository
+ one AFK workflow category + one batching window
    -> one afk_run_id
```

Example:

```text
afk_run_id: run-001
    +-- source delivery: github-101
    +-- source delivery: github-102
    +-- source delivery: github-103
    +-- issue #10
    +-- issue #11
    +-- issue #12
    +-- PR #55, once created and explicitly bound
```

Each provider delivery ID remains webhook provenance. It must remain available in the enriched event, batch item, or engineering-event/entity record. `source_event_id` is not the lifecycle grouping key, and a provider delivery ID must not be replaced with an invented event ID.

The singular root `afk_runs.source_event_id` field is not a substitute for retaining all delivery provenance contributed to a batch.

## Consequences

- Multiple issue webhooks can consolidate into one lifecycle and one PR/MR.
- Failed and successful AWX attempts remain visible under the same lifecycle.
- Costs and audits aggregate through `afk_run_id` without confusing AWX attempts with logical lifecycles.
- Webhook enrichment must support batch-level lifecycle identity rather than automatically creating one run per delivery.
- `fast-api-eda-gateway` calls Gateway APIs and does not access the Gateway database directly.
- Lifecycle provisioning uses the existing `awx-execution-bindings` client credential, not the `opencode-collector` usage credential.
- A new PR/MR requires a new recovery lifecycle if the predecessor already owns a change request.

## Agent Rules

```text
afk_run_id          = logical AFK lifecycle
awx_job_id          = one AWX execution attempt
external_session_id = one OpenCode session
source_event_id     = provider webhook delivery provenance
change_request_*    = the lifecycle's single optional PR/MR identity
```

Never assume:

```text
AWX job ID        == afk_run_id
OpenCode session  == afk_run_id
PR/MR ID          == afk_run_id
source event ID   == afk_run_id
```

Preserve:

```text
1 afk_run_id -> N AWX jobs
1 afk_run_id -> N OpenCode sessions
1 afk_run_id -> N engineering entities
1 afk_run_id -> 0..1 PR/MR
1 PR/MR       -> 0..1 afk_run_id
```

## Related ADRs

- ADR 0024: Preserve AWX execution binding history
- ADR 0025: Pre-provision AFK run identity at webhook ingress
