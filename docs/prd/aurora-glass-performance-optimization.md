# PRD: Aurora Glass Performance Optimization

**Origin:** GitHub Issue [#353](https://github.com/weiyentan/opencode-gateway/issues/353) — *Aurora Glass Dashboard Performance and Token Semantics Improvements*
**Status:** Draft
**Created:** 2026-08-06
**Updated:** 2026-08-06 (post-grill session, frontend-scope revision)

---

## Problem Statement

The Aurora Glass dashboard suffers from multiple interrelated issues that degrade both user experience and system reliability:

1. **Token Metrics Ambiguity** — The "Total Tokens" KPI lacks a clear definition relative to active usage. Operators cannot distinguish between total tokens processed historically versus tokens currently active in sessions. This ambiguity extends to the Model Mix breakdown and the Token Breakdown panels used in compact sessions and agent runs.

2. **Live Events Naming Inconsistency** — The term "Live Events" does not accurately describe the panel's actual behavior, which presents periodic health-alert snapshots rather than a true real-time activity stream. The naming sets incorrect operator expectations.

3. **Client Reference Data Not Cached Efficiently** — Client metadata is fetched on every dashboard interaction rather than being cached locally, increasing server load and delaying initial rendering.

4. **Missing Freshness Signals** — Operators have no visible indication of when the dashboard was last refreshed, making it difficult to judge whether displayed data is current or stale.

5. **Unclear KPI Labels** — Historical metrics lack date-range context, and health metrics do not convey their recency, leading to misinterpretation of dashboard values.

These problems compound each other: inconsistent naming makes debugging harder, uncached reference data increases unnecessary API calls, and missing freshness signals leave operators guessing about data staleness. All fixes are scoped to incremental changes in the existing no-build `frontend/app.js`.

---

## Solution

Incremental improvements entirely within the existing no-build frontend (`frontend/app.js`):

### Frontend Improvements

- **Token Semantics Alignment**: Rename the "Total Tokens" KPI to **"Active Tokens"** (calculated as input tokens + output tokens). Update the Model Mix values column header to "Active Tokens." Ensure compact session and agent-run Token Breakdown totals remain additive across all four buckets. High-usage session detection continues to be based on Active Tokens.

- **Rename Live Events → Operational Events**: Rename the panel and all associated labels, tooltips, and references to "Operational Events." Retain the current health-alert snapshot behavior. Explicitly mark a true real-time activity stream as out of scope.

- **Client Metadata Caching**: Cache client metadata responses for 10 minutes. On cache miss after staleness, serve stale data immediately while triggering a background refresh. Handle unknown client IDs by refreshing them in the background without blocking the UI.

- **Freshness Indicators**: Add a global last-refresh timestamp at the top of the dashboard. Add subtle per-panel indicators showing refresh state, last-updated time, and failure status.

- **KPI Label Clarity**: Append date ranges to historical metric labels (e.g., "Active Tokens — Last 7 Days"). Append "Current" or a timestamp to health metric labels (e.g., "System Health — Current").

---

## User Stories

1. As a dashboard operator, I want the "Active Tokens" KPI to clearly represent input + output tokens for the selected period, so that I can understand current token consumption.

2. As a dashboard operator, I want the Model Mix values labeled as "Active Tokens," so that the terminology is consistent across all token-related panels.

3. As a dashboard operator, I want the compact session and agent-run Token Breakdown totals to be additive across all four buckets, so that I can verify the math adds up.

4. As an administrator, I want high-usage session detection to continue using Active Tokens as its threshold metric, so that existing alerting rules remain valid.

5. As a dashboard operator, I want the "Live Events" panel renamed to "Operational Events," so that the label matches its actual snapshot-based behavior.

6. As a dashboard operator, I want Operational Events to show health-alert snapshots (not a live streaming feed), so that my expectations match what I see.

7. As a dashboard operator, I want client metadata to be cached for 10 minutes, so that repeated dashboard interactions don't re-fetch the same data.

8. As a dashboard operator, I want to see stale client data immediately while a background refresh happens, so that the dashboard never blocks waiting for fresh data.

9. As a dashboard operator, I want unknown client IDs to trigger a background refresh without freezing the UI, so that I can still navigate the dashboard.

10. As a dashboard operator, I want a global last-refresh timestamp visible at the top of the dashboard, so that I know how fresh the entire view is.

11. As a dashboard operator, I want subtle per-panel freshness indicators (refreshing, updated, failed), so that I can quickly spot which panels need attention.

12. As a dashboard operator, I want historical metric labels to include their date range (e.g., "Last 7 Days"), so that I understand the time window each metric covers.

13. As a dashboard operator, I want health metric labels to show "Current" or a timestamp, so that I know they reflect the latest state.

14. As a developer, I want these changes made incrementally in the existing `frontend/app.js` without requiring a build step or new framework, so that the deployment process stays simple.

---

## Implementation Decisions

### Token Metrics

- **KPI Renaming**: "Total Tokens" → **"Active Tokens"** everywhere in the UI (dashboard header, tooltips, exports).
- **Calculation**: Active Tokens = input tokens + output tokens (unchanged logic, only label change).
- **Model Mix**: Column header renamed from whatever it was to "Active Tokens."
- **Token Breakdown**: Compact session and agent-run Panel totals remain **additive** across all four buckets (no change to the summation logic).
- **High-Usage Detection**: Continues to use Active Tokens as the basis for flagging high-usage sessions (no change to the detection logic).

### Operational Events (formerly Live Events)

- **Rename Only**: Replace all occurrences of "Live Events" with "Operational Events" in labels, tooltips, URLs, and API response keys if applicable.
- **Behavior Preserved**: The panel continues to present health-alert snapshots on a schedule. No streaming or WebSocket integration is added.
- **Out of Scope**: A true real-time activity stream is explicitly excluded from this iteration.

### Client Metadata Caching

- **Cache Duration**: 10-minute TTL for client metadata responses stored in the browser.
- **Stale Fallback**: When the cache expires, serve the stale copy immediately; do not block the UI while refetching.
- **Background Refresh**: Trigger a silent background fetch to update the cache. If the ID is unknown (never seen before), still refresh in the background without surfacing an error to the operator.

### Freshness Indicators

- **Global Timestamp**: Display a single "Last Refreshed" timestamp at the top of the dashboard indicating when the most recent full refresh occurred.
- **Per-Panel States**: Each panel shows one of three subtle states:
  - **Refreshing** — a small spinner or pulsing indicator during data fetch.
  - **Updated** — a timestamp showing when this panel's data was last successfully loaded.
  - **Failed** — a minimal error indicator (no stack traces) when a fetch fails.

### KPI Labels

- **Historical Metrics**: Append the date range to the metric name. Examples:
  - "Active Tokens — Last 7 Days"
  - "Error Rate — Last 24 Hours"
- **Health Metrics**: Append "Current" or a timestamp. Examples:
  - "System Health — Current"
  - "Uptime — Aug 6, 2026 14:30 UTC"

### Testing

- **Frontend Unit Tests**: Cover caching logic (TTL expiry, stale fallback, background refresh), freshness indicator state transitions, and KPI label formatting.
- **Behavior / Integration Tests**: Verify end-to-end flows — dashboard load with cached vs. uncached client metadata, Operational Events rename reflected correctly, freshness indicators update properly.
- **No New Browser Framework**: Tests use the existing test infrastructure. No Playwright, Cypress, or Puppeteer required unless already present.

### Rollout

- **No Feature Flags**: All changes ship together. There is no gradual rollout via feature toggles.
- **Backward-Compatible Frontend Deployment**: The frontend remains backward-compatible with existing backend APIs. Since no backend changes are included, deployment is a straightforward static file push.

### Code Location

- All changes go into the existing `frontend/app.js` in the no-build setup.
- No database migrations, no backend code changes, no deployment configuration updates.

---

## Out of Scope

The following items are explicitly excluded from this PRD iteration:

- **True Activity Stream**: A real-time, streaming activity feed (WebSocket/SSE) is deferred. Operational Events remains snapshot-based.
- **New Frontend Framework**: Migrating `frontend/app.js` to React, Vue, Svelte, or any build-tool-powered framework is out of scope.
- **Backend Optimizations**: Database indexing, application profiling, replica scaling, or any backend-layer performance work is tracked separately and is not part of this PRD.
- **New API Endpoints**: No new API endpoints will be created. Optimizations work with existing endpoints through client-side caching and response shaping.
- **Mobile App Development**: Native or PWA mobile clients are not addressed in this iteration.
- **Third-Party Integrations**: Adding integrations with external monitoring or alerting platforms (PagerDuty, Opsgenie, etc.) is deferred.
- **UI Theme / Visual Redesign**: Color schemes, typography, and layout aesthetics are unchanged unless directly caused by a performance fix.
- **Browser Automation / Scraping**: Any headless browser orchestration or web scraping capabilities are out of scope.

---

## Further Notes

1. **Token Semantics Are Narrowly Scoped**: The Active Tokens renaming and clarification applies specifically to dashboard-facing KPIs and breakdown panels. Broader token lifecycle concerns (plugin communication, gateway handshakes) are tracked separately and are not part of this PRD.

2. **Operational Events Naming Is Final**: The decision to rename from "Live Events" to "Operational Events" resolves the expectation mismatch. No additional operator survey is needed for this change since the behavior (snapshot-based health alerts) already justifies the new name.

3. **Caching Strategy Is Conservative**: A 10-minute TTL balances freshness against server load. Stale fallback ensures operators always see something, and background refresh guarantees eventual consistency. Unknown-ID handling prevents errors from propagating to the UI.

4. **All Changes Are Frontend-Side**: This PRD exclusively modifies `frontend/app.js`. No database migrations, backend code changes, or infrastructure adjustments are included. The deployment is a simple static file replacement.

5. **Freshness Indicators Are Subtle, Not Disruptive**: The global timestamp and per-panel states are designed to be informative without adding visual clutter. They should be detectable at a glance but not dominate the dashboard aesthetic.

6. **Rollout Is Simple**: With no feature flags and backward-compatible changes, deployment is a straightforward push of the updated `frontend/app.js`. Since there are no backend dependencies, rollback is a matter of restoring the previous file version.
