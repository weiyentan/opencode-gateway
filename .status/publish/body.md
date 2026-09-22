## Summary

This automated develop-loop run implemented the following issues:

| Issue | Title |
|-------|-------|
| #724 | Extract shared dashboard vocabulary as importable constants |
| #725 | Add freshness floor to dashboard summary API |
| #726 | Bound AWX reconciliation concurrency with configurable semaphore |
| #728 | Remove redundant session join from refresh usage discovery |
| #729 | Split verifier into independent AFK and reporting failure domains |
| #730 | Add PostgreSQL golden-dataset integration test for AFK dashboard |

## Changes

- **#724**: Extracted METRIC_COLUMNS, UNAMBIGUOUS_CTE, and CR_EVENT_FILTER from the dashboard engine as importable constants. Backfill, verify, and refresh scripts now import from the engine.
- **#725**: Added oldest_derived_at freshness floor field to AFKDashboardSummaryBucket and AFKDashboardSummary schemas. Additive and backward-compatible.
- **#726**: Added GATEWAY_AWX_RECONCILIATION_MAX_CONCURRENCY env var (default 10, min 1) to bound concurrent AWX HTTP lookups during execution reconciliation.
- **#728**: Removed redundant JOIN afk_run_sessions from refresh usage-event discovery branch.
- **#729**: Split verify_afk_dashboard_daily.py into independent AFK and reporting verifiers with independent exit statuses. Shared helpers moved to verify_helpers.py.
- **#730**: Added PostgreSQL golden-dataset integration test (12 tests) exercising the full AFK Dashboard lifecycle.

## Review

A consolidated diff review is available.

Closes #724
Closes #725
Closes #726
Closes #728
Closes #729
Closes #730
