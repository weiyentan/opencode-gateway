# PRD: Center Agent Runs Pagination

## Problem Statement

On the Aurora Glass dashboard's Agent Runs tab, pagination works correctly, but the pager controls sit below the table left-aligned within the tab area. In the Aurora Glass design language the content is centered, so the left-aligned pager looks unbalanced against the rest of the dashboard. The user wants the pagination centered "in the box" — the Agent Runs tab area it currently occupies.

## Solution

Center the pagination controls horizontally within the Agent Runs tab area, at their current position below the table. The pagination behavior — page state, URL persistence, page-size selection, refresh resilience, and filter interaction — is unchanged. This is a presentation-only adjustment.

## User Stories

1. As an Aurora Glass user, I want the pagination controls centered below the Agent Runs table, so that the dashboard looks visually balanced.
2. As an Aurora Glass user, I want the centered pager to stay centered regardless of how many page buttons are shown (single page, many pages with ellipses), so that the layout stays consistent.
3. As an Aurora Glass user, I want the pager to remain centered on desktop, tablet, and phone widths, so that the fix holds across the responsive breakpoints.
4. As an Aurora Glass user, I want the pager controls to wrap onto multiple lines on narrow screens instead of overflowing, so that all controls remain reachable without horizontal scrolling.
5. As an Aurora Glass user, I want all existing pagination behavior (page selection, page-size change, back/forward navigation, refresh resilience, filter preservation) to keep working exactly as before, so that centering does not regress functionality.
6. As a maintainer, I want the centering change to be CSS-only, so that the change is small, low-risk, and does not disturb the DOM structure or the JavaScript renderer.
7. As a maintainer, I want the existing pagination tests to remain green without modification, so that no test churn accompanies a pure layout fix.

## Implementation Decisions

- **Centering mechanism**: add `justify-content: center` to the pagination control's flex layout (the `.agent-runs-pagination` rule in the frontend stylesheet). Today the rule is `display: flex; align-items: center; gap: 6px; flex-wrap: wrap` with no `justify-content`, which defaults to left-aligned (`flex-start`).
- **No DOM change**: the pager stays where it is — inside the Agent Runs tab (`#tab-agent-runs`), as a sibling after the Agent Runs panel, below the table. It is NOT moved inside the glass panel. The `<nav>` container is a flex-column child of the tab and already spans the tab width, so centering requires no parent changes.
- **No JavaScript change**: the `renderAgentRunPagination` renderer, the pure pagination helpers (`parseAgentRunPagination`, `parseAgentRunPageSize`, `computePageItems`, `nearestValidAgentRunPage`), the URL/history state hooks, and the page-size selector in the filter bar are all untouched.
- **Vertical spacing**: refine the pager's spacing to a compact, distinct gap — approximately 16px above the pager (separating it from the table/panel) and 4px below it. The current rule's `margin-bottom: 14px` is replaced by this top/bottom pairing.
- **Wrap behavior**: `flex-wrap: wrap` is retained, so the centered pager wraps onto additional lines on narrow viewports while remaining centered.
- **Responsive**: centering applies at all breakpoints (desktop ≥1025px, tablet 761–1024px, phone ≤760px). No per-breakpoint overrides are required.
- **Accessibility**: no changes to `aria-label`, `aria-current`, or the disabled states of Previous/Next buttons.

## Testing Decisions

- A good test for this change is one that verifies external visual behavior; since centering is purely presentational, the meaningful verification is visual rather than assertion-based.
- **No new automated tests are added.** The existing suite already covers:
  - 16 pagination test blocks in the frontend pure-function tests (state parsing, page-item calculation, render markup, page selection, page-size reset, popstate re-sync, resilience fallbacks).
  - A stylesheet rule-presence test that asserts `.agent-runs-pagination`, `.pagination-btn.pagination-current`, and `.pagination-ellipsis` rules exist — this stays green because the rule remains present (only a declaration is added).
- **Visual verification** at three widths (desktop ≥1025px, tablet 761–1024px, phone ≤760px), checking:
  - The pager is horizontally centered below the table.
  - A large page count (ellipsis visible) stays centered.
  - A single-page count stays centered.
  - On the phone band, the pager wraps cleanly without horizontal overflow.
- The existing backend tests and frontend behavior tests must all remain passing.

## Out of Scope

- Any change to pagination behavior: page-size options, URL persistence, filter interaction, refresh resilience, current-page no-op, popstate handling.
- Moving the pager inside the glass panel (the pager stays outside the panel, below it).
- Changes to the page-size selector in the filter bar.
- Backend/API changes (the paginated agent-runs endpoint is untouched).
- Deployment / image-bump changes (the downstream k8s MR is unaffected by this frontend change).
- New automated tests for the centering itself.

## Further Notes

- The pager markup is static and empty in the HTML (`<nav class="agent-runs-pagination" id="agent-runs-pagination" aria-label="Agent Runs pages">`); the JavaScript renderer fills it in on every refresh cycle. Because centering is handled purely by the flex rule, the renderer needs no knowledge of it.
- Buttons reuse the existing `.filter-clear` secondary-button styling plus `.pagination-btn`; the current page additionally gets `.pagination-current`. None of these classes change.
- The existing PRD at `docs/prd/agent-runs-pagination.md` covers the original pagination feature (issues #425–#429); this PRD is a follow-up presentation fix and does not reopen those decisions.
- No CONTEXT.md glossary additions are needed — the change introduces no new domain vocabulary.
