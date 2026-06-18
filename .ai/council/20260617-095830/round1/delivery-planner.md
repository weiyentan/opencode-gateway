# Council Opinion: Delivery Planner

## Summary

This work is deliverable but only as a 3-phase rollout — the critical blocker is that OpenCode Serve likely does not expose the needed metadata today, so Phase 0 is discovering what's available. Without that, all downstream work is speculative.

## Assessment

I've mapped the delivery into three phases. Each phase decomposes into vertical slices that can be built, tested, and shipped incrementally.

**Phase 0 — OpenCode Serve API Discovery (2 slices, ~30 min each)**
- Slice 0a: Inspect OpenCode Serve's actual REST API responses for `GET /session/{id}` to see what metadata is already returned (model, token counts, etc.)
- Slice 0b: If insufficient, define a new `GET /session/{id}/metadata` endpoint on OpenCode Serve — or a `?include=usage` query param

These are the **critical path**. We cannot spec the data model without knowing what's available. This is also the highest-risk item: if OpenCode Serve doesn't track per-session token usage or tool calls, we need OpenCode Serve changes before any Gateway work begins.

**Phase 1 — Gateway Storage & Retrieval (3-4 slices, ~30 min each)**
- Slice 1a: New `session_metadata` table in PostgreSQL (per ADR 0001, separate table pattern) with columns for model name/version, input/output tokens, estimated cost, wall-clock duration, status
- Slice 1b: Alembic migration + domain model + repository method
- Slice 1c: Fetch session metadata from OpenCode Serve at job completion (alongside the existing diff fetch in `create_job`)
- Slice 1d: Extend `GET /jobs/{job_id}` response to include metadata, and add `GET /jobs/{job_id}/session-meta` endpoint

**Phase 2 — Enhanced Detail (2-3 slices, optional)**
- Only if Phase 0 confirms per-tool-call or per-message data is available
- Slice 2a: `session_tool_calls` table (tool name, args, success/fail, duration)
- Slice 2b: Query/filter API for cost analytics, error rate computation
- Slice 2c: TTL-based cleanup for raw tool call data (retain summaries forever, raw data for N days)

## Key Concerns

- **The #1 risk is OpenCode Serve's API surface.** The current `SessionInfo` model (protocol.py line 15-27) has zero token or model fields. If OpenCode Serve doesn't expose them, the Gateway cannot store them — and we're blocked before we start. Phase 0 must be the first delivery slice, not an afterthought.
- **Retrieval timing is tricky.** The Gateway currently fetches the diff *after* the job is marked completed. If the session is cleaned up before we fetch metadata, we lose the data. The metadata fetch should happen *before* the session is cleaned up, which means we need to reorder the lifecycle in `create_job`.
- **Per-tool-call detail is a storage risk.** If every session makes 50+ tool calls and we store each one, storage grows fast for low value. I recommend only storing summary-level metrics in Phase 1 and deferring tool-call-level detail to Phase 2 with explicit TTL-based cleanup.
- **The delivery plan depends on an unknown.** We cannot estimate Phase 1 until Phase 0 is done. If OpenCode Serve changes are needed, that work lives outside the Gateway repository and has its own timeline.

## Recommendations

1. **Start with Phase 0 immediately** — one developer session to curl the OpenCode Serve API and determine what's available. This is a 15-minute investigation that de-risks the entire proposal.
2. **Use a separate `session_metadata` table** (per ADR 0001 pattern) rather than bloating `gateway_jobs`. The metadata has different query patterns, retention policies, and consumers than job data.
3. **Ship Phase 1 as "session summary metadata" only** — model name, token counts, estimated cost, wall-clock duration. This is the smallest shippable chunk that delivers value to Paperclip and cost-analytics consumers.
4. **If OpenCode Serve changes are needed, push them as a parallel track.** Gateway work should not block on OpenCode Serve changes — do both in parallel where possible.

## Questions That Need Answers

1. What does `GET /session/{id}` actually return from the current OpenCode Serve build? Can someone curl a real instance and share the JSON?
2. Does OpenCode Serve track token usage internally? If so, is it exposed or only in logs?
3. Who is the primary consumer in the first 30 days? If it's Paperclip for cost allocation, we only need model name + token counts. If it's operators debugging failures, we need tool call detail. The consumer determines the Phase 1 scope.
4. What is the cleanup lifecycle of a completed OpenCode session? Does it hang around long enough for the Gateway to fetch metadata after job completion, or does the cleanup window close quickly?

**Summary**: Proceed with Phase 0 (API discovery) immediately — without it the rest is guesswork. If OpenCode Serve exposes metadata, Phase 1 is deliverable in ~2 hours of slicing. Per-tool-call detail should be deferred.
