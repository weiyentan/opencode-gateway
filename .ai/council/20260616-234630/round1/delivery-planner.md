# Council Opinion: Delivery Planner

## Summary

The OpenCode Gateway issue breakdown is well-structured with a clear dependency DAG, but individual issues are too large (60–300 minutes per issue vs. a target 15–30 minutes), the highest-risk item (AWX executor) was deferred to the critical path rather than tackled early, and the synchronous `POST /jobs` design couples the entire delivery timeline to a single blocking operation — all of which means the project was delivered with higher risk and lower incremental validation than it should have had.

## Assessment

### 1. Issue breakdown quality: good structure, wrong granularity

The effort estimate document (2026-06-04) shows thoughtful issue decomposition — 14 issues with clear dependencies, a parallelization plan, and explicit risk flags. The dependency DAG correctly identifies the critical path (#2 → #4 → #6 → #12) and the alternative path through the AWX executor (#2 → #4 → #10). This is mature planning.

**However, the granularity fails the 15–30 minute test.** Here are the actual estimates:

| Issue | Estimate | Confidence |
|-------|----------|------------|
| #2 Skeleton | 60–90 min | High |
| #3 Observations | 90–150 min | High |
| #4 Jobs + Executor | 120–180 min | High |
| #5 OpenCode Client | 120–180 min | Medium |
| #6 Workspaces | 90–150 min | High |
| #7 Diff retrieval | 90–150 min | Medium |
| #9 Policy | 90–150 min | High |
| #10 AWX Executor | **180–300 min** | **Low** |
| #11 Approvals | 90–150 min | High |
| #12 Cleanup | 90–150 min | Medium |
| #13 Paperclip | 90–150 min | Medium |

Every single issue exceeds the 15–30 minute target. The *smallest* issue (#8 Abort, 60–120 min) is 2–4x too large. The largest issue (#10 AWX executor) is 6–20x too large *and* has the lowest confidence.

This matters because large issues create delivery risk:
- **No mid-issue intervention points.** If an issue takes 3 hours, the first checkpoint is at the 3-hour mark. A 30-minute issue gets a checkpoint every 30 minutes.
- **Estimation error compounds.** When a 120-minute issue with Medium confidence overruns by 50%, you lose 3 hours. When a 300-minute issue with Low confidence overruns by 50%, you lose 7.5 hours.
- **Blockers go unnoticed.** A developer blocked 45 minutes into a 5-hour issue has no structured escalation point until far too late.

### 2. Risk ordering is inverted

The effort estimate document correctly flags #10 (AWX executor) as the highest-risk item — 🔴 High severity, Low confidence, HITL (human-in-the-loop) required. In sound delivery planning, the highest-risk, highest-uncertainty items should be tackled *first*, not buried in Layer 2a (blocked by #4).

Instead, the delivery plan does the opposite:
- Layer 0: Low-risk items (#2 skeleton)
- Layer 1: Medium-risk items (#3, #4, #5)
- Layer 2a: **Highest-risk item (#10)** deferred behind #4
- Layer 3: Paperclip (#13) — the entire *reason* the Gateway exists — is dead last

This means:
- The AWX executor integration risk was not validated until ~60% of the project's wall-clock time had elapsed.
- If #10 had revealed a fundamental flaw in the executor interface design (e.g., the polling model doesn't work, the three-template mapping is too rigid), the rework would have cascaded through #4, #6, and #7.
- The Paperclip adapter — the primary consumer — was never used to validate the API surface during development.

**The correct ordering for a risk-first delivery plan:** Build a minimal end-to-end working path (skeleton → *mock* Paperclip client that calls the Gateway → local executor) first, then extend. The AWX executor should have been either a spike or a Phase 2 item, not a Layer 2a dependency.

### 3. The synchronous `POST /jobs` is a delivery bottleneck

The Senior Engineer correctly identified the 1127-line synchronous `create_job` handler as an architectural smell. From a delivery perspective, this is worse than a code smell — it's a **delivery bottleneck**:

- **Cannot be split.** Because the entire job lifecycle (runner selection → policy check → workspace creation → OpenCode start → task execution → diff fetch → cleanup scheduling) happens synchronously in a single request handler, you cannot independently deliver, test, or iterate on any sub-step.
- **End-to-end latency is unbounded.** If an OpenCode session takes 8 minutes, that `POST /jobs` call blocks for 8 minutes. The delivery team cannot measure, improve, or set SLAs on job submission without first breaking this synchronous chain.
- **Testing is fragile.** Integration tests must either mock the entire 7-step pipeline or run it end-to-end with real timeouts, making tests slow and flaky.

The PRD describes "the minimum demo flow" as a linear 7-step sequence culminating in diff retrieval. But the implementation treats it as a single blocking RPC, not an async workflow. From a delivery standpoint, the Gateway is structured as a synchronous proxy dressed as an async orchestrator.

### 4. Future-surface tax is a delivery tax

ADR 0002 documents that 7 of 13 interface methods across two abstraction boundaries are "intentional future surface" — never called at runtime. The ADR calls this "reducing cognitive load for readers." As a delivery planner, I call it a **concrete cost**:

- Every unused method had to be: specified, implemented, tested, reviewed, merged, and documented.
- Every future executor implementor must provide stubs for `restart_opencode` and `collect_state` — even though the Gateway never calls them.
- Every code reviewer must evaluate these stubs for correctness and safety.

The effort estimate didn't account for this tax. The 90–300 minute estimates per issue assume you're building only what's needed. In reality, each executor-related issue carried an extra ~20% overhead for implementing and testing unused interface surface.

**The cheaper delivery approach:** Start with 3–4 concrete methods. Add the others when a call site materializes. The ADR 0002 refinement already proves the team knew these were unused — the mistake was keeping them as required ABC methods instead of optional protocols or concrete default methods.

### 5. Missing incremental validation milestones

The project claims "95% complete" based on issue completion. But there are no documented validation milestones:
- **When was the Gateway first connected to a real OpenCode Serve instance?** Not in the issue breakdown — the OpenCode client (#5) was built against a mock server.
- **When was the Gateway first run through AWX?** Issue #10 was the AWX executor, but it's not clear if the full Gateway → AWX → Runner VM → OpenCode chain was ever tested end-to-end.
- **When was the Paperclip API surface validated?** Issue #13 (Paperclip adapter) is the only incomplete issue — meaning the API was designed without its primary consumer.

This is the biggest delivery red flag. An end-to-end vertical slice (even with mocked components) should have been the *first* deliverable, not the last. The delivery plan has 14 horizontal layers (API, DB, executor, client) that were built in depth before validating the integration breadth.

### 6. What the delivery plan got right

To be fair, the effort estimate document is better than most:
- Dependency DAG with explicit layers and parallelization paths.
- Risk register with severity, affected issues, and mitigations.
- Wall-clock estimate with best-case and worst-case.
- Clear recognition of #10 as the highest-risk item — even though the ordering was wrong.

The critical path analysis is sound. The parallelization across Layers 1 will save real time. And the clear dependency documentation means anyone picking this up knows what blocks what.

## Key Concerns

1. **Every issue is 2–10x too large** for the 15–30 minute target. The smallest issue is 60 minutes; the largest is 300 minutes with Low confidence. This removes the ability to course-correct within a single work session.

2. **Risk ordering is inverted.** The highest-risk issue (#10 AWX executor, 180–300 min, Low confidence) was deferred to Layer 2a instead of tackled as a spike in Layer 0. A failed assumption here would have cascaded through 60% of the project's work.

3. **Synchronous `POST /jobs` prevents incremental delivery.** A 7-step blocking handler means you cannot deliver, test, or iterate on any sub-step independently. The entire end-to-end flow is a single monolithic delivery unit.

4. **Future-surface tax inflates delivery cost.** 54% of interface methods serve no runtime purpose but consumed implementation, testing, and review effort. This is dead code delivered at the expense of real features.

5. **No end-to-end validation milestone.** The API was designed and built without its primary consumer (Paperclip). Integration validation was deferred to the trailing edge of the project, maximizing the cost of any discovery of API-surface mismatch.

6. **"95% complete" is misleading.** The metric is issue-closure count, not validated integration success. With the Paperclip adapter unfinished and no evidence of a real AWX+OpenCode end-to-end test, the actual integration confidence is much lower than 95%.

## Recommendations

1. **Reslice large issues into 15–30 minute chunks for any future work.** Specifically, #10 (AWX executor) should be broken into: (a) mock AWX client with the three API methods, (b) standalone AWX connection test, (c) template contract validation, (d) polling fallback with timeout, (e) end-to-end smoke test with real AWX. Each of these is ~30 minutes with clear pass/fail criteria.

2. **Build the end-to-end vertical slice before adding more layers.** For the Paperclip adapter (#13), the delivery plan should be: (a) wire up a mock Paperclip client that calls `POST /jobs` and polls for completion, (b) validate the contract, (c) add retry and callback logic. Do not extend the Gateway API surface until Paperclip has successfully consumed it.

3. **Convert `POST /jobs` to `202 Accepted` + background processing.** This single change breaks the monolithic handler into independently deliverable, testable steps. Each lifecycle phase (policy check, workspace creation, OpenCode start, task execution, diff retrieval) becomes an observable state transition that can be developed and tested in isolation.

4. **Remove unused interface methods from the required ABC.** Move `restart_opencode` and `collect_state` to a separate `AdvancedExecutorPlugin` subclass or an optional protocol. The same for the five unused OpenCode client methods. This immediately eliminates ~20% overhead from every future executor implementation.

5. **Add documented validation milestones for the remaining work.** Before declaring MVP complete, the project should demonstrate: (a) a job successfully submitted through the Gateway, executed by AWX, on a real Runner VM, producing a diff; (b) the same flow failing cleanly when disk pressure exceeds threshold; (c) a Paperclip-style client submitting a job and receiving a callback. These are the real "done" criteria.

6. **Track not just issues closed but integration surfaces validated.** The 17/18 metric is misleading. Switch to a delivery dashboard that shows: (a) components built, (b) integration points tested (Gateway↔AWX, Gateway↔OpenCode, AWX↔RunnerVM, Gateway↔Paperclip), (c) end-to-end flow pass rate.

## Questions That Need Answers

1. **What was the actual delivery cadence?** How many of the 14 issues were delivered within their estimated range? Which overran and by how much? Without this data, we can't assess whether the estimation methodology was sound.

2. **Was a real end-to-end integration test ever run through the full Gateway → AWX → Runner VM → OpenCode chain?** If yes, what blocked the remaining issues? If no, what is the plan to validate the integration before calling MVP done?

3. **Why was the AWX executor (#10) deferred to the critical path instead of executed as an early spike?** Was there an infrastructure blocker, or was this a deliberate risk-ordering decision?

4. **Did the team consider splitting the synchronous `create_job` handler into an async workflow with intermediate statuses?** If yes, what drove the decision to keep it synchronous? If no, what would it take to refactor now?

5. **What is the smallest end-to-end scenario that proves the Gateway works?** If I had to demo the Gateway doing one useful thing in 30 minutes, what would that demo look like — and has it been verified?

---

**Summary:** The OpenCode Gateway has a well-structured issue breakdown and clear dependency DAG, but every issue exceeds the 15–30 minute target, the highest-risk item was deferred to the critical path instead of tackled early, the synchronous `POST /jobs` design prevents incremental delivery of the 7-step workflow, and the absence of any end-to-end validation milestone before claiming 95% completion means the delivery plan maximized component throughput at the expense of integration confidence.
