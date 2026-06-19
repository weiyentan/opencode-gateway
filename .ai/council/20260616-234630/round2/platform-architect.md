# Council Response: Platform Architect

## Reactions to Other Members

### Agreement

**Skeptic — "Could AWX replace the Gateway?"** — This is the most important question in the review, and I'm glad it was asked bluntly. The answer is no, and here's why: AWX is a job execution engine, not a stateful control plane. AWX launches playbooks and polls their completion. It has no concept of workspace lifecycle, no port allocation database, no policy engine (disk-pressure pre-flight checks, memory thresholds), no OpenCode Serve REST client, and no observation ingestion API. If we tried to fold these concerns into AWX, we'd need custom AWX plugins, custom inventory callbacks, and a fundamentally different AWX deployment model. The Gateway is the right layer — a thin state engine that delegates infrastructure actions to the executor (AWX) and coding actions to OpenCode Serve. The separation is architecturally correct. However, the Skeptic is right that 54% unused interface surface weakens this argument. Every unused method is ammunition for the claim that the Gateway is over-engineered.

**Senior Engineer — dual database access is a maintenance trap.** I concede this fully. Two ORM patterns in one codebase for the same database is indefensible. It doubles the cognitive surface for new contributors, splits query patterns unpredictably, and makes schema migrations harder to reason about. One pattern must be eliminated.

**Delivery Planner — risk ordering is inverted.** The AWX integration (issue #10) is the highest-risk architectural dependency because it validates the entire executor abstraction. I was wrong to not flag this ordering as a problem. The AWX executor is the only concrete plugin — if it can't be made reliable, the entire Gateway collapses into a speculative abstraction. Deferring it to the end is classic mis-ordering of platform risk. The vertical-slice-first approach (AWX → create_workspace → start_opencode → run session → stop → cleanup → collect in one milestone) is the only sane way to validate the design.

**Cost Reviewer — Postgres vs. SQLite.** On the surface, SQLite seems cheaper. But from an architecture standpoint, the decision hangs on a single axis the Cost Reviewer didn't fully account for: **the Gateway is designed to support multiple concurrent callers (Paperclip agents, humans, CI systems) who all need to observe the same state**. SQLite with WAL mode can handle this, but at the cost of write-contention under concurrent job submissions, no native role-based access, no connection pooling, and a more complex backup story. Postgres is the right choice for a multi-process, API-accessible state store. However, the observation data does *not* need Postgres — that's where the Cost Reviewer has a real point.

### Disagreement

**Cost Reviewer — "Add SSH-direct executor, remove observations pipeline."** Removing observations breaks the policy engine, which is one of the Gateway's few genuinely novel features. Pre-flight disk-pressure checks are what keep Runner VMs from falling over mid-session. You cannot skip observations and still have a working policy engine. However, I *do* agree that the observation storage choice is wrong for time-series data — more on this below.

**Skeptic — "Postgres is wrong tool for time-series."** This I partially disagree with. At the Gateway's scale (hundreds of runners, not thousands), Postgres with proper partitioning handles time-series observations without fuss. The real problem is not Postgres vs. TimescaleDB — it's that **observations currently have no retention policy**. Unbounded time-series growth in any database is a table-management incident waiting to happen. A simple `DELETE FROM runner_observations WHERE recorded_at < now() - interval '7 days'` cron task would solve this. If the system scales beyond that, we migrate observations to a separate time-series store — but that's a future concern, not an MVP blocker.

**Product Owner — "Approval gates are a layer violation with Paperclip."** I disagree. The Gateway's abstract `PolicyEngine` interface is exactly the right place for pluggable pre-flight checks. Paperclip might add its own governance layer above, but the Gateway should still enforce its own operational safety (disk pressure, memory pressure, staleness). These are two different concerns: Paperclip governs *what work is approved*; the Gateway governs *where that work can safely run*. The Policy Engine belongs in the Gateway.

### New Concerns Raised

**Senior Engineer's 1127-line `create_job` handler.** I hadn't examined the handler size, but 1127 lines for a single route handler is a red flag for any architecture review. That function is managing state transitions, calling executors, querying OpenCode, and writing to the database — it has too many responsibilities. Even if it's decomposed internally, it's one function where a bug can corrupt a job's entire lifecycle. This is a blast-radius concern and I should have caught it.

**Cost Reviewer's point about 650-test suite cost.** From an operational standpoint, a 650-test suite that takes >5 minutes to run is a deployment pipeline bottleneck. If those tests don't provide proportional coverage confidence, they're a maintenance tax. I'd like to see a coverage report sliced by domain — how many tests per architectural layer?

## Updated Position

My position has **crystallized but not reversed**. The architecture is sound in its separation of concerns (Gateway ↔ Executor Plugin ↔ AWX ↔ OpenCode Serve ↔ Runner VM). The four-layer model is correct. But I now see several real weaknesses I was too charitable about:

1. **Observation storage is over-engineered for MVP.** The separate tables are fine (per ADR 0001), but the entire observation pipeline should be optional. If a runner doesn't push observations, it should still be usable — it just won't get pre-flight checks. This makes the system deployable without a working observation pipeline and lets us add it incrementally.

2. **Polling must be replaced with webhooks before production.** I was too accepting of "polling is fine for MVP." It's not. Polling ties up Gateway connection slots and AWS API rate limits. AWX supports webhook callbacks natively. The Gateway should register a `/webhooks/awx/{job_id}` endpoint and let AWX call back on completion. This unblocks the synchronous `POST /jobs` concern as well — if AWX calls back, the Gateway can respond to the original caller with a `202 Accepted` (job tracking ID) immediately.

3. **Template ID coupling is indeed fragile.** My original suggestion of a config validation endpoint is a band-aid. The real fix is to require the AWX executor to verify template existence on startup and cache the results. A failed `GET /api/v2/job_templates/{id/` should cause a startup crash, not a runtime failure.

4. **The port allocation scheme needs a reservation timeout.** If a workspace creation fails mid-flight, the port is orphaned. ADR 0003's `cleanup_status` column is mentioned but there's no automatic port reclamation. Add a `TTL` column: mark ports as `RESERVED` on allocation, and if the workspace isn't created within 5 minutes, release the port back to the pool.

## Remaining Gaps

1. **No end-to-end test across all four layers.** The test suite tests individual layers well, but there's no integration test that goes Gateway → AWX executor (mock) → OpenCode Serve (mock) → Runner VM (mock). A vertical-slice integration test would catch boundary bugs that unit tests miss.

2. **Observation authentication is still an open question.** The Skeptic flagged this and I have no better answer. `POST /observations` currently has no documented auth mechanism. If it's unauthenticated, anyone can poison the policy engine with fake observations, causing runners to be falsely blocked or falsely cleared. This is a production security gap and must be addressed before any real deployment.

3. **Database migration strategy is unclear.** Alembic is present, but there's no documented migration workflow (revision naming convention, rollback policy, zero-downtime migration plan). For a system that tracks job state, migrations need to be backward-compatible for at least one release cycle.

4. **The Gateway has no graceful degradation mode.** If Postgres goes down, the Gateway goes completely dark. There's no read-only fallback, no cached state, no degraded-operation mode. For an orchestration control plane, this is a significant availability gap. A caching layer (even in-memory with TTLs) for runner status and active workspaces would allow `GET` endpoints to function during a database outage.

## Architecture Path Forward

Here's what I recommend as a concrete, staged architecture plan that addresses the cross-cutting concerns raised by the Council:

### Phase 1: Core Validation (Vertical Slice)
- Implement the AWX executor first, not last
- Build one end-to-end job flow: `create_workspace → start_opencode → (session runs) → stop → cleanup → collect`
- Use AWX webhook callbacks instead of polling
- Remove all unused executor protocol methods (`restart_opencode`, `collect_state` remain but strip the ABC requirement)
- Remove unused OpenCodeClientProtocol methods from the ABC
- **Result**: validated architecture with minimal surface area

### Phase 2: Concurrency & Database Consolidation
- Convert `POST /jobs` to async, returning `202 Accepted` with a job ID
- Consolidate to a single database access pattern (keep SQLAlchemy, drop raw asyncpg queries, or vice versa — pick one)
- Refactor `create_job` handler: decompose into a state machine orchestrator with separate functions for each state transition
- Add port reservation timeout (5-minute TTL, auto-reclaim)
- **Result**: maintainable, concurrent, blast-radius-reduced codebase

### Phase 3: Observability & Safety
- Add startup template validation in the AWX executor
- Implement observation authentication (shared bearer token, minimum)
- Add observation data retention policy (configurable TTL, default 7 days)
- Implement `GET /jobs` and `GET /workspaces` with in-memory cache for degraded-mode reads
- Write vertical-slice integration test across all layers
- **Result**: production-safe with monitoring, auth, and graceful degradation

### Phase 4: Interface Pruning & Cost Optimization
- Audit the test suite: remove tests for removed protocol methods, consolidate coverage
- Document the Paperclip contract as a separate ADR (not in Gateway code)
- Remove the observations pipeline as a core requirement — make it a pluggable optional module
- Evaluate whether the Policy Engine can be simplified to a single configurable threshold check rather than a full pluggable interface
- **Result**: lean, defensible, documented codebase ready for Paperclip integration

**Summary:** The Gateway's four-layer architecture is sound but burdened by speculative future surface, untested high-risk dependencies, and missing production guardrails — cut the unused interface methods to the bone, validate with a vertical AWX slice first, replace polling with webhooks, add observation auth and retention, consolidate database access, and only then build out the remaining surface area.
