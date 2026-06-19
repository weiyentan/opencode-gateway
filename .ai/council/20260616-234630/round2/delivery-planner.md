# Council Response: Delivery Planner

## Reactions to Other Members

### Agreement

**Senior Engineer and Platform Architect — AWX executor should be built FIRST, not last.**

This is the most consequential realignment in the council. Two roles that rarely converge on sequencing both now agree the highest-risk item belongs at the front of the queue. The Senior Engineer's Phase 3 explicitly reorders "AWX executor *first* to validate the abstraction." The Platform Architect's Phase 1 says "implement the AWX executor first, build one end-to-end job flow." This directly validates my Round 1 claim that risk ordering was inverted.

The practical implication: if all three of us agree the AWX vertical slice should lead, that is a supermajority on delivery sequencing. The original plan's Layer 0→1→2a→2b ordering is dead. The new sequencing is: **prune surface → async POST /jobs → AWX vertical slice → Paperclip validation → production safety**. AWX moves from the trailing edge to the leading edge (after the two prerequisite refactors).

**Product Owner — Paperclip integration should gate production readiness.**

This aligns perfectly with my Round 1 position. I said the Gateway's API was designed without its primary consumer; the PO now says "the Gateway should not be declared production-ready until the Paperclip adapter is implemented." This changes the delivery plan in a specific way: Paperclip is no longer a trailing "nice-to-have" issue (#13 of 18). It becomes **Gate 3** — the validation milestone that confirms the API surface is fit for purpose. Until a Paperclip-style client successfully submits a job, polls for completion, and retrieves a diff through the Gateway, the system is not done. This is no longer negotiable.

**Skeptic — three hard gates (surface-area pruning, end-to-end validation, observation auth).**

The Skeptic's three gates map almost perfectly onto my resliced delivery plan. Gate 1 (surface-area pruning) matches my recommendation to remove unused methods. Gate 2 (end-to-end validation) is the AWX vertical slice. Gate 3 (observation auth) belongs in the production-safety phase. The Skeptic and I agree on the *nature* of the gates; we differ only on sequencing — the Skeptic wants them as preconditions before proceeding, while I treat them as the delivery plan itself. In practice this is the same thing: the project cannot declare completion until these gates are passed.

**Multiple roles — synchronous POST /jobs is a blocker, not a smell.**

The Product Owner, Senior Engineer, Skeptic, Platform Architect, and I all converge: the 1127-line synchronous handler is the single largest architectural defect. The PO calls it "a blocker for production readiness, not a nice-to-have." The Skeptic calls it "the central architectural failure." The Senior Engineer calls it "the single largest risk in the codebase." This is not a debate anymore — it's a known defect that must be fixed. The only question is *how* to reslice it into incremental work.

**Cost Reviewer — 650-test suite is disproportionately expensive.**

I agree that the test suite carries a tax. But I share the Senior Engineer's nuanced view: the problem isn't the test *count* — it's the test *surface*. 54% of the interface methods are unused, so a corresponding portion of the test suite tests code paths that never run in production. When we prune the surface area by 7 methods, the test suite should shrink by ~30% automatically. The remaining tests for the core state machine (9 states, concurrent transitions, persistent side effects) are appropriate. The delivery plan must separate the pruning-driven consolidation from any deliberate test reduction.

### Disagreement

**Cost Reviewer — "Swap Postgres for SQLite as the default."**

From a pure delivery standpoint, this is the wrong time to swap databases. The Gateway already has 5 Alembic migrations, working asyncpg code, SQLAlchemy models, and test fixtures wired to Postgres. Rewriting the data layer to support SQLite would be a sprint-sized distraction that delays the async POST /jobs fix, the AWX vertical slice, and the Paperclip validation — which are the three things everyone agrees are highest priority. SQLite viability is a valid optimization *after* the core workflow is validated, not a prerequisite. I would move this to Phase 5 (cost optimization), not Phase 0.

**Platform Architect — post details about polling model and webhooks.**

The Platform Architect's recommendation to "replace polling with webhooks" is architecturally correct but delivery-wrong as an MVP requirement. Adding webhook callbacks to AWX requires: (a) a public callback URL that AWX can reach — which means the Gateway needs a stable network endpoint and potentially a reverse proxy — (b) a new `/webhooks/awx/{job_id}` endpoint with HMAC verification, (c) a registration handshake between Gateway and AWX, and (d) fallback polling for when the webhook is missed. This is a 2-3 week project on its own. Polling with a 5-second interval works for MVP. Replace it in Phase 5 (production hardening). The delivery plan should not let the perfect (webhooks) block the good (async POST /jobs with polling).

**Senior Engineer — "Template ID coupling is acceptable."** I agree more with the Senior Engineer than the Platform Architect on this one. Template IDs change on playbook updates — that is a deploy-time event, not a runtime fragility. The startup validation check is a 15-minute issue, not a redesign driver.

### New Concerns Raised

**Observation authentication (Skeptic, now acknowledged by everyone).** This is not just a security gap — it's a delivery blocker for the observation pipeline. If ingestion is unauthenticated, the entire policy engine is poisonable. But the solution might be simpler than everyone assumes: **remove observation ingestion from MVP scope entirely**. The policy engine can evaluate runner health based on status set by operators or external monitoring via `PATCH /runners/{id}/status`. This eliminates the auth problem, eliminates the custom push-agent requirement, and eliminates three Postgres tables — without eliminating the policy engine itself. The observation *ingestion* endpoint becomes a Phase 5 addition with proper auth designed from day one. This is the fastest path to a secure MVP.

**OpenCode session cost guardrails (Cost Reviewer, now echoed by Product Owner and Senior Engineer).** I hadn't considered this in Round 1, but from a delivery perspective, this is a single field addition to the job model (`max_cost` / `max_duration_minutes`) plus a passthrough to OpenCode Serve's API. It's a 30-minute issue. It should be in the production safety phase.

**No concurrency or capacity numbers (everyone).** This is the most frustrating gap because it's the easiest to fix. Without expected jobs/day, concurrent runners, and port utilization, the architectural decisions around Postgres vs SQLite, 1000-port range, and polling vs webhooks are based on intuition. I will add "document expected load parameters" as a 15-minute discovery issue in Phase 0.

## Updated Position

My Round 1 position has **strengthened in diagnosis but evolved in prescription**. Let me summarize:

**What I still hold:**
- Every original issue exceeded the 15-30 minute target (this is still true).
- Synchronous POST /jobs prevents incremental delivery (unanimously confirmed).
- No end-to-end validation milestones existed (the Skeptic's end-to-end gate confirms this gap).
- Future-surface tax inflated delivery cost (54% unused methods — unanimous).
- "95% complete" was a misleading metric (everyone except the original team agrees).

**What has changed:**
- **AWX ordering.** My Round 1 position was internally contradictory: I wanted the AWX executor validated early, but I also wanted to defer it to Phase 2 as a spike. The Senior Engineer and Platform Architect convinced me: the AWX executor is not a spike — it *is* the product. Build it second (after async refactor), not fourth.
- **Paperclip is now the validation gate, not the trailing edge.** The PO's evolution on this is decisive. I now treat Paperclip integration as the **completion milestone**, not a separate issue.
- **Observation ingestion can be deferred.** The Skeptic and Cost Reviewer convinced me that the custom push-metric pipeline is separable from the policy engine. I now advocate removing `POST /observations` from MVP scope entirely, keeping the policy engine as a consumer of runner status set by other means.
- **The test suite problem is upstream.** The Senior Engineer's insight — "simpler code needs fewer tests" — reframed the test cost debate. The fix isn't to delete tests; it's to prune the surface area that generates them. The unused protocol methods are the root cause.

## Resliced Delivery Plan: Stage-Gated, 15-30 Minute Issues

Here is a concrete delivery plan built from the council's convergence points. Each issue is scoped to 15-30 minutes. Each gate has a clear pass/fail criterion.

---

### Gate 0: Scope Pruning & Baseline (Estimated: 1 day)

**Purpose:** Eliminate 54% unused surface before building anything new.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 0.1 | 15 min | Document expected load parameters: expected jobs/day, concurrent runners, observation volume. Post as ADR or operations guide entry. | Published document |
| 0.2 | 15 min | Remove `restart_opencode` and `collect_state` from `ExecutorPlugin` ABC. Move to optional `AdvancedExecutorPlugin` subclass. | No required stubs in new executors |
| 0.3 | 15 min | Remove 5 unused methods from `OpenCodeClientProtocol`. Keep only `get_session_diff` and `abort_session`. | Protocol has 2 methods |
| 0.4 | 15 min | Delete or deprecate Pydantic models for removed methods. | `git diff --stat` confirms reduction |
| 0.5 | 30 min | Prune tests for removed protocol methods. Target: remove ~180 tests (30% of 650, proportional to 7/13 methods). | Test count drops to ~470 |

**Gate 0 criterion:** Interface methods drop from 13 to 6. Test suite drops to ≤500 tests.

---

### Gate 1: Async Job Submission (Estimated: 2.5 days)

**Purpose:** Convert the synchronous blocker to an async workflow.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 1.1 | 15 min | Define the async contract: `POST /jobs` returns `202 Accepted` with `Location: /jobs/{id}`, caller polls `GET /jobs/{id}` for status. Write ADR or update API spec. | Published ADR |
| 1.2 | 30 min | Add `pending` job status and enforce valid transitions at DB level (CHECK constraint on `gateway_jobs.status`). | DB migration with constraint |
| 1.3 | 30 min | Extract `validate_job_request()` from `create_job` — validates input, returns job ID. | Unit test passes |
| 1.4 | 30 min | Extract `select_runner()` from `create_job` — runner selection logic, returns runner ID. | Unit test passes |
| 1.5 | 30 min | Extract `reserve_workspace()` from `create_job` — port allocation, returns workspace ID. | Unit test passes |
| 1.6 | 30 min | Build background worker that polls `pending` jobs and transitions through lifecycle. | Background job reaches `completed` |
| 1.7 | 30 min | Wire `POST /jobs` to return 202 immediately; background worker handles executor calls. | `POST /jobs` returns in <50ms |
| 1.8 | 15 min | Add `GET /jobs/{id}` endpoint that returns current status and (if complete) diff URL. | E2E test verifies polling flow |

**Gate 1 criterion:** `POST /jobs` returns 202 in <50ms. Full lifecycle completes via background worker. No dead code left in the old synchronous handler.

---

### Gate 2: AWX Vertical Slice (Estimated: 2.5 days)

**Purpose:** Validate the Gateway→AWX→Runner VM→OpenCode chain before adding more layers.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 2.1 | 15 min | Write AWX template contract spec: expected `extra_vars` keys, template names, and expected outputs for all 3 templates. | Published spec |
| 2.2 | 15 min | Add startup validation: Gateway verifies 3 template IDs exist via `GET /api/v2/job_templates/{id}/` on boot. | Startup fails with clear message for invalid ID |
| 2.3 | 30 min | Implement `create_workspace` in `AWXExecutorPlugin` — launches AWX template, polls (5s interval, 300s timeout), returns result. | Manual test: workspace created on runner |
| 2.4 | 30 min | Implement `start_opencode` in `AWXExecutorPlugin` — launches lifecycle template with `action: start`. | Manual test: OpenCode process running on runner |
| 2.5 | 30 min | Implement `stop_opencode` + `cleanup_workspace` in `AWXExecutorPlugin`. | Manual test: OpenCode stopped, workspace removed |
| 2.6 | 30 min | Build end-to-end smoke test: submit job → Gateway creates workspace → starts OpenCode → (mock session) → stops → cleans up → returns diff. | Full flow passes in test environment |
| 2.7 | 15 min | Document the AWX triple-launch overhead timing for 3 sample runs. Publish ratio of AWX time to coding time. | Data published in operations guide |

**Gate 2 criterion:** A real job (with a real or simulated OpenCode session) runs end-to-end through Gateway→AWX→Runner VM and returns successfully. AWX overhead ratio documented.

---

### Gate 3: Paperclip Validation (Estimated: 1.5 days)

**Purpose:** Validate the API surface against its primary consumer.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 3.1 | 30 min | Write Paperclip-Gateway contract: API surface, expected callbacks, error semantics, polling contract. Publish as ADR. | Published ADR |
| 3.2 | 30 min | Build mock Paperclip client that submits a job via `POST /jobs`, polls `GET /jobs/{id}`, and retrieves diff. | Client script runs successfully |
| 3.3 | 30 min | Run Paperclip client against real Gateway API end-to-end. Document failures or API mismatches discovered. | Failure log published |
| 3.4 | 15 min | Fix any API surface mismatches discovered in 3.3 (contract changes, error format fixes, missing headers). | Client succeeds on re-run |

**Gate 3 criterion:** A Paperclip-style client successfully calls the Gateway API end-to-end: submit → poll → diff retrieval. All API surface mismatches fixed.

---

### Gate 4: Production Safety (Estimated: 1.5 days)

**Purpose:** Address auth, cost guardrails, and operational gaps.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 4.1 | 15 min | Add `max_duration_minutes` and `max_cost` fields to job request model. Pass through to OpenCode Serve API. | Field documented and passthrough verified |
| 4.2 | 15 min | Consolidate database access: move policy layer from raw asyncpg to SQLAlchemy async (or vice versa — pick one). | Single DB access pattern in codebase |
| 4.3 | 15 min | Rename `ObservationBasedPolicy.check()` to `check_and_set_runner_status()`, or extract mutation from check. | Method name matches behavior |
| 4.4 | 30 min | Add DB-level CHECK constraints on `gateway_jobs.status` for all valid transitions. | Direct SQL insert of invalid status fails |
| 4.5 | 15 min | Add port reservation timeout: 5-minute TTL on port allocation, auto-reclaim on expiry. | Orphaned ports reclaimed after TTL |
| 4.6 | 15 min | Add observation auth design decision: either (a) defer observation ingestion from MVP and document that policy engine reads runner status from `PATCH /runners`, or (b) implement shared-token auth on `POST /observations`. | Auth mechanism documented in ADR or operations guide |

**Gate 4 criterion:** All four safety gaps closed. DB enforces state machine. Ports auto-reclaim. Cost guardrails present. Auth decision documented.

---

### Gate 5: Test Suite Rationalization (Estimated: 1 day)

**Purpose:** Eliminate dead-weight tests and replace with high-value vertical integration tests.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 5.1 | 15 min | Delete tests for removed protocol methods (Gates 0.2 and 0.3). | Test count drops proportionally |
| 5.2 | 30 min | Replace top-3 layer-mocked unit tests with a single vertical-slice integration test: Gateway→AWX executor (real or mock)→OpenCode client→state machine. | Integration test passes in CI |
| 5.3 | 15 min | Consolidate `test_jobs.py` (3498 lines): extract shared fixtures, remove duplicate coverage. | File shrinks by ≥30% |
| 5.4 | 15 min | Add coverage threshold for core state machine paths only. Exclude pruned surface. | Coverage report shows ≥90% on core paths |
| 5.5 | 15 min | Set CI timeout for test suite. If runtime >5 minutes, flag for further consolidation. | CI baseline established |

**Gate 5 criterion:** Test suite ≤400 tests, CI runtime ≤5 minutes, vertical-slice integration test exists.

---

### Gate 6: Delivery Documentation (Estimated: 0.5 day)

**Purpose:** Fix the measurement problem.

| Issue | Time | Description | Verifier |
|-------|------|-------------|----------|
| 6.1 | 15 min | Switch delivery dashboard from "issues closed" to "integration surfaces validated." Show: Gateway↔AWX, Gateway↔RunnerVM, Gateway↔Paperclip, Gateway↔OpenCode. | Dashboard published |
| 6.2 | 15 min | Publish success metrics: target job completion rate (≥95%), P95 end-to-end latency (≤10 min), maximum concurrent jobs per runner (configurable, default 5), session cost overrun prevention (100% within limits). | Metrics published in README or operations guide |

**Gate 6 criterion:** Delivery measures integration completeness, not feature completion.

---

### Delivery Plan Summary

| Gate | Name | Issues | Est. Time | Critical Path |
|------|------|--------|-----------|---------------|
| 0 | Scope Pruning | 5 | 1 day | → Gate 1 |
| 1 | Async Job Submission | 8 | 2.5 days | → Gate 2 |
| 2 | AWX Vertical Slice | 7 | 2.5 days | → Gate 3 |
| 3 | Paperclip Validation | 4 | 1.5 days | → completion |
| 4 | Production Safety | 6 | 1.5 days | parallel to Gate 3 |
| 5 | Test Rationalization | 5 | 1 day | after Gate 0 |
| 6 | Documentation | 2 | 0.5 day | after Gate 3 |
| **Total** | | **37 issues** | **~10.5 days** | |

All 37 issues are 15-30 minutes each. The critical path is sequential (Gates 0→1→2→3). Gates 4 and 5 can run in parallel with Gates 2 and 3. Total wall-clock time: ~10.5 days (2 weeks).

## Remaining Gaps

1. **The AWX triple-launch overhead measurement (Issue 2.7) is deferred to Gate 2 but might change the architecture.** If the Cost Reviewer's concern is validated — AWX orchestration >30% of total job time — the Gateway may need an SSH-direct executor added in a Phase 2. This is a discovery risk I'm accepting by placing measurement alongside implementation rather than before it.

2. **The observation auth decision (Issue 4.6) is a design choice, not an implementation.** If the team chooses to defer observation ingestion, the policy engine still works with manual/external status setting. But if they choose to keep observations in MVP, they need a full auth implementation that I haven't scoped here. I believe deferral is the right call, but I cannot force it as Delivery Planner.

3. **No webhook callback implementation.** I explicitly deferred this to Phase 5 (post-MVP) against the Platform Architect's recommendation. Polling works for MVP scale. When concurrency demands exceed the polling model, webhooks can be added. This is a conscious tradeoff.

4. **The Paperclip contract (Issue 3.1) is unknowable without Paperclip stakeholders.** The "mock Paperclip client" in Issue 3.2 is a simulation, not a real integration. The real Paperclip adapter (#13) is validation, not delivery completion. The Gateway should still be released and used by early adopters who interact via the raw API — Paperclip is the validation milestone, not the only use case.

---

**Summary:** The six council members converged on four actionable truths — synchronous POST /jobs is a blocker, 54% unused surface must be pruned, the AWX executor should be built first not last, and Paperclip integration is the validation gate — and I have translated those truths into a 37-issue delivery plan where every issue is independently deliverable in 15-30 minutes, gated by clear pass/fail criteria, sequenced to validate the highest-risk integration (Gateway↔AWX↔Runner VM↔OpenCode) before adding more abstraction layers, and culminating in Paperclip validation as the definition of done.
