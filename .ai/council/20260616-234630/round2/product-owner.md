# Council Response: Product Owner

## Reactions to Other Members

### Agreement

**Senior Engineer and Delivery Planner on synchronous `POST /jobs`:**
I missed this entirely in my Round 1. Thank you. A `POST /jobs` that blocks for 5+ minutes is not an async orchestration API — it's a synchronous RPC dressed as a job system. From a product perspective, this is the single biggest UX failure in the current design. Any caller (Paperclip agent, CI pipeline, human operator) that submits a job must hold a connection open for the entire workspace creation → coding session → diff retrieval lifecycle. That's unacceptable at MVP or any scale. A `202 Accepted` with polling or callback is the minimum viable contract. I now consider this a blocker for production readiness, not a nice-to-have.

**Skeptic on the Paperclip paradox:**
Your framing — "building the lock before the key" — is sharper than my own. The Gateway's strongest value proposition is "Paperclip needs a clean API," yet Paperclip integration (#13) is the one unfinished issue. I flagged this as a risk in R1, but you're right: it's more than a risk. It's a fundamental validation gap. Without Paperclip consuming the API end-to-end, we cannot confirm the API surface is correct, the state machine matches what an orchestrator needs, or the error semantics are usable. This should have been a prerequisite, not a trailing item.

**Everyone on future-surface YAGNI (7 of 13 methods):**
In my R1 I said "2 of 6 executor methods are speculative," which understates the problem. The Senior Engineer correctly quantified 7 of 13 methods across both abstraction boundaries (54%) that are never called at runtime. Skeptic's label — "YAGNI codified in architecture" — is fair. The ADR 0002 refinement documents this honestly, but honesty doesn't eliminate the concrete tax: every method must be implemented, tested, and reviewed by every new executor author. I now agree these should be pruned to only the methods that have call sites.

**Platform Architect on missing observation lifecycle:**
This is a critical operational gap I didn't consider in R1. Three time-series tables with no retention policy, partitioning, or downsampling will degrade predictably. As PO, the product question is: what is our observation retention SLA? Without one, we can't tell operators how long they have to debug historical runner issues, and we can't size the database. This must be documented and implemented before production.

**Cost Reviewer on session budget controls:**
You're right that the Gateway can launch an OpenCode session with no cost guardrails. A runaway session burning $500 in LM calls is indistinguishable from a $5 one. This is a product gap, not just an engineering one. The Gateway's job model should include `max_cost` and/or `max_duration_minutes` fields that are passed to OpenCode Serve. Without these, the Gateway is an accelerant for cost overruns, not a control plane.

**Delivery Planner on risk ordering:**
Your point about #10 (AWX executor) being deferred to the critical path instead of tackled as an early spike is well-taken. The highest-risk, lowest-confidence item should have been validated before the architecture was fully committed. This is a planning lesson: when 60% of the project depends on an assumption that hasn't been tested, that assumption becomes the schedule risk.

### Disagreement

**Skeptic: "AWX could handle this directly without a new service."**
I pushed back on this in R1, and I still believe the Gateway solves a real problem — but I need to be more precise about *which* problem. AWX can absolutely orchestrate a linear workflow: launch a template, wait, launch another. What AWX cannot do:

- **Domain-specific state machine.** AWX tracks job template runs, not coding workspace lifecycles. A coding job has states like `awaiting_workspace`, `running_session`, `diff_ready`, `aborting` — these don't map to AWX's job states. The Gateway's state machine encodes the coding domain, not the automation domain.
- **Pre-flight policy with domain knowledge.** AWX surveys can validate input variables, but they cannot say "don't send this job to Runner X because OpenCode would run out of disk mid-session." The policy engine's disk/memory pressure checks are specific to coding workloads.
- **Port allocation.** AWX has no concept of port management for sidecar services. The Gateway's transactional port allocation solves a real problem that AWX doesn't address.
- **Observation pipeline.** AWX gets facts during playbook runs. It doesn't maintain a continuous telemetry stream about runner health. The observation system fills a gap that neither AWX nor typical monitoring tools cover for coding-specific resource constraints.

That said, the Skeptic's push makes me realize: the Gateway's *defensible* scope is narrower than what was built. The observation pipeline, policy engine, port allocation, and domain state machine are genuinely new. The abstract executor interface with 54% unused surface is not. **If we prune the unused surface and prove the value with Paperclip, the Gateway's necessity becomes much clearer.** In its current over-abstracted form, the Skeptic's criticism is partly justified.

**Cost Reviewer: "Remove the observations/policy pipeline; use existing monitoring."**
This is the recommendation I disagree with most strongly. The observation pipeline is not a duplicate of Prometheus or Datadog — it encodes domain-specific knowledge about coding workload constraints. Prometheus can tell you "disk is at 85%." It cannot tell you "that 85% disk usage means OpenCode's SQLite database has only 500MB of headroom for the next session, so reject the job." The policy engine translates generic metrics into coding-specific go/no-go decisions.

Existing monitoring tools also lack the tight feedback loop the Gateway needs: a runner pushes metrics → Gateway evaluates policy → job is accepted or rejected — all within the same request flow. Routing through an external monitoring system (alert → operator → manual PATCH /runners) would add minutes of latency and operator fatigue.

However, I concede two points to the Cost Reviewer:
1. The observation *ingestion* could be simpler — a Prometheus exporter scrape target on the Gateway might be cheaper than a push-agent on every runner.
2. The three-table design is over-normalized for observation data. A single generic table with a metric type discriminator would be simpler and more extensible.

The pipeline as a concept stays. The implementation can be leaner.

**Cost Reviewer: "Swap Postgres for SQLite as the default."**
This is reasonable for single-node deployments but dangerous as a default recommendation. If the Gateway grows to support multiple concurrent runners and callers (which the PRD assumes), SQLite's write-lock contention becomes a real bottleneck. Postgres is the right default for an orchestration layer that expects concurrent access. I would support making SQLite viable for single-node dev deployments, but not as the production default.

**Platform Architect and Senior Engineer on database access patterns:**
The raw asyncpg in the policy layer is a maintenance trap — I agree with the Senior Engineer. But I disagree with "use SQLAlchemy everywhere" as the only fix. The policy layer's queries are simple threshold reads. A dedicated data-access module (as the Senior Engineer suggests) is the better path, keeping the policy logic fast and focused rather than routing through the ORM for simple aggregates.

### New Concerns Raised

1. **Observation authentication (Skeptic):** How do Runner VMs authenticate to `POST /observations`? I hadn't considered this at all. Unauthenticated ingestion is a poisoning vector for the entire policy engine. This is a security gap that needs resolution before production.

2. **Template ID coupling (Platform Architect):** The AWX executor cannot validate at startup that the three template IDs map to templates that accept the expected `extra_vars`. A misconfiguration produces a runtime error, not a startup error. This is an operational reliability issue that I should have flagged — operators need confidence that their Gateway config is correct before accepting jobs.

3. **AWX polling blocks concurrency (Platform Architect):** The Gateway holding a worker thread for each in-flight AWX operation (default 5s poll, 300s timeout) means concurrency scales inversely with AWX latency. A webhook callback model would decouple these. This isn't just an engineering detail — it affects how many simultaneous jobs the Gateway can handle, which is a product capacity metric.

4. `check()` **mutates state (Senior Engineer):** A method named `check` that writes to the database is a naming violation that will cause debugging pain. This is a code quality issue, but it also signals unclear design — the policy layer should not have side effects on runner status as part of a "check."

5. **No vertical-slice validation milestone (Delivery Planner):** The project's 95% completion metric counts issues closed, not integrations validated. The Gateway was built horizontally (all of Layer 1, then all of Layer 2, etc.) rather than vertically (one end-to-end flow at a time). This is a product delivery lesson: build the thin end-to-end slice first, then thicken layers.

## Updated Position

My position has evolved substantially after reading all six opinions:

**What I still firmly believe:**
- The orchestration gap is real. AWX alone cannot handle the coding-domain-specific state machine, policy engine, port allocation, and observation pipeline.
- The four-layer architecture is the right separation of concerns — though it needs significant trimming.
- The observation/policy pipeline is differentiated value and should stay, but can be implemented more simply.

**What has changed:**

1. **Synchronous `POST /jobs` is a blocker, not a smell.** I underweighted this in R1. A 202 Accepted workflow is essential. The current design cannot ship to production.

2. **Prune the future surface immediately.** The consensus across Skeptic, Senior Engineer, Delivery Planner, and even Platform Architect (who flagged the executor interface burden) is overwhelming. I now advocate removing `restart_opencode`, `collect_state`, and the 5 unused OpenCode client methods from the required interfaces. This directly addresses the Skeptic's "54% YAGNI" critique and reduces the abstraction tax the Cost Reviewer and Delivery Planner identified.

3. **Paperclip integration should gate production readiness.** I said "schedule it as next priority" in R1. I now believe: the Gateway should not be declared production-ready until the Paperclip adapter (#13) is implemented and a Paperclip-style client has successfully submitted a job, polled for completion, and retrieved a diff through the Gateway's API. This is the only way to validate the API surface against its primary consumer.

4. **Approval gates (#11) need a clearer owner.** The Skeptic's "AWX could handle this" argument plus my own concern about Paperclip layer violation makes me think approval gates should be moved to Paperclip or defined more narrowly as Gateway-level safeguards (e.g., "max concurrent jobs per runner"), not as a general-purpose workflow approval system.

5. **Success metrics must be defined before production.** I flagged this gap in R1. The consensus from every council member either assumes these exist or questions their absence. Let me propose concrete numbers:

| Metric | Proposed Target | Rationale |
|--------|----------------|-----------|
| Job completion rate (no manual intervention) | ≥ 95% | Core reliability promise |
| P95 end-to-end latency (submit → diff available) | ≤ 10 min | Covers workspace creation + coding session |
| Observation staleness detection | ≤ 120s | Runner health must be current within 2 minutes |
| Maximum concurrent jobs per runner | Configurable, default 5 | Prevents VM resource exhaustion |
| Policy false-positive rate | < 1% | Healthy runners should not be rejected |
| Session cost overrun prevented | 100% within configured limits | Cost guardrails must be enforced |

## Remaining Gaps

1. **No cost model for the AWX triple-launch pattern.** The Cost Reviewer is right that 3 AWX job launches per coding job is expensive overhead. I need actual measurement data — what fraction of total job time is AWX orchestration versus actual coding? If it's >30%, we need to reconsider the executor architecture.

2. **Observation authentication is undefined.** The Skeptic raised this and no one addressed it. How runners authenticate to push telemetry must be specified before the system can be secured.

3. **The Paperclip integration missing is now the central risk.** If Paperclip's API needs differ from what the Gateway provides, the rework cost is high because the entire API surface was designed without its primary consumer. An immediate spike to define the Paperclip-Gateway contract is the highest-priority next step.

4. **Concurrency and capacity unknowns.** No one knows: expected job volume per day, expected observation volume, expected concurrent runner count. These numbers are necessary to validate Postgres vs. SQLite, the port range, and the polling vs. webhook decision. I will make defining these a prerequisite to the production-readiness milestone.

5. **Webhook reliability is unaddressed.** The Cost Reviewer noted that `asyncio.create_task(dispatch_webhooks(...))` with no retry or queue means webhooks silently fail under load. If webhooks are the callback mechanism for job completion, this is a data loss risk.

**Summary:** The Gateway solves a real problem and its architecture is fundamentally sound, but it carries significant YAGNI surface area (54% unused interface methods), its synchronous job submission is a production blocker, the Paperclip integration gap invalidates any "95% complete" claim, and the project needs success metrics, observation authentication, and session cost guardrails before it can be considered production-ready — proceed with targeted surgery, not wholesale rewrite.
