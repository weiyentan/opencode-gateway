# Council Opinion: Skeptic

## Summary
I'm not convinced we should store richer session metadata in the Gateway at all — the proposal conflates four distinct use cases, assumes data exists that we haven't verified, and undermines a boundary that was deliberately designed.

## Assessment

Let me start with the elephant in the room: **Assumption 2 is unverified.** "The desired information is available from the OpenCode Serve API." Has anyone checked? Does the OpenCode Serve REST API actually expose token counts, tool call logs, or model hyperparameters? If it doesn't, this proposal requires simultaneous changes to OpenCode Serve — a separate codebase with separate priorities. That's not a "store it in the Gateway" problem; it's a "build it in OpenCode Serve first" problem.

Next, **the boundary question is waved away**. The CONTEXT.md is explicit: the Gateway coordinates execution but does NOT own session content. Tokens, tool calls, and messages *are* session content. Proposing to store them in the Gateway is a de facto responsibility shift, not a minor schema extension. Either we change the boundary (which requires an ADR), or we accept that this data should stay in OpenCode Serve and be queried on demand.

**Four use cases, one solution.** Observability, cost tracking, audit, and analytics are not the same thing. They have different retention needs, query patterns, and cardinalities. Shoving them into one Postgres schema extension is a recipe for a table that does nothing well. Pick the one real use case with an actual user asking for it, not four hypotheticals.

**What's the cost?** The brief says "acceptable" without a number. How many sessions per day? What's the average session duration? How many tool calls? Estimated annual storage cost at $0.50/GB for managed Postgres? Without this, "acceptable" is a hand-wave.

**The MVP is stable with 650+ tests.** That's the time to ask: is richer session data a genuine need or gold-plating? I see no evidence of a user complaint, a lost debugging incident, or a billing surprise driving this. The proposal reads like it was generated from "what would be nice to have" rather than "what hurts right now."

## Key Concerns

- **Assumption 2 is untestable without checking the OpenCode Serve API** — we may be proposing a solution to a problem that requires upstream work we don't control
- **Storing session content in the Gateway violates the existing architectural boundary** — this needs to be a conscious ADR-level decision, not a schema tweak
- **Four use cases bundled into one proposal guarantees over-engineering** — each has different data shapes, retention, and query patterns
- **No cost estimate** — "acceptable" is meaningless without expected session volume, row counts, and storage projections
- **No trigger** — what incident, user complaint, or failure mode is this solving? If the answer is "none yet," we're gold-plating

## Recommendations

1. **Before anything else: verify what OpenCode Serve actually exposes.** If it doesn't expose token counts or tool call logs, this proposal is dead in the water without upstream changes.
2. **Pick one use case.** Cost allocation is the most concrete. Drop observability (we have observations and events), audit (log-based), and analytics (separate store). Ship *one* table for *one* purpose.

## Questions That Need Answers

- Has anyone called the OpenCode Serve API to confirm token counts and tool call data are returned? If not, why are we designing around a speculative interface?
- Is there a real operator or Paperclip user asking for this data, or is this a theoretical improvement?
- If we *only* stored model name and token counts per job (the simplest possible schema), does that satisfy the actual need?
