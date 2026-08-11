## Problem Statement

The Aurora Glass dashboard shows token and cost KPIs for a rolling 30-day window, but the period is not displayed anywhere in the UI. A user looking at the dashboard cannot tell what time range the "Total Tokens" or "Est. Cost" values cover. This makes the dashboard useless for bill reconciliation — the user cannot compare dashboard totals against their OpenAI/Anthropic invoice because the 30-day rolling window never aligns with a calendar month.

The Sessions panel hardcodes "last 7 days" in its subtitle, and the Client/Project panel hardcodes "Last 30 days" in its subtitle, but these are disconnected constants in the JavaScript rather than a single source of truth.

## Solution

Replace the hardcoded date windows with a date-range bar above the KPI row. The bar has a preset dropdown and optional custom date inputs. The date-range bar and KPI row are shared chrome above the tab panels — they render on every tab, not just Overview — so the aggregate totals (Active Tokens, Est. Cost, Sessions) are visible on the Agent Runs tab with the same selected range applied (issue #411). All panels on the Overview tab (KPIs, Model Mix, Agents & LLMs, Client/Project Breakdown) respect the same selected range.

The range defaults to **current calendar month-to-date** on first load, so it immediately aligns with the user's billing period. Presets let the user switch to last month, the last 30 days, or a fully custom range.

The backend aggregates API already accepts `start_date` and `end_date` parameters — no backend changes are needed. This is a frontend-only change to Aurora Glass.

## User Stories

1. As a dashboard user, I want to see what time period the KPI values cover, so that I can interpret the numbers correctly.
2. As a dashboard user, I want the dashboard to default to the current calendar month, so that I can reconcile against my LLM provider invoice.
3. As a dashboard user, I want a "Last Month" preset, so that I can review the previous billing period after close.
4. As a dashboard user, I want a "Last 30 Days" preset, so that I can see a trailing window when I need it.
5. As a dashboard user, I want a "Last 7 Days" preset, so that I can inspect recent usage at a glance.
6. As a dashboard user, I want a "Custom" option with date-from and date-to inputs, so that I can inspect any arbitrary period.
7. As a dashboard user, I want the selected range to apply to all panels on the Overview tab — KPIs, Model Mix, Sessions, Agents & LLMs, and Client/Project Breakdown — so that the dashboard is internally consistent.
8. As a dashboard user, I want the KPI subtitles to show the resolved range (e.g. "Jul 1–27, 2026" instead of "input X / output Y"), so that the period is always visible at a glance.
9. As a maintainer, I want the date-range logic extracted as testable pure functions, so that it can be unit tested without a browser.

## Implementation Decisions

### Modules modified

**`frontend/app.js`** — Add a date-range state object `{ preset, startDate, endDate }` that drives all fetch calls. Add two new pure functions:

- `computeDateRange(preset) => { startDate, endDate }` — maps a preset label to `start_date` / `end_date` ISO strings. Presets: `this-month`, `last-month`, `last-30-days`, `last-7-days`, `custom`.
- `formatRangeLabel(startDate, endDate) => string` — formats a range into a human-readable label like `"Jul 1–27, 2026"` or `"Jun 1–30, 2026"`.

Modify `fetchAll()` to pass the selected range to all aggregate, session, and record endpoints. Remove the `AGG_WINDOW_DAYS` and `SESSION_WINDOW_DAYS` constants. Modify `renderKPIs()` to display the formatted range label in the KPI subtitles. Remove the hardcoded `"last 7 days"` text from the Sessions KPI subtitle rendering.

Add event wiring for the preset dropdown and custom date inputs. Changing the preset or date inputs triggers a re-fetch.

**`frontend/index.html`** — Add the date-range bar element with the preset dropdown and custom date inputs. Remove the hardcoded `"last 7 days"` static text from the sessions KPI card.

**`frontend/style.css`** — Add styles for the date-range bar, dropdown, and custom date inputs.

**`frontend/tests/test_pure_functions.js`** — Add unit tests for `computeDateRange` and `formatRangeLabel`.

### API contract (unchanged)

The existing `GET /api/v1/usage/aggregates` endpoint accepts `start_date` and `end_date` as required ISO-8601 query parameters. The frontend already calls it with these params — only the values change.

### Edge case behaviour

- **Preset on a partial month** (e.g. "This Month" on July 5): shows July 1–July 5. The subtitle reads "Jul 1–5, 2026".
- **Preset on the first of a month** (e.g. "This Month" on July 1): shows July 1 only. Subtitle reads "Jul 1, 2026".
- **No data in range**: KPIs show `--` (existing behaviour, no change needed).
- **End date in the future**: silently clamped to today.
- **Custom start > custom end**: validated client-side; re-fetch is not triggered.

## Testing Decisions

- **Good test**: a pure function test that calls `computeDateRange('this-month')` on a known date (mocked via `Date.now()`) and asserts the exact `{ startDate, endDate }` returned. The test does not touch the DOM or the network.
- **Modules tested**: `computeDateRange` and `formatRangeLabel` in `frontend/tests/test_pure_functions.js`, following the existing pattern of duplicated pure functions in the test file.
- **Prior art**: `frontend/tests/test_pure_functions.js` already tests `fmtNum`, `fmtCost`, `fmtDuration`, `fmtRelative`, `deriveProvider`, `escHtml`, `fmtTodoProgress`, `statusBadgeClass`, `fmtCodeChanges`, `truncate`, and `shortUUID` in the same style: duplicate the pure function, run assertions with a custom test runner, exit with code 1 on failure.
- **Not tested**: DOM event wiring and fetch orchestration — consistent with the current approach, which has no DOM or integration tests for the frontend. The existing smoke test (`test_smoke_local_stack.py`) verifies the dashboard loads.

## Out of Scope

- Per-section independent date ranges (all panels share the same range).
- Backend schema changes or new API endpoints.
- Usage caps, quota alerts, or budget tracking.
- Saving the selected range preference (resets to current month on page load).
- E2E or browser-level tests for the dropdown interaction.

## Further Notes

The date-range bar renders the selected range label in the KPI subtitles, replacing the current "input X / output Y" breakdown. The input/output breakdown is still visible on hover or in the detail views. This ensures the period is always visible without cluttering the KPI card.

### Implementation Note (issue #411)

The date-range bar and KPI row are implemented as shared chrome **above** the tab panels (`#tab-overview`, `#tab-agent-runs`, and `#tab-clients-projects`), not scoped inside the Overview tab. The aggregate totals (Active Tokens, Est. Cost, Sessions — read from the aggregates total row by `renderKPIs`) therefore render on every tab, including the Agent Runs tab, with the dashboard date range applied. The Agent Runs tab additionally uses a full-viewport layout: when active it becomes a flex column sized from the viewport, the panel stretches to fill it, and its `.table-scroll` owns the vertical scroll region so a long run list scrolls inside the panel rather than the page. The full-viewport height is band-scoped so the three responsive viewport bands (full table, condensed, stacked cards) each fit the viewport without a page-level double scroll.
