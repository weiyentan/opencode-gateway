# Council Opinion: Skeptic

## Summary

The OpenCode Gateway is a well-crafted solution to a problem that may not need solving. It introduces a full standalone service, Postgres database, four abstraction layers, and 650+ tests to orchestrate a linear six-step workflow that could be handled by AWX job templates calling OpenCode Serve's HTTP API directly. The architecture shows clear signs of premature abstraction ("intentional future surface" on 7 of 13 interface methods), and the entire project rests on the unvalidated assumption that there will be callers (Paperclip) who need this specific API shape. I am not convinced the Gateway earns its keep over a simpler AWX-native approach.

## Assessment

### 1. The core question: Why does this exist?

The team already operates AWX. OpenCode Serve exposes an HTTP API. The Gateway's job is to: receive a job request → pick a runner → create a workspace → start OpenCode → wait → collect diff → clean up. That is a linear, seven-step workflow.

AWX job templates can call HTTP APIs via `uri` modules. They can chain templates via workflow templates. They already handle job tracking, retries, credentials, RBAC, and audit logging. So what exactly does the Gateway add that AWX doesn't already provide? Let me enumerate:

- **Job/workspace state in Postgres**: AWX already stores job state. The Gateway duplicates this in a separate database, requiring reconciliation.
- **Pre-flight policy checks**: These are simple threshold queries against observation data. They could run as an AWX survey validation or an initial playbook task.
- **Diff retrieval**: An HTTP GET from AWX to the OpenCode Serve instance.
- **Observation telemetry**: A cron job on the Runner VM pushing metrics. Could go to AWX as a fact, or to a standalone metrics endpoint.
- **Unified API for Paperclip**: This is the real value proposition. But Paperclip integration (#13) is the one issue NOT done. The primary consumer of the API doesn't exist yet.

The Gateway's strongest justification is "Paperclip needs a clean API." But since Paperclip doesn't exist yet, we're building a restaurant kitchen before we know if anyone will eat there.

### 2. Abstraction layering: elegant or excessive?

The architecture has four layers: API → Core → Executor Plugin → OpenCode Client. Let's audit how much of each layer is actually used at runtime:

**Executor Plugin (6 methods)**: 4 called, 2 are "intentional future surface" — never invoked.

**OpenCodeClientProtocol (7 methods)**: The ADR says 2 are called at runtime (get_session_diff, abort_session). The docstring in protocol.py claims 4 are active. Either way, 3–5 of 7 methods are unused.

That means **7 of 13 interface methods across the two key abstraction boundaries are not called at runtime**. That's 54% unused surface area. The ADR 0002 docstrings call this "intentional future surface," which is a polite term for YAGNI.

This is classic premature abstraction: you build a generic interface because you *might* need it later, but in doing so you add complexity, testing burden, and cognitive load today for a future that may never arrive. When was the last time a second executor backend was actually implemented? The registry has only "awx" and "local" — and "local" is a dev-only stub. There is no SSH executor, no Kubernetes executor. The abstraction exists to justify itself.

### 3. Postgres for time-series observations

The observation tables (runner_observations, workspace_observations, opencode_instance_observations) are time-series data. ADR 0001 acknowledges that "PostgreSQL handles many tables efficiently" and "there is no performance concern at MVP scale." The qualifier "at MVP scale" is doing heavy lifting.

Time-series data has different access patterns than transactional orchestration data. Queries like "give me the average disk usage over the last 24 hours" require table scans or specialized indexes in Postgres. A TSDB like TimescaleDB or InfluxDB handles this natively with continuous aggregates and downsampling.

ADR 0001 dismissed the single-generic-table approach because it "shifts type safety to application code." But for time-series telemetry, that is exactly the right trade — the observation data is write-heavy, append-only, and rarely updated. The type safety concern is overblown for metrics that are essentially (timestamp, runner_id, disk_pct, mem_pct).

The decision to use three separate tables means every new observation type requires: (a) a new table, (b) a new migration, (c) new Pydantic models, (d) a new API endpoint or modification. That's a lot of ceremony for adding a metric like "network_io" or "cpu_temperature."

### 4. Port range: 1000 is a guess

The port range of 10000–10999 (1000 ports) is presented as fact in ADR 0003. Where does this number come from? Is it based on expected concurrency? VM capacity? Network constraints? There is no rationale — it's just "1000 is enough."

What happens when runner VMs host multiple workspaces? The port range scales per VM, not globally. What happens at 1001 concurrent workspaces on a single VM? The system presumably starts failing with port conflicts, but there's no graceful degradation, no alerting, no auto-scaling trigger.

A hardcoded magic number for a scalability-critical resource is a red flag. If you're going to put port allocation in the database (Postgres as lock server), you should at least document how the range was derived and what happens at the limit.

### 5. The AWX dependency chain

The Gateway → AWX → Runner VM → OpenCode Serve chain has five active systems. ADR 0004 acknowledges that if AWX is unreachable, "Gateway cannot directly intervene on the VM." This means:

- The Gateway's operational uptime is strictly bounded by AWX's uptime.
- When AWX is down, the Gateway is a glorified Postgres viewer.
- Debugging failures requires tracing through: Gateway logs → AWX API → AWX job logs → AWX execution node → VM syslog. ADR 0004 flags this as a negative consequence but doesn't offer mitigations.

The stated benefit of the Gateway is abstraction and decoupling, yet the system is tightly coupled to AWX for every meaningful operation. If AWX goes away (or the team switches executors), the Gateway's entire operational model changes. This is not abstraction — it's indirection with the same coupling.

### 6. Observation authentication gap

Runners push observations to `POST /observations`. How do they authenticate? The Gateway needs to accept telemetry from untrusted network sources (Runner VMs). ADR 0004 says "Gateway holds no infra secrets," which is an internal security posture. But what about external auth? 

If runner VMs need to authenticate to push observations, they need some kind of token or API key. Where is that stored? On the VM? That's an infrastructure secret stored on the infrastructure. ADR 0004's concern applies equally here — if a VM is compromised, the observation auth token is compromised.

If observation ingestion is unauthenticated, then anyone who can reach the Gateway endpoint can poison the observation database, causing false policy violations or denial of service.

This looks like an unaddressed security gap.

### 7. 650+ tests for an untested system

The project has 650+ tests across 28 test files. That's a solid test suite. But for a project that's 95% done with zero production usage, this test count is suspicious. It can mean:

- **Good**: Thorough testing of a complex system.  
- **Worrisome**: The codebase is complex enough to need 650+ tests for confidence.  
- **Also worrisome**: Tests were written to validate abstractions rather than behaviors.

The fact that 54% of interface methods are unused at runtime means a significant portion of those tests cover code paths that have never been exercised in an integrated environment. Tests for `restart_opencode` and `collect_state` are testing things that cannot fail in production because they don't run in production.

## Key Concerns

1. **Unvalidated core assumption**: The Gateway's primary raison d'être is serving Paperclip, but Paperclip integration (#13 of 18) is the only issue not done. The API was designed for a consumer that doesn't exist yet — building the lock before the key.

2. **54% unused interface surface**: Of 13 methods across two abstraction boundaries, 7 are documented as "intentional future surface." This is YAGNI codified in architecture. Every unused method is a testing burden, a maintenance liability, and a cognitive cost for new developers.

3. **AWX could do this without a new service**: The Gateway reimplements job tracking, state management, and workflow orchestration that AWX already provides. The team's existing strength is AWX, yet the Gateway abstracts AWX away. This is circular — you're building a layer on top of a tool you already know how to use directly.

4. **Five-system failure chain**: A single job requires Gateway + AWX API + AWX execution + Runner VM + OpenCode Serve. Any one failure breaks the chain. The Gateway adds a failure point without reducing any existing ones.

5. **Observation auth not addressed**: How do Runner VMs authenticate to push telemetry? ADR 0004's "no infra secrets" policy is silent on this. If unauthenticated, the policy engine is trivially poisonable.

6. **Port range of 1000 is an arbitrary guess**: No documented rationale for the number. No degradation behavior at the limit.

7. **Postgres for time-series is the wrong tool for scale**: Fine at MVP scale, but the architecture doesn't account for the query pattern mismatch. The generic-table alternative was dismissed for purity (type safety) rather than pragmatism.

## Recommendations

1. **Kill the "intentional future surface" methods** — Remove `restart_opencode`, `collect_state`, and the unused OpenCode client methods. They are dead code. Re-add them when a real call site exists. This immediately reduces the abstraction surface by half and eliminates the corresponding testing burden.

2. **Prove the Gateway's value with a real Paperclip integration before adding more features** — Do not ship the Gateway to production without a working Paperclip adapter. Until Paperclip calls the API successfully in an end-to-end test, the Gateway is a solution in search of a problem.

3. **Consider making the Gateway a thinner AWX wrapper** — Instead of a full Postgres-backed service, consider whether the Gateway could be a lightweight proxy that translates Paperclip's API calls into AWX job launches, with state tracked in AWX's existing database. This eliminates the Postgres dependency and the state reconciliation problem.

4. **Document the observation auth story** — Even if the answer is "we use a shared secret per VM" or "we rely on network segmentation," write it down. The current silence on this is a security gap.

5. **Replace the magic port number with a configurable capacity model** — Make the port range configurable. Document how to choose the value. Add alerting when usage approaches the limit.

## Questions That Need Answers

1. **What would break if we deleted the Gateway today and called OpenCode Serve from AWX job templates directly?** If the answer is "Paperclip would need to change," that's fine — Paperclip doesn't exist yet, so the API surface can be whatever Paperclip needs. If the answer is something else, what is it?

2. **Who is the real user of this system?** Is it Paperclip (which doesn't exist), or is it a human operator? The API surface and documentation suggest Paperclip, but the 29 user stories in the PRD describe "platform engineers" and "gateway operators" too. Who is the primary stakeholder?

3. **What is the expected observation volume?** 10 observations per minute? 10,000? Without this number, the decision between Postgres and a TSDB is guesswork. What happens to query performance when the runner_observations table has 10 million rows?

4. **How does a Runner VM authenticate to POST /observations?** If the answer involves a token stored on the VM, where does that token come from and how is it rotated?

5. **Has anyone run this end-to-end with a real OpenCode Serve instance on a real VM through AWX?** Or has it only been tested with the local executor in dev? The "95% done" claim is based on issue completion, not production validation.

**Summary:** The OpenCode Gateway is a well-engineered solution in search of a proven problem — it introduces a full service, database, and four abstraction layers to orchestrate a workflow that AWX could handle directly, with 54% of its interface methods unused at runtime and its primary consumer (Paperclip) still unbuilt.
