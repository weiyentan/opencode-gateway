# PRD: Client / Project Usage Breakdown Fetch Error

## Problem Statement

The Aurora Glass dashboard's **Client / Project Usage Breakdown** panel shows a ⚠ Fetch error instead of aggregate usage data grouped by client and project. The frontend correctly reports the error via `fetchErrors.aggClientProject` — the backend API call `GET /api/v1/usage/aggregates?group_by=client,project` is failing at the database level.

The root cause is a SQL correctness bug in the aggregates query builder. When `group_by` includes `project`, the query selects a standalone `project_label` output column (via `_PROJECT_LABEL_SQL`) but the `GROUP BY` clause only includes the concatenated expression `oc.name || '|' || (_PROJECT_LABEL_SQL)`. PostgreSQL requires every non-aggregate SELECT expression to appear in `GROUP BY` unless it is functionally dependent on the grouped columns. Because `_PROJECT_LABEL_SQL` references columns from a LEFT JOINed table (`opencode_source_projects`), PostgreSQL cannot infer functional dependency, and the query fails at runtime.

The existing unit tests mock the database layer — they return synthetic rows directly from `conn.fetch`, so they never exercise SQL validity against a real PostgreSQL parser. The bug was invisible in tests.

## Solution

Fix the SQL query in the aggregates endpoint so that `group_by=client,project` (and `group_by=project` alone) produces valid PostgreSQL that returns aggregate rows grouped by client name and resolved Project Label, with a separate `project_label` output column for display.

**Grouping rule**: The breakdown groups by **OpenCode Client** plus resolved **Project Label**. Unresolved project metadata rolls up under the Project Label `unknown`. Two different raw project IDs that resolve to the same Project Label under the same client are merged into one row (consistent with GROUP BY semantics).

**SQL fix**: When `project` is in the group-by list, include the standalone `_PROJECT_LABEL_SQL` expression in the `GROUP BY` clause alongside the concatenated group expression. The `project_label` output column is then deterministic and PostgreSQL accepts the query.

## User Stories

1. As a dashboard operator, I want the Client / Project Usage Breakdown panel to render without a fetch error, so that I can see which clients and projects are consuming tokens.
2. As a dashboard operator, I want to see project usage aggregated by resolved Project Label (display name, not raw source ID), so that the breakdown is readable.
3. As a dashboard operator, I want unresolved project metadata to appear under the label `unknown`, so that no usage is silently dropped from the breakdown.
4. As a dashboard operator, I want the Client / Project totals in the breakdown to reconcile with the main usage KPIs, so that I can trust the numbers.
5. As a dashboard operator, I want the expand/collapse drilldown in the Client / Project panel to continue working after the fix, so that I can explore per-project usage.
6. As a developer, I want a regression test that inspects the generated SQL shape (not only mocked response data), so that GROUP BY correctness is verified without a real PostgreSQL instance.
7. As a developer, I want the fix to require no frontend changes, so that the scope is limited to the backend query builder.

## Implementation Decisions

### Module: `app/api/usage.py` — `_fetch_aggregates`

- The `_group_expression` function remains unchanged — it produces the concatenated `client|project` value for the `group_value` output column.
- The `_fetch_aggregates` function is the only changed function. When `has_project` is true, the standalone `_PROJECT_LABEL_SQL` expression is added to the `GROUP BY` clause as an additional group-by column (not replacing the concatenated expression).
- The `GROUP BY` clause becomes: `GROUP BY {group_expr}, {_PROJECT_LABEL_SQL}` when project is in group_parts.
- No changes to `_build_aggregate_filters`, `_group_expression`, `_PROJECT_LABEL_SQL`, or any other helper.
- No changes to the `AggregateRow` Pydantic schema — `project_label` is already defined.

### No frontend changes

The frontend already handles the fetch error correctly. Once the backend returns valid data, the `renderClientProjectBreakdown` function in `app.js` will render it without any code changes. The existing pipe-delimited `group_value` parsing, `resolveProjectLabel`, expand/collapse drilldown, and error indicators all work correctly with valid API responses.

### Glossary terms recorded in CONTEXT.md

The following terms were clarified during the grilling session and written to `CONTEXT.md`:
- **Client / Project Usage Breakdown** — A user-facing aggregate view that groups usage by OpenCode Client and resolved Project Label.
- **Unknown Project Label** — The label `unknown` represents usage whose project metadata could not be resolved into a displayable Project Label.

## Testing Decisions

### What makes a good test

- Tests should verify external behavior: the API returns 200 with grouped data for `group_by=client,project`.
- Tests should also verify generated SQL shape to catch GROUP BY correctness without a real PostgreSQL instance (the existing pattern of mocking `conn.fetch` masks SQL-level bugs).
- Tests should not assert implementation details like line numbers or SQL formatting whitespace.

### Which modules will be tested

- **`tests/test_usage.py` — `TestClientProjectAggregates`**: Existing class at line 466. One new test method: `test_group_by_client_project_sql_shape` that captures the SQL string passed to `conn.fetch` and asserts the `GROUP BY` clause includes the standalone project-label expression.
- Existing response-pathology tests in `TestClientProjectAggregates` already verify that when `conn.fetch` returns rows, the API serializes them correctly. These tests continue to pass because the fix does not change response serialization.

### Prior art in the codebase

- Existing tests in `test_usage.py` already use `mock_conn.fetch.call_args` to inspect SQL (e.g., `test_limit_and_offset_are_respected` at line 846 checks that the SQL contains `LIMIT` and `OFFSET`). The new test follows the same pattern.
- The `_mk_aggregate_row` factory function at line 183 already supports `project_label` and `group_value` parameters — no changes needed.

## Out of Scope

- No frontend changes to Aurora Glass.
- No changes to the `AggregateRow` schema or any other Pydantic model.
- No changes to the records-with-context endpoint or its GROUP BY logic (that endpoint uses a separate helper `_rwc_group_expression` which already includes the project-label expression in GROUP BY via `_rwc_group_by_columns` — it is unaffected).
- No changes to the agent-runs endpoint.
- No database migrations.
- No config or threshold changes.
- No ADR creation (the domain decision is recorded in CONTEXT.md only).
- No end-to-end tests against a real PostgreSQL instance.

## Further Notes

- This bug only manifests when the query is executed against a real PostgreSQL database. The mocked-db tests passed because they return pre-constructed rows without executing SQL.
- The records-with-context endpoint (`/api/v1/usage/records-with-context`) has a separate GROUP BY builder (`_rwc_group_expression`) that does include the project-label expression in its GROUP BY list — it is not affected by this bug.
- The `_group_expression` function is also used by the `get_aggregates` endpoint's ungrouped total row path (no `group_parts`), which does not need any change.
- The existing SQL in `_group_expression` for `project` prepends and appends parentheses: `(_PROJECT_LABEL_SQL)`. The GROUP BY addition should use the same parenthesized expression so they match.
