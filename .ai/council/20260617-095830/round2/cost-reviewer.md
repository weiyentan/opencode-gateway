# Council Response: Cost Reviewer

## Reactions to Other Members

### Agreement

I concede on **push vs. pull**. The **Platform Architect**, **Senior Engineer**, and **Delivery Planner** all made the same case: the observation pipeline is for time-series machine telemetry, not discrete session events. Forcing session summaries through `POST /observations` would require type discrimination, lose the clean FK to `gateway_jobs`, and create confusing domain coupling. I was optimizing for "reuse existing infrastructure" and underestimated the semantic cost. A best-effort async pull at job completion is the cleaner pattern.

I also shift my position on **separate table vs. extended columns**. The emerging consensus (Platform Architect, Senior Engineer, Delivery Planner) favors a separate `session_summaries` table per ADR 0001. I now agree — different retention policies, query patterns, and consumers justify the additional table. The cost of a JOIN between `gateway_jobs` and `session_summaries` is negligible at the volumes we're projecting (~10K jobs/month).

**Phase 0 (API discovery) is unanimous** and I fully endorse it. The worst cost scenario is building Phase 1 against an API contract that doesn't exist — that's days of wasted engineering. A 15-minute curl of `GET /session/{id}` is the highest-ROI action in this entire proposal.

### Disagreement

The **Delivery Planner** correctly notes that compute-at-query time prevents an indexed query like "all jobs where cost > $5." Their concern is valid at scale but premature here: at 10K–100K sessions, multiplying `input_tokens × rate + output_tokens × rate` across all rows is a millisecond table scan. However, I'll offer a compromise: use a PostgreSQL **generated stored column** for `estimated_cost_cents`. It's computed once at write time (not at query time), automatically updates if rates change via a migration, and is indexable. This preserves my concern (no stale stored prices) while satisfying the Delivery Planner's query-pattern requirement.

The **Senior Engineer** wants to drop Phase 2 (per-tool-call detail) from the roadmap entirely. I support this on cost grounds. Tool-call detail would increase row count by 50–200× per session with no demonstrated consumer. The cost of building it, testing it, and maintaining schema compatibility with OpenCode Serve's internal data model dwarfs any speculative value. If Paperclip needs per-tool-call data, query OpenCode Serve live — don't store a copy.

### New Concerns Raised

The **Skeptic**'s "no user trigger" concern has cost implications I didn't fully weigh: even the cheapest feature has an opportunity cost. The engineering time for Phase 1 (migration, domain model, fetch logic, test updates, documentation) is roughly 4-6 hours of senior engineer time. No council member has confirmed a Paperclip user actually waiting on this data. That doesn't mean we shouldn't build it — but we should acknowledge Phase 1 as an investment in future capability, not a fix for a current pain point.

The **Senior Engineer**'s 650-test burden is the hidden cost in every estimate. Adding a post-completion fetch requires updating factory defaults, adding mock OpenCode Serve responses, and potentially conditionalizing test flows. I estimate this alone at 4-8 hours of test engineering — real cost, not "we'll figure it out."

## Updated Position

**Phase 0 is the only Phase 1.** That's my revised recommendation:

1. **Phase 0 (now):** Curl the OpenCode Serve API. 15 minutes. Cost: negligible. Outcome: either we have a data source or we don't.
2. **If Phase 0 confirms data exists:** Build Phase 1 as a separate `session_summaries` table (5-6 columns: model, input/output tokens, duration, tool-call count, error count). Fetch via best-effort async pull at job completion. Use a generated stored column for estimated cost. Defer Phase 2 indefinitely.
3. **If Phase 0 shows no data:** Kill the proposal or submit an OpenCode Serve feature request. Do not proceed with speculative Gateway work.

## Remaining Gaps

- **No confirmed consumer.** The Product Owner says "Paperclip cost-tracking" but we haven't confirmed Paperclip actually needs or will use this data. A feature nobody consumes is the most expensive kind.
- **Session cleanup timing unknown.** If the OpenCode Serve session is destroyed before the Gateway's post-completion fetch completes, we lose the data. The pull becomes best-effort *and* unreliable — that's a cost we accept or not.
- **Test engineering estimate not accounted for.** Someone needs to audit the 650-test suite and quantify how many tests touch the completion flow. That number determines whether Phase 1 takes 2 hours or 2 days.

**Summary**: Reaffirm that session-summary storage is cheap (~200 bytes/session) but concede the observation-pipeline idea; endorse Phase 0 as the only justifiable next step, a separate `session_summaries` table with a generated stored cost column if data exists, and indefinite deferral of per-tool-call detail — but the true cost risk is not storage, it's building a feature whose consumer we haven't confirmed.
