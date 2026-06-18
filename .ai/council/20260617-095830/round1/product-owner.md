# Council Opinion: Product Owner

## Summary

I recommend **refining** this proposal before proceeding — the user value is real but vaguely defined, and the proposal conflates multiple distinct use cases (cost tracking, debugging, audit, analytics) that each have different data requirements, consumers, and tolerances for staleness.

## Assessment

The Gateway stores what a Job *did* (diff, summary, branch, MR) — it does not store *how* the LLM got there. That distinction matters. Operators and Paperclip consumers currently have no insight into whether a session was expensive, failure-prone, or slow. That's a genuine gap.

However, I have three structural concerns:

**1. Fuzzy user stories.** The brief lists Paperclip, operators, and cost-analytics as consumers, but these are three different personas with three different needs. A Paperclip orchestrator needs real-time per-session cost data for budget enforcement. An operator debugging a failure needs tool-call-level detail after the fact. A cost-analytics dashboard needs aggregated monthly spend. We cannot build one model that serves all three well, and if we try, we'll build something too generic that serves none well. Which persona do we ship for first?

**2. Boundary question is not optional.** The brief frames responsibility boundary as an open question, but this is the critical architectural decision. If the Gateway becomes a copy of OpenCode Serve's session data, we've created a synchronization problem and a source-of-truth conflict. The MVP of this feature should be *query-through, not store-copy* — i.e., the Gateway indexes where session data lives on the Runner VM and queries it on demand, caching only what's absolutely necessary.

**3. No measurement of success.** The proposal has no acceptance criteria. What does "done" look like? "Paperclip can query token cost per job before billing the next one"? "Operator dashboard shows tool failure rate trends"? We need concrete, testable outcomes before writing a single migration.

## Key Concerns

- **Undifferentiated use cases**: cost allocation, compliance, debugging, and billing each need different granularity, retention, and latency. A single model risks over-engineering for the least common denominator.
- **Lack of user validation**: It's unclear whether actual Paperclip or operator users have requested this, or whether we're anticipating future need without evidence.
- **Gateway mission creep**: Storing tool-call-level detail pulls the Gateway closer to being an OpenCode Serve replica. The Gateway coordinates execution; it should not become the session database.
- **No cost estimate**: "Acceptable" is not a cost estimate. We need storage projections (rows per job, retention period, query patterns) to assess whether this is worth the schema complexity.

## Recommendations

1. **Pick one persona, ship for them first.** I recommend starting with Paperclip's need: per-job token cost for budget enforcement at session completion. This has clear value, clear boundaries, and a natural data model (extend `gateway_jobs` with cost columns).
2. **Query-through, don't store-copy.** The Gateway should fetch session metadata from OpenCode Serve's API at job completion and cache the summary in `gateway_jobs`. Tool-call-level and message-level data should remain in OpenCode Serve and be queryable on demand.
3. **Define success criteria before implementation.** For the Paperclip cost-tracking scenario: "Paperclip can retrieve the total cost of a job via `GET /jobs/{id}` within 500ms of session completion."

## Questions That Need Answers

- Which consumer persona do we prioritize for the first delivery — Paperclip (budget enforcement), operators (debugging), or finance (cost analytics)?
- Have any actual Paperclip or Gateway users requested session cost data, or is this speculative?
- Does OpenCode Serve's API currently expose token counts and model name at session completion, or would this require changes on the OpenCode Serve side too?
- What is the estimated storage growth per 1,000 jobs with the proposed model, and what would the monthly cost be?
- If we query-through instead of store-copy, what's the latency budget for fetching session data from the Runner VM?

**Summary**: Proceed with refinement, not implementation — ship for Paperclip cost-tracking first, keep tool-call-level detail in OpenCode Serve, and define acceptance criteria before writing code.
