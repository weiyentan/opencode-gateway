# PRD: Token Usage Refinement

## Problem Statement

The "Active Tokens" label on the Gateway dashboard is misleading. It implies real-time activity ("active"), but it actually represents a historical aggregate of input + output tokens over a selected date range. Users also cannot see cache read/write token activity without drilling into lower-level views, even though cache activity is a major contributor to total token volume and cost.

## Solution

Rename the "Active Tokens" KPI card to "Token Usage" and expand it to display four token components:

- **Headline value**: input + output tokens (preserves historical continuity)
- **Breakdown line 1**: input, output (component parts of the headline)
- **Breakdown line 2**: cache read, cache write (sibling visibility)

The card retains its existing aggregate query, date-range filtering, and panel freshness behaviour. No backend or API changes are required.

## User Stories

1. As a dashboard user, I want the token card labelled "Token Usage" so that the metric does not imply real-time activity
2. As a dashboard user, I want the headline number to represent input + output tokens so that my primary model-work metric is unchanged
3. As a dashboard user, I want input and output token counts shown beneath the headline so that I can see the component split at a glance
4. As a dashboard user, I want cache read tokens shown beneath the headline so that I can see how much prompt reuse occurred
5. As a dashboard user, I want cache write tokens shown beneath the headline so that I can see how much cache was populated
6. As a dashboard user, I want cache read/write values shown even when zero so that the card layout remains stable across refreshes
7. As a dashboard user, I want the date-range subtitle to remain unchanged so that temporal context is preserved
8. As a dashboard user, I want the panel freshness indicator to remain unchanged so that staleness behaviour is consistent
9. As a dashboard user, I want the headline number formatted with the existing number formatter so that large values remain readable (e.g. 745.7K)
10. As a dashboard user, I want the breakdown values formatted with the same formatter so that all token counts use a consistent notation
11. As a dashboard user, I want zero-valued cache components displayed as "0" so that missing cache activity is explicit
12. As a dashboard user, I want the layout to be single-line or two-line so that the card does not grow excessively large
13. As a dashboard user, I want the token card to remain the leftmost card in the KPI row so that scan order is preserved
14. As a dashboard user, I want the "Est. Cost (USD)" card to remain untouched so that the cost display is not affected by this change
15. As a dashboard user, I want the existing aggregate endpoint reused so that no additional network requests are made
16. As a dashboard user, I want the selected date range to continue filtering by `reported_at` so that historical totals are consistent
17. As a developer, I want the new terminology recorded in CONTEXT.md so that domain language stays current
18. As a developer, I want the frontend regression tests updated so that the new label and breakdown are covered
19. As a developer, I want the API and database schema untouched so that this change is purely cosmetic and revertible
20. As a developer, I want zero backend changes so that deployment risk is nil

## Implementation Decisions

### Frontend-only change

This PRD modifies only the frontend layer. No API contract, database schema, or backend query changes are involved.

### Reuse existing aggregate query

The card will continue to use the same `GET /api/v1/usage/aggregates` total-row response. The aggregate response already carries all four required fields:

| Response field | Maps to |
|---|---|
| `total_input_tokens` | Input Tokens |
| `total_output_tokens` | Output Tokens |
| `total_cache_read_tokens` | Cache Read Tokens |
| `total_cache_write_tokens` | Cache Write Tokens |

### Headline calculation

The headline remains `input + output`. The four components are displayed separately.

### Display layout

```
TOKEN USAGE                                  Updated just now

745.7K
Input 420.1K · Output 325.6K
Cache read 840.2K · Cache write 120.0K
Sep 5–6, 2026
```

- Large headline: `fmtNum(input + output)`
- Line 1: `Input {fmtNum(input)} · Output {fmtNum(output)}`
- Line 2: `Cache read {fmtNum(cache_read)} · Cache write {fmtNum(cache_write)}`
- Subtitle: existing date-range label (unchanged)

### Zero-value handling

All four components are always displayed. Zero-valued cache tokens render as `0`.

### Panel freshness and state tracking

No changes to `panelStates`, `PANEL_ENDPOINTS`, or `shouldRenderPanel` logic.

### CSS

The existing `.kpi-card.kpi-tokens` card styling is sufficient. The breakdown lines may need a new CSS class (e.g. `.kpi-breakdown`) for secondary text styling.

## Testing Decisions

### What makes a good test

Test external rendering behaviour (label text, visible values, breakdown format) rather than internal implementation details.

### Modules to test

| Module | Assertion |
|---|---|
| `index.html` | Label text reads "Token Usage" |
| `index.html` | `<th>Active Tokens</th>` is removed from table headers |
| `app.js` `renderKPIs` | Headline = input + output |
| `app.js` `renderKPIs` | Breakdown lines render all four components |
| `app.js` `renderKPIs` | Zero cache values render as "0" |
| `test_pure_functions.js` | All "Active Tokens" label assertions updated to "Token Usage" |
| `issue_577_tests.js` | All "Active Tokens" label assertions updated to "Token Usage" |

### Prior art

`frontend/tests/test_pure_functions.js` contains existing render assertions for the KPI cards. Same test patterns apply.

## Out of Scope

- Backend API or database changes
- Cost breakdown or cost category split
- URL synchronisation for date range
- New aggregate endpoint or query
- Contextual tooltip or help text
- Change to the auto-refresh cycle
- Rename of the deprecated `active_tokens` API response field

## Further Notes

The `active_tokens` field in the `/api/v1/usage/*` response bodies is already deprecated (sunset `2026-11-20`). This PRD renames only the UI label; the deprecated API field name remains unchanged. No ADR is required because this is a reversible presentation change with no architectural consequences.
