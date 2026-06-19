# Council Response: Cost Reviewer

## Reactions to Other Members

### Agreement

**Senior Engineer — "Simpler code needs fewer tests."** This reframes my test-cost concern in a way I find persuasive. I came into Round 1 saying "650 tests is disproportionately expensive for 4K lines of orchestration glue." You're right that the fix isn't to arbitrarily delete tests — it's to simplify the code, prune the 54% unused surface, and let the test suite shrink as a natural consequence. The problem isn't 650 tests per se; it's that each test exercises abstraction layers rather than behaviors, and 30% of them test code paths that can't fail in production because they never run. When we cut the 7 unused protocol methods, the tests for them should go too. I concede the framing: *simplify the code, not the test suite.*

**Delivery Planner — The 37-issue stage-gated plan is cost-conscious.** Your plan directly addresses nearly every cost concern I raised in Round 1. Specifically:

| My R1 Concern | Gate/Issue That Addresses It |
|---|---|
| AWX triple-launch overhead dominates cost | Gate 2, Issue 2.7: measure AWX orchestration-to-coding ratio |
| 650-test suite is too expensive | Gate 5: rationalize to ≤400 tests, ≤5 min CI |
| No cost guardrails for OpenCode sessions | Gate 4, Issue 4.1: max_duration_minutes, max_cost |
| Future-surface tax inflates everything | Gate 0: prune 7 unused methods immediately |
| Observation ingestion duplicates tooling | Gate 4, Issue 4.6: defer or document auth |

The sequencing is right — prune before building, measure before optimizing — and the 15–30 minute issue granularity is a structural cost control in itself. No single mistake can cost more than 30 minutes of rework. This is the delivery equivalent of bounded-error arithmetic.

**Skeptic — Three hard gates.** I agree with all three, with one sequencing caveat. Gate 1 (surface-area pruning) directly reduces development and testing cost before any new work starts — pure efficiency. Gate 2 (end-to-end validation) is critical because it will produce the AWX overhead measurement I've been asking for. Gate 3 (observation auth) is necessary only if observations stay in MVP; if observation ingestion is deferred (see below), Gate 3 becomes "document the design" rather than "implement auth." But the gates are sound.

**Product Owner and Platform Architect — The policy evaluation logic is differentiated value.** I accept this. My Round 1 recommendation was too binary when I said "remove the observations/policy pipeline." The Skeptic drew the right distinction: separate the *ingestion* (the custom push-metric pipeline) from the *evaluation* (the domain-specific disk/memory pressure rules). The evaluation logic is value I should not have dismissed. More on this below.

### Disagreement

**Product Owner, Platform Architect, Senior Engineer, Delivery Planner — "Postgres is the right choice, SQLite is wrong."** I concede the main argument. Let me be specific about what I am conceding and what I am not.

**Conceded:** The Gateway needs to support concurrent writers — multiple callers (Paperclip, humans, CI systems) submitting jobs, the Gateway writing state, and an observation ingestion pipeline writing concurrently. At the expected multi-service deployment topology, SQLite's write-lock contention (even with WAL mode) is a real risk. The Delivery Planner is right that now is the wrong time to swap databases — the three higher-priority items (async POST /jobs, AWX vertical slice, Paperclip validation) would be delayed by a data-layer rewrite with no proportional benefit. I withdraw the "swap Postgres for SQLite" recommendation as a general directive.

**Not conceded — and this matters more than the database choice itself:** The observation data does not justify Postgres. The Platform Architect concedes this ("the observation data does NOT need Postgres"). The Senior Engineer concedes this ("Postgres is overkill if the observation pipeline is trimmed"). The Skeptic has been saying this since R1. Three separate tables for telemetry, stored in a transactional RDBMS, with no retention policy, ingested through a push-agent on every runner VM — this is the cost hotspot, not the state machine data.

The fix: store observation data in a simpler form (single generic table with metric type discriminator, as the Product Owner now suggests) or defer the observation *ingestion* endpoint from MVP entirely while keeping the policy engine consuming runner status from `PATCH /runners/{id}/status` set by existing monitoring tools. This is where the real cost saving lives.

**Product Owner — "The observation pipeline is differentiated value."** I accept that the *policy evaluation* logic is value. But the *observation ingestion pipeline* — the push-agent on every runner VM, the `POST /observations` endpoint with unauthenticated access, the three Postgres tables with no retention policy — is a cost center that has no consumer today. The Senior Engineer and Skeptic both asked: who reads observations? The answer appears to be "the policy engine" — but the policy engine could read the same signal from existing monitoring tools via a simple `GET /runners/{id}/status` that follows from a `PATCH`. The Product Owner's Prometheus-scrape-target suggestion is a good middle ground: the Gateway exposes a `/metrics` endpoint that Prometheus scrapes, and the policy engine reads from that same data. No custom push-agent, no auth problem (Prometheus handles it), no three-table design.

I accept the pipeline stays as a concept. I ask only that the implementation be drastically simplified: one table, Prometheus-compatible ingestion, or deferral to Phase 5.

### New Concerns Raised

**Delivery Planner — "Cost guardrails should be in Gate 0/1, not Gate 4."** This is the most important scheduling point in the entire plan. Every job launched before Issue 4.1 is implemented is a potential unbounded-cost session — $500 in LM calls indistinguishable from $5. The Delivery Planner has cost guardrails in Gate 4 (production safety), which is ~7.5 days of delivery after Gate 0. I argue this should be in Gate 1 or Gate 2 at the latest. Adding `max_duration_minutes` and `max_cost` fields to the job request model is a 30-minute issue (the Delivery Planner's own estimate). There is no architectural dependency that prevents it from being done earlier. I'd move Issue 4.1 to Gate 1 — after surface pruning but before any real AWX jobs run.

**Platform Architect — "Observation ingestion should be optional."** We agree. The observation pipeline as a required component of MVP creates cost (Postgres tables, auth design, push-agents) for value that has no consumer. Making observations optional — and the default deployment mode relying on `PATCH /runners` for runner health — would let the team validate the policy engine without the telemetry overhead.

**Senior Engineer — AWX triple-launch overhead is unmeasured.** Your disagreement with my AWX cost concern wasn't a refutation — it was a call for data. You said "the AWX coupling paradox" can't be evaluated without measurement. Issue 2.7 in the Delivery Planner's plan addresses this. I'm satisfied that the plan includes measurement before architectural commitment.

**Skeptic — The observation auth gap.** This is a production blocker whether observations stay in MVP or not. If they stay, auth must be designed before deployment. If they're deferred, the auth design must be documented for when they're added. The Skeptic's Gate 3 formulation is correct.

## Updated Position

My position has **conceded several points but sharpened on the two that matter most for cost**.

**What I concede:**

1. **Postgres as the state store.** The Gateway needs it for concurrent writer support. The Delivery Planner is right that now is the wrong time to swap. I withdraw the SQLite recommendation as a general directive.

2. **The policy evaluation logic is value.** My Round 1 dismissal of the entire observation/policy pipeline was too broad. The domain-specific threshold checks (disk pressure, memory pressure) are genuinely novel in the coding-orchestration space. I accept they should stay.

3. **Test count is a symptom, not a cause.** The Senior Engineer's reframing — "simpler code needs fewer tests" — correctly shifts the intervention point from "delete tests" to "simplify code." The 650-test count will drop when the 54% unused surface is pruned and the God function is decomposed. I no longer advocate arbitrary test deletion.

**What I hold firm on:**

1. **The observation ingestion pipeline (push-agents, three Postgres tables, authless endpoint) should be deferred from MVP.** The policy engine can consume runner health from existing monitoring tools via `PATCH /runners/{id}/status`. This eliminates the custom telemetry cost while preserving the policy evaluation value. The Product Owner and Platform Architect defended the *evaluation* logic, not the *ingestion* pipeline — and those are separable.

2. **Session cost guardrails are non-negotiable and should be added in Gate 1 or Gate 2, not Gate 4.** Every day the Gateway can launch unbounded-cost OpenCode sessions is a financial risk day. A 30-minute issue to add `max_cost` and `max_duration_minutes` to the job model should not be deferred to the production safety phase.

3. **The AWX triple-launch overhead measurement (Issue 2.7) is the single most important data point for validating the entire cost model.** If AWX orchestration >30% of total job time, the executor architecture needs fundamental rethinking (SSH-direct executor, lighter agent, or AWX workflow simplification). The Delivery Planner correctly placed measurement alongside implementation rather than before it, but I want to emphasize: this data point determines whether the Gateway's cost structure is viable.

**Where I converged with the council:**

| Axis | My R1 Position | Council R2 Position | My R2 Position |
|---|---|---|---|
| Postgres vs SQLite | Swap for SQLite | Keep Postgres | Concede — Postgres is right for concurrent access |
| Observation pipeline | Remove entirely | Keep policy eval, simplify ingestion | Accept policy eval; request simplified/deferred ingestion |
| Test suite | Delete 30% arbitrarily | Simplify code, tests shrink naturally | Accept — prune surface first, tests follow |
| Cost guardrails | Add to job model | Multiple members agree | Hold firm — move to Gate 1/2 |
| AWX cost measurement | "Go measure it" | Issue 2.7 in delivery plan | Satisfied — plan includes it |

## The Delivery Planner's 37-Issue Plan: Cost Review

From a pure cost perspective, here is my assessment of each gate:

**Gate 0 (Scope Pruning)** — **High cost-value.** Removing 7 unused protocol methods eliminates ~30% of test surface, ~20% of documentation burden, and ~20% of implementation friction for new executors. This is the highest-ROI gate in the plan. The 5 issues at 15 min each = 75 minutes of work that pays for itself within the next gate.

**Gate 1 (Async Job Submission)** — **High cost-value.** The synchronous `create_job` handler is the root cause of complex, expensive-to-maintain tests. Making it async reduces test flakiness, simplifies error handling, and enables independent delivery of each lifecycle step. The 8 issues at 2.5 days is the largest gate but its ROIs are (a) reduced testing cost, (b) reduced debugging cost, (c) ability to deliver each lifecycle step as a separate 15-30 min issue.

**Gate 2 (AWX Vertical Slice)** — **Contains the critical cost measurement (Issue 2.7).** This is the moment we learn whether my AWX cost concern is real (orchestration >30% of job time) or theoretical. The gate is necessary regardless of the answer: if AWX overhead is high, we know to build an SSH-direct executor; if low, we can stop worrying about the triple-launch pattern. The 2.5-day investment is essential due diligence.

**Gate 3 (Paperclip Validation)** — **Cost efficiency through early validation.** Preventing API-surface mismatches costs 1.5 days now vs. weeks of rework later. This is the cheapest insurance policy in the plan.

**Gate 4 (Production Safety)** — **Concern: cost guardrails are too late.** Issue 4.1 (max_duration_minutes, max_cost) should be in Gate 1. Everything else in this gate is appropriately sequenced. Observation auth (4.6) depends on whether ingestion is deferred — if it is, 4.6 becomes a documentation issue (15 min), not an implementation issue.

**Gate 5 (Test Rationalization)** — **Delivers on my R1 concern.** Targeting ≤400 tests and ≤5 min CI runtime from the current 650+ tests and unknown CI time. The 5 issues at 1 day is a reasonable estimate. Sequencing it after Gate 0 (pruning) is correct — you can't rationalize tests for methods you've already removed.

**Gate 6 (Delivery Documentation)** — **Low cost, high value.** 2 issues at 30 min total. The success metrics (Issue 6.2) will provide cost accountability going forward.

**Overall cost assessment of the plan:** **$8,500–$17,000 of engineering time** (assuming 10.5 days × 1-2 engineers at $800/day fully loaded). This is 6-12% of what the project has already cost (estimated at $140K for the existing codebase). The plan is cost-justified because it addresses the three biggest cost risks in the current system: (1) unbounded session costs (if we move Issue 4.1 earlier), (2) unmeasured AWX overhead, and (3) test suite that slows every change.

**One recommendation:** Add a budget line item to Gate 2: if Issue 2.7 reveals AWX orchestration >30% of total job time, allocate 2 additional days to build an SSH-direct executor plugin. This is a conditional cost — spend only if the measurement triggers it. Making it explicit in the plan prevents the "we knew this was a problem but didn't budget for it" scenario.

## Remaining Gaps

1. **No Postgres hosting cost estimate exists in the plan.** The monthly cost of a production Postgres instance (RDS, Cloud SQL, or self-hosted) should be documented alongside the expected job volume. This is a $15–$200/month variable depending on scale, and it directly affects whether the operational cost is acceptable.

2. **The observation ingestion deferral is still a design decision, not an implementation.** If the team decides to keep observations in MVP (against the converging recommendation of the Skeptic, Senior Engineer, Delivery Planner, and myself), the delivery plan needs an additional Gate 4 issue for observation auth implementation — not just documentation. I estimate that adds 1-2 days to the plan.

3. **Cost guardrails are still in the wrong gate.** I've stated my case. If the council agrees, Issue 4.1 moves to Gate 1. If not, the Gateway will launch unbounded-cost sessions for ~7.5 days of delivery wall-clock time. I accept the council's decision but want my objection recorded.

4. **The AWX overhead measurement (Issue 2.7) has no pass/fail criterion.** What happens if the AWX orchestration-to-coding ratio is 40%? The plan should specify: if >30%, allocate SSH-direct executor budget. This is a simple threshold-based decision rule that makes the measurement actionable.

---

**Summary:** The Gateway is cost-feasible *if* we (a) move session cost guardrails to Gate 1 so no unbounded-cost job can launch before they exist, (b) measure the AWX triple-launch overhead before committing to the executor architecture, (c) defer the observation ingestion pipeline and run the policy engine against runner status set by existing monitoring tools, and (d) accept the 37-issue $8,500–$17,000 delivery plan as a necessary investment to validate the cost model before scaling — my final position is conditional approval with one non-negotiable (cost guardrails before real jobs) and two strong recommendations (defer observation ingestion, measure AWX overhead with a threshold decision rule).
