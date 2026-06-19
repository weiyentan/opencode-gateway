# OpenCode Gateway Follow-up Review Issues

This document captures the remaining issues after the second review of `weiyentan/opencode-gateway`.

The previous implementation batch fixed a lot of the foundation, especially around AWX wiring, auth hardening, cleanup states, redaction, and strict AWX artifact validation.

However, several issues should remain open or be reopened because the implementation is not yet fully aligned with the ADRs and intended workflow.

## Summary

### Mostly fixed

- AWX executor dependency construction
- Cleanup state transitions
- Production auth defaults
- Secret redaction
- Strict AWX artifact validation

### Still needs work

- Job lifecycle still marks jobs complete too early
- Port allocation is not atomic with persistence
- Runner admin/health split exists but scheduling and policy still bypass parts of it
- Schema validation should include all production tables
- Integration tests exist but need to prove the real lifecycle behaviour

---

# Issue 1 — Fix job lifecycle so OpenCode startup does not equal job completion

## Problem

The Gateway still marks a job as `completed` too early.

The current lifecycle appears to do this:

```text
create job
→ create workspace
→ start OpenCode
→ store OpenCode session ID
→ mark job completed
```

Starting OpenCode is not the same thing as completing the coding task.

A job should only be completed after the Gateway has evidence that OpenCode reached a verified terminal state.

Examples of valid terminal evidence include:

- branch pushed
- commit SHA recorded
- diff generated and fetched successfully
- PR/MR created
- OpenCode returned a terminal result
- human review state reached
- task failed
- task aborted

## Why this matters

The intended architecture is:

```text
AWX → OpenCode develop-loop → branch/MR → human review
```

If the Gateway marks jobs complete immediately after OpenCode starts, the audit trail becomes misleading.

The system may report success even though:

- OpenCode is still running
- no branch was pushed
- no PR/MR exists
- no diff was produced
- the coding task failed later
- the human review step was never reached

## Expected behaviour

Job lifecycle should move through explicit states.

Suggested happy path:

```text
pending
→ provisioning_workspace
→ starting_opencode
→ running
→ awaiting_review
→ completed
```

Suggested failure path:

```text
pending
→ provisioning_workspace
→ failed
```

Suggested abort path:

```text
running
→ aborting
→ aborted
```

## Suggested implementation

- Do not mark a job `completed` from the initial `create_job` request.
- Treat successful OpenCode startup as `running`, not `completed`.
- Add a separate endpoint, webhook, poller, or observation path for OpenCode terminal results.
- Only transition to `completed` when a verified output exists.
- Store useful terminal metadata:
  - OpenCode session ID
  - branch name
  - commit SHA
  - PR/MR URL
  - diff summary
  - final task summary
  - failure reason, if failed
- Add job events for every lifecycle transition.

## Acceptance criteria

- [ ] Job is not marked `completed` immediately after OpenCode startup.
- [ ] Successful OpenCode startup moves the job to `running`.
- [ ] Completion requires a verified terminal signal.
- [ ] Failed OpenCode startup marks the job `failed`.
- [ ] Failed AWX/executor calls mark the job `failed`.
- [ ] Missing terminal result does not mark the job `completed`.
- [ ] Job events record every lifecycle transition.
- [ ] Completion metadata is persisted.
- [ ] Tests cover the happy path.
- [ ] Tests cover OpenCode startup failure.
- [ ] Tests cover missing or invalid terminal result.
- [ ] Tests prove OpenCode startup alone does not complete the job.

## Files likely affected

- `app/api/jobs.py`
- `app/core/models/job.py`
- `app/opencode/`
- `app/executors/`
- `app/core/scheduler.py`
- `tests/test_jobs.py`
- `tests/test_opencode_*.py`
- `tests/integration/`

---

# Issue 2 — Make port allocation and persistence atomic

## Problem

Port allocation has improved, but it still has a race condition.

The current implementation appears to:

```text
acquire advisory lock
→ find unused port
→ return selected port
→ release advisory lock
→ caller later persists the port
```

This leaves a gap between selecting the port and saving it.

Two concurrent requests can still do this:

```text
request A selects port 10001
request A releases lock before persisting

request B selects port 10001
request B releases lock before persisting

both try to use port 10001
```

## Related ADR

ADR 0003 — Postgres Port Allocation

## Why this matters

OpenCode Serve sessions require unique ports.

The Gateway is supposed to be the source of truth for port allocation. If two sessions can receive the same port, concurrent OpenCode runs can collide or become unreachable.

## Expected behaviour

Port allocation and persistence should happen in the same locked transaction.

The Gateway should guarantee:

- no duplicate active ports
- no ports outside `10000–10999`
- no race between allocation and persistence
- database-level protection even if application logic has a bug

## Suggested implementation

Use an atomic allocation function.

Example:

```text
allocate_and_assign_port(conn, workspace_id)
→ begin transaction
→ acquire advisory transaction lock
→ find available port
→ update workspace with selected port
→ commit transaction
→ return selected port
```

Also add a database-level constraint.

Suggested constraints:

```sql
CHECK (port IS NULL OR port BETWEEN 10000 AND 10999)
```

Suggested uniqueness rule:

```sql
UNIQUE(port)
WHERE port IS NOT NULL
AND cleanup_status NOT IN ('cleaned')
```

If partial unique indexes are awkward, use an equivalent active-port table or allocation table.

## Acceptance criteria

- [ ] Port selection and persistence happen under the same lock.
- [ ] The advisory lock is not released before the port is persisted.
- [ ] Database constraint prevents ports outside `10000–10999`.
- [ ] Database uniqueness prevents duplicate active ports.
- [ ] Concurrent job/workspace creation cannot allocate the same port.
- [ ] Port allocation failure is handled clearly when the pool is exhausted.
- [ ] Cleanup releases or makes ports reusable according to the intended lifecycle.
- [ ] AWX receives the allocated port when starting OpenCode.
- [ ] Tests cover concurrent allocation.
- [ ] Tests cover port exhaustion.
- [ ] Tests cover port reuse after cleanup, if intended.
- [ ] Tests cover duplicate-port protection at the database layer.

## Files likely affected

- `app/core/ports.py`
- `app/api/workspaces.py`
- `app/api/jobs.py`
- `app/executors/awx/plugin.py`
- `app/core/models/`
- `alembic/versions/`
- `tests/test_ports.py`
- `tests/test_workspaces.py`
- `tests/integration/`

---

# Issue 3 — Fix runner scheduling so admin status and health status are both enforced

## Problem

The runner model now has separate `admin_status` and `health_status`, which is good.

However, the policy and scheduling logic do not fully enforce both fields.

The policy appears to allow a runner early when:

```text
admin_status == ONLINE
```

That bypasses parts of the observed health, freshness, disk pressure, and memory pressure checks.

Some job selection logic may also still use the older `status = 'HEALTHY'` field instead of the newer split model.

## Why this matters

Administrative state and observed health are separate concepts.

The Gateway needs to know:

1. whether an operator allows the runner to receive work
2. whether the runner is currently healthy enough to receive work

A runner should only receive work if both are true.

## Expected behaviour

Scheduling should require:

```text
admin_status == online
AND health_status is acceptable
AND latest observation is fresh
AND disk pressure is acceptable
AND memory pressure is acceptable
AND capacity is available
```

Examples:

```text
admin_status = maintenance
health_status = healthy
→ runner is not eligible
```

```text
admin_status = online
health_status = stale
→ runner is not eligible
```

```text
admin_status = online
health_status = healthy
latest observation too old
→ runner is not eligible
```

## Suggested implementation

- Update policy so `admin_status == online` does not bypass health checks.
- Use `admin_status` and `health_status` consistently in runner selection queries.
- Stop using the legacy `status` field for scheduling decisions.
- Consider deprecating or removing the old `status` field once migration is complete.
- Ensure observations only update `health_status`, not `admin_status`.
- Ensure operator actions only update `admin_status`, not `health_status`.

Suggested runner eligibility rule:

```sql
WHERE COALESCE(r.admin_status, 'online') = 'online'
AND COALESCE(r.health_status, 'unknown') = 'healthy'
```

Then apply deeper policy checks after candidate selection.

## Acceptance criteria

- [ ] Runner scheduling checks `admin_status`.
- [ ] Runner scheduling checks `health_status`.
- [ ] `admin_status = offline` prevents scheduling.
- [ ] `admin_status = maintenance` prevents scheduling.
- [ ] `health_status != healthy` prevents scheduling unless explicitly allowed.
- [ ] Stale observations prevent scheduling.
- [ ] Disk pressure prevents scheduling.
- [ ] Memory pressure prevents scheduling.
- [ ] Observations cannot change admin status.
- [ ] Operator status changes cannot fake observed health.
- [ ] Legacy `status` is not used for scheduling decisions.
- [ ] Tests cover online and healthy runner eligibility.
- [ ] Tests cover online but unhealthy runner rejection.
- [ ] Tests cover maintenance but healthy runner rejection.
- [ ] Tests cover stale runner rejection.

## Files likely affected

- `app/policy/observation.py`
- `app/api/jobs.py`
- `app/api/runners.py`
- `app/core/scheduler.py`
- `app/core/models/runner.py`
- `tests/test_policy_*.py`
- `tests/test_runners.py`
- `tests/test_jobs.py`
- `tests/integration/`

---

# Issue 4 — Add runner_events and webhooks to schema validation

## Problem

The schema validation/checking logic verifies a list of required tables, but it may not include all tables that current production code depends on.

In particular, the current implementation should verify tables such as:

- `runner_events`
- `webhooks`

If production code writes to these tables but startup validation does not check them, the app can start successfully and then fail later at runtime.

## Why this matters

The Gateway is a stateful control plane. It should fail fast if the database schema is incomplete.

Silent schema drift is dangerous because it turns deployment mistakes into runtime failures.

## Expected behaviour

Startup schema validation should include every required production table.

At minimum, required tables should include:

```text
gateway_jobs
workspaces
job_events
approvals
runners
runner_events
runner_observations
workspace_observations
opencode_instance_observations
webhooks
```

## Suggested implementation

- Update the required table list used by schema validation.
- Include `runner_events`.
- Include `webhooks`, if production code still depends on it.
- Add tests that fail startup when each required table is missing.
- Ensure Alembic migrations create all required tables.

## Acceptance criteria

- [ ] Schema validation includes `runner_events`.
- [ ] Schema validation includes `webhooks`, if still used.
- [ ] Fresh Alembic migration creates all required production tables.
- [ ] Startup fails clearly if `runner_events` is missing.
- [ ] Startup fails clearly if `webhooks` is missing and still required.
- [ ] Tests cover missing required table detection.
- [ ] Documentation explains that Alembic is the production schema path.

## Files likely affected

- `app/db/schema.py`
- `app/db/setup.py`
- `alembic/versions/`
- `tests/test_schema.py`
- `tests/integration/test_schema_*.py`

---

# Issue 5 — Verify lifecycle integration tests prove the real control-plane behaviour

## Problem

Integration tests now exist, but they need to prove the actual desired lifecycle behaviour.

The most important behaviour to test is that OpenCode startup does not equal job completion.

The integration suite should prove the intended control-plane path:

```text
submit job
→ allocate workspace
→ allocate port atomically
→ call AWX create workspace
→ call AWX start OpenCode
→ record OpenCode session
→ job remains running or awaiting review
→ terminal OpenCode result arrives
→ job reaches awaiting_review or completed
→ cleanup workspace
```

## Why this matters

The highest-risk parts of this project are the seams:

- Gateway to AWX
- AWX artifact parsing
- workspace creation
- port allocation
- OpenCode session tracking
- job lifecycle transitions
- cleanup

Unit tests can pass while the real workflow remains broken.

## Expected behaviour

Integration tests should prove:

- successful lifecycle
- failure lifecycle
- no premature completion
- atomic port allocation
- strict AWX artifact validation
- runner eligibility
- cleanup terminal states

## Suggested implementation

Create integration tests using fake/mocked external services:

- fake AWX client
- fake OpenCode client
- test database
- deterministic AWX artifact responses
- deterministic OpenCode terminal result

Test both the happy path and major failure paths.

## Acceptance criteria

- [ ] Integration test covers successful job submission through OpenCode startup.
- [ ] Integration test verifies workspace creation.
- [ ] Integration test verifies port allocation and persistence.
- [ ] Integration test verifies AWX receives the allocated port.
- [ ] Integration test verifies OpenCode session ID is stored.
- [ ] Integration test proves job is not completed immediately after OpenCode startup.
- [ ] Integration test covers terminal OpenCode result.
- [ ] Integration test covers transition to `awaiting_review`.
- [ ] Integration test covers transition to `completed`, if completion is allowed before human review.
- [ ] Integration test covers AWX failure.
- [ ] Integration test covers malformed AWX artifacts.
- [ ] Integration test covers OpenCode startup failure.
- [ ] Integration test covers cleanup success.
- [ ] Integration test covers cleanup failure.

## Files likely affected

- `tests/integration/`
- `tests/conftest.py`
- `app/api/jobs.py`
- `app/api/workspaces.py`
- `app/executors/awx/`
- `app/opencode/`
- `app/core/ports.py`
- `app/core/scheduler.py`

---

# Suggested implementation order

## Batch 1 — Control-plane correctness

1. Fix job lifecycle so OpenCode startup does not equal completion.
2. Make port allocation and persistence atomic.
3. Fix runner scheduling to enforce both admin and health state.

## Batch 2 — Schema and test confidence

4. Add `runner_events` and `webhooks` to schema validation.
5. Strengthen integration tests around the real lifecycle.

## Recommended closing rule

Do not close the original implementation batch until these are true:

- job completion requires verified terminal output
- concurrent port allocation cannot collide
- scheduler uses `admin_status` and `health_status` correctly
- startup validation covers every production table
- integration tests prove the full lifecycle
