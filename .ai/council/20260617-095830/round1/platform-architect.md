# Council Opinion: Platform Architect

## Summary

The proposal to store richer LLM session data in the Gateway is architecturally viable for **aggregated job-level metadata** (model, total tokens, total cost) as direct extensions to `gateway_jobs`, but **detailed per-message and per-tool-call data must not live in the Gateway's operational Postgres** — it belongs in OpenCode Serve's SQLite or a dedicated analytics store. The current clean boundary between Gateway (orchestration) and OpenCode Serve (session content) should be preserved.

## Assessment

Let me evaluate this against the six key questions I always ask:

**Where does this fit?** The Gateway sits between Paperclip (above) and OpenCode Serve + Executor Plugin (below). Its database is an operational state store for job orchestration, not an analytics warehouse. The existing `opencode_instance_observations` table proves the pattern for periodic snapshots of instance-level data, but this proposal is asking for session-level *detail*, not health snapshots.

**What boundaries does it cross?** The critical boundary is: Gateway owns *that* a session happened and *what came out of it* (diff, summary, MR URL). OpenCode Serve owns *how* it happened (messages, tool calls, token spend, model parameters). Storing per-message tool calls or model hyperparameters in Gateway Postgres crosses this boundary. The current `opencode_session_id` field is deliberately a string reference — a pointer, not a copy.

**What happens if it fails?** If we store detailed session data in the Gateway's operational database:
- Volume risk: per-message/tool-call data per session could generate thousands of rows per job. At scale, this table dwarfs `gateway_jobs` and competes with job-dispatch queries for buffer cache and I/O.
- Staleness risk: If we cache a snapshot of session data, consumers may act on stale token counts or statuses, leading to incorrect cost allocation or billing.
- Sync failure: Any mechanism that pulls data from OpenCode Serve into Gateway Postgres introduces a new failure mode — partial writes, missed updates, inconsistency between Gateway and OpenCode Serve.

**Is the architecture clean or leaky?** The current architecture is clean: Gateway orchestrates, OpenCode Serve executes. Pulling session internals into the Gateway is architecturally leaky — it turns the Gateway's Postgres into a secondary cache of OpenCode Serve's SQLite, which is a source of truth that lives on the Runner VM and that the Gateway has no authority over. Every sync channel between the two is technical debt.

**Does this respect the AWX/OpenCode responsibility boundary?** Yes — this doesn't involve AWX. The question is purely about the Gateway ↔ OpenCode Serve boundary, which the proposal would blur.

**What operational burden does this introduce?** Significant, if we store fine-grained data. Retention policies, archival, vacuuming, query performance monitoring for a table that could grow unboundedly with per-message data. If we query OpenCode Serve live instead, we add latency and availability coupling to every read path.

## Key Concerns

1. **Gateway Postgres is not an analytics store.** Adding per-message/tool-call data to an OLTP operational database is a well-known anti-pattern. At 10+ messages per session with tool calls, token usage, and timestamps each, one job could produce 50-100+ rows. 10,000 jobs = 500K-1M rows. Queries against `gateway_jobs` for dispatch logic compete directly with this analytical data.

2. **The data may not exist yet.** The current `SessionInfo` model in the OpenCode Serve protocol only exposes `id`, `status`, `workspace_path`, `task_description`, `created_at`, and `updated_at`. No model name, token counts, or tool calls. The proposal assumes OpenCode Serve exposes this, but it does not today. This means OpenCode Serve changes are a prerequisite, which changes the scope significantly.

3. **Data gravity pulls in the wrong direction.** Session detail data should live close to OpenCode Serve (its SQLite DB on the Runner VM). Pulling it into Gateway Postgres creates a sync dependency, a consistency burden, and a migration problem when the data model inevitably evolves. The Gateway becomes responsible for schema compatibility with a system it doesn't control.

## Recommendations

1. **Store aggregated session metrics on `gateway_jobs` directly.** Add columns for `llm_model`, `total_input_tokens`, `total_output_tokens`, `estimated_cost_usd`, `session_duration_seconds`, `tool_call_count`, `error_count`. These are job-level attributes (one row per job) and are naturally part of the job record. They don't cross the boundary — they describe *what the job consumed*, not *how the session executed internally*.

2. **Leave per-message and per-tool-call data in OpenCode Serve.** The Gateway should query it on-demand via a new protocol method (e.g., `get_session_telemetry(session_id)`) when Paperclip or operators need it. If latency is a concern, implement a lightweight materialized cache with TTL — but don't make the operational schema the cache layer.

3. **If an analytics store is needed, add one.** A dedicated observability pipeline (push from OpenCode Serve or pull by Gateway) into a time-series or columnar store is the correct architectural pattern for detailed session analytics. The Gateway's Postgres should remain focused on its job: reliable job orchestration.

## Questions That Need Answers

- Does OpenCode Serve's REST API actually expose token usage, tool calls, and model information today? The current `SessionInfo` model suggests it does not — has this been verified?
- What is the expected data volume per session (rows per job)? Without this, we can't assess storage cost or query impact.
- What is the acceptable staleness window for cost/analytics queries? Can Paperclip tolerate a 30-second delay, or does it need real-time data?
- Are we building this for Paperclip (an external caller) or for Gateway operators? The consumption pattern determines whether a live query or a store is appropriate.

**Summary**: Proceed with aggregated job-level session metrics on `gateway_jobs`, but firmly reject storing per-message or per-tool-call detail in Gateway Postgres — query OpenCode Serve live for that, or add a dedicated analytics pipeline.
