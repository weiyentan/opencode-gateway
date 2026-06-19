# Council Opinion: Cost Reviewer

## Summary

The Gateway project itself is cost-efficient — it adds no new model/LM calls and only modest HTTP proxying overhead per job — but its operational architecture (AWX triple-launch pattern per job, full Postgres dependency, 650+ tests for a ~4K-line orchestration layer, and five Alembic migrations for what amounts to CRUD on four tables) will dominate the total cost of ownership at scale, not the runtime of the Gateway itself.

## Assessment

Let me separate two things: the Gateway's *runtime cost* (which is nearly zero — it's a thin FastAPI passthrough with no model calls) from its *operational cost* (which is where the real expense lives).

**Runtime cost: fine.** The Gateway makes zero LM calls. Its per-job overhead is a handful of asyncpg queries and one or two HTTP calls to the executor. This is negligible compared to the OpenCode session itself, which is where all the model tokens are burned. No concern here.

**Operational cost: this is where I have serious concerns.** Let me enumerate.

### 1. The AWX triple-launch pattern is expensive infrastructure overhead

Every coding job triggers up to 3 AWX job template launches: `create-workspace`, `opencode-lifecycle` (start), and later `workspace-teardown` (collect + cleanup). Each launch spins up an Ansible playbook on the Runner VM, pulls any required roles/collections, and runs shell commands.

At scale (say, 100 jobs/day), that's 300 AWX job launches per day *just for orchestration*, before any coding happens. Each AWX launch has a non-trivial overhead: inventory sync, fact gathering, Python environment setup. For short-lived workspaces, the AWX orchestration overhead could rival or exceed the actual coding time. An SSH-direct executor (bypassing AWX for the lifecycle boilerplate) would cut this cost by 2/3.

The `LocalExecutor` proves the point — it's 119 lines of no-op stubs. The fact that the production executor needs a full AWX job launch to say "mkdir /workspace/abc" is a sign that the abstraction boundary is too heavy for the operations it performs.

### 2. Postgres is overkill for the data volume

The Gateway stores jobs, workspaces, runners, observations, approvals, and events. At the expected scale (hundreds to thousands of jobs, periodic observations), this fits comfortably in SQLite. Five Alembic migrations exist for four core tables plus webhooks and indexes.

The operational cost of Postgres is real: connection pooling, migrations in CI/CD, backup management, connection string rotation. For orchestration metadata at this volume, a SQLite file on the Gateway's own disk would work identically and eliminate an entire database service from the deployment. The config alone has 6 database-related env vars (`GATEWAY_DATABASE_HOST`, `GATEWAY_DATABASE_PORT`, `GATEWAY_DATABASE_NAME`, `GATEWAY_DATABASE_USER`, `GATEWAY_DATABASE_PASSWORD`, plus min/max connections and timeout).

**If Postgres is needed for high-availability deployments, it should be optional** — the code shouldn't require it. Currently, `ObservationBasedPolicy.check()` silently skips enforcement when the DB connection is `None`. That tells me the DB dependency was bolted on, not designed in.

### 3. The policy engine duplicates monitoring tooling

The `ObservationBasedPolicy` checks disk usage, memory usage, and staleness by querying a `runner_observations` table that Runner VMs must `POST /observations` to populate. This is a custom-built monitoring system.

Any organization that operates VMs already has at least one monitoring tool (Prometheus + node_exporter, Datadog, New Relic, Nagios). Requiring each Runner VM to push observations to the Gateway's Postgres on a separate endpoint means:
- Every runner must have a push-agent or cron job sending HTTP requests
- The Gateway must store and query time-series data in a relational database (which is the wrong tool for time-series — even ADR 0001 acknowledges this by proposing separate tables)
- The policy logic must be maintained alongside the rest of the Gateway

A cheaper approach: let existing monitoring tools set the runner's status via the `PATCH /runners/{id}/status` endpoint (or equivalent), and remove the observations pipeline entirely from the MVP. The Gateway already supports manual `online`/`offline`/`maintenance` statuses — extend that pattern rather than building a custom telemetry system.

### 4. The testing footprint is disproportionately large

650+ tests across 28 test files for what is conceptually a ~4,000-line orchestration layer. The test-to-code ratio is roughly 2:1. While thorough testing is admirable, this carries a real cost:
- CI pipeline time for every PR
- Maintenance burden when refactoring (every test file uses mocked asyncpg connections and executor plugins)
- Cognitive overhead for new contributors

The `conftest.py` alone is 204 lines of mock factories. The `test_jobs.py` file is 3,498 lines. `test_executors_awx.py` tests mock the AWX transport. The integration tests spin up a real Postgres via testcontainers.

For a project at 95% completion (17/18 issues done), this testing investment might have been better spent on the remaining Paperclip integration adapter (#13) or on integration testing against a real OpenCode Serve instance.

### 5. The four-layer architecture adds maintenance surface area

Each of the 6 executor lifecyle methods has its own Pydantic request/response model pair (12 models total in `app/executors/models.py`). The `AWXExecutorPlugin` is 369 lines, the `LocalExecutor` is 119 lines. The `serve_client.py` has its own client protocol and models.

For every new feature, the developer touches: API layer → core models → lifecycle rules → executor interface → executor implementation → OpenCode client. That's 6 touchpoints minimum. Each touchpoint is a code review, a test update, and a potential bug. The cost isn't just in writing the code — it's in the friction of changing it.

### 6. Webhook dispatch on every job event

The `create_job` endpoint fires `asyncio.create_task(dispatch_webhooks(...))` on every job outcome (completion, failure, policy rejection). Each webhook dispatch iterates over webhook registrations and sends HTTP POST requests. This is fine for low volume, but at scale these become noisy background tasks competing with request-handling threads. The current code doesn't have a webhook queue, retry logic, or dead-letter handling — so webhooks will silently fail under load.

## Key Concerns

1. **AWX triple-launch per job dominates infrastructure cost**: 3 AWX job launches per coding job, each running a full Ansible playbook, for tasks that amount to "mkdir" and "systemctl start." An SSH-direct executor or lightweight agent would cut orchestration cost by ~66%.

2. **Postgres is a heavy dependency for orchestration CRUD**: Requires connection pooling, migration management, backup/restore procedures, and 6 env vars. SQLite would handle the data volume at zero operational cost.

3. **Custom monitoring/policy engine duplicates existing tooling**: The observation-based policy is a bespoke push-metric system that reinvents what Prometheus, Datadog, and every other monitoring platform already do well.

4. **Test suite cost exceeds the value of the code it tests**: 650+ tests for ~4,000 lines of orchestration glue is expensive to maintain and slow to iterate on.

5. **No cost guardrails for the OpenCode sessions themselves**: The Gateway tracks job state but has no mechanism to set budget limits, token caps, or timeouts on the OpenCode sessions it launches. An unbounded session that costs $500 in model calls is indistinguishable from a $5 one.

## Recommendations

1. **Make the executor plugin support a lightweight SSH-direct mode** in addition to AWX. This lets operators skip the AWX overhead for environments where Ansible orchestration isn't needed. The `LocalExecutor` already shows this is feasible — the 6-method interface would map trivially to SSH commands.

2. **Swap Postgres for SQLite as the default** and make Postgres an optional production-only backend, gated behind a config flag. The data model (jobs, workspaces, runners, events) doesn't need a separate database service. This eliminates an entire class of operational expense.

3. **Remove the observations/policy pipeline from MVP scope.** Instead, let operators set runner statuses manually or via existing monitoring tools. The `PATCH /runners/{id}/status` endpoint already exists in concept — use it. Revisit custom telemetry only if demand emerges.

4. **Audit the test suite for cost-effectiveness.** The 3,498-line `test_jobs.py` is a red flag. Consolidate and reduce test surface area by using higher-level integration tests that exercise the real dependency graph rather than mocking every layer separately.

5. **Add session budget controls to the Gateway's job model.** Even a simple `max_cost` or `max_duration_minutes` field on the job, passed to OpenCode Serve as a parameter, would prevent runaway costs. Without this, the Gateway can launch an expensive coding session but has no way to stop it.

## Questions That Need Answers

1. Have you measured the median latency and runner-resource overhead of the AWX triple-launch pattern? What fraction of total job time is spent in AWX orchestration vs. actual coding?

2. What is the expected job volume per day? If it's under 1,000 jobs/day, why does this need Postgres instead of SQLite?

3. Who owns the Runner VM monitoring today (Prometheus, Datadog, something else)? Why can't that system set the runner's health status instead of building a custom observation pipeline?

4. What happens when a webhook dispatch fails under load? The current `asyncio.create_task` with no retry or queue means webhooks are silently dropped.

5. Has anyone estimated the total monthly operational cost (Postgres hosting, AWX job runner overhead, Gateway container resources) for the expected load? That number should exist before we declare this "feasible."

**Summary:** The Gateway's per-job runtime cost is near-zero and its architecture is clean, but the operational cost of the AWX triple-launch pattern, mandatory Postgres dependency, and custom monitoring pipeline will dominate TCO at any meaningful scale, and the 650-test suite represents an expensive insurance policy for a project that still has incomplete integration points.
