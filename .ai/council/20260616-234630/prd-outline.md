# PRD Outline — Refinement Guidance

**Council Session:** 20260616-234630
**Decision:** Refine
**Next step:** Complete the 7 refinement gates below, then re-run `/council-run` for re-validation before proceeding to PRD creation.

---

## What Must Change Before the Next Council Run

The Council assessed the OpenCode Gateway as fundamentally sound but overbuilt by approximately 40%. The following specific refinements must be completed before the project is ready for a formal PRD and production readiness declaration.

---

### Gate 0: Scope Pruning (Must Complete First)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 0.1 | Document expected load parameters: jobs/day, concurrent runners, observation volume | 15 min | Without this, all architectural decisions (port range, Postgres sizing, polling vs webhooks) are guesses |
| 0.2 | Remove `restart_opencode` and `collect_state` from `ExecutorPlugin` ABC; move to optional subclass | 15 min | These 2 methods are never called at runtime. Every new executor must implement stubs for them — pure YAGNI |
| 0.3 | Remove 5 unused methods from `OpenCodeClientProtocol` (`health`, `list_sessions`, `get_session`, `create_session`, `delete_session`); keep only `get_session_diff` and `abort_session` | 15 min | 7 of 13 total interface methods (54%) are unused across both boundaries. Pruning to 6 methods immediately reduces the abstraction tax |
| 0.4 | Delete deprecated Pydantic models for removed methods | 15 min | Dead model code inflates the package and confuses new contributors |
| 0.5 | Prune tests for removed methods (target: ~180 tests removed, suite drops from 650 to ~470) | 30 min | Tests for dead code paths cannot fail in production because they never run — they are pure maintenance cost |

**Gate 0 pass/fail criterion:** Interface methods drop from 13 to 6. Test suite drops to ≤500 tests.

---

### Gate 1: Async Job Submission (Must Complete Second)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 1.1 | Define async contract for `POST /jobs`: 202 Accepted with `Location: /jobs/{id}`, caller polls `GET /jobs/{id}` | 15 min | Publish as ADR — this is a design decision first, an implementation second |
| 1.2 | Add DB-level CHECK constraint on `gateway_jobs.status` for all valid transitions | 30 min | State machine must be enforced at storage level, not just application code |
| 1.3–1.5 | Decompose the 1127-line `create_job` handler: extract `validate_job_request`, `select_runner`, `reserve_workspace` as independent testable units | 30 min each | The God function is the single largest architectural defect per unanimous council agreement |
| 1.6 | Build background worker that polls `pending` jobs and transitions through lifecycle | 30 min | Enables async processing without blocking the HTTP request |
| 1.7 | Wire `POST /jobs` to return 202 immediately; background worker handles executor calls | 30 min | Latency drops from minutes to <50ms |
| 1.8 | Add `GET /jobs/{id}` that returns current status and diff URL when complete | 15 min | Completes the polling contract |

**Gate 1 pass/fail criterion:** `POST /jobs` returns 202 in <50ms. Full lifecycle completes via background worker.

**Important:** Move cost guardrails (Issue 4.1 — `max_cost` and `max_duration_minutes` on job model) into Gate 1. The Cost Reviewer's non-negotiable is that no real jobs should launch without session budget controls. The Delivery Planner originally placed this in Gate 4; the Council recommends moving it to Gate 1.

---

### Gate 2: AWX Vertical Slice (Must Complete Third)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 2.1 | Write AWX template contract spec (expected `extra_vars`, outputs) | 15 min | Document the implicit contract that currently causes runtime errors when mismatched |
| 2.2 | Add startup validation: Gateway verifies 3 AWX template IDs via `GET /api/v2/job_templates/{id}/` on boot | 15 min | Fail fast at startup instead of at first job submission |
| 2.3–2.5 | Implement `create_workspace`, `start_opencode`, `stop_opencode`, `cleanup_workspace` in `AWXExecutorPlugin` | 30 min each | Validate the executor abstraction against a real backend |
| 2.6 | Build end-to-end smoke test: full Gateway → AWX → Runner VM → OpenCode chain | 30 min | This is the integration test that the project lacks today |
| 2.7 | Measure AWX triple-launch overhead ratio (AWX time / coding time) for 3 sample runs | 15 min | If AWX orchestration >30%, allocate SSH-direct executor budget |

**Gate 2 pass/fail criterion:** A real job runs end-to-end through Gateway→AWX→Runner VM→OpenCode and returns successfully. AWX overhead ratio documented.

---

### Gate 3: Paperclip Validation (Gates Production Readiness)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 3.1 | Write Paperclip-Gateway contract: API surface, callbacks, error semantics, polling contract | 30 min | The primary consumer must define the contract, not the other way around |
| 3.2 | Build mock Paperclip client that calls `POST /jobs`, polls `GET /jobs/{id}`, retrieves diff | 30 min | Synthetic validation before real Paperclip exists |
| 3.3 | Run Paperclip client against real Gateway; document failures and mismatches | 30 min | Reveal API surface flaws before production |
| 3.4 | Fix all API surface mismatches discovered | 15 min | The Gateway API must pass the Paperclip test |

**Gate 3 pass/fail criterion:** A Paperclip-style client successfully calls the Gateway API end-to-end: submit → poll → diff retrieval. All mismatches fixed.

> **The project is not production-ready until Gate 3 passes.** This is the validation milestone that replaces the misleading "17/18 issues" metric.

---

### Gate 4: Production Safety (Parallel with Gates 2–3)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 4.1 | Add `max_duration_minutes` and `max_cost` to job model, pass through to OpenCode Serve | 15 min | **Please action: Move this to Gate 1.** See note above |
| 4.2 | Consolidate database access: move policy layer from raw asyncpg to SQLAlchemy async | 15 min | Eliminate the dual-DB maintenance trap |
| 4.3 | Rename `ObservationBasedPolicy.check()` to describe its side effects, or extract mutation | 15 min | A method named `check` should not write to the database |
| 4.4 | Add DB-level CHECK constraints on status transitions | 30 min | Defense-in-depth for state machine integrity |
| 4.5 | Add port reservation timeout (5-minute TTL, auto-reclaim orphaned ports) | 15 min | Prevent port leaks from failed workspace creation |
| 4.6 | Document observation auth decision: defer ingestion from MVP or implement shared-token auth | 15 min | The observation auth gap was flagged by the Skeptic and acknowledged by all roles |

**Gate 4 pass/fail criterion:** All safety gaps closed. DB enforces state machine. Ports auto-reclaim. Cost guardrails present. Auth decision documented.

---

### Gate 5: Test Suite Rationalization (After Gate 0)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 5.1 | Delete tests for removed protocol methods | 15 min | Dead code coverage is waste |
| 5.2 | Build vertical-slice integration test (Gateway→AWX→OpenCode→state machine) | 30 min | Replace 3+ mocked unit tests with one real integration test |
| 5.3 | Consolidate `test_jobs.py` (3498 lines) | 15 min | Extract fixtures, eliminate duplicate coverage |
| 5.4 | Set coverage threshold for core state machine paths only | 15 min | 90%+ on core paths, exclude pruned surface |
| 5.5 | Set CI timeout (5 min max) | 15 min | Prevent test suite from becoming deployment bottleneck |

**Gate 5 pass/fail criterion:** Test suite ≤400 tests, CI runtime ≤5 minutes, vertical-slice integration test exists.

---

### Gate 6: Delivery Documentation (After Gate 3)

| # | Issue | Est. Size | Rationale |
|---|-------|-----------|-----------|
| 6.1 | Switch delivery dashboard from "issues closed" to "integration surfaces validated" | 15 min | The 17/18 metric is misleading — measure what matters |
| 6.2 | Publish success metrics: ≥95% job completion rate, ≤10 min P95 latency, max 5 concurrent jobs/runner (configurable), 100% cost overrun prevention | 15 min | Without targets, "production ready" is arbitrary |

**Gate 6 pass/fail criterion:** Delivery measures integration completeness, not feature completion. Success metrics are published.

---

## Summary of Refinements

| Gate | Focus | Issues | Est. Time | Must Complete Before |
|------|-------|--------|-----------|---------------------|
| 0 | Scope Pruning | 5 | 1 day | Gate 1 |
| 1 | Async Job Submission | 8 | 2.5 days | Gate 2 |
| 2 | AWX Vertical Slice | 7 | 2.5 days | Gate 3 |
| 3 | Paperclip Validation | 4 | 1.5 days | Production readiness |
| 4 | Production Safety | 6 | 1.5 days | (parallel with 2–3) |
| 5 | Test Rationalization | 5 | 1 day | After Gate 0 |
| 6 | Documentation | 2 | 0.5 day | After Gate 3 |
| **Total** | | **37 issues** | **~10.5 days** | |

> **Do NOT write a formal PRD until after Gates 0–3 are complete.** Run `/council-run` again after completing these gates for re-validation. The current PRD (docs/prd/opencode-gateway.md) should be updated to reflect the refined scope — specifically: remove unused surface area, specify the async contract, define success metrics, document the Paperclip validation gate, and include the cost model.
