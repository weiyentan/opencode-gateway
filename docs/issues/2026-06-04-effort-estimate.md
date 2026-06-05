# Effort Estimate: OpenCode Gateway Issues (#1–#14)

Estimated: 4 June 2026
Methodology: issue-analyst (6-factor scoring: files touched, new concepts, pattern familiarity, test coverage, external integration, risk factors)

## Per-Issue Estimates

| Issue | Title | Est. Time | Conf. | Key Factors |
|---|---|---|---|---|
| #1 | PRD: OpenCode Gateway | ✅ Done | — | Already published as docs/prd/opencode-gateway.md |
| #2 | Gateway skeleton with health endpoint | 60–90 min | High | Pure greenfield FastAPI scaffold: main.py, app/core/config.py, app/db/session.py, app/api/health.py, Pydantic settings, Postgres pool |
| #3 | Runner registration and observation ingestion | 90–150 min | High | 3 observation tables (ADR 0001). runners, runner_observations, workspace_observations, opencode_instance_observations models. Composite indexes. |
| #4 | Job submission and tracking with local executor | 120–180 min | High | Core state machine (pending→running→completed/failed). ExecutorPlugin ABC with 6 methods. LocalExecutor no-op impl. Plugin loader. |
| #5 | OpenCode client protocol and HTTP implementation | 120–180 min | Medium | httpx.AsyncClient wrapper. Protocol abstraction. Mock HTTP server tests. OpenCode Serve API discovery uncertainty. |
| #6 | Workspace lifecycle management | 90–150 min | High | Workspaces table CRUD. Pin/cleanup lifecycle. Port allocation logic (ADR 0003). Depends on executor interface. |
| #7 | Job diff retrieval via OpenCode client | 90–150 min | Medium | First integration of OpenCode client into job API. Diff storage decision (DB blob vs filesystem). |
| #8 | Job abort via OpenCode client | 60–120 min | Medium | Aborting→aborted transitions. Error handling for unreachable OpenCode. |
| #9 | Pre-flight policy: disk pressure guardrails | 90–150 min | High | Policy engine on observation data. Configurable thresholds. Runner status transitions (BLOCKED_DISK_PRESSURE, UNKNOWN). |
| #10 | AWX executor plugin | 180–300 min | Low | HITL — AWX infrastructure setup required. AWX API auth, job template mapping, polling, cancellation. Highest uncertainty. |
| #11 | Approval gates for risky operations | 90–150 min | High | Approvals table. needs_approval job status. Approve/reject endpoints. Audit logging. |
| #12 | Background cleanup scheduler | 90–150 min | Medium | Async scheduler via FastAPI lifespan. Configurable interval. Cleanup policy (72h success / 7d failure). |
| #13 | Paperclip integration adapter | 90–150 min | Medium | Callback webhook on job completion. Retry policy. Structured result envelope. Documentation. |
| #14 | Gateway container image and docker-compose setup | 45–75 min | High | Multi-stage Dockerfile, docker-compose.yaml with Postgres, .dockerignore, .env.example, README update. |

## Dependency DAG

```
#2 (foundation)
├── #3 (observations)
│   └── #9 (policy)
├── #4 (jobs + executor interface)
│   ├── #6 (workspaces)
│   │   └── #12 (cleanup scheduler)
│   ├── #10 (AWX executor) [HITL]
│   ├── #11 (approvals)
│   └── ─┐
├── #5 (OpenCode client) ─┘
│   ├── #7 (job diff) ────┐
│   └── #8 (job abort)     │
│                          │
└── #14 (docker setup)     │
                           │
                ┌──────────┘
                #13 (Paperclip adapter)
```

## Wall-Clock Estimate (parallelized by dependency layer)

| Layer | Issues | Parallel Max | Cumulative |
|---|---|---|---|
| Layer 0 (unblocked) | #2 → 60–90 min | 60–90 min | 1–1.5 h |
| Layer 1 (blocked by #2) | #3, #4, #5, #14 → parallel | 120–180 min | 3–4.5 h |
| Layer 2a (blocked by #4) | #6, #10, #11 → parallel | 180–300 min | 6–9.5 h |
| Layer 2b (blocked by #3) | #9 → parallel with 2a | 90–150 min | (overlaps) |
| Layer 2c (blocked by #4, #5) | #7, #8 → parallel with 2a | 90–150 min | (overlaps) |
| Layer 3 (blocked by #6, #7) | #12, #13 → parallel | 90–150 min | 7.5–12 h |

**Critical path:** #2 → #4 → #6 → #12  
**Alternative path (if HITL unblocked):** #2 → #4 → #10  
**Best case:** ~6 hours  
**Worst case:** ~10 hours

## Top Risks

| Risk | Severity | Issues Affected | Mitigation |
|---|---|---|---|
| #10 AWX executor requires real AWX infrastructure | 🔴 High | #10 | Mock server for unit tests; real integration deferred until AWX instance available |
| OpenCode Serve API surface not fully discovered | 🟡 Medium | #5, #7, #8, #13 | Document discovered API surface; mock server must match real API shapes |
| #4 executor interface is most coupled (6 dependents) | 🟡 Medium | #4, #6, #7, #8, #10, #11, #13 | Validate interface design (ADR 0002) before downstream work; keep it minimal |
| Diff storage not yet decided (DB blob vs filesystem) | 🟢 Low | #7 | Decide early in #7 implementation; filesystem simpler for large diffs |
| Port allocation race conditions on concurrent creation | 🟢 Low | #6 | Use Postgres advisory lock or SELECT FOR UPDATE |
| Callback webhook delivery guarantees | 🟢 Low | #13 | Best-effort semantics; exponential backoff with max retries |
