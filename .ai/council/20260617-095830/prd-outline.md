# Refine Guidance: LLM Session Information Storage

**Council Session:** 20260617-095830
**Decision:** Refine
**Confidence:** 8/10

## What Must Change Before Another Council Run

The Council identified one structural blocker that must be resolved before this idea is ready for a PRD. This is not a "nice to have" — it is a prerequisite for any Gateway implementation work.

### 1. Execute Phase 0: OpenCode Serve API Discovery (Showstopper)

The unanimous finding: **we do not know what data is available.**

The entire proposal rests on Assumption #2 — that OpenCode Serve's REST API exposes token counts, model name, and tool-call counts per session. The current `SessionInfo` Pydantic model in `app/opencode/protocol.py` (lines 15-27) contains only `id`, `status`, `workspace_path`, `task_description`, `created_at`, and `updated_at`. No token fields, no model fields.

**Before any Gateway migration or schema design:**
1. Call `GET /session/{id}` on a real OpenCode Serve instance and capture the full JSON response
2. Call `GET /session/{id}/diff` and `GET /session/{id}/log` — check if they contain metadata beyond their stated purpose
3. Document every available field, its data type, and whether it's available at session completion
4. If the required fields do not exist, determine:
   - Does OpenCode Serve track this data internally (e.g., SQLite DB) but not expose it via REST?
   - Would a new OpenCode Serve endpoint (e.g., `GET /session/{id}/metadata`) be feasible?
   - Who owns that work and what is the timeline?

**Kill criteria:** If OpenCode Serve does not expose token counts and model name, and there is no committed timeline for adding them, this proposal should be shelved or reframed as an OpenCode Serve feature request.

### 2. Confirm the Consumer Persona

The Council identified Paperclip cost-tracking as the strongest MVP candidate, but no member confirmed that Paperclip (or any actual user) has requested this data.

**Before the next Council run:**
- Confirm with the Paperclip team (or the intended consumer) that they need per-job token counts and cost estimates
- Document their query pattern: per-job, per-runner, per-time-window? Real-time or batch? What latency is acceptable?
- Define acceptance criteria in measurable terms (e.g., "Paperclip can query `GET /jobs/{id}` and receive `total_input_tokens`, `total_output_tokens`, `estimated_cost_cents` within 500ms of session completion")

### 3. Verify Session Cleanup Timing

The Delivery Planner identified a lifecycle concern: the Gateway currently fetches the diff after job completion, but the OpenCode Serve session on the Runner VM may be cleaned up immediately after the job completes.

**Before the next Council run:**
- Determine how long a completed session remains available via the OpenCode Serve API
- If the cleanup window is short (< 30 seconds), the post-completion fetch may need to happen before the job is marked complete, or the executor cleanup sequence needs reordering

### 4. Audit Test Engineering Scope

The Senior Engineer flagged that the existing 650+ test suite exercises the job completion flow thoroughly. Adding a session metadata fetch requires updating factory defaults, adding mock OpenCode Serve responses, and potentially conditionalizing test behavior.

**Before the next Council run:**
- Audit the test suite to count how many tests hit the completion flow in `POST /jobs` and the diff/log retrieval endpoints
- Estimate the mock implementation cost (4-8 hours was suggested)
- Determine whether the test strategy should use dependency injection (preferred) or conditional branching

### 5. ADR for Boundary Extension

The Platform Architect and Skeptic both noted that storing session execution metrics extends the Gateway's responsibility beyond pure orchestration. This is architecturally significant.

**Before the next Council run:**
- Draft an ADR documenting the decision to store aggregated session metrics in Gateway Postgres
- Define the boundary: aggregated metrics (model, tokens, duration) are in-scope; per-message and per-tool-call detail are explicitly out-of-scope
- Reference this Council session as the decision forum

## Recommended Data Model (Conditional on Phase 0)

If Phase 0 confirms the data exists, the Council converged on:

- **Table**: `session_summaries` (separate table, per ADR 0001 pattern)
- **Columns**: `id` (UUID PK), `job_id` (UUID FK to gateway_jobs), `llm_model` (TEXT), `total_input_tokens` (INTEGER), `total_output_tokens` (INTEGER), `session_duration_seconds` (FLOAT), `tool_call_count` (INTEGER), `error_count` (INTEGER), `estimated_cost_cents` (generated stored column from tokens × model rate), `created_at` (TIMESTAMPTZ)
- **Retrieval**: Best-effort async pull from OpenCode Serve API at job completion (before session cleanup)
- **Failure behavior**: If fetch fails, complete the job without metadata and log a warning
- **Cost computation**: Generated stored column avoids stale pricing and enables indexed cost queries
- **Query**: Extend `GET /jobs/{job_id}` response with summary fields. Optionally add `GET /jobs/{job_id}/cost-summary`

## What is Explicitly Out of Scope (Unanimously Rejected)

- Per-message or per-tool-call detail in Gateway Postgres
- Pushing session data through the observation pipeline
- Storing model hyperparameters (temperature, max_tokens, system prompt)
- Real-time session telemetry streaming
- A general-purpose analytics warehouse inside the Gateway

## Next Action for the Gate

1. **Execute Phase 0** — one developer session to inspect the OpenCode Serve API
2. Return with findings
3. If Phase 0 is positive, initiate a new Council run or proceed directly to `/to-prd` with the data model above
4. If Phase 0 is negative (data does not exist), shelve or escalate to OpenCode Serve team
