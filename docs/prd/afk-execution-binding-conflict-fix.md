# PRD: Fix AFK Execution Binding Conflict on Multi-Stage Runs

## Problem Statement

Legitimate AWX review and fix executions are rejected by the Gateway with HTTP 409 Conflict when they share the same `afk_run_id` as a completed development execution. This causes the MR Review Runner (template 222) and MR Develop Runner (template 224) to silently fail, suppressing terminal reporting and leaving the AFK observability view incomplete.

The root cause is that the Gateway closes the parent AFK Run as soon as its first successful AWX execution completes. The EDA rulebook correctly reuses the same `afk_run_id` for the full develop → review → fix sequence, but the Gateway's ADR 0027 rule rejects new bindings for a projected-completed run.

The intended model is:

- A **change_request** (PR/MR) anchors one **AFK Run**.
- One AFK Run has one **AWX Execution Binding** containing many uniquely identified AWX jobs.
- AWX execution outcomes are historical child facts and never close or reopen the AFK Run.
- The AFK Run remains open while its change request is open and becomes completed only when that change request is merged.

## Solution

Remove the Gateway's completed-run rejection for new AWX job IDs. Stop projecting `afk_runs.status` from child AWX execution outcomes. Make provider change-request events (merge/close) the sole authority for AFK Run lifecycle finalization.

## User Stories

1. As an operator, I want the MR Review Runner to start successfully after the Develop-Loop Runner completes, so that automated code review runs on the generated PR/MR.
2. As an operator, I want the MR Develop Runner to start successfully after the MR Review Runner completes, so that requested fixes are applied to the same PR/MR.
3. As an operator, I want all three AWX jobs (develop, review, fix) recorded independently in the Gateway database, so that I can trace the full execution history of one AFK Run.
4. As an operator, I want the AFK Run to remain open while the PR/MR is open, so that the lifecycle reflects the actual state of the change request.
5. As an operator, I want the AFK Run to become completed only when the PR/MR is merged, so that the lifecycle reflects successful delivery.
6. As an operator, I want the AFK Run to have a distinct terminal state when the PR/MR is closed without merging, so that I can distinguish delivered work from abandoned work.
7. As an operator, I want same-AWX-job-id replay to remain idempotent, so that network retries do not create duplicate records.
8. As an operator, I want new AWX job IDs under an existing AFK Run to always be accepted, so that the multi-stage pipeline is never blocked by a false lifecycle conflict.
9. As an operator, I want the existing `afk_runs.status` field to remain available temporarily for backward compatibility, while `EngineeringOutcome.status` becomes authoritative for lifecycle state.
10. As a developer, I want the Gateway domain glossary to accurately describe the intended model, so that future contributors understand the relationship between AFK Runs, bindings, and AWX jobs.
11. As a developer, I want a new ADR recording this architectural correction, so that the rationale is preserved and the superseded ADR 0027 rule is explicitly retired.
12. As an operator, I want the playbook conflict suppression for `afk_enabled=true` terminal reporting to be removed, so that terminal outcomes are always submitted after accepted starts.
13. As an operator, I want failed or cancelled AWX jobs that cannot report their terminal outcome to be reconciled by an external process, so that execution records are eventually complete.
14. As an operator, I want historical AFK Runs to not be automatically rewritten by this change, so that the bugfix is scoped and safe.
15. As an operator, I want a controlled multi-stage AFK run to verify the fix end-to-end, so that the pipeline is confirmed working in production.

## Implementation Decisions

### Modules to modify

1. **Gateway Repository (`afk_outcomes/repository.py`)**
   - Delete the completed-run rejection at lines 2170-2182 (explicit `afk_run_id` path).
   - Delete the completed-run rejection at lines 2240-2247 (change-request identity lookup path).
   - Remove calls to `_project_afk_run_status()` from both rejection paths.
   - Retain `_project_afk_run_status()` and `_converge_afk_run_status()` as deprecated compatibility helpers; they are no longer called during AWX binding writes.

2. **Gateway API (`app/api/afk_executions.py`)**
   - The `409 Conflict` response remains only for genuinely contradictory replay of an already-recorded AWX job ID.
   - The completed-parent rejection is removed; the API accepts all new AWX job IDs for an existing `afk_run_id`.

3. **Gateway Domain Glossary (`CONTEXT.md`)**
   - Update the `AWX Execution Binding` definition to describe it as a foreign-key association containing many AWX jobs.
   - Update the `AFK Run` definition to state it is anchored by one change request and contains one binding with many jobs.
   - Update the `RunStatus` definition to state it is independent of AWX execution outcomes and driven by change-request events.

4. **ADR (`docs/adr/0028-change-request-owns-afk-run-lifecycle.md`)**
   - New ADR recording the corrected lifecycle model and superseding ADR 0027's terminal-binding rule.

5. **AWX Playbooks (`openclaw_ansible_playbooks`)**
   - Remove the start-phase conflict suppression in `tasks/terminal_reporting.yml` for `afk_enabled=true` flows.
   - Preserve same-job-id idempotent replay behavior.

6. **Tests (Gateway)**
   - Add integration test: develop job completes, review job accepted, fix job accepted, all under same `afk_run_id`.
   - Add test: provider merge event finalizes AFK Run.
   - Add test: provider close-without-merge event finalizes AFK Run with distinct terminal status.
   - Update existing completed-binding rejection tests to reflect the new behavior.
   - Retain same-job-id idempotent replay tests.

7. **Tests (Playbooks)**
   - Verify terminal reporting is submitted for `afk_enabled=true` after accepted start.

### Architectural decisions

- **ADR 0028 supersedes ADR 0027** for the terminal-binding rule. ADR 0027 is not deleted but its completed-run rejection behavior is retired.
- **No schema or API redesign.** The existing per-job row shape is preserved. The logical grouping is semantic, not structural.
- **`afk_runs.status` is deprecated in favor of `EngineeringOutcome.status`** for lifecycle state, but the field remains populated for backward compatibility during transition.
- **No automatic historical backfill.** Existing incorrectly completed runs are not rewritten.
- **AWX reconciliation is out of scope** for this bugfix; tracked in issue #637.

## Testing Decisions

- Tests should cover external behavior (API responses, database state) rather than implementation details.
- Gateway integration tests should exercise the full create-or-replay path with a shared `afk_run_id` across multiple AWX job IDs.
- The playbook tests should verify that terminal reporting is submitted after an accepted start phase for AFK-enabled flows.
- Prior art exists in `tests/integration/test_execution_bindings.py` and `tests/integration/test_afk_run_convergence.py`.
- The regression test for the reported bug (jobs 9260 → 9261 → 9262 with same `afk_run_id`) should be the primary acceptance test.

## Out of Scope

- AWX status reconciliation for failed/cancelled jobs that cannot report terminal outcomes (tracked in issue #637).
- Automatic rewriting of historical AFK Runs incorrectly marked completed.
- Schema or API shape changes to the execution binding model.
- Issue association with AFK Runs (future enhancement).
- Feature flags; this is a corrective change deployed directly after tests pass.

## Further Notes

The EDA rulebook and AWX playbooks already implement the correct multi-stage behavior. The bug is entirely within the Gateway's lifecycle projection logic and its completed-run rejection rule. The fix is therefore narrow and well-contained.
