## Problem Statement

Aurora Glass currently displays only the first 50 Agent Run rows. Although the Gateway API already returns pagination metadata and accepts `limit` and `offset`, the dashboard does not expose page navigation. Users therefore cannot reach the remaining matching Agent Runs in a date range or filtered result set.

## Solution

Add one server-side pagination control below the Agent Runs panel. Keep the existing Agent Runs rows, columns, ordering, filters, and detail behavior unchanged. The control will use the existing API pagination contract and provide numbered pages, Previous/Next navigation, ellipses for large page counts, and page-size choices of 25, 50, and 100 rows.

Pagination state will be persisted in the browser URL using `page` and `page_size` query parameters. Existing filters will remain part of the request and URL state.

## User Stories

1. As an observability user, I want to see numbered pages below Agent Runs, so that I can reach all matching runs.
2. As an observability user, I want to move to the next or previous page, so that I can browse results sequentially.
3. As an observability user, I want to jump directly to a numbered page, so that I can reach a distant result set efficiently.
4. As an observability user, I want ellipses when there are many pages, so that pagination remains compact.
5. As an observability user, I want to choose 25, 50, or 100 rows per page, so that I can balance scanning speed and request size.
6. As an observability user, I want changing page size to return to page 1, so that I do not land on an invalid or unexpected offset.
7. As an observability user, I want existing date, agent, and status filters preserved while paging, so that pagination stays within my selected result set.
8. As an observability user, I want applying or changing filters to return to page 1, so that the result list starts at the beginning of the new result set.
9. As an observability user, I want the current page and page size in the URL, so that I can reload or bookmark the current result set.
10. As an observability user, I want browser Back and Forward to navigate page changes, so that normal browser navigation works as expected.
11. As an observability user, I want the current page to remain selected during automatic refresh, so that new data does not unexpectedly move me to another page.
12. As an observability user, I want the existing rows to remain visible while another page loads, so that pagination does not cause distracting blank states.
13. As an observability user, I want a failed page request to preserve the previously displayed rows and existing error treatment, so that a transient failure does not erase useful data.
14. As an observability user, I want an invalid page to fall back to the nearest valid page, so that changing data does not leave me on an unusable empty page.
15. As a keyboard user, I want pagination controls to be real buttons with meaningful labels, so that I can operate them without a mouse.
16. As a screen-reader user, I want the active page identified with `aria-current`, so that I know which result page is selected.
17. As an observability user, I want the existing Agent Runs row content and columns unchanged, so that pagination does not alter the meaning or layout of the table.

## Implementation Decisions

- Modify the Aurora Glass frontend pagination state and fetch flow; the Gateway API already supports `limit`, `offset`, and `total` for Agent Runs.
- Keep the existing Agent Runs table unchanged. Do not add per-row numbering or change row contents, columns, ordering, filters, or detail interactions.
- Add one pagination control block below the Agent Runs panel, outside the panel box.
- Use page sizes 25, 50, and 100, with 50 as the default to preserve current behavior and performance.
- Translate page state into the existing API contract: `limit` equals `page_size` and `offset` equals `(page - 1) * page_size`.
- Derive total page count from the API response `total` and the selected page size.
- Implement page-item calculation as a pure, isolated function that returns numbered pages and ellipsis markers for compact navigation.
- Use Previous, numbered page, and Next controls. Disable Previous on the first page and Next on the last page.
- Use accessible button labels, `aria-current="page"` for the active page, and disabled controls where navigation is unavailable.
- Persist `page` and `page_size` in URL query parameters. Missing, malformed, or unsupported values fall back to page 1 and page size 50.
- Page changes update the URL with browser history. Filter and page-size changes reset to page 1 and replace the current URL state rather than adding unnecessary history entries.
- Preserve the current page during automatic refresh and refetch the same server-side offset.
- Preserve currently displayed rows during loading and request failures.
- If the current page becomes invalid because the total changes, calculate the nearest valid page, update the URL, and fetch that page.
- Do not add a database migration, feature flag, resource adjustment, or new API endpoint.
- Do not publish an issue or apply an `afk`/`ready-for-agent` label as part of this PRD.

## Testing Decisions

- Tests should verify observable pagination behavior rather than implementation details.
- Extend the existing frontend pure-function test suite to cover:
  - URL construction with page size and calculated offset.
  - Defaulting and validation of URL pagination parameters.
  - Page-item calculation for small, boundary, and large page counts.
  - Previous/Next disabled states and active-page accessibility attributes.
  - Reset behavior for filters and page-size changes.
  - Browser history behavior for page changes versus replacement behavior for resets.
  - Preservation of rows during loading and errors.
  - Fallback to the nearest valid page when the current page becomes empty or invalid.
- Extend the existing Agent Runs backend tests to verify custom `limit` and `offset` values and the returned pagination metadata. No backend behavior change is expected.
- Preserve existing frontend tests for filter URL generation, Agent Runs rendering, and refresh/error handling.
- The pure pagination calculation should be tested independently because it has a small interface and many boundary cases.

## Out of Scope

- Changing Agent Runs row content, columns, sorting, filters, or detail views.
- Adding a per-row number column.
- Loading all matching Agent Runs into the browser before paginating.
- Adding a second pagination control above the table or duplicating controls inside the panel.
- Cursor-based pagination or changing the existing API pagination contract.
- Database schema changes or new indexes for this feature.
- Increasing Kubernetes CPU or memory resources, adding replicas, or adding a feature flag.
- Adding unique-agent aggregation or changing the meaning of an Agent Run.
- Creating tracker issues or applying triage labels.

## Further Notes

- The current deployment was inspected through the supplied kubeconfig. The Gateway is healthy on a single replica with 200m CPU and 256Mi memory limits, and the three-instance PostgreSQL cluster is healthy. Server-side pages of 25, 50, and 100 are consistent with the current resource profile.
- The current API uses offset pagination. Very deep offsets may eventually have higher database latency; this PRD does not change that strategy. Query timing can be monitored after rollout if users routinely navigate to deep pages.
- The current working tree contains unrelated uncommitted changes. Implementation should preserve them and modify only the files required for this feature.
