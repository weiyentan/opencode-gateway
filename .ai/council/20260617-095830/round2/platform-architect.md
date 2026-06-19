# Council Response: Platform Architect

## Reactions to Other Members

### Agreement

I'm shifting ground on storage strategy. The **Delivery Planner** convinced me that a separate `session_summaries` table (per ADR 0001) is cleaner than extending `gateway_jobs` — the metadata has different retention policies, query patterns, and consumers, and it avoids schema bloat on the core dispatch table. I'm adopting this.

The **Skeptic** is right that I hand-waved the boundary question. Aggregated metrics are less invasive than per-message data, but they still represent the Gateway reaching into session content. This deserves a conscious ADR decision, not an implicit schema change. I owe greater honesty about what this costs architecturally.

The **Cost Reviewer**'s recommendation to compute estimated cost at query time (from token counts + a model pricing table) is architecturally superior to storing cost — it eliminates stale-pricing migration burden. I'm adopting this.

The **Senior Engineer**'s "optional post-completion fetch, not blocking" pattern fixes a gap in my Round 1 — I assumed the fetch would complete, but the Senior Engineer correctly notes that coupling job finalization to a cache-fill operation introduces a new failure mode. Best-effort with a warning log is the right default.

### Disagreement

I disagree with the **Cost Reviewer** on push vs. pull. Reusing the observation pipeline for session summaries is architecturally leaky. Observations are time-series machine-health telemetry (disk, memory, load). Session summaries are discrete structured events with different schema, consumers, and guarantees. Multiplexing both through `POST /observations` forces the ingestion handler to discriminate types and makes consumers guess at delivery semantics. A dedicated pull from the Gateway (or a new dedicated push endpoint at job completion) keeps the observation pipeline clean and the responsibility boundaries explicit. The architecture should not conflate "what machines do" with "what sessions cost."

I also push back on the **Skeptic**'s "reject unless a user is crying about it" stance. The absence of a production incident doesn't mean a capability gap doesn't exist — Paperclip's need for per-job cost data to enforce budgets is a legitimate forward-looking requirement. But the Skeptic is right that we must verify the OpenCode Serve API before committing to anything.

### New Concerns Raised

The **Delivery Planner**'s retrieval-timing concern is critical: if the OpenCode Serve session is cleaned up before the Gateway fetches metadata, we lose the data. The Gateway currently fetches the diff after job completion — but session cleanup could happen in the same teardown window. This means either (a) reordering the lifecycle to fetch metadata before cleanup, or (b) ensuring OpenCode Serve retains session data long enough. Either option imposes requirements on the executor plugin's teardown sequence.

The **Senior Engineer**'s 650-test burden is real. Every test that exercises job completion will need changes — factory defaults, mock OpenCode Serve responses, or conditionalized behavior. This is at least a day of test engineering, not a quick add-on.

## Updated Position

I reaffirm my core stance, with revisions:

- **Proceed** with aggregated session-summary metadata (model, tokens, duration, tool-call count, error count) as a separate `session_summaries` table with 1:0..1 FK to `gateway_jobs`.
- **Prerequisite**: Phase 0 (OpenCode Serve API discovery) — this is non-negotiable. If the data does not exist on the OpenCode Serve side, the proposal blocks.
- **Fetch mechanism**: Optional post-completion pull by the Gateway (best-effort, non-blocking), not via the observation pipeline.
- **Cost computation**: At query time from token counts, not stored.
- **Acceptance criteria**: Define for the first persona (Paperclip cost-tracking) before writing code.
- **ADR required**: Document the boundary decision — the Gateway now stores session execution metrics, which is a deliberate extension of its responsibility.

## Remaining Gaps

1. **Cleanup ordering**: We must confirm the lifecycle sequence — does the session on the Runner VM survive long enough for the Gateway to pull metadata after job completion?
2. **Phase 0 urgency**: No one has a definitive answer on what OpenCode Serve exposes. This must be resolved in the next working session.
3. **Test engineering scope**: The 650-test impact has not been estimated in hours. Someone needs to audit the test suite for affected completion-flow tests.
4. **ADR authorship**: Who writes the ADR for the boundary extension? This should happen in parallel with Phase 0, not after.

**Summary**: Shifted to a separate `session_summaries` table, adopted query-time cost computation and best-effort fetch, but stand firm against reusing the observation pipeline — Phase 0 API discovery and an explicit ADR are hard prerequisites before any Gateway migration.
