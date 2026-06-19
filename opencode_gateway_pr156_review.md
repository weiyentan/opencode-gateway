# OpenCode Gateway PR #156 Review

Review target:

- Branch: `ai/develop-loop/issue-slices-consolidated-2026-06-19`
- PR: `https://github.com/weiyentan/opencode-gateway/pull/156`

## Summary verdict

**Do not merge PR #156 yet.**

The PR implements a lot of the shape of the requested fixes, but the two biggest behavioural problems are still present in the actual job path:

1. Jobs are still marked `completed` too early.
2. Atomic port allocation exists, but `create_job()` does not use it.

There are also important test and runner-scheduling issues that should be fixed before merging.

---

## Review scorecard

| Area | Status | Review |
|---|---:|---|
| Schema validation | Mostly fixed | `runner_events` and `webhooks` are now included in required table checks. |
| Port DB constraints | Mostly fixed | CHECK constraint and partial unique index exist. |
| Atomic port allocation | Not wired correctly | `allocate_and_assign_port()` exists, but `create_job()` still uses `allocate_port()` and then updates the workspace separately. |
| Job lifecycle | Still broken | `create_job()` still marks jobs `completed` immediately after OpenCode starts. |
| Runner admin/health | Partially fixed | Selection uses `admin_status` and `health_status`, but observed runners may never become eligible unless manually admitted. |
| Integration tests | Not trustworthy yet | Some tests still assert the old `completed after POST /jobs` behaviour. |
| AWX port handoff | Mostly fixed | Port is passed to `StartOpencodeRequest`, but the returned AWX port should be checked against the allocated DB port. |

---

# Blocking issue 1 — Job lifecycle still completes too early

## Problem

The job lifecycle still treats successful OpenCode startup as job completion.

The current flow appears to be:

```text
create job
→ create workspace
→ allocate port
→ start OpenCode
→ store OpenCode session ID
→ mark job completed
→ fire job.completed webhook
→ fetch diff after completion
```

This is still wrong.

OpenCode startup means the job is running. It does **not** mean the coding task is complete.

## Why this matters

The intended architecture is:

```text
AWX → OpenCode develop-loop → branch/MR → human review
```

If `POST /jobs` marks a job complete immediately after OpenCode starts, the Gateway can report success even when:

- OpenCode is still running
- no branch was pushed
- no PR/MR exists
- no diff was produced
- the coding task failed later
- the human review step was never reached

This breaks the control-plane audit trail.

## Required behaviour

`POST /jobs` should stop at:

```text
pending
→ provisioning_workspace
→ starting_opencode
→ running
```

A later terminal signal should move the job to:

```text
running
→ awaiting_review
```

or:

```text
running
→ failed
```

or, only when intentionally allowed:

```text
awaiting_review
→ completed
```

## Required fix

Remove the immediate “mark completed” block from `create_job()`.

Do not set:

- `status = completed`
- `completed_at`
- fake completion summary
- `job.completed` webhook

inside the initial job creation request.

## Acceptance criteria

- [ ] `POST /jobs` returns a job in `running` state after successful OpenCode startup.
- [ ] `completed_at` remains `NULL` after initial OpenCode startup.
- [ ] `job.completed` webhook is not fired from the initial `POST /jobs`.
- [ ] Completion only happens through a terminal result path, such as `/jobs/{id}/complete`, webhook, poller, or explicit finalization.
- [ ] Job events show lifecycle transitions.
- [ ] Tests prove OpenCode startup alone does not complete the job.

## Files likely affected

- `app/api/jobs.py`
- `app/core/models/job.py`
- `app/opencode/`
- `tests/test_jobs.py`
- `tests/integration/`

---

# Blocking issue 2 — Atomic port allocation exists but is not used by `create_job()`

## Problem

The branch now has an atomic helper:

```text
allocate_and_assign_port()
```

That is the right idea.

However, `create_job()` still appears to use the old split path:

```text
allocated_port = allocate_port()
→ later UPDATE workspaces SET port = allocated_port
```

This reintroduces the race between selecting a port and persisting it.

## Why this matters

Two concurrent jobs can still do this:

```text
request A selects port 10001
request A releases lock before persisting

request B selects port 10001
request B releases lock before persisting

both try to use port 10001
```

The database constraint helps, but the hot path should still use the atomic helper. The application should not rely on database conflicts as normal control flow.

## Required behaviour

Port allocation and workspace assignment should happen in one locked operation.

Expected flow:

```text
allocate_and_assign_port(conn, workspace_id)
→ acquire advisory transaction lock
→ find available port
→ update workspace with selected port
→ return selected port
```

## Required fix

Change `create_job()` from this style:

```python
allocated_port = await allocate_port(conn)

await conn.execute(
    "UPDATE workspaces SET port = $2 WHERE id = $1",
    workspace_id,
    allocated_port,
)
```

to this style:

```python
allocated_port = await allocate_and_assign_port(conn, workspace_id)
```

## Acceptance criteria

- [ ] `create_job()` uses `allocate_and_assign_port()`.
- [ ] `create_job()` does not call `allocate_port()` and then separately persist the port.
- [ ] Port selection and persistence happen under the same lock.
- [ ] DB CHECK constraint for `10000–10999` remains.
- [ ] Partial unique index for active workspace ports remains.
- [ ] Tests cover concurrent job creation or concurrent port allocation.
- [ ] Tests cover duplicate-port prevention.

## Files likely affected

- `app/api/jobs.py`
- `app/core/ports.py`
- `tests/test_ports.py`
- `tests/integration/`

---

# Blocking issue 3 — Integration tests are proving the wrong lifecycle

## Problem

Some integration tests still assert that `POST /jobs` results in a `completed` job.

That is the old behaviour and should no longer be true.

A test that claims “no premature completion” but still expects the initial `POST /jobs` response to be `completed` is proving the wrong thing.

## Correct expectation

The initial job creation request should result in:

```text
status = running
completed_at = NULL
```

Then a second action should simulate terminal completion:

```text
POST /jobs/{job_id}/complete
```

or a webhook/poller result.

That terminal path can then move the job to:

```text
awaiting_review
```

or:

```text
completed
```

depending on the intended workflow.

## Required fix

Update tests so they enforce the new lifecycle.

## Acceptance criteria

- [ ] No integration test expects `POST /jobs` to return `completed`.
- [ ] Tests assert that `POST /jobs` returns `running`.
- [ ] Tests assert that `completed_at` is `NULL` after OpenCode startup.
- [ ] Tests simulate a terminal result separately.
- [ ] Tests prove `running → awaiting_review`.
- [ ] Tests prove `awaiting_review → completed`, if that is the desired final path.
- [ ] Tests cover failure paths.

## Files likely affected

- `tests/integration/test_happy_path_lifecycle.py`
- `tests/integration/test_completion_cleanup_integration.py`
- `tests/test_jobs.py`

---

# High issue 4 — Runner test helpers are incompatible with the new admin/health model

## Problem

The scheduler now expects runners to have:

```text
admin_status = online
health_status = HEALTHY
```

However, some test helpers appear to create runners using only the legacy `status` field.

That means tests may not be exercising the real scheduling model.

## Required fix

Update runner test helpers to set all relevant runner status fields.

Example:

```sql
INSERT INTO runners (
  id,
  runner_id,
  hostname,
  executor_type,
  labels,
  status,
  admin_status,
  health_status,
  created_at,
  updated_at
)
VALUES (
  ...,
  'HEALTHY',
  'online',
  'HEALTHY',
  ...
)
```

## Acceptance criteria

- [ ] Test helpers set `admin_status`.
- [ ] Test helpers set `health_status`.
- [ ] Tests no longer rely only on legacy `status`.
- [ ] Tests cover eligible runner: `admin_status=online`, `health_status=HEALTHY`.
- [ ] Tests cover rejected runner: `admin_status=maintenance`, `health_status=HEALTHY`.
- [ ] Tests cover rejected runner: `admin_status=online`, `health_status=UNKNOWN`.

## Files likely affected

- `tests/integration/conftest.py`
- `tests/test_runners.py`
- `tests/test_jobs.py`
- `tests/integration/`

---

# High issue 5 — Decide runner auto-admission behaviour

## Problem

Observation ingestion creates or updates observed runner health, but new observed runners may not get `admin_status = online`.

If scheduler requires:

```text
admin_status = online
AND health_status = HEALTHY
```

then a newly observed runner may never be schedulable unless a separate manual admin step happens.

This may be correct, but it needs to be explicit.

## Design decision required

Choose one option.

### Option A — Manual admission control

New observed runners default to:

```text
admin_status = NULL
health_status = HEALTHY
not eligible for jobs
```

Then an operator must explicitly admit the runner:

```text
POST /runners/{id}/admin-status online
```

This is safer.

### Option B — Auto-admit healthy observed runners

New observed runners default to:

```text
admin_status = online
health_status = HEALTHY
eligible for jobs
```

Observation ingestion must still never overwrite `admin_status` on existing runners.

This is more automatic.

## Recommendation

Use **Option A** for safety, especially for AFK/AWX workflows.

But document it and test it.

## Acceptance criteria

- [ ] Runner admission behaviour is explicitly chosen.
- [ ] Documentation explains the behaviour.
- [ ] Tests cover new observed runner eligibility or non-eligibility.
- [ ] Observations never overwrite admin status on existing runners.
- [ ] Scheduler behaviour matches the documented design.

## Files likely affected

- `app/api/observations.py`
- `app/api/runners.py`
- `app/api/jobs.py`
- `README.md`
- `tests/test_observations.py`
- `tests/test_runners.py`
- `tests/integration/`

---

# Medium issue 6 — Verify AWX returned port matches Gateway allocated port

## Problem

The Gateway allocates a port and passes it to AWX when starting OpenCode.

The AWX plugin validates that AWX returned a port, which is good.

However, the job path should also verify that the returned AWX port matches the port allocated by the Gateway.

## Why this matters

If AWX starts OpenCode on a different port than the Gateway allocated, the database and actual runtime will disagree.

That can cause debugging problems such as:

- Gateway health checks hitting the wrong port
- cleanup targeting the wrong OpenCode session
- UI linking to the wrong session
- multiple sessions colliding

## Required behaviour

After `start_opencode()` returns:

```python
if start_response.port != allocated_port:
    mark job failed
    emit job event
    cleanup workspace
    raise HTTPException(...)
```

## Acceptance criteria

- [ ] Gateway compares AWX returned port with allocated port.
- [ ] Mismatched port marks the job failed.
- [ ] Mismatched port emits a job event.
- [ ] Mismatched port does not continue the lifecycle.
- [ ] Tests cover AWX returning a mismatched port.

## Files likely affected

- `app/api/jobs.py`
- `app/executors/awx/plugin.py`
- `tests/test_awx_*.py`
- `tests/test_jobs.py`
- `tests/integration/`

---

# Good fixes already present in the PR

These parts look like meaningful progress and should be preserved.

## Schema validation

The required table list now includes:

```text
runner_events
webhooks
```

alongside the core job, workspace, runner, and observation tables.

## Port database constraints

The branch includes:

- CHECK constraint for port range `10000–10999`
- partial unique index for active workspace ports

These should stay.

## Strict AWX artifact validation

The AWX plugin no longer appears to fall back to fake zero UUIDs or fake ports.

It validates artifacts for:

- create workspace
- start OpenCode
- collect state

This should stay.

## Runner events and admin endpoint

The runner API now writes runner status changes to `runner_events`.

There is also a dedicated admin-status path that updates only `admin_status`.

This should stay.

---

# Required fixes before merge

```markdown
## Required fixes before merge

1. Remove immediate job completion from `POST /jobs`.
   - `POST /jobs` should stop at `running`.
   - `completed_at` must remain NULL.
   - `diff` should not be a fake "Job completed" summary.

2. Use `allocate_and_assign_port()` in `create_job()`.
   - Do not call `allocate_port()` plus separate `UPDATE workspaces`.
   - Keep DB CHECK and partial unique index.

3. Fix integration tests.
   - Tests must assert that `POST /jobs` returns `running`, not `completed`.
   - Completion must happen through `/jobs/{id}/complete` or a simulated terminal webhook.
   - Test helpers must set `admin_status='online'` and `health_status='HEALTHY'`.

4. Decide runner auto-admission behaviour.
   - Either observed runners require manual `/admin-status online`, or new observed runners are auto-admitted.
   - Document and test whichever behaviour is chosen.

5. Verify AWX returned port equals allocated port.
   - Fail the job if AWX reports a different port.
```

---

# Merge recommendation

Do **not** merge PR #156 yet.

The PR has strong scaffolding, but the hot path still has the two original core problems:

```text
OpenCode startup still equals job completion.
Port allocation still uses a split allocation/persistence path.
```

Once those are fixed, the PR will be much closer to merge-ready.
