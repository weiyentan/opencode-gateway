# Council Opinion: Senior Engineer

## Summary

The Gateway is well-structured code with excellent test coverage and disciplined architecture, but it carries significant YAGNI surface area (7 of 13 protocol/executor methods are unused "future surface"), a monolithic 1127-line `create_job` handler that mixes too many concerns synchronously, and a dual-access database pattern (raw asyncpg in the policy layer, SQLAlchemy models elsewhere) that will be a maintenance headache.

## Assessment

I've walked the entire codebase — 28 test files, all ADRs, the core API handlers, the executor plugins, the policy engine, and the OpenCode client. Here's what I found.

**The good:**
- Test coverage is genuinely impressive. 650+ tests with real async patterns, mock transport layers, and both unit and integration test suites. This is not "stub coverage" — the conftest.py, mock fixtures, and test organization show engineering discipline.
- The four-layer architecture (API → Core → Executor → OpenCode Client) is the right abstraction level. Each layer has a clear boundary and the dependency direction is enforced naturally by the import graph.
- The ADR process is real, not ceremonial. ADR 0002's refinement (documenting which methods are "active surface" vs "future surface") is the kind of honesty that keeps a codebase navigable.
- Pydantic at every boundary, strict mypy, ruff linting — the toolchain hygiene is excellent.
- Graceful database degradation (start without Postgres, health endpoint works) is a pragmatic touch that signals real operational thinking.

**The concerning:**

1. **The `create_job` handler is a God function (1127 lines in `app/api/jobs.py`).** It does runner selection, policy checks, DB inserts, workspace creation, OpenCode start, synchronous diff fetching, webhook fire-and-forget, and cleanup_after scheduling — all in one request handler. If OpenCode Serve takes 5+ minutes to produce a diff, this POST request stays open for 5+ minutes. For a system that claims to be an async job orchestration layer, the fact that job submission is synchronous end-to-end is a significant architectural smell. The Gateway should submit and return immediately with a `202 Accepted`, then asynchronously process the job lifecycle.

2. **Dual database access pattern.** The policy layer (`ObservationBasedPolicy`) uses raw `asyncpg` SQL queries, while the ORM models are SQLAlchemy. This means:
   - Two query dialects to maintain.
   - Raw SQL bypasses Alembic migration guarantees — if a column is renamed in the model, the raw SQL in `policy/observation.py` breaks silently.
   - The `check()` method has a side effect: it writes to the database (`_set_runner_status`). A method called `check` that mutates state is a naming violation that will surprise future maintainers.

3. **Significant future-surface tax.** Per ADR 0002's own admission:
   - 2 of 6 executor methods (`restart_opencode`, `collect_state`) are never called.
   - 5 of 7 OpenCode client methods (`health`, `list_sessions`, `get_session`, `create_session`, `delete_session`) are never called at runtime.
   That's 7 of 13 total protocol methods (54%) that are pure future surface. Every new executor implementation must implement these, every test suite must cover them, and every code reviewer must evaluate them. This is the textbook definition of YAGNI. The ADR refinement documents it honestly, but the design still forces the cost. A simpler protocol interface (3 active methods + optional extension) would halve the abstraction tax.

4. **AWX executor leaks abstraction.** The mapping is clean structurally, but:
   - `collect_state` maps to the `gateway-workspace-teardown` template with `action: collect` — a teardown template should not be collecting state. This is a semantic mismatch.
   - The `_workspace_path()` helper hardcodes a filesystem convention (`/home/runner/workspaces/{uuid}`) that assumes a Unix filesystem layout. A future SSH or Kubernetes executor would inherit this assumption.
   - The AWX client reimplements timestamp parsing with manual `endswith("Z")` ISO 8601 normalization — indicating that the target AWX API responses aren't well-specified or are inconsistent.

5. **Job state machine lacks database-level enforcement.** The `VALID_TRANSITIONS` frozenset in `lifecycle.py` is enforced in application code only. There are no `CHECK` constraints, no `ENUM` types in PostgreSQL, and no trigger-based guards. A bug in the ORM layer or a direct SQL query that skips the `can_transition()` call can silently corrupt job state. For a system that brands itself as an execution control plane, state integrity should be enforced at the storage level.

6. **Port range of 1000 is tight.** At 1000 concurrent workspaces across all runners, the system hits a hard ceiling. The ADR 0003 rationale calls it "sufficient," but in production, a single runner with moderate throughput could exhaust 1000 ports in a matter of hours if retention is generous. Port allocation via `SELECT port FROM workspaces WHERE port IS NOT NULL` also becomes a full sequential scan at high occupancy, which the rationale hand-waves as "trivially fast."

## Key Concerns

- **Synchronous POST /jobs is an architectural smell** — a 5-minute blocking HTTP request is not an async job system.
- **7 of 13 protocol methods are pure YAGNI** — the abstraction tax is paid today for hypothetical tomorrows.
- **Dual database access (raw asyncpg + SQLAlchemy)** creates a silent maintenance trap where a schema migration can break the policy layer without any static detection.
- **`check()` mutates state** — naming that hides side effects will cause debugging pain.
- **No database-level state machine enforcement** — application-only transition validation is fragile.
- **AWX plugin leaks filesystem assumptions** via hardcoded Unix paths and semantically mismatched template mappings.

## Recommendations

1. **Make `POST /jobs` asynchronous.** Return `202 Accepted` with the job ID immediately and process the lifecycle (workspace creation, OpenCode start, task execution, diff collection) in a background task. This removes the synchronous timeout risk and aligns the API with how async job systems should work. It also simplifies error handling — today the handler has nested try/except blocks for policy violations, executor failures, and diff failures in a single function.

2. **Prune the future surface.** Drop `restart_opencode` and `collect_state` from the required ExecutorPlugin ABC. Make them optional extension methods. Similarly, slim the OpenCodeClientProtocol to only the methods actually called (`get_session_diff`, `abort_session`). Add the others back when a call site materializes. The ADR 0002 refinement already knows these are unused — formalize that knowledge in the code.

3. **Consolidate on one database access pattern.** Either use SQLAlchemy everywhere (my preference — it keeps migration safety) or extract the policy layer's raw SQL into a dedicated data access module that is co-located with the migrations. The current split between `policy/observation.py` using asyncpg and `db/models/` using SQLAlchemy is unsustainable.

4. **Add database-level CHECK constraints** on the `gateway_jobs.status` column to restrict to valid transitions. This is a low-cost defense-in-depth measure that costs nothing at runtime and prevents silent corruption.

5. **Rename `ObservationBasedPolicy.check()` to `check_and_block_if_unhealthy()`** or extract the database mutation from the check — a method named `check` should not have side effects.

## Questions That Need Answers

- Was synchronous `POST /jobs` an intentional MVP shortcut or an oversight? If intentional, what is the plan to decouple job submission from job execution?
- What is the actual expected concurrency at launch? Is 1000 ports genuinely sufficient, or does the team expect to hit that ceiling in the first month?
- Are the integration tests (`tests/integration/`) run in CI against a real Postgres, or only locally? The dual-access pattern concern is moot if the policy layer has integration test coverage that catches schema drifts.
- Is the `collect_state → gateway-workspace-teardown` mapping in the AWX executor a known wart with a planned fix, or is it considered acceptable? If a teardown template collects state, what happens when a real teardown action runs concurrently?
- Has anyone measured the end-to-end latency of `POST /jobs` with the local executor? The synchronous path includes policy check + DB writes + executor calls + OpenCode start + diff fetch — knowing the p50/p95 would ground the async-vs-sync debate in data.

**Summary:** The Gateway is well-engineered at the code level but has a synchronous architecture at odds with its async job-orchestration premise, carries significant YAGNI abstraction surface, and has a dual-database-access maintenance trap that will cause real pain during schema evolution.
