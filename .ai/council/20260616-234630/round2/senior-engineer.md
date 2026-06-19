# Council Response: Senior Engineer

## Reactions to Other Members

### Agreement

**Platform Architect — observation lifecycle is real.** I flagged the sync God function but missed that observations have no retention, compaction, or aggregation strategy. Observations are write-once-read-never in the current API surface. Until Paperclip or some consumer queries them, we're building a telemetry pipeline with no consumer. That's work without value.

**Delivery Planner — risk ordering is inverted.** I share their frustration that job creation (the hottest, most security-sensitive path) is the last thing the issue tracker slices, while template configuration gets first-class treatment. The issue tracker should reflect execution priority, not implementation convenience. A vertical slice — one end-to-end job through the whole stack — would have surfaced the synchronous-handler bottleneck and the dual-DB problem before we wrote 650 tests around a half-built architecture.

**Skeptic — the "five-system failure chain" is a real operational concern.** Gateway → AWX → Runner VM → systemd → opencode-serve is too many moving parts to diagnose when a job fails. Their thumbnail of "run `opencode --headless --listen :8080` directly" is provocative and worth serious consideration.

**Cost Reviewer — test suite cost is real.** 650 tests for a Gateway that, as currently implemented, only routes create → poll → return is disproportionate. I'd push back on *all* of them being waste — a well-tested state machine pays for itself — but the ratio of test surface to production value is inverted.

### Disagreement

**Skeptic — "AWX could replace the Gateway."** This is the strongest critique in the council, and I need to take it seriously rather than dismiss it. However, it conflates *what AWX does well* (imperative playbook execution with a mature RBAC and job-tracking UI) with *what the Gateway provides* (a domain-model API with a typed job/workspace/runner state machine, policy engine, and plugin abstraction). AWX has no concept of a workspace lifecycle, no observation-based policy engine, and no OpenCode-specific state machine. The question isn't "can AWX do this?" — it can, via custom playbooks — but "should the Gateway exist as a dedicated control plane?" I think yes, *provided* the Gateway is kept thin enough that it doesn't become a translation layer between one REST API (the caller) and another (AWX). If the Gateway is just adding latency and another failure mode to what AWX already does natively, the Skeptic is right. The Gateway needs to earn its existence by providing state, policy, and observability that AWX cannot.

**Cost Reviewer — "Swap Postgres for SQLite."** This is seductive but dangerous for the stated use case. SQLite cannot handle concurrent writers the way this architecture demands — Paperclip, the Gateway API, and the observation ingestion pipeline all write concurrently. SQLite's WAL mode helps reads, but write contention under a multi-service deployment will produce `database is locked` errors at the worst possible moments. Postgres is the right choice for this workload. However, I agree that Postgres is overkill if the observation pipeline is trimmed (more on that below). If observations are removed, the remaining schema is small enough that SQLite *might* work — but only with a single-writer pattern that constrains deployment topology. That's a tradeoff, not a free lunch.

**Cost Reviewer — "650 tests are disproportionately expensive."** I partially disagree. A state machine with 9 states, concurrency-sensitive transitions, and persistent side effects (workspace creation, opencode lifecycle) *needs* thorough testing. The problem isn't the test count — it's that the production code is too complex, so the tests are too complex. If we simplify the handler (make it async, decompose the God function), the tests should *shrink*, not grow. The target isn't "fewer tests" — it's "simpler code that needs fewer tests."

**Platform Architect — "Template ID coupling is fragile."** I flagged the AWX filesystem assumptions, but I think template ID coupling is acceptable. Template IDs change rarely (only on playbook update), and they're configuration, not code. The real fragility is the implicit contract between the AWX playbook `extra_vars` and what the Gateway sends. That's the coupling I'd worry about — not a config integer.

### New Concerns Raised

**Skeptic — observation authentication.** I hadn't considered that `POST /observations` is currently an unauthenticated endpoint (or at least, the auth story is absent from the spec). If runner VMs are sending telemetry, that endpoint needs runner-scoped credentials, not just the same bearer token used for job submission. A compromised observation endpoint could be used to spoof runner health and cause the policy engine to make bad dispatch decisions.

**Cost Reviewer — OpenCode session cost guardrails.** This is a genuine blind spot. The Gateway has no mechanism to enforce budget limits on OpenCode API usage (token spend, session duration, tool call count). If Paperclip submits a job that triggers a runaway OpenCode session, the cost is unbounded. This needs to be surfaced as a requirement, even if the enforcement mechanism lives in OpenCode Serve rather than the Gateway.

**Platform Architect — Port allocation SPOF.** I dismissed port management as "simple enough" in my R1 opinion, but on reflection, a single counter in Postgres *is* a bottleneck. If the Gateway restarts mid-allocation, or if two concurrent create-workspace calls race on the port counter, we get collisions. This needs a stronger allocation strategy — perhaps a bitmap or a range-reservation pattern.

## Updated Position

My R1 opinion was solid on the engineering problems (synchronous handler, dual DB access, YAGNI surface) but too forgiving of deferred operational concerns (observations lifecycle, port allocation, auth gaps). I'm *strengthening* my stance on YAGNI — seeing the Skeptic, Cost Reviewer, and Delivery Planner all converge independently on the same 54% figure is hard to ignore. Seven unused protocol methods are not "future-proofing"; they're dead weight that inflates the test suite, the docs, and the cognitive load of onboarding.

I stand by my core engineering critique. The `create_job` God function is the single largest risk in the codebase. But I now see that the scope problem (too much unused surface) is the upstream cause of the complexity problem (too many states, too many edge cases in the handler). Solve scope first, then complexity.

## Remaining Gaps

1. **No concrete async migration plan.** Everyone agrees synchronous POST /jobs is a problem, but no one has sketched what the async workflow looks like end-to-end. Does the Gateway return a 202 with a Location header and the caller polls? Does the Gateway push via webhook? This needs a design decision before the Delivery Planner can slice work.

2. **The AWX coupling paradox.** The Delivery Planner wants to defer AWX executor implementation (issue #10), but the Skeptic's argument that "AWX could replace the Gateway" can't be evaluated without a concrete AWX-only prototype. If we're going to keep the Gateway, we should build the AWX executor *first*, not last, so we can prove the abstraction works.

3. **Observation pipeline has no consumer.** Before investing in retention policies, aggregation queries, or TimescaleDB, we need to answer: who reads observations and what decisions do they make from them? Without a consumer, the entire subsystem is speculative infrastructure.

4. **AuthN/AuthZ architecture is absent.** Neither the API key model for callers nor the runner authentication for observations is specified. This is a security review waiting to happen.

## Concrete Engineering Path Forward

Here is what I would advocate for the next 6-8 weeks of engineering:

**Phase 1: Trim to the Minimal Viable State Machine (Weeks 1-2)**

- Strip the 7 unused protocol methods from the Gateway API. Keep only: `POST /jobs`, `GET /jobs/:id`, `GET /runners`, `POST /observations` (if Paperclip or the policy engine needs it) — everything else is dead code.
- Remove the future executor surface (`restart`, `collect_state` is fine; `copy_workspace`, `migrate_workspace`, `rollback_workspace` are not). The ExecutorPlugin interface drops from 13 methods to 6, and 2 of those (start/stop) are trivially implemented.
- Result: the issue tracker shrinks by ~40%. The test suite sheds ~200 tests.

**Phase 2: Async Job Creation (Weeks 2-4)**

- Convert `POST /jobs` from synchronous execution to a 202-accepted pattern. The handler validates the request, enqueues a job record in `pending` state, and returns immediately. A background worker (or AWX webhook callback) transitions the job through its state machine.
- Decompose the current 1127-line `create_job` handler into: `validate_request`, `select_runner`, `reserve_workspace`, `launch_executor`, and `poll_for_completion` — each as an independent, testable unit. The `poll_for_completion` moves out of the critical path entirely.
- Result: POST /jobs latency drops from 30-300s to <50ms. The state machine becomes testable without mocking AWX.

**Phase 3: Pick One Executor and Prove It Works End-to-End (Weeks 4-5)**

- Implement the AWX executor plugin *first* (reordering Delivery Planner's risk order). This validates the executor abstraction against a real backend.
- Build one vertical slice: submit a job → AWX creates workspace → AWX starts opencode → poll for completion → AWX collects workspace → return diff. This catches integration failures before we've built abstractions on abstractions.
- Result: a working end-to-end system that proves the Gateway's value (or reveals it doesn't have any, at which point we should consider the Skeptic's alternative).

**Phase 4: Resolve the Dual-DB Problem (Week 6)**

- Pick one: either asyncpg everywhere (for the state machine queries) or SQLAlchemy everywhere (for the domain model). The current dual-access pattern is a maintenance time bomb — every schema migration, every transaction boundary, every connection pool configuration must be duplicated.
- My preference: SQLAlchemy 2.0 with async support. It gives us the ORM for domain objects and raw SQL for performance-sensitive observation queries, all through one connection pool. Drop the raw asyncpg calls entirely.

**Phase 5: Ship with Monitoring, Not Observations (Weeks 7-8)**

- Strip the observation pipeline from the MVP. Runner health can be determined by job success/failure rate and a simple HTTP health endpoint on the runner, pushed to the Gateway periodically.
- If Paperclip later needs time-series telemetry, observations can be reintroduced as an extension — ideally backed by a purpose-built time-series store (or a simple Prometheus push gateway), not Postgres.
- Add a single "OpenCode session cost" field to the job response so callers (Paperclip) can enforce their own budgets.

This sequence prioritizes *working software with a narrow, well-tested surface* over *broad architecture with speculative abstractions*. The Gateway needs to earn its existence by being simpler than the alternatives — and right now, it is not yet simple enough.

**Summary:** The Gateway has a solid foundation but is overbuilt by roughly 40% — strip the unused surface, make job creation async, reorder risk to prove the AWX integration works end-to-end before layering more abstraction, and resolve the dual-DB problem before it becomes a maintenance liability.
