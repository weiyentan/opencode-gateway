# PRD: Agent Run Status Derivation — Stale Run Detection & `currentStatus` Contract

## Problem Statement

The Gateway Agent Runs list shows sessions that are no longer active (the agent process has exited, the VM has gone away, or the session simply stopped producing telemetry) as **running** for up to 60 minutes after their last message. This happens because the status heuristic uses a single quiet threshold (default 60 min) to distinguish "still running" from "completed/blocked/unknown". There is no observability feedback loop from OpenCode to confirm whether an agent process is still alive.

An operator viewing the dashboard sees stale sessions marked with a green "running" badge, creating a misleading sense of ongoing activity. This erodes trust in the status display and undermines the dashboard's primary value as an observability tool.

The existing code and decisions have laid groundwork — ADR 0010 established a two-field API contract (`status`/`currentStatus`), the heuristic is documented, and the status vocabulary is defined in CONTEXT.md — but the implementation has gaps:

1. The backend API still returns a single `status` field (the computed value) instead of the two-field contract.
2. The frontend badge rendering uses `status` instead of `currentStatus`.
3. The quiet threshold (60 min) is too long for the "running" classification given observed session cadence.
4. No "stale" status exists in the API enum despite being defined in the domain vocabulary.
5. No low-confidence signal (e.g. session age exceeding a shorter tentative threshold) is surfaced to distinguish "probably still running" from "probably done."

## Solution

Derive a more honest `currentStatus` for every agent run using a refined heuristic that adds a short tentative threshold and a "stale" status bucket, split the API response into `status` (raw persisted) and `currentStatus` (derived) fields, tune the default thresholds based on observed OpenCode serve session idle-timeout patterns, and update the frontend to consume `currentStatus` with appropriate badge styling including a new "stale" badge.

### Current heuristic (baseline)

```
if no messages → unknown
if last_message_at < 60 min ago → running
if last_message_at > 24 h ago → unknown
if has_parent → blocked
else → completed
```

### Refined heuristic (target)

```
if no messages → unknown
if last_message_at < 15 min ago → running       // tightened from 60 min
if last_message_at < 2 h ago AND no_parent → completed   // confidently done
if last_message_at < 2 h ago AND has_parent → blocked     // confidently waiting
if last_message_at >= 2 h ago → stale            // new — liveness no longer trusted
if last_message_at > 48 h ago → unknown           // extended unknown threshold
```

The thresholds are configurable (`_QUIET_THRESHOLD_MINUTES`, now more accurately a set of config constants). The 15-minute running threshold aligns with common OpenCode serve idle-timeout settings. The 2-hour stale threshold is a conservative safety margin — any session quiet for more than 2 hours without a terminal signal is displayed as "stale" rather than "running".

The `stale` status is **not** terminal; it is an observability-gap label. A still-running agent that eventually sends a heartbeat or message will transition back to `running`. The stale label means "we have not seen activity in a concerning amount of time, but have no proof of termination."

## User / Operator Flows

1. **Dashboard viewer** opens the Agent Runs panel. Sessions with recent activity (< 15 min) display a green `running` badge. Sessions quiet for 15+ min but with messages and no parent display a green `completed` badge.

2. **Dashboard viewer** sees a session that was active 2–48 hours ago with no parent — it displays `completed` (grey-green). This is correct for a finished session.

3. **Dashboard viewer** sees a session quiet for 2+ hours that was previously `running`. The badge switches from green `running` to amber `stale`. The operator can tell something may be wrong without needing to check individual session timestamps.

4. **Dashboard viewer** sees a child session (has parent) quiet for 15+ minutes. It shows `blocked` (amber), indicating it is waiting on a parent and has paused.

5. **Operator** inspects a session detail view and sees `stale` status for a root session. They investigate the Runner VM or OpenCode instance. No operator action is required if the session is genuinely finished — `stale` is a display classification, not a lifecycle signal.

6. **API consumer** calls `GET /agent-runs` and receives both `status` (raw database value) and `currentStatus` (derived). The consumer uses `currentStatus` for display; `status` is available for debugging and backward compatibility.

7. **Developer** tunes the threshold constants. Only the backend Python file changes; frontend badge rendering automatically picks up the new `currentStatus` values because it reads the field, not the constant.

8. **Developer** filters agent runs by status: `GET /agent-runs?status=running` filters on the SQL-computed `currentStatus`, returning only sessions currently classified as running.

## Domain / Status Vocabulary

| Status | Meaning | Badge Color | Terminal? |
|---|---|---|---|
| `running` | Actively executing with recent liveness evidence (< quiet threshold). | Cyan (#5ef2ff) | No |
| `completed` | Terminal heuristic — quiet beyond threshold, no parent, has messages. | Green (#4ade80) | Heuristic-yes |
| `blocked` | Intentionally paused awaiting external condition (parent session). Has messages, quiet, has parent. | Amber (#fbbf24) | No (can resume) |
| `stale` | Liveness no longer trusted; no terminal signal received. Session has been quiet for extended period. | Amber-yellow (#fbbf24 with different icon) | No (observability gap) |
| `unknown` | No messages observed, or session so old it exceeds unknown threshold. | Grey (#94a3b8) | N/A |

`stale` is distinguished from `blocked` in meaning: `blocked` implies a tracked dependency (the parent session); `stale` implies an untracked disappearance — the session simply stopped producing data without explanation. Both use amber badges but with distinct iconography or tooltip text.

The full vocabulary is already recorded in CONTEXT.md. This PRD adds `stale` as a fifth value in the `currentStatus` enum and updates the domain definitions to reflect the refined heuristic.

## Backend / API `currentStatus` Contract

### Two-field separation

The API response for `AgentRunSummary` and `AgentRunDetail` MUST include two fields:

- **`status`** — The raw value from the database `sessions` column (currently null/absent in the schema; will be populated as the persisted value). Preserved for backward compatibility and debugging. Deprecated for UI use.
- **`currentStatus`** — The derived/computed status from backend heuristics. This is the canonical status label. All UI rendering MUST use `currentStatus`.

This is already decided in ADR 0010 (backend-computed-run-status) but not yet implemented. This PRD confirms that decision and adds the `stale` value.

### Schema changes

```python
# New status enum
CURRENT_STATUS_VALUES: frozenset[str] = frozenset(
    {"running", "completed", "blocked", "stale", "unknown"}
)

# AgentRunSummary gains currentStatus
class AgentRunSummary(BaseModel):
    ...
    status: str = Field(description="Raw persisted status from database")
    currentStatus: str = Field(description="Derived status from backend heuristics")

# AgentRunDetail gains currentStatus similarly
class AgentRunDetail(BaseModel):
    ...
    status: str = Field(description="Raw persisted status from database")
    currentStatus: str = Field(description="Derived status from backend heuristics")
```

### Status derivation (SQL/backend)

The `_compute_status` Python function and `_status_case_expression` SQL expression MUST be updated to produce the refined heuristic and emit `stale` instead of `completed` for sessions quiet > 2 hours.

The thresholds are promoted to configuration:

```python
# Configurable thresholds (from app settings, with defaults)
QUIET_THRESHOLD_MINUTES: int = 15       # Within this → running
STALE_THRESHOLD_HOURS: int = 2          # Beyond this → stale (if not unknown-threshold)
UNKNOWN_THRESHOLD_HOURS: int = 48       # Beyond this → unknown
```

The `_status_case_expression` SQL uses `now()` at query time so status is always current on read.

### Backend-computed filter parameter

The existing `?status=running` query parameter continues to filter on the computed `currentStatus`. The implementation should ensure the SQL CASE expression in the filter CTE uses the same logic as the non-filtered case.

## First-Slice Heuristic (Minimum Viable)

The first slice should:

1. Add `currentStatus` as a separate field alongside `status` in both `AgentRunSummary` and `AgentRunDetail` Pydantic models.
2. Update `_compute_status` and `_status_case_expression` to emit all five values (`running`, `completed`, `blocked`, `stale`, `unknown`) using tightened thresholds (15 min quiet, 2 h stale, 48 h unknown).
3. Keep `status` as a pass-through from the raw database column (currently it is the computed value; this introduces a distinction that requires a schema migration to add a `status` column to `sessions` — or alternatively, the first slice sets `status` to a fixed `unknown` until the column is populated by collectors).
4. Add CSS class `.badge-stale` to Aurora Glass.
5. Update `statusBadgeClass` in `app.js` to handle `stale`.
6. Update all badge renderings in `app.js` to read `r.currentStatus` instead of `r.status`.

**Deferred from first slice:**
- Persisting a raw `status` column in the `sessions` table (requires collector changes).
- Showing `stale` with distinct iconography beyond badge color.
- Adding a feedback loop from OpenCode (e.g. process-exit hook, heartbeat endpoint).

## UI Behavior

| View | Current (baseline) | Target |
|---|---|---|
| Agent Runs list table | Reads `r.status` → `statusBadgeClass()` → badge CSS | Reads `r.currentStatus` → `statusBadgeClass()` → badge CSS |
| Agent Run detail page | Reads `d.status` | Reads `d.currentStatus` |
| Child run summaries in detail | Reads `c.status` | Reads `c.currentStatus` |
| Status filter dropdown | Filters by `status` query param | Filters by `currentStatus` query param (same API param `?status=`) |
| Badge colors | running=cyan, completed=green, blocked=amber, unknown=grey | Same + stale=amber (same CSS as blocked but distinct class for tooltip/styling) |
| Auto-refresh | Table refreshes data periodically; stale runs flip to completed after ~60 min | Table refreshes data periodically; stale runs flip to `stale` after ~15 min, then to `unknown` after ~48 h |

The auto-refresh mechanism (currently periodic re-fetch) continues to work because `currentStatus` is computed on read — every API call produces current status values. No polling-interval changes are needed for this slice.

## Acceptance Criteria

1. `GET /agent-runs` response includes both `status` and `currentStatus` fields for every run.
2. `GET /agent-runs/{id}` response includes both `status` and `currentStatus` fields.
3. A session with `last_message_at` < 15 min ago returns `currentStatus: "running"`.
4. A session with `last_message_at` between 15 min and 2 h ago, messages > 0, no parent, returns `currentStatus: "completed"`.
5. A session with `last_message_at` between 15 min and 2 h ago, messages > 0, has parent, returns `currentStatus: "blocked"`.
6. A session with `last_message_at` >= 2 h ago but < 48 h ago, messages > 0, returns `currentStatus: "stale"`.
7. A session with no messages or null `last_message_at` returns `currentStatus: "unknown"`.
8. A session with `last_message_at` >= 48 h ago returns `currentStatus: "unknown"`.
9. `GET /agent-runs?status=running` returns only runs whose computed `currentStatus` is `running`.
10. `GET /agent-runs?status=stale` returns only runs whose computed `currentStatus` is `stale`.
11. Frontend badge for `stale` renders with `.badge-stale` CSS class.
12. Frontend badge for all statuses reads from `r.currentStatus`, not `r.status`.
13. All existing `statusBadgeClass` unit tests pass (updated for `stale`).
14. Backend Python unit tests exist for `_compute_status` with the new thresholds and for each status value.

## Risks and Assumptions

| Risk | Mitigation |
|---|---|
| **15 min running threshold is too short** for long-running agent sessions. A slow-but-alive session might flip to `stale` while still working. | Thresholds are configurable. Start with 15 min; tune based on observed OpenCode serve session idle-timeout. A follow-up can add heartbeat endpoint. |
| **`stale` causes operator alarm** for sessions that are simply long-finished but not terminally acknowledged. | `stale` is a non-terminal observability label. Documentation and tooltip text clarify: "No activity observed for 2+ hours. The session may have completed or may still be running without emitting telemetry." |
| **Two-field API contract (`status` + `currentStatus`) confuses API consumers** who expect a single status field. | Document `status` as deprecated-for-UI, keep it for backward compat. Frontend uses `currentStatus` only. API documentation marks `status` as "raw persisted value, prefer `currentStatus` for display." |
| **SQL CASE expression drifts from Python `_compute_status`** if one is updated without the other. | Both implementations live in the same file (`app/api/usage.py`) with a docstring cross-reference. Add a test that proves consistency. ADR 0010 already documents that they must remain consistent. |
| **No raw `status` column exists yet** in the `sessions` table — we would be populating `status` with a fixed value. | First slice sets `status` to the computed value (status quo) until a real `status` column is added via migration and collector changes. The ADR two-field contract is adopted as target architecture, implemented incrementally. |

## Open Questions

1. **What should `status` (the raw field) contain in the first slice, given there is no persisted status column?** Options:
   a. Keep it as the computed value (current behavior) and defer the split.
   b. Set it to `null` or `unknown` and mark consumers must migrate to `currentStatus`.
   c. Introduce the `sessions.status` column and populate it via a migration default.

   **Recommendation**: Option (a) for the first slice — keep single-field behavior, add `currentStatus` alongside it, then migrate consumers to `currentStatus`. In a follow-up, introduce a stored column and have `status` reflect that column.

2. **Should `stale` be added to the filter enum?** Yes — `?status=stale` should filter for stale runs. This requires updating `VALID_AGENT_RUN_STATUSES`.

3. **Should the stale threshold apply differently to root vs. child sessions?** A child session that has been quiet for 2+ hours with a still-running parent could arguably be `blocked` rather than `stale`. The current heuristic already handles this by checking `parent_session_id` first (if quiet and has parent → blocked). For the refined heuristic, children should also respect the 2-hour threshold before going to `stale` — a child quiet for 2+ hours is `stale`, not `blocked`.

4. **What about sessions with zero messages but an active parent?** These currently classify as `unknown`. Should a child session with no messages be `blocked` instead? Defer to post-first-slice refinement.

5. **Does the frontend auto-refresh need tuning?** Current refresh interval is in `app.js`. If it significantly exceeds the quiet threshold (15 min), stale status transitions could be delayed for the user. Check the interval; if > 15 min, consider shortening to 2–5 min for the agent runs panel.

6. **How do we validate thresholds in staging?** Set up a long-running OpenCode serve instance, observe actual message cadence and idle gaps, then tune the production threshold constants accordingly.

## Out of Scope

- Adding a OpenCode heartbeat/probe endpoint for authoritative liveness.
- Persisting a `status` column in the `sessions` table (requires collector changes).
- Adding `stale`-specific iconography (tooltip text usable as fallback).
- Changing the session overlap or date-range filtering logic.
- Adding historical status-tracking or status-change event logging.
- Adding a Paperclip or external orchestrator integration.
- Changing the frontend framework or build toolchain.
- Adding CORS, ingress, or deployment manifest changes.

## Implementation Decisions

1. **Two-field API contract** as per ADR 0010: `status` (raw persisted) and `currentStatus` (derived). First slice keeps `status` as the computed value until a stored column is introduced.

2. **Five status values**: `running`, `completed`, `blocked`, `stale`, `unknown`. `stale` is added to `VALID_AGENT_RUN_STATUSES`.

3. **Thresholds**: `QUIET_THRESHOLD_MINUTES = 15`, `STALE_THRESHOLD_HOURS = 2`, `UNKNOWN_THRESHOLD_HOURS = 48`. All configurable via settings.

4. **`_compute_status` and `_status_case_expression`** remain in `app/api/usage.py` as documented in ADR 0010. Both are updated to emit `stale`.

5. **SQL CASE expression** uses the same threshold intervals. Consistency between Python and SQL is tested.

6. **Frontend** reads `currentStatus` everywhere. `statusBadgeClass` gains a `stale → badge-stale` mapping. CSS `.badge-stale` is added with amber styling. The existing `.badge-running`, `.badge-completed`, `.badge-blocked`, `.badge-unknown` classes remain.

7. **Testing**:
   - Unit tests for `_compute_status` covering all five values and boundary conditions.
   - Unit test proving Python and SQL CASE expression produce the same result for a set of sample inputs.
   - Frontend tests for `statusBadgeClass` covering `stale` and that `currentStatus` is used.
   - Integration test: `GET /agent-runs?status=stale` returns expected runs.

## Further Notes

- This PRD supersedes earlier informal discussion about "stale runs showing as running" by formalizing the stale status, threshold refinement, and two-field contract.
- ADR 0010 (backend-computed-run-status) and ADR 0010 (session-currentstatus-heuristic) are accepted decisions referenced by this PRD. Implementation should not reopen those decisions.
- The domain vocabulary in CONTEXT.md already defines "Stale" as a run status. This PRD adds it to the API and UI.
- Follow-up work: heartbeat endpoint, persisted status column, Paperclip feedback integration.
