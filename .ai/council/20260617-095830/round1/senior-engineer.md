# Council Opinion: Senior Engineer

## Summary

I recommend a **refine** stance — proceed with adding a handful of aggregated session-metadata columns to `gateway_jobs` (model name, total tokens, estimated cost) that can be populated at job completion via the existing OpenCode Serve API, but firmly reject storing per-message or per-tool-call detail in the Gateway's operational Postgres, as that introduces unacceptable schema coupling, data-volume risk, and a new failure mode in the job lifecycle.

## Assessment

The brief lists 15+ new data fields across 7 categories. Let me evaluate what each would cost to build and maintain in the Gateway:

**Aggregated job-level metrics** (model, total tokens, cost, duration, tool-call count, error count): These are straightforward. They fit as nullable columns on `gateway_jobs`, populated once at session completion by the existing mutation endpoint. The job state machine already handles completion; we'd add a "collect session metadata" step between session-end and job-complete. Risk is low, test impact is moderate (update factory defaults and existing completion-flow tests).

**Per-message and per-tool-call detail**: This is where the engineering cost explodes. A single long session can produce hundreds of messages with nested tool calls. Normalizing this into relational tables (e.g., `session_messages`, `session_tool_calls`) creates tables that grow linearly with session length, not job count. At 500 messages × 2 tool calls per message, that's 1,500 rows per job from these tables alone. Query performance for job-dispatch logic degrades as these tables grow because Postgres can't partition by job cleanly without careful index design. And the data model would need to chase every OpenCode Serve schema change — every new tool type, every new message field becomes a migration.

**The biggest hidden complexity**: OpenCode Serve's current API does not expose token counts, model name, or tool-call data via `SessionInfo`. The brief's assumption #2 is unverified. If we need OpenCode Serve changes first, we're looking at a cross-repository dependency — which means coordinated releases, version compatibility, and fallback behavior when the OpenCode Serve API doesn't return the expected fields. That's not a 30-minute issue; it's a multi-sprint initiative.

**Failure modes**: If session-metadata collection fails (OpenCode Serve VM is down, API returns an error), the job completion flow now has a new error path. Do we mark the job as failed? Do we complete it without metadata and backfill later? Either choice adds complexity to the state machine and retry logic. The current job completion flow is clean — we should not couple job finalization to a cache-fill operation.

**Testing burden**: The 650+ test suite covers job orchestration thoroughly. Adding session-metadata collection introduces integration test dependencies on OpenCode Serve's API responses. Every test that exercises job completion would need mock OpenCode Serve responses for session metadata, or we'd need to conditionalize the behavior. Either path adds maintenance surface area.

## Key Concerns

1. **Assumption #2 (OpenCode Serve API availability) is unverified** and could be a showstopper. If the data isn't exposed by OpenCode Serve today, this becomes a cross-repo effort with version-coordination overhead.
2. **Per-message/tool-call data adds 5–50× more rows per job** than the current schema, introducing query-performance risk for the operational dispatch path.
3. **New failure mode in job completion flow**: Collecting session metadata becomes a blocking step in a previously reliable path, or a non-blocking backfill that creates consistency headaches.
4. **650+ tests will need updates** — at minimum factory defaults, at worst a new set of integration mocks for OpenCode Serve API responses.
5. **Schema coupling to OpenCode Serve internals**: Every new tool type or message field in OpenCode Serve requires a Gateway migration. The Gateway becomes version-locked to OpenCode Serve's data model.

## Recommendations

1. **Start with 6 aggregated columns on `gateway_jobs` only**: `llm_model`, `total_input_tokens`, `total_output_tokens`, `estimated_cost_cents`, `session_duration_seconds`, `tool_call_count`. These are job-level attributes and don't cross the boundary.
2. **Implement as an optional post-completion fetch**: Add a `collect_session_metadata(session_id)` step in the job completion flow that is best-effort — if the OpenCode Serve API is unreachable, complete the job without metadata and log a warning. Do not make this a blocking failure.
3. **Defer per-message detail entirely**: If Paperclip or operators need message-level data, expose it via a new Gateway endpoint that proxies to the OpenCode Serve API on the Runner VM. No storage, no sync, no schema coupling.
4. **Verify the OpenCode Serve API first**: Before writing any Gateway code, confirm that OpenCode Serve can return the 6 aggregated fields at session completion. If it cannot, the scope changes fundamentally.

## Questions That Need Answers

- Has anyone verified that OpenCode Serve's REST API exposes token counts, model name, and tool-call counts per session? The `SessionInfo` model suggests it does not — can the OpenCode Serve team confirm what's available?
- What's the acceptable staleness window for session metadata? If the OpenCode Serve VM is unreachable for 30 seconds after job completion, should the job still transition to "succeeded"?
- How many tool calls per session do we observe in production today? Without this baseline, we can't estimate row-count growth for per-tool-call tables.
- Are we willing to coordinate Gateway and OpenCode Serve releases if Gateway depends on new OpenCode Serve API endpoints?

**Summary**: Add 6 aggregated columns to `gateway_jobs` as an optional post-completion fetch, reject per-message/tool-call detail in Gateway Postgres, and verify OpenCode Serve API capability before writing any migrations — the cross-repo coordination risk alone makes this a refine, not a proceed.
