# Council Response: Delivery Planner

## Reactions to Other Members

### Agreement

I strongly agree with the **Skeptic** and **Platform Architect** on the biggest risk: **we don't know what the OpenCode Serve API actually exposes**. My Phase 0 (API discovery) is the critical path gate — no credible delivery plan exists until we confirm which fields are queryable post-session. The **Senior Engineer**'s caution about cross-repo coordination reinforces this: if the API requires OpenCode Serve changes, the delivery timeline doubles.

The **Product Owner**'s call to pick one persona first is correct. I'll commit: **Paperclip cost-tracking is the smallest shippable chunk**. Six aggregated columns on job completion, nothing more for Phase 1.

The **Cost Reviewer**'s data that a session summary is ~200 bytes settles the storage cost question convincingly. Storage is not a concern.

### Disagreement

**Separate table vs. extending `gateway_jobs`**: The **Platform Architect** and **Senior Engineer** argue for extending `gateway_jobs` with nullable columns. I now lean toward this approach — *if* we can keep it to 4-6 columns and a single post-completion fetch. My initial separate-table design introduced a JOIN that buys nothing for Phase 1. However, I *disagree* with the **Platform Architect** that this means rejecting all per-message detail forever. A separate table for Phase 2 (tool-call detail) remains the right pattern — just not for Phase 1.

**Push vs. pull**: The **Cost Reviewer**'s proposal to push via the observation pipeline is architecturally wrong. The observation pipeline is for Runner VM telemetry (disk, memory, load). Mixing session metadata into that stream conflates domains and creates a confusing data flow. My Phase 0 will confirm whether `GET /session/{id}` is viable; if it is, pull is cleaner and avoids pipeline coupling.

**Compute cost at query time**: The **Cost Reviewer** suggests compute-at-query. For cost tracking, I need a concrete counterargument: Paperclip needs to query *which* jobs exceeded a cost threshold without scanning every row. Pre-computed storage enables indexed queries; compute-at-query kills that at scale. Store the computed cost, compute the breakdown.

### New Concerns Raised

The **Skeptic**'s concern about "no user trigger" is the most under-addressed gap. Who asks for this data? When? If Paperclip queries after every job, the API must handle hundreds of near-simultaneous post-completion fetches. If Paperclip queries on-demand, concurrency drops but staleness increases. My delivery plan assumed a polling model — the group hasn't validated that.

## Updated Position

My position has shifted: **Phase 0 is non-negotiable and must complete before any schema work begins.** Phase 1 extends `gateway_jobs` (not a separate table) with 6 aggregated columns populated by a post-completion pull from the OpenCode Serve API. Phase 2 is deferred until Paperclip validates the Phase 1 schema with real usage. The observation pipeline approach is rejected.

## Remaining Gaps

1. **OpenCode Serve API contract** — still unknown. Phase 0 must return API field names and data types before I can finalize column definitions.
2. **Concurrency model for post-completion fetch** — if 50 jobs finish simultaneously, does the pull queue or fan out?
3. **Cost threshold query pattern** — needs Paperclip input on whether they query by job, by runner, by time window, or all three.

**Summary**: Proceed only after Phase 0 API discovery — then extend `gateway_jobs` with 6 aggregated columns for Paperclip cost-tracking as the single Phase 1 delivery, deferring separate-table detail to Phase 2 and rejecting the observation pipeline approach entirely.
