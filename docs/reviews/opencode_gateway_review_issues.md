# OpenCode Gateway Review Issues

This file contains GitHub-issue-ready markdown generated from the code/ADR review of `weiyentan/opencode-gateway`.

Suggested implementation order:

1. Fix AWX executor dependency construction
2. Make Alembic the production schema source of truth
3. Implement ADR 0003 as a first-class port allocation contract
4. Do not mark jobs completed until OpenCode reaches a verified terminal state
5. Split runner admin status from observed health status
6. Replace fake job events for runner status changes with system/runner events
7. Complete workspace cleanup state transitions
8. Harden production authentication defaults
9. Redact and control secret-like environment variables
10. Fail hard when AWX artifacts are missing or malformed
11. Add end-to-end lifecycle integration test for Gateway → AWX → OpenCode

---

## Issue 1 — Fix AWX executor dependency construction

```markdown
## Problem

The Gateway has an executor plugin abstraction, but the AWX executor is not always constructed through the correct factory path.

The app factory appears to instantiate the configured executor class directly. This works for simple/local executors, but the AWX executor requires an AWX client and template IDs. There is a separate executor factory that wires this properly, but the app lifecycle should consistently use that path.

This creates a risk that `AWXExecutorPlugin` either fails at startup or is created without the dependencies required to launch AWX job templates.

## Related ADR

- ADR 0002 — Executor Plugin Interface

## Why this matters

AWX is the real infrastructure executor in the intended architecture. If the Gateway cannot reliably instantiate the AWX executor, the whole `Gateway → AWX → OpenCode` control-plane path is fragile.

## Expected behaviour

The application should construct executors through one consistent dependency path.

AWX mode should:

- create an authenticated AWX client
- inject required job template IDs
- fail fast if required AWX configuration is missing
- be covered by tests

## Suggested implementation

- Refactor app startup/factory code to use the existing executor factory rather than direct class construction.
- Ensure `AWXExecutorPlugin` is never instantiated without:
  - AWX URL
  - AWX token
  - create workspace template ID
  - start OpenCode template ID
  - cleanup template ID, if applicable
- Add validation for missing AWX settings at startup.
- Add tests for:
  - local executor construction
  - AWX executor construction
  - missing AWX config fails loudly
  - invalid executor type fails loudly

## Acceptance criteria

- [ ] App startup uses the same executor construction path for all executor types.
- [ ] AWX executor receives a fully configured AWX client.
- [ ] Missing AWX settings fail at startup with a clear error.
- [ ] Tests cover successful AWX executor construction.
- [ ] Tests cover failure when required AWX settings are missing.
- [ ] No direct `executor_cls()` construction remains in production startup code unless the executor has no required dependencies.

## Files likely affected

- `app/core/factory.py`
- `app/executors/factory.py`
- `app/executors/awx/plugin.py`
- `app/core/config.py`
- `tests/`
```

---

## Issue 2 — Make Alembic the production schema source of truth

```markdown
## Problem

The project currently has schema creation logic in `schema.sql` as well as Alembic migrations.

The Alembic migration includes newer tables such as runner and observation tables, but the startup schema SQL does not include all of those structures. This means a fresh environment that relies on app startup schema creation can differ from an environment that ran Alembic.

This creates schema drift and makes production behaviour unpredictable.

## Related ADR

- ADR 0001 — Separate Observation Tables

## Why this matters

The Gateway is a stateful control plane. Job state, runner state, workspace state, and observations must be reliable. If different deployment paths create different database schemas, the system can fail only after it reaches a specific code path.

## Expected behaviour

There should be one production source of truth for database schema.

Preferably:

- Alembic is the production migration path.
- `schema.sql` is either removed, generated, or explicitly limited to test/dev use.
- Startup should validate required tables exist.
- Missing schema should fail loudly rather than partially boot.

## Suggested implementation

- Treat Alembic as the production source of truth.
- Change startup behaviour so production does not silently call incomplete `schema.sql`.
- Add a startup check that verifies required tables exist:
  - `gateway_jobs`
  - `workspaces`
  - `job_events`
  - `approvals`
  - `runners`
  - `runner_observations`
  - `workspace_observations`
  - `opencode_instance_observations`
- Make local/dev setup documentation explicit:
  - run migrations first
  - then start the app
- Either:
  - remove `schema.sql`, or
  - regenerate it from migrations, or
  - clearly mark it as test-only

## Acceptance criteria

- [ ] Production startup does not rely on stale/incomplete `schema.sql`.
- [ ] Alembic migrations create all required tables.
- [ ] Startup fails clearly if required tables are missing.
- [ ] Tests use the same schema path as production or explicitly document why not.
- [ ] Documentation explains how to initialise and migrate the database.
- [ ] ADR 0001 tables are present in a fresh migrated database.

## Files likely affected

- `app/db/schema.sql`
- `app/db/connection.py`
- `app/core/factory.py`
- `alembic/versions/`
- `README.md`
- `tests/`
```

---

## Issue 3 — Implement ADR 0003 as a first-class port allocation contract

```markdown
## Problem

ADR 0003 says Postgres must be the source of truth for OpenCode Serve port allocation, using the range `10000–10999`.

The current implementation has an internal port allocation helper, but the contract is incomplete:

- port allocation is not clearly part of the job/workspace lifecycle
- AWX does not appear to receive the allocated port as part of `start_opencode`
- the allocation logic can exceed the allowed range
- there is no clear API contract for AWX to request or consume the assigned port
- there is no database constraint enforcing the valid port range

## Related ADR

- ADR 0003 — Postgres Port Allocation

## Why this matters

Multiple OpenCode sessions may run concurrently. If port allocation is not centralised and persisted, OpenCode Serve instances can collide or become unreachable.

The Gateway should own this coordination because it is the control plane.

## Expected behaviour

When a workspace/session is created:

1. Gateway allocates a port from Postgres.
2. The port is persisted against the workspace or OpenCode instance.
3. The port is passed to AWX/OpenCode.
4. The same port is used for observation, health checks, and cleanup.
5. Ports stay within `10000–10999`.
6. Ports are released or made reusable after cleanup.

## Suggested implementation

- Add a clear port allocation function/service.
- Use a database transaction and advisory lock for allocation.
- Enforce `port BETWEEN 10000 AND 10999` at the database level.
- Decide whether the port belongs to:
  - workspace
  - OpenCode instance
  - both, with one authoritative source
- Pass the allocated port into the AWX `start_opencode` job template.
- Add tests for:
  - single allocation
  - concurrent allocation
  - upper bound enforcement
  - no duplicate active ports
  - port reuse after cleanup, if intended
  - failure when pool is exhausted

## Acceptance criteria

- [ ] Port allocation is part of workspace/session creation.
- [ ] Allocated port is persisted in Postgres.
- [ ] AWX receives the allocated port when starting OpenCode.
- [ ] Database constraint enforces valid port range.
- [ ] Concurrent allocations cannot produce duplicates.
- [ ] Allocation cannot return a port outside `10000–10999`.
- [ ] Tests cover collision prevention and pool exhaustion.
- [ ] ADR 0003 is fully reflected in code and tests.

## Files likely affected

- `app/api/workspaces.py`
- `app/api/jobs.py`
- `app/executors/awx/plugin.py`
- `app/core/models/`
- `app/db/schema.sql`
- `alembic/versions/`
- `tests/`
```

---

## Issue 4 — Do not mark jobs completed until OpenCode reaches a verified terminal state

```markdown
## Problem

The current job lifecycle can mark a Gateway job as `completed` immediately after workspace creation and OpenCode startup.

That is too optimistic. Starting OpenCode is not the same as completing the coding task.

A job should not be considered complete until the Gateway has evidence that OpenCode or the executor reached a verified terminal state, such as:

- branch pushed
- diff produced
- MR/PR created
- task failed
- task aborted
- approval required

## Related architecture intent

The intended workflow is:

`AWX → OpenCode develop-loop → branch/MR → human review`

The Gateway should track this lifecycle accurately.

## Why this matters

If jobs are marked complete too early, the system gives a false sense of success. This weakens auditability and makes it hard to reason about failed or partially completed automation.

## Expected behaviour

Job creation should move through explicit lifecycle states.

Example state flow:

```text
pending
→ provisioning_workspace
→ starting_opencode
→ running
→ awaiting_review
→ completed
```

Failure flow:

```text
pending
→ provisioning_workspace
→ failed
```

Abort flow:

```text
running
→ aborting
→ aborted
```

## Suggested implementation

- Expand job lifecycle states or clarify existing states.
- Separate “OpenCode started successfully” from “job completed successfully”.
- Add an explicit OpenCode session/result observation step.
- Only mark `completed` when a verified terminal signal exists.
- Store useful completion metadata:
  - branch name
  - commit SHA
  - PR/MR URL
  - summary
  - failure reason
  - OpenCode session ID
- Add event logging for each lifecycle transition.

## Acceptance criteria

- [ ] Job is not marked `completed` immediately after OpenCode startup.
- [ ] Starting OpenCode creates a `running` or equivalent state.
- [ ] Completion requires a verified terminal result.
- [ ] Failed OpenCode startup marks the job `failed`.
- [ ] Failed executor calls mark the job `failed`.
- [ ] Job events record every lifecycle transition.
- [ ] Tests cover successful lifecycle.
- [ ] Tests cover OpenCode startup failure.
- [ ] Tests cover missing/invalid terminal result.

## Files likely affected

- `app/api/jobs.py`
- `app/core/models/job.py`
- `app/opencode/`
- `app/executors/`
- `app/core/scheduler.py`
- `tests/test_jobs.py`
- `tests/test_opencode_*.py`
```

---

## Issue 5 — Split runner admin status from observed health status

```markdown
## Problem

Runner observations can currently overwrite runner status.

This creates a dangerous conflict between human/operator intent and telemetry. For example, if an operator marks a runner as `offline` or `maintenance`, a later observation heartbeat may set it back to `healthy`.

Administrative state and observed health are different concepts and should be modelled separately.

## Why this matters

The scheduler and policy engine need to know both:

1. whether the runner is allowed to receive work
2. whether the runner appears healthy enough to receive work

If observations can override admin intent, the Gateway may schedule jobs onto a runner that was intentionally taken out of service.

## Expected behaviour

Runner state should be split into two fields.

Example:

```text
admin_status:
- online
- offline
- maintenance

health_status:
- healthy
- degraded
- blocked_disk_pressure
- stale
- unknown
```

Scheduling should require both:

```text
admin_status == online
AND health_status is acceptable
```

Observations should update health status only. Operator actions should update admin status only.

## Suggested implementation

- Add separate `admin_status` and `health_status` columns to runners.
- Migrate existing `status` data safely.
- Update observation ingestion so it cannot overwrite admin status.
- Update scheduler policy to evaluate both statuses.
- Update API responses and tests.
- Add explicit endpoint/action for changing admin status.

## Acceptance criteria

- [ ] Runner admin state is separate from observed health state.
- [ ] Observations cannot move a runner out of `offline` or `maintenance`.
- [ ] Scheduler respects admin status.
- [ ] Scheduler respects observed health status.
- [ ] Tests cover offline runner receiving observations.
- [ ] Tests cover maintenance runner receiving observations.
- [ ] Tests cover healthy online runner being eligible for jobs.
- [ ] API response clearly exposes both statuses.

## Files likely affected

- `app/core/models/runner.py`
- `app/policy/observation.py`
- `app/api/runners.py`
- `app/core/scheduler.py`
- `alembic/versions/`
- `tests/test_policy_*.py`
- `tests/test_runners.py`
```

---

## Issue 6 — Replace fake job events for runner status changes with system/runner events

```markdown
## Problem

Runner status changes are being recorded in `job_events` using a fake zero UUID as the job ID.

This is incorrect because `job_events.job_id` is intended to reference a real Gateway job. If there is a foreign key to `gateway_jobs(id)`, inserting a fake job ID can fail or corrupt the event model.

Runner lifecycle events should not be forced into job lifecycle events.

## Why this matters

Events are the audit trail of the Gateway. Mixing system events, runner events, and job events makes the audit trail confusing and can break referential integrity.

## Expected behaviour

Job events should only describe job-related lifecycle changes.

Runner/system events should have their own event model.

Example tables:

```text
job_events
runner_events
system_events
```

Runner status changes should be recorded in `runner_events`.

## Suggested implementation

- Add a `runner_events` table.
- Move runner status-change logging out of `job_events`.
- Add event fields such as:
  - runner_id
  - event_type
  - old_status
  - new_status
  - reason
  - created_at
  - metadata
- Update runner API to write to `runner_events`.
- Add tests proving runner status changes do not write fake job events.
- Consider a generic `system_events` table only if needed.

## Acceptance criteria

- [ ] Runner status changes no longer write to `job_events`.
- [ ] No fake zero UUID job IDs are used.
- [ ] `job_events` only references real jobs.
- [ ] Runner status changes are recorded in `runner_events`.
- [ ] Database constraints remain valid.
- [ ] Tests cover runner status update event creation.
- [ ] Tests cover foreign key integrity.

## Files likely affected

- `app/api/runners.py`
- `app/core/models/`
- `app/db/schema.sql`
- `alembic/versions/`
- `tests/test_runners.py`
- `tests/test_events.py`
```

---

## Issue 7 — Complete workspace cleanup state transitions

```markdown
## Problem

Workspace cleanup can move a workspace into a cleaning state, but successful cleanup does not clearly transition the workspace to a final cleaned state.

This makes cleanup hard to reason about and weakens idempotency. The scheduler may not know whether cleanup completed, failed, or is still in progress.

## Why this matters

The Gateway is responsible for tracking workspace lifecycle. If cleanup status is ambiguous, stale workspaces and OpenCode sessions may remain around longer than expected.

This matters especially for long-running AFK/OpenCode agents on VMs.

## Expected behaviour

Workspace cleanup should have explicit lifecycle transitions.

Example:

```text
active
→ cleaning
→ cleaned
```

Failure flow:

```text
active
→ cleaning
→ cleanup_failed
```

Repeated cleanup should be safe and idempotent.

## Suggested implementation

- Add explicit cleanup terminal states.
- Update cleanup endpoint/service to write final state after executor cleanup succeeds.
- Capture cleanup failure reason.
- Store cleanup timestamps:
  - cleanup_started_at
  - cleanup_completed_at
  - cleanup_failed_at
- Add retry count if scheduler will retry cleanup.
- Ensure cleanup does not break if the workspace is already cleaned.

## Acceptance criteria

- [ ] Successful cleanup transitions workspace to `cleaned`.
- [ ] Failed cleanup transitions workspace to `cleanup_failed`.
- [ ] Cleanup failure reason is stored.
- [ ] Cleanup timestamps are stored.
- [ ] Cleanup is idempotent.
- [ ] Scheduler can distinguish active, cleaning, cleaned, and failed cleanup states.
- [ ] Tests cover successful cleanup.
- [ ] Tests cover failed cleanup.
- [ ] Tests cover repeated cleanup call.

## Files likely affected

- `app/api/workspaces.py`
- `app/core/models/workspace.py`
- `app/core/scheduler.py`
- `app/executors/`
- `alembic/versions/`
- `tests/test_workspaces.py`
- `tests/test_cleanup.py`
```

---

## Issue 8 — Harden production authentication defaults

```markdown
## Problem

The API key middleware allows requests through when no Gateway API key is configured.

This is convenient for local development, but risky for production. If the Gateway is accidentally exposed without an API key, it could accept unauthenticated job creation or lifecycle operations.

## Why this matters

The Gateway can trigger AWX/OpenCode automation. Even if it does not hold VM/SSH infra secrets directly, it is still a powerful control-plane service.

Production should fail closed, not open.

## Expected behaviour

Authentication should be required by default outside local development.

Suggested behaviour:

```text
GATEWAY_ENV=dev
→ API key may be optional

GATEWAY_ENV=prod
→ API key required

GATEWAY_ALLOW_INSECURE_AUTH=true
→ explicitly allow no API key, with warning
```

## Suggested implementation

- Add an explicit environment setting such as `GATEWAY_ENV`.
- Require `GATEWAY_API_KEY` unless dev mode or explicit insecure mode is enabled.
- Log a clear warning when insecure auth is enabled.
- Use constant-time comparison for API key checks.
- Add tests for:
  - missing API key in prod fails startup
  - missing API key in dev is allowed
  - invalid API key is rejected
  - valid API key is accepted
  - insecure mode requires explicit opt-in

## Acceptance criteria

- [ ] Production mode requires an API key.
- [ ] Missing API key in production fails fast.
- [ ] Development mode can run without an API key.
- [ ] Insecure auth requires explicit opt-in.
- [ ] API key comparison uses constant-time comparison.
- [ ] Tests cover all auth modes.
- [ ] Documentation explains local vs production auth behaviour.

## Files likely affected

- `app/core/auth.py`
- `app/core/config.py`
- `app/core/factory.py`
- `README.md`
- `tests/test_auth.py`
- `tests/test_config.py`
```

---

## Issue 9 — Redact and control secret-like environment variables

```markdown
## Problem

Executor requests can include environment variables, and the local executor may log them.

This creates a potential secret leak if users pass tokens, API keys, passwords, or credentials through `env_vars`.

Even if the Gateway should not hold infrastructure secrets directly, it still needs safe handling for user-provided job metadata.

## Related ADR

- ADR 0004 — Gateway Holds No Infrastructure Secrets

## Why this matters

Logs are often shipped to central systems and retained. If secrets are written to logs, they may be exposed beyond the runtime environment.

The Gateway should not accidentally become a secret disclosure point.

## Expected behaviour

Secret-like values should not be logged.

The Gateway should either:

- reject secret-like `env_vars`, or
- allow them only through an explicit safe secret reference mechanism, or
- redact them everywhere before logging/events.

## Suggested implementation

- Add a redaction helper for keys matching patterns like:
  - `TOKEN`
  - `PASSWORD`
  - `SECRET`
  - `KEY`
  - `CREDENTIAL`
  - `AUTH`
- Redact values in logs, job events, error messages, and executor debug output.
- Consider allowing only a safe allowlist of env vars.
- Consider changing executor input to support secret references rather than raw secret values.
- Add tests for redaction.

## Acceptance criteria

- [ ] Secret-like env var values are never logged in plaintext.
- [ ] Secret-like env var values are never written to job events in plaintext.
- [ ] Executor logs redact secret-like values.
- [ ] Tests cover common secret key names.
- [ ] Tests cover nested metadata redaction if metadata is logged.
- [ ] Documentation states how secrets should be passed safely.
- [ ] ADR 0004 remains true in practical operation, not just in principle.

## Files likely affected

- `app/executors/local/plugin.py`
- `app/executors/awx/plugin.py`
- `app/api/jobs.py`
- `app/core/logging.py`, if present
- `app/core/config.py`
- `tests/test_security.py`
- `tests/test_executors.py`
```

---

## Issue 10 — Fail hard when AWX artifacts are missing or malformed

```markdown
## Problem

The AWX executor currently appears to tolerate missing or malformed AWX artifact output by falling back to placeholder values such as a zero UUID.

That is unsafe. If AWX does not return the expected workspace ID, path, OpenCode session ID, or other lifecycle metadata, the Gateway should treat the operation as failed.

## Why this matters

The Gateway should not create fake lifecycle state. If AWX fails to produce a real workspace/session artifact, the job cannot be trusted.

A placeholder UUID may cause later operations to target nonexistent resources or create misleading audit history.

## Expected behaviour

AWX artifact parsing should be strict.

If required artifact fields are missing or invalid:

- mark the job/workspace operation as failed
- store a clear failure reason
- emit a job event
- do not continue to the next lifecycle step

## Suggested implementation

- Define required AWX artifact schemas for each AWX job template:
  - create workspace
  - start OpenCode
  - stop/restart OpenCode
  - cleanup workspace
- Validate artifacts before returning from executor methods.
- Replace placeholder fallback values with explicit exceptions.
- Add tests for:
  - valid artifacts
  - missing workspace ID
  - invalid UUID
  - missing workspace path
  - missing OpenCode session ID
  - AWX job failed
  - AWX job succeeded but artifact invalid

## Acceptance criteria

- [ ] AWX executor does not return placeholder UUIDs.
- [ ] Missing required artifacts cause a clear failure.
- [ ] Invalid artifact values cause a clear failure.
- [ ] Job lifecycle records the failure.
- [ ] Tests cover malformed AWX artifacts.
- [ ] Tests cover missing AWX artifacts.
- [ ] Documentation defines expected AWX artifact format.

## Files likely affected

- `app/executors/awx/plugin.py`
- `app/executors/awx/client.py`
- `app/api/jobs.py`
- `app/api/workspaces.py`
- `tests/test_awx_*.py`
- `README.md`
```

---

## Issue 11 — Add end-to-end lifecycle integration test for Gateway → AWX → OpenCode

```markdown
## Problem

The codebase has useful unit-level tests, but it needs a stronger lifecycle test that proves the intended control-plane flow works from beginning to end.

The critical path is:

```text
submit job
→ allocate workspace
→ allocate port
→ call AWX create workspace
→ call AWX start OpenCode
→ record OpenCode session
→ observe/update job status
→ await review or complete
→ cleanup workspace
```

## Why this matters

Most of the architectural risk is in the seams between components, not inside one function.

The system can have passing unit tests while still failing the real workflow because of missing dependencies, missing artifacts, incorrect state transitions, or schema drift.

## Expected behaviour

There should be at least one integration-style test that exercises the full happy path using mocked AWX/OpenCode responses.

There should also be failure-path tests for the major lifecycle edges.

## Suggested implementation

Create an integration test suite with fake/mocked services:

- fake AWX client
- fake OpenCode client
- test Postgres or transaction-isolated database
- deterministic artifact responses

Test the full job lifecycle.

## Acceptance criteria

- [ ] Integration test covers successful job submission through OpenCode start.
- [ ] Integration test verifies workspace is created.
- [ ] Integration test verifies port is allocated and persisted.
- [ ] Integration test verifies AWX receives expected variables.
- [ ] Integration test verifies OpenCode session/result is recorded.
- [ ] Integration test verifies job does not complete prematurely.
- [ ] Integration test covers cleanup.
- [ ] Failure-path tests cover AWX failure.
- [ ] Failure-path tests cover malformed AWX artifact.
- [ ] Failure-path tests cover OpenCode startup failure.

## Files likely affected

- `tests/integration/`
- `tests/conftest.py`
- `app/api/jobs.py`
- `app/api/workspaces.py`
- `app/executors/awx/`
- `app/opencode/`
```

---
