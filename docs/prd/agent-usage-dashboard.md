# PRD: Add dynamic Agent Usage breakdown to Aurora Glass dashboard

## Problem Statement

Aurora Glass shows aggregate usage and per-run token breakdowns, but the main dashboard does not provide a direct view of token usage grouped by the OpenCode agent identity that produced it. Users cannot quickly compare usage from agents such as `build`, `exploratory`, or `autonomous-coordinator` across the selected reporting period.

## Solution

Add a dynamic **Agent Usage** panel to the blank bottom-left area of the Overview dashboard. The panel will group canonical usage by the agent identity recorded in Session Context and render one row for every observed agent. It will use the existing aggregate API and shared dashboard filters, with no database migration or backfill.

## User Stories

1. As an Aurora Glass user, I want to see token usage grouped by OpenCode agent, so that I can compare which agents consume the most model usage.
2. As an Aurora Glass user, I want every observed agent identity to appear automatically, so that the panel does not require a hard-coded list of agent names.
3. As an Aurora Glass user, I want agents such as `build`, `exploratory`, and `autonomous-coordinator` to appear as separate rows when observed, so that agent-specific usage remains distinguishable.
4. As an Aurora Glass user, I want missing, null, or blank agent identities grouped under `unknown`, so that unclassified usage remains visible without empty rows.
5. As an Aurora Glass user, I want to see total tokens for each agent, so that I can identify the highest-usage agents.
6. As an Aurora Glass user, I want input and output token counts for each agent, so that I can distinguish prompt volume from generated output.
7. As an Aurora Glass user, I want cache-read tokens shown separately, so that cached activity does not obscure active model work.
8. As an Aurora Glass user, I want cache-write tokens shown separately when nonzero, so that cache activity is fully represented.
9. As an Aurora Glass user, I want the existing compact token format used elsewhere in the dashboard, so that the new panel is easy to scan.
10. As an Aurora Glass user, I want estimated cost per agent, so that token usage can be related to spend.
11. As an Aurora Glass user, I want request count per agent, so that usage totals have operational context.
12. As an Aurora Glass user, I want the highest-token agents listed first, so that the most significant usage is immediately visible.
13. As an Aurora Glass user, I want alphabetic ordering as a stable tie-breaker, so that equal-usage rows do not jump unpredictably.
14. As an Aurora Glass user, I want Agent Usage to follow the shared dashboard date range, so that all Overview aggregates describe the same period.
15. As an Aurora Glass user, I want Agent Usage to follow existing aggregate filters, so that I can compare filtered results consistently with other panels.
16. As an Aurora Glass user, I want usage initially observed without context to move from `unknown` to its recorded agent after Session Context arrives, so that late-arriving metadata is reflected without manual repair.
17. As an Aurora Glass user, I want an explicit empty state when the selected range has no agent usage, so that an empty result is understandable.
18. As an Aurora Glass user, I want a failed Agent Usage request not to break the rest of the dashboard, so that other observability data remains usable.
19. As an Aurora Glass user, I want prior successful Agent Usage data preserved with a stale/error indication when refresh fails, so that transient failures do not erase useful information.
20. As an Aurora Glass user, I want the panel to work on mobile and desktop, so that the dashboard remains usable across screen sizes.

## Implementation Decisions

- Extend the existing `/api/v1/usage/aggregates` contract with `agent` as a supported `group_by` dimension.
- Preserve the existing behavior and response semantics for all current aggregate dimensions and callers.
- Resolve agent identity at read time from the latest available Session Context associated with each canonical usage event's session.
- Aggregate from canonical usage events; do not create a second usage source or denormalized agent aggregate table.
- Normalize null and blank agent values to `unknown`.
- Return the existing aggregate token and cost fields, including input, output, cache-read, cache-write, total/cached values, estimated cost, and request count.
- Sort Agent Usage rows by total token usage descending, then agent name ascending.
- Add a display-only Agent Usage panel in the Overview tab's existing left-column blank area.
- Reuse the existing compact Token Breakdown presentation: total; input/output; and an optional cache line. Omit the cache line when both cache-read and cache-write values are zero.
- Apply the shared dashboard date range and existing aggregate filters.
- Keep the Agent Runs table unchanged; Agent Usage summarizes across runs and does not initially provide drilldown or click-to-filter behavior.
- Fetch and render Agent Usage independently so failures are isolated from other dashboard panels.
- Ship enabled by default with no feature flag.
- Do not add a database migration or historical backfill.

## Testing Decisions

- Test externally observable aggregate API behavior for `group_by=agent`.
- Cover multiple dynamically discovered agent identities and verify that no fixed category list is required.
- Cover null and blank Session Context agent values mapping to `unknown`.
- Verify agent identity is resolved through Session Context while token totals come from canonical usage events.
- Verify input, output, cache-read, cache-write, estimated cost, request count, and ordering fields.
- Verify existing aggregate dimensions remain backward compatible.
- Add frontend tests for dynamic row rendering, the compact token breakdown, empty state, and isolated fetch-error/stale behavior.
- Add layout assertions consistent with existing frontend tests to verify placement in the Overview left column and responsive behavior.
- Follow existing Gateway aggregate endpoint tests and Aurora Glass pure-function/frontend test patterns; test user-visible behavior rather than private implementation details.

## Out of Scope

- Hard-coded agent categories.
- Changes to the existing Agent Runs table or detail overlay.
- Agent row drilldown, click-to-filter, or navigation to individual runs.
- New database tables, schema changes, migrations, or backfills.
- Changes to token accounting semantics or the existing compact Token Breakdown formatter.
- Changes to Aurora Glass authentication, collector ingest, or canonical event reconciliation.
- Changes to Kubernetes deployment configuration.

## Further Notes

The domain glossary defines **Agent Usage** as an Aurora Glass aggregate grouped by recorded OpenCode agent identity. The grouping is intentionally read-time derived so late Session Context ingestion is reflected automatically. The feature should follow existing panel freshness and error-indicator conventions.

