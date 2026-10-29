## Summary
This automated develop-loop run implemented the following issues:

| Issue | Title |
|-------|-------|
| #840  | Use original Alembic revisions in AFK reporting and change-request integration tests |

## Changes
- Replaced copied/mutated Alembic version trees with unmodified revision chain via `alembic.ini` (`script_location` points directly at the checked-in `alembic` directory, no `shutil.copy` or `from __future__ import annotations` injection)
- Added disposable DB attestation (`DatabaseTarget.from_env()` + `verify_disposable_target()`) before destructive operations (`DROP TABLE … CASCADE`, `alembic upgrade head`); fail-closed on non-disposable targets
- Isolated Alembic's `asyncio.run`-based `env.py` via `asyncio.to_thread` to avoid event-loop conflicts
- Preserved HTTP/SQL E2E behaviours and schema upgrade/teardown protections — test bodies unchanged, previous `pytest.skip` guards removed so misconfiguration surfaces as error
- Files changed (3):
  - `tests/integration/test_change_request_summary.py`
  - `tests/integration/test_afk_outcomes_correlations_jsonb.py`
  - `tests/integration/test_reporting_aggregates.py`

## Review
Consolidated diff review available at `.status/handoff/diff-review.md` — **VERDICT: approve-with-comments**

Related to #840
Depends on #826 #828 #838 — do not close #840 until those are merged.


---
*Changes were pushed directly to `ai/feat/issues-840` by the autonomous develop-loop.*

