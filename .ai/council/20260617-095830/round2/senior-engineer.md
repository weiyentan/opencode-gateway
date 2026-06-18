# Council Response: Senior Engineer

## Reactions to Other Members

### Agreement

The **Skeptic** and I are aligned on the critical blocker: assumption #2 (OpenCode Serve API capability) is completely unverified. I appreciate the Skeptic's sharper framing — this isn't just a risk, it's a potential showstopper. The proposal is designing around a speculative interface.

The **Delivery Planner** convinced me that a separate `session_summaries` table (per ADR 0001) is the right pattern. Adding 6 columns to `gateway_jobs` is simpler in the short term, but it mixes job-orchestration data with session-analytics data that has different query patterns, retention rules, and consumers. A separate table with a 1:1 FK to `gateway_jobs` respects the domain boundary while keeping the join cheap. I've changed my mind here.

The **Cost Reviewer** makes a strong case that storing estimated cost at write time becomes stale when pricing changes. Computing cost at query time from token counts + a model pricing lookup table avoids data migrations and stale numbers. I'll adopt this — drop `estimated_cost_cents` from the storage schema.

### Disagreement

**Push vs. pull**: The Cost Reviewer suggests extending the observation pipeline to push session summaries, but this would decouple the data from the job lifecycle. Observations are time-series health snapshots — they fire periodically, not once per job completion. A session summary arriving via observations could arrive late, out of order, or not at all, and there's no natural FK to `gateway_jobs` in the observations schema. A best-effort pull at job completion (async, non-blocking) preserves the 1:1 relationship without coupling job finalization to OpenCode Serve's availability. The observation pipeline is the wrong shape for this data.

**Per-tool-call deferral**: The Delivery Planner's Phase 2 speculates about storing tool-call detail. I remain opposed to storing it in Gateway Postgres even as a deferred phase. The right answer is to query OpenCode Serve live — we should explicitly drop Phase 2 from the roadmap to avoid scope creep and leave the delivery plan at Phase 1 only.

### New Concerns Raised

The **Product Owner** identified something I overlooked: **we have no acceptance criteria**. "Paperclip can query token cost per job" isn't a testable outcome — we need a concrete latency, availability, and schema contract before implementation. The Skeptic's question about "what incident is this solving?" also lands: I assumed this was useful, but nobody has identified a real failure mode driving this.

## Updated Position

I still support proceeding with aggregated session metadata, but with these changes from my Round 1:

1. **Separate `session_summaries` table** (not columns on `gateway_jobs`) — per ADR 0001, with a 1:1 FK to `gateway_jobs`.
2. **Store only token counts, model name, duration, tool-call count, error count** — no price or cost columns. Compute cost at query time.
3. **Best-effort async pull** at job completion — not blocking, not push via observations. No new failure mode.
4. **Phase 0 first** — verify what OpenCode Serve exposes before any migration. This is unanimous across all 6 opinions.
5. **Drop Phase 2 (tool-call detail) entirely** from the roadmap. Query-through if Paperclip ever needs it.

## Remaining Gaps

- **No acceptance criteria still.** The Product Owner's question stands: what does "done" look like with a measurable outcome?
- **Session cleanup timing.** The Delivery Planner flagged that OpenCode Serve may clean up sessions quickly after completion. If the session is gone before we fetch metadata, we lose the data. We need to understand the cleanup window before designing the fetch timing.
- **We haven't picked a persona.** All 6 opinions agree on "start with one," but we haven't confirmed it's Paperclip for cost allocation. That decision gates the schema shape.

**Summary**: Changed my position from extending `gateway_jobs` to supporting a separate `session_summaries` table (per ADR 0001), adopted the Cost Reviewer's point about compute-time pricing, but maintain that pull (not push via observations) is the correct retrieval pattern — and that Phase 0 API discovery and acceptance criteria must precede any implementation.
