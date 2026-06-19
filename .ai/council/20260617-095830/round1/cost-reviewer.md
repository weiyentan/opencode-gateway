# Council Opinion: Cost Reviewer

## Summary
Storing per-session LLM metadata in the Gateway is low-cost and justified, but per-message or per-tool-call granularity would be wasteful until a concrete query demand exists. I recommend proceeding only at the session-summary level and reusing the existing observation pipeline rather than building a new pull-based integration.

## Assessment

Let me evaluate the cost dimensions honestly.

**Storage cost is negligible.** A session-summary row — model name, token counts (input/output), wall-clock duration, estimated cost, error count — is maybe 200 bytes per session. At scale (say 10,000 sessions/month), that's ~2 MB of new data per month. PostgreSQL doesn't even notice. Indexes cost more than the data.

**The real cost is in the retrieval mechanism, not the storage.** The brief proposes the Gateway *fetching* this data from OpenCode Serve via its REST API. That means:
- One HTTP request per completed job (a few cents at cloud scale — negligible)
- Coupling the Gateway's job-completion flow to OpenCode Serve's availability
- Potential retry overhead if OpenCode Serve is slow or down

This is cheap in absolute terms but introduces a new synchronous dependency in the completion path. If OpenCode Serve is unreachable, does the job get marked complete without session metadata? Or does it block? Blocking costs us runtime — jobs pile up.

**Cheaper path: reuse the existing observation pipeline.** The `POST /observations` endpoint already ingests time-series data from Runner VMs. OpenCode Serve instances already push health data. Extending that same mechanism to push session summaries post-completion avoids:
- New API surface
- New synchronous fetch code
- New credential/authentication flows

Push (via observations) is cheaper than pull (via a new client).

## Key Concerns

- **Per-message/tool-call detail would be a cost trap.** A single session can generate 50–200 tool calls. Storing those at scale (10K sessions × 100 tool calls = 1M rows/month) is doable but the *query* cost for Paperclip/analytics could spike — joins across tool calls slow down fast. Nobody has demonstrated a concrete need for this granularity yet.
- **Retrieval cost if OpenCode Serve doesn't have an API for this.** The brief assumes OpenCode Serve exposes session metadata via REST. If it doesn't, we'd need to parse SQLite databases or log files on the Runner VM — that's a costly executor operation (SSH + file transfer + parsing) per session. Verify this assumption before building anything.
- **Cost of unused data.** Storing is cheap. Storing data nobody queries is still cheap, but it wastes engineering time to model, implement, and maintain it. Scope to what Paperclip actually needs.

## Recommendations

1. **Start at session-summary granularity only.** Model name, token counts, duration, estimated cost, error count. Add this as an optional metadata JSONB column on `gateway_jobs`, or as a new `session_summaries` table with a 1:1 FK to `gateway_jobs`. Revisit per-message granularity only when a concrete Paperclip integration demands it and provides the query patterns.

2. **Push, don't pull.** Extend the observation pipeline: have OpenCode Serve (or an exec-utor plugin step after session completion) POST a session summary to `POST /observations` with a new `session_summary` observation type. This keeps the Gateway's job-completion path fast and decoupled.

3. **Default to not storing cost estimates in the database.** Compute estimated cost at query time from token counts + model lookup table. This avoids stale pricing data and gives us flexibility to change cost models without migrations.

## Questions That Need Answers

- Does OpenCode Serve's REST API expose session token counts and model name today, or would this require OpenCode Serve changes?
- What is Paperclip's actual query pattern? Frequency (daily, real-time), cardinality (all sessions, last 100), and grouping (by repo, by user, by model)? The answer determines whether we need indexes or a separate analytics store.
- Can the observation pipeline accept a new event type without breaking existing consumers?

**Summary**: Session-summary metadata is cheap to store and worth doing via the existing observation pipeline, but per-message granularity is premature cost and complexity until a concrete query pattern emerges.
