# Council Opinion: Platform Architect

## Summary

The OpenCode Gateway architecture is sound and well-disciplined — the four-layer separation, executor plugin interface, and security boundaries (ADR 0004) demonstrate mature engineering judgment — but the Postgres-backed observation store and the tight operational coupling between Gateway config and AWX template IDs introduce concrete risks that the project should address before declaring MVP complete.

## Assessment

**System fit.** The Gateway fills a genuine orchestration gap. OpenCode Serve owns coding sessions; AWX owns infrastructure operations. Neither tracks the cross-cutting state (job → workspace → runner → session mapping, runner health, port allocation, cleanup policies) that any real integration needs. Without this layer, every Paperclip, GitLab CI, or AWX caller duplicates the same state machine. The Gateway's position in the stack is correct.

**Four-layer architecture.** The API → Core → Executor → OpenCode Client layering is appropriate, not over-engineered. Each layer has clear responsibility, and the dependency direction is strict: API calls Core, Core calls Executor and OpenCode Client, never the reverse. The Pydantic model at every boundary pattern is a strength — it makes the contract explicit and testable without needing a real executor or OpenCode instance.

**Executor plugin interface.** This is the cleanest part of the architecture. Six methods with typed Pydantic models is the right granularity — no AWX-specific terms leak through. But I have a concern: the interface assumes all executors return results synchronously (within a timeout). A future SSH executor that connects to 50ms-latency remote VMs may behave very differently from a local executor. The interface doesn't account for partial progress reporting or long-running operations that outlive the Gateway request timeout. This is likely fine for MVP but worth noting.

**Observation store in Postgres.** ADR 0001's choice of separate tables per entity is reasonable at MVP scale, but I have reservations. Observation data is genuinely time-series: append-heavy, rarely updated, queried by time range. Postgres can handle this up to moderate scale, but the architecture has no retention policy or downsampling strategy. As observation volume grows (e.g., 50 runners pushing metrics every 60 seconds = 72,000 observations/day per type), querying `GET /runners/{id}` which reads the last 50 of each observation type will degrade. The PRD describes three separate tables — that's schema complexity I accept, but the absence of any data lifecycle management (partitioning, retention, aggregation) is a gap.

**Port allocation in Postgres (ADR 0003).** This is a pragmatic, clever decision. Using Postgres transactional semantics to atomically grab a port avoids a distributed lock service while staying within the existing DB. However, it couples port management to database availability — if the DB is under load, port allocation becomes a bottleneck for workspace creation. At MVP scale this is fine, but it's worth documenting that port allocation latency will be the first thing to break under load.

**Operational burden.** Three specific concerns:

1. **Template ID coupling.** The AWX executor requires three template IDs in Gateway config. Changing an AWX template's ID (e.g., recreating it after a misconfiguration) requires a Gateway config update and restart. This is a tight operational coupling. The Gateway should validate at startup that the template IDs exist (it checks they're non-zero) but cannot validate that the templates match the expected `extra_vars` contract. A misconfigured template ID produces a runtime error, not a startup error.

2. **Polling model.** The AWX executor polls AWX at a configurable interval (default 5s) up to a timeout (default 300s). This means the Gateway holds an HTTP connection open and blocks a worker thread for each in-flight AWX operation. If you have 10 simultaneous workspace creations, up to 10 Gateway workers are polling AWX instead of serving API requests. The GATEWAY_AWX_POLL_INTERVAL and TIMEOUT are configurable, but the architectural pattern locks up Gateway concurrency slots.

3. **Graceful degradation.** The health endpoint works without Postgres, but the job submission path does not. If Postgres is down, the Gateway becomes an expensive HTTP 503 generator. The graceful degradation is partly illusory — it's graceful for `/health` checks only.

**Blast radius.** ADR 0004's decision to keep infra secrets out of the Gateway is the most important security boundary in the system. If the Gateway is compromised, an attacker can:
- Submit arbitrary coding jobs (limited to what OpenCode can do)
- Launch AWX job templates (limited to the three configured templates)
- Read job results and diffs

They cannot:
- SSH into Runner VMs directly
- Escalate to other AWX-managed infrastructure
- Access OpenCode's internal SQLite state

This is sound. The one gap: the AWX API token is itself a powerful credential. If the Gateway's environment is compromised, the attacker gets an AWX bearer token that can launch any job template the token has permission for — potentially well beyond the three Gateway templates. The severity depends on AWX RBAC configuration, but the Gateway cannot enforce AWX-side restrictions.

## Key Concerns

1. **Observation data has no lifecycle management.** Three time-series tables with no partitioning, retention policy, or downsampling. This will become an operational problem before other bottlenecks surface.

2. **AWX polling blocks Gateway concurrency slots.** The synchronous polling pattern for AWX job completion consumes Gateway worker threads. At scale, this reduces the Gateway's ability to serve API requests.

3. **AWX template IDs are a fragile operational coupling.** No mechanism to validate that AWX templates match the expected contract at startup. Runtime errors when they don't.

4. **Port allocation is a single point of failure.** Coupled to Postgres availability and can become a bottleneck under concurrent workspace creation load.

5. **Two unused executor methods** (`restart_opencode`, `collect_state`) remain abstract in the base class, forcing every future executor implementation to provide them even before the Gateway calls them. This is documented "future surface" but still adds concrete implementation burden for anyone writing a new executor.

## Recommendations

1. **Add observation retention policy early.** Even if it's a simple `DELETE FROM runner_observations WHERE observed_at < NOW() - INTERVAL '30 days'` run by the scheduler. Document the retention SLA. Partition by time if the observation rate is predictable.

2. **Replace AWX polling with a callback/webhook pattern.** Instead of the Gateway polling AWX at 5s intervals, have AWX call back to the Gateway via a webhook when the job completes. This frees Gateway concurrency slots and reduces latency. (This is a Phase 2 recommendation, not an MVP blocker.)

3. **Add AWX template contract validation.** At a minimum, add a `GET /api/v2/job_templates/{id}/` call at Gateway startup when using the AWX executor, verifying the template exists and returning a clear error if it doesn't. Better: extend `AWXExecutorPlugin` to verify the expected `extra_vars` keys are accepted by the template.

4. **Document the Postgres bottleneck risk for port allocation.** In ADR 0003 or the operations guide, explicitly state that `SELECT ... FOR UPDATE` on the port allocation table is the first scalability bottleneck and document the symptom (workspace creation latency) and mitigation (increase `max_connections` or fall back to OS port allocation).

5. **Add a circuit breaker or timeout guard for AWX calls.** If AWX is unreachable or slow, the Gateway should fail fast rather than block a worker for the full 300s timeout. The `AWXConnectionError` and `AWXTimeoutError` exceptions exist but there's no retry policy or circuit breaker visible in the client.

## Questions That Need Answers

1. What is the expected observation volume per day, and what is the retention policy for observation data? Without this, the three observation table design cannot be evaluated for adequacy.

2. Does the Gateway have a startup health check for the AWX executor — i.e., does it verify the three template IDs are valid before accepting the first job?

3. What is the timeout behavior for the AWX executor when the Gateway's own request timeout is shorter than `GATEWAY_AWX_TIMEOUT_SECONDS`? Does a client disconnect mid-poll leave a dangling background task?

4. How does the Gateway handle concurrent workspace creations when the port range (1000 ports) is exhausted? Is there documented behavior for port starvation?

5. Has any load testing been done on the observation ingestion endpoint? At what request rate does `POST /observations` become a bottleneck?

---

**Summary:** The Gateway architecture is well-structured and makes the right trade-offs for an MVP, but the Postgres-bound observation store, synchronous AWX polling model, and fragile template ID coupling are concrete operational risks that need documented mitigation strategies before the system is considered production-ready.