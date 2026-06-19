# Council Response: Skeptic

## Reactions to Other Members

### Agreement

**Senior Engineer and Delivery Planner on synchronous `POST /jobs` as the central architectural failure.** The 1127-line God function is not just a code smell — it's the single strongest piece of evidence that the Gateway doesn't yet know what it is. A job orchestration system whose primary API call blocks for up to 5 minutes, holding a connection open across the entire workspace creation → coding session → diff retrieval lifecycle, is a synchronous RPC with an async workflow's nameplate. When the Senior Engineer says "solve scope first, then complexity" and the Delivery Planner says this "prevents incremental delivery," they're converging on the same root cause: the Gateway was architected as a proxy, not a control plane. This is the single issue I'd fix before anything else.

**Product Owner: "The Gateway's defensible scope is narrower than what was built."** This is the most important concession in the entire Council discussion. If the Product Owner — the role responsible for scope — agrees that the Gateway is overbuilt, that's a signal I should pay very close attention to. Your partitioning (state machine + policy engine = genuine value; abstract executor surface with 54% unused = speculative) matches my own assessment almost exactly. The difference between your R1 and your R2 is the difference between defending what the Gateway *could* be and acknowledging what it *actually* is. I respect the shift.

**Cost Reviewer on observation monitoring duplication.** You were spot-on that `POST /observations` reinvents a wheel that every organization already owns. The Product Owner's rebuttal — "the policy engine encodes coding-specific knowledge" — is valid *for the policy evaluation step*, but it doesn't justify building a custom push-metric pipeline from scratch. The policy engine could consume runner health data from Prometheus, Datadog, or any existing source, via the `PATCH /runners/{id}/status` endpoint that already exists in the API concept. The observation *ingestion* pipeline is the duplication. The policy *evaluation* logic is the value. These are separable, and I'll address this below.

**Cost Reviewer on 650-test suite cost and the Senior Engineer's counter.** The Senior Engineer says "simpler code needs fewer tests," which is true, but the Cost Reviewer's point stands: right now, 650 tests cover a codebase where 54% of the interface surface is untested in production. The test suite tests abstraction layers, not behaviors. When the unused methods are pruned and the God function is decomposed, the test suite should shrink naturally. But that's a forward-looking argument — the Cost Reviewer's point about *today's* cost is unanswerable.

**Delivery Planner on risk ordering inverted.** The AWX executor being deferred to the critical path is the most consequential delivery mistake in this project. The entire Gateway architecture is validated by one concrete executor implementation. If the AWX executor had revealed a fundamental flaw — say, the polling model doesn't work at scale, or the three-template mapping is too rigid, or the `collect_state → teardown` semantic mismatch is unfixable — the rework would have cascaded through at least 6 issues. The fact that everyone except the original delivery plan recognizes this tells me the project was built with a blind spot on validation sequencing.

### Disagreement

**Platform Architect: "Postgres is fine for time-series at this scale."** I need to push back harder. The issue isn't whether Postgres *can* store time-series data — of course it can, at hundreds-of-runners scale with partitioning, it'll work. The issue is that the architecture *chose* a relational model (three domain-specific tables) for data that is write-once, append-heavy, and rarely updated. Each new observation type requires a new table, a new migration, new Pydantic models, and a new endpoint. That's ceremony for what should be (timestamp, runner_id, metric_name, value) tuples in a single generic table. ADR 0001 dismissed this approach for "shifting type safety to application code" — but for telemetry data, that's exactly the right trade. The Product Owner now agrees: "A single generic table with a metric type discriminator would be simpler and more extensible." When two roles that don't normally converge on data model design agree against the three-table approach, it's time to reconsider.

**Product Owner and Platform Architect: "The observation/policy pipeline is differentiated value and must stay."** I concede that the policy *evaluation* logic (translating disk/memory metrics into coding-specific go/no-go decisions) is genuinely novel and belongs in the Gateway. But the observation *ingestion* pipeline is a separate architectural concern. The cost of maintaining custom push-agents on every Runner VM, a dedicated `POST /observations` endpoint with undefined auth (more on this below), and three Postgres tables for telemetry is not justified when existing monitoring infrastructure can set the runner's status. **I am not proposing removing the policy engine. I am proposing separating the policy engine from the observation ingestion pipeline.** The policy engine evaluates thresholds against whatever state the runner is in. That state can come from:
  - Existing monitoring tools calling `PATCH /runners/{id}/status`
  - Manual operator intervention  
  - An observation ingestion endpoint (if one is needed later)

This keeps the value (domain-specific policy evaluation) while eliminating the cost (custom telemetry pipeline). If the observation pipeline is genuinely needed at scale, add it *then* — with proper auth, retention, and a single generic table.

**Senior Engineer: "Template ID coupling is acceptable, the implicit extra_vars contract is the real fragility."** This is a matter of degree. I agree that the `extra_vars` contract is fragile (the Gateway sends keys that the AWX template must accept, with no compile-time validation). But from an operational perspective, a misconfigured template ID that crashes at runtime on the first job is just as bad as a runtime `extra_vars` mismatch. Both produce the same user-visible failure mode: "I configured my AWX executor, started the Gateway, submitted a job, and got a runtime error." The Platform Architect's startup validation check would catch both classes of error if it's thorough enough — but it would need to check the template schema, not just existence.

### New Concerns Raised

**Observation authentication (my own concern — now reinforced by the Product Owner, Platform Architect, and Senior Engineer all acknowledging it as an unaddressed gap).** In my R1, I called out that `POST /observations` has no documented auth story. Every other council member who touched on this agreed it's a gap. The fact that *no one* has an answer — not even the team who built it — confirms this is not just undocumented; it's undefined. This is a production blocker. If runner VMs authenticate with a token stored on the VM, and that VM is compromised, the attacker can poison the observation database, driving false policy violations or false health clears. If the endpoint is unauthenticated, anyone on the network can poison it. This needs a design decision before any production deployment.

**OpenCode session cost guardrails (raised by Cost Reviewer, now echoed by Product Owner and Senior Engineer).** The Gateway launches OpenCode sessions with no mechanism to limit token spend, session duration, or tool call count. An unbounded session costing $500 in LM calls is indistinguishable from a $5 one at the Gateway level. This is a product gap the Product Owner now acknowledges. If the Gateway is a "control plane," the bare minimum control is knowing when to kill an expensive session.

**The `check()` method that mutates state (Senior Engineer).** I missed this in R1. A method named `check` that writes to the database is a violation of the principle of least surprise. It's a small thing, but it signals unclear design ownership — the policy layer shouldn't have side effects on runner status during a "check." The Senior Engineer's suggestion (rename it or extract the mutation) is the right call.

**No end-to-end vertical slice exists (Delivery Planner, Platform Architect, Senior Engineer — all converge independently).** This is the most damning operational finding in the entire Council. The project claims 95% completion, yet no one can point to a single end-to-end job flow through the full Gateway → AWX → Runner VM → OpenCode Serve chain that was run and verified. The Gateway was built in horizontal layers (all of Layer 1, then all of Layer 2) rather than vertical slices. This means the integration interfaces — which are the whole point of the Gateway — have never been validated together. The Delivery Planner's "95% complete is misleading" critique is validated by every other council member who examined the integration surface.

## Updated Position

My position has **shifted from "reject" to "proceed conditionally — with more conditions than anyone else."** Let me explain.

**What I now concede:**
- The domain-specific job state machine (workspace lifecycle, coding session states, diff availability) is genuinely novel and something AWX does not provide. The Product Owner's defense — that AWX tracks playbook runs, not coding workspace states — is correct. I was too dismissive of this in R1.
- The pre-flight policy engine (disk/memory pressure checks specific to OpenCode workload constraints) is differentiated value. Even if I want to decouple it from the observation ingestion pipeline, the *evaluation logic* itself is a genuine Gateway contribution.
- The port allocation in Postgres is a pragmatic solution to a real problem (multiple coding sessions on a single Runner VM need unique ports) that AWX has no concept of.
- The four-layer separation (API → Core → Executor → OpenCode Client) is architecturally sound. The Platform Architect's defense of this layering is well-reasoned.

**What I do NOT concede:**
- The AWX triple-launch pattern per job is indefensible overhead for operations that amount to "mkdir" and "systemctl start." The Cost Reviewer's estimate that 3 AWX launches per job dominates infrastructure cost at scale has not been refuted by anyone. Until the team measures and publishes the AWX orchestration overhead as a percentage of total job time, this remains an unvalidated cost assumption.
- Postgres for time-series observations remains the wrong tool, even at "MVP scale." The three-table design with no retention policy proves the point: the architecture treats telemetry as relational data, but telemetry is not relational. The simple fix: one generic table, configurable retention, auth on ingestion.
- The 54% unused interface surface is indefensible. Every council member who examined this agreed it's a problem. The Product Owner now calls it "speculative." The Platform Architect flags it as "ammunition for the claim the Gateway is over-engineered." The Senior Engineer says "prune to the bone." The Delivery Planner says "future-surface tax inflates delivery cost." The Cost Reviewer says "test suite tests dead code." This is not a contrarian position anymore — it's the consensus.
- The Paperclip paradox is unresolved. The Gateway's API was designed for a consumer that doesn't exist yet and has never validated the surface. The Product Owner now says "Paperclip integration should gate production readiness." I agree, but I go further: **the Gateway should not be declared feature-complete until a Paperclip-style client has submitted a job, polled for completion, and retrieved a diff through the Gateway's API end-to-end.** Without this, "95% complete" is a features-built metric, not a system-works metric.

**Where I clarify:**
- On "AWX could replace the Gateway": I was half-right and half-wrong. AWX could replace the *executor invocation* and the *job tracking* layers of the Gateway. But AWX cannot replace the domain-specific state machine, the policy engine, or the port allocation. The Gateway's defensible core is real. The problem is that the Gateway built a large shell around that core — the abstract executor interface, the unused protocol methods, the custom observation pipeline — that inflates the system without adding proportional value. **If the Gateway were stripped to its core (state machine + policy evaluation + port management) and the executor interface were a thin 3-method concrete class, my R1 argument would collapse.** The fact that it's not that simple today validates my skepticism.

**My verdict: conditional proceed, with three hard gates:**

1. **Surface-area gate**: Strip the 7 unused protocol methods, make `restart_opencode` and `collect_state` optional, and consolidate the OpenCode client to only the methods that have call sites. The remaining interface should be no more than 6 methods total (down from 13). Target: 40-50% reduction in abstraction surface before any new features.

2. **Validation gate**: Run one end-to-end job through the full Gateway → AWX → Runner VM → OpenCode Serve chain, documented with timestamps, outputs, and failure cases. The test should include: successful completion, policy rejection (disk pressure), and graceful AWX failure. Without this, the project cannot claim integration confidence.

3. **Observation auth gate**: Design and document the runner authentication mechanism for observation ingestion before production. Even if the MVP answer is "we don't need observations yet; we set runner status via PATCH," write it down and implement a basic token-based auth when observations are added back.

Until these three gates are met, I cannot sign off on production readiness. The Gateway is a promising foundation, but it's not yet simpler than the alternatives it competes with.

## Remaining Gaps

1. **No measured AWX orchestration overhead.** The Cost Reviewer's concern that the AWX triple-launch pattern dominates job cost is unrefuted. Someone needs to time the AWX portion vs. the coding portion of a real job and publish the ratio. If AWX orchestration >30% of total job time, the executor architecture needs rethinking. This data does not exist.

2. **The Paperclip contract is undefined.** The Gateway's API was designed in a vacuum. The Paperclip adapter (#13) needs to be a discovery exercise, not an implementation task: what does Paperclip actually need from the Gateway? Until that contract is written down (as an ADR or API specification), the Gateway's surface area is speculative.

3. **Observation pipeline has no consumer.** The Senior Engineer and I both flagged this: no one reads observations today. The Product Owner defends the pipeline as "differentiated value," but value that is never consumed is not value — it's cost. Build the consumer (Paperclip dashboard? operator alerting?) before expanding the producer.

4. **The synchronous POST /jobs fix has no design.** Everyone agrees it's a blocker. No one has specified the async workflow. Does the Gateway return 202 with a Location header and the caller polls? Does the Gateway push via webhook on completion? This needs an ADR-level design decision before the Delivery Planner can slice the implementation work.

5. **No concurrency or capacity numbers exist.** Expected jobs per day, concurrent runners, observation volume, port utilization rate — none of these are documented. The 1000-port range, the Postgres-vs-SQLite decision, and the polling-vs-webhook tradeoff all depend on these numbers. Without them, architectural decisions are guesses.

**Summary:** The Gateway has a legitimate core — domain state machine, policy evaluation, port management — that AWX cannot replace, but the project built a 54%-unused abstraction shell around that core, validated no end-to-end integration before claiming completion, left observation authentication undefined, and shipped a synchronous `POST /jobs` that defeats the purpose of an async job system — proceed only after pruning the surface area to the bone, running one real job end-to-end, and documenting who authenticates what.
