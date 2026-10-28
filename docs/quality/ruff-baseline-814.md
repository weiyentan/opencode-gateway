# Ruff Baseline Inventory — Issue #814

> **Parent:** #804 · **Type:** AFK research/verification — measurement only, no production or gate changes.
> **Checked commit:** `4cb263eb83dbbfef6d5e96b39aa716d8f5edf2c6` (worktree `tmp/issue-814-inventory-ruff-baseline-evidence`)
> **Ruff:** `0.15.16` · **Python:** `3.12.14` · **Date:** 2026-10-29 · **Tree:** clean, `ruff check .` → `All checks passed!`
> **pyproject gate:** `[tool.ruff] line-length=100`, `select=["E","F","I","UP"]`, `ignore=["UP045","UP007","UP017","UP035","E501","I001"]`
> **Isolated baseline config per rule:** `ruff check --isolated --select <RULE> --line-length 100 --target-version py312 .` (no `pyproject.toml` mutation; disposable local tooling only). Repro commands in §8.

## 1. Summary (CI-baseline evidence)

| Rule | Meaning | Violations | Files | Fixable | Primary locus | Class |
|------|---------|-----------:|------:|--------:|---------------|-------|
| **I001** | unsorted / unformatted imports (isort) | **99** (`86` w/o isolated MCP delta) | 90 | 99 (100%) | `alembic/` 32, `tests/` 32, `opencode-gateway-mcp/` 13*, `app/` 8, `afk_outcomes/` 7 | mechanical |
| **UP035** | deprecated `typing` import (`List`→`list`, etc.) | **42** | 42 | 42 (100%) | `alembic/` 35, `scripts/` 4 | mechanical |
| **UP007** | `Union[X,Y]` / `Optional[X]` → `X | Y` (`non-pep604-annotation-union`) | **73** | 25 | 72/73 (99%) | `alembic/` 72, `app/api/usage.py` 1 | mechanical* |
| **UP045** | `Optional[X]` → `X | None` (`non-pep604-annotation-optional`) | **212** | 9 | 212 (100%) | `app/db/models/` 189 (projection 89 + afk 61 + ingest 17…), `scripts/` 21 | mechanical |
| **UP017** | `datetime.timezone.utc` → `datetime.UTC` | **99** | 45 | 99 (100%) | `tests/` 75, `app/` 14, `scripts/` 8 | **semantics-sensitive** (intentional py39 compat) |
| **E501** | line too long (>100) | **212** | 37 | 0 (0%) | `app/api/ingest.py` 76, `app/core/` 12, `tests/` 86 | mechanical (formatting) |
| **Total** | globally suppressed surface | **737** violations | — | — | — | — |

\* I001 isolated 99 vs non-isolated `ruff check --select I001` 86: delta is 13 `opencode-gateway-mcp/` files that are sorted per that subproject's own `pyproject.toml` (see §3). They are *not* suppressed by the top-level ignore — isolated `--isolated` uses global defaults. CI `ruff check .` still visits `opencode-gateway-mcp/` (see `ruff check --show-files` 309 files) but per-directory config makes them pass.

No `docs/` violations (docs are `*.md`, not linted). `frontend/` 0 violations for these rules. `ruff check .` and `mypy app/ afk_outcomes/` remain green (see §7) — no gate changed.

## 2. Current global ignores

`pyproject.toml:29`

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "UP"]
# Pre-existing modernization/style violations scoped for the CI-wiring PR (#532).
ignore = ["UP045", "UP007", "UP017", "UP035", "E501", "I001"]
```

* Scope:* widespread pre-existing violations tracked for future cleanup. Gate stays active for all other rules (notably `F` family — unused imports/vars/redefinitions). Subproject `opencode-gateway-mcp/pyproject.toml` has `select=["E","F","I","UP"]` with **no** `ignore` — it is not gated by the top-level ignore (per-directory resolution).

## 3. Narrow `noqa` inventory and justification

Total `noqa` directives: **288** occurrences. Breakdown by code (distinct occurrences, not per-file):

| Code | Count | Typical sites | Justification |
|------|------:|---------------|---------------|
| `UP017` | 119 | `afk_outcomes/models.py` 2, `afk_outcomes/providers/github.py` 2, `afk_outcomes/serialization.py` 2, `app/consumer/*` 4+, `tests/integration/test_afk_consumer.py` 10, `scripts/afk_e2e_test.py` 3, file-header `# ruff: noqa: UP017` in `app/api/admin_reconcile.py`, `tests/test_admin_reconcile.py`, `scripts/backfill_tool_calls.py`, `scripts/seed.py` | **Intentional py39 compat** — `timezone.utc` preserved because `datetime.UTC` is 3.11+. Every occurrence carries ` - datetime.UTC is 3.11+` reason. *Do not bulk-fix without dropping py39 support.* |
| `UP007` | 42 | `alembic/versions/0018`, `0033`, `0035`, `0036`, `0037`… (3 per file: `down_revision`, `branch_labels`, `depends_on: Union[str, …]`) | Alembic scaffolding template uses `Union`. Mechanical but migrations are append-only; low value vs churn. |
| `BLE001` | 42 | `afk_outcomes/service/execution_reconciliation.py` 2, `app/consumer/*`, `scripts/*`, `app/db/session.py` 8 | `except Exception` broad catch — intentional (reconciliation/consumer resilience). Unrelated to suppressed six. |
| `E402` | 39 | `app/db/*`, `scripts/*` | `import` not at top due to circular / env setup — intentional. |
| `F401` | 15 | `alembic/env.py` (`Base` registers models) | Unused import is side-effect import — intentional. |
| `UP042` | 13 | `afk_outcomes/models.py` StrEnum classes, `afk_outcomes/service/execution_reconciliation.py` | `class X(str, Enum)` → `StrEnum` is 3.11+; `keep importable on 3.9` — intentional compat, not suppressed globally. |
| `E501` | 7 | `opencode-gateway-mcp/tests/test_afk_run_story.py:552`, `test_change_request_story.py` 6 | Long assertion strings / httpx response literals in tests — narrow, justified (readability vs wrap). Not a global suppression replacement. |
| `UP041` | 4 | — | — |
| Other | 7 | `F811`, `F841`, `UP036`, `UP046`, `PLW0603`, `E731` | Isolated, justified. |

**No `noqa: I001` / `noqa: UP035` / `noqa: UP045` found** — those suppressed rules have zero narrow exceptions; all suppressed violations are invisible (caught by global `ignore`, not by `noqa`). That is exactly the debt this baseline surfaces — *do not treat ignored violations as solved.*

The `UP017` and `UP007` `noqa`s are *additive* on top of the global ignore: even after the global ignore is removed, those 119 + 42 `noqa` lines would still silence violations. They are narrow, reasoned, and should remain; they are not “suppression floods”.

## 4. Per-rule isolated evidence

> Each section records the exact isolated command, violation count, file count, fixability, classification (test/docs/production), and file list. Regenerate any section with the one-liner in §8. Full JSON (`--output-format json`) was used for file-precise counts; statistics cross-checked with `--statistics`.

### I001 — `unsorted-imports` (isort)

- **Command:** `ruff check --isolated --select I001 --line-length 100 --target-version py312 --no-cache .`
- **Also (non-isolated, respects pyproject):** `ruff check --select I001 --no-cache .` → 86 violations (delta 13 = `opencode-gateway-mcp/` sorted per its own config; see summary).
- **Statistics:** `99 I001 [*] unsorted-imports — 99 fixable`
- **Violation total:** 99 in 90 files. Classification (violations, not files): `tests/` 32, `alembic/` 32, `opencode-gateway-mcp/` 13, `app/` 8, `afk_outcomes/` 7, `scripts/` 6, `run_frontend_tests.py` 1.
- **File distribution (violations per file, sorted descending):**

| Violations | Path |
|-----------:|------|
| 7 | `tests/test_auth.py` |
| 3 | `tests/test_agent_runs.py` |
| 2 | `tests/integration/test_afk_dashboard_golden_dataset.py` |
| 1 | `afk_outcomes/closure_episodes.py`, `afk_outcomes/repository/__init__.py`, `afk_outcomes/repository/closure_projection.py`, `afk_outcomes/repository/crud.py`, `afk_outcomes/repository/executions.py`, `afk_outcomes/run_status.py`, `afk_outcomes/service/execution_reconciliation.py` |
| 1 | `alembic/versions/0000_initial_baseline.py` … `0049_recovery_checkpoints.py` (32 files, 1 each — see full list in JSON) |
| 1 | `app/api/afk_runs.py`, `app/api/reporting_ingest.py`, `app/api/usage.py`, `app/consumer/afk_consumer.py`, `app/core/factory.py`, `app/core/reconciliation.py`, `app/core/schemas/identity.py`, `app/db/models/reporting.py` |
| 1 | `opencode-gateway-mcp/src/opencode_gateway_mcp/server.py` + 12 mcp test files (only under `--isolated`; pass under subproject config) |
| 1 | `scripts/backfill_afk_dashboard_daily.py`, `scripts/backfill_usage_dashboard_daily.py`, `scripts/refresh_usage_dashboard_daily.py`, `scripts/verify_afk_dashboard_daily.py`, `scripts/verify_helpers.py`, `scripts/verify_reporting_resource_aggregates.py` |
| 1 | 35 further test files (1 each — full list reproducible via JSON; see §8) |

- **Safe-automation candidate:** **Yes — exact, fully mechanical, 100% autofixable** (`ruff check --select I001 --fix`). Single import-block re-sort per file, no semantics. Churn is broad (90 files) but per-file diff is tiny and reviewable. Small-file cluster strategy applies (see §6).
- **Hotspots:** `tests/test_auth.py` (7) is the densest; otherwise uniformly 1 per file — no high-density file.

### UP035 — `deprecated-import`

- **Command:** `ruff check --isolated --select UP035 --line-length 100 --target-version py312 --no-cache .`
- **Statistics:** `42 UP035 [*] deprecated-import — 42 fixable`
- **Violation total:** 42 in 42 files. Classification: `alembic/` 35 violations (35 files), `scripts/` 4, `afk_outcomes/` 1, `tests/` 2.
- **File list (1 per file):** `afk_outcomes/repository/closure_projection.py`, `alembic/versions/0000` … `0049` (35 files: `0000`, `0001`, `0012`, `0013`, `0014`, `0015`, `0016`, `0017`, `0019`, `0020`, `0021`, `0022`, `0023`, `0026`, `0027`, `0028`, `0029`, `0030`, `0031`, `0032`, `0034`, `0036`, `0037`, `0038`, `0039`, `0040`, `0041`, `0042`, `0043`, `0044`, `0045`, `0046`, `0047`, `0048`, `0049`), `scripts/afk_e2e_test.py`, `scripts/verify_afk_dashboard_daily.py`, `scripts/verify_helpers.py`, `scripts/verify_reporting_resource_aggregates.py`, `tests/integration/test_read_path_live_protocol.py`, `tests/test_read_path_rollup_parity.py`.
- **Safe-automation candidate:** **Yes — 100% autofixable, mechanical** (`--fix`). Changes deprecated `typing` imports to `collections.abc`/builtin generics; safe under `py312`.
- **Hotspots:** None — uniformly 1 per file; low risk.

### UP007 — `non-pep604-annotation-union` (`Union[X,Y]` → `X | Y`)

- **Command:** `ruff check --isolated --select UP007 --line-length 100 --target-version py312 --no-cache .`
- **Statistics:** `73 UP007 [-] non-pep604-annotation-union — 72 fixable` (1 not safe-autofixable).
- **Violation total:** 73 in 25 files. Classification: `alembic/` 72 violations (24 files × 3 each), `app/api/usage.py` 1.
- **File distribution:** 3 per file for 24 alembic versions (`0000`, `0001`, `0012`, `0013`, `0014`, `0015`, `0016`, `0017`, `0019`, `0020`, `0021`, `0022`, `0023`, `0026`, `0027`, `0028`, `0029`, `0030`, `0031`, `0032`, `0034`, `0046`, `0047`, `0048`); 1 in `app/api/usage.py`.
- **Safe-automation candidate:** **Mostly mechanical, 99% autofixable** — but needs `from __future__ import annotations` or `py312` runtime guarantee for non-annotation contexts. Alembic files lack future annotations; however alembic migrations run under `py312` in CI, so `X | Y` is runtime-evaluated and safe. One violation not marked safe (check per file before bulk fix).
- **Hotspots:** Alembic cluster — 24 files × 3, uniform, low per-file risk.

### UP045 — `non-pep604-annotation-optional` (`Optional[X]` / `Union[X, None]` → `X | None`)

- **Command:** `ruff check --isolated --select UP045 --line-length 100 --target-version py312 --no-cache .`
- **Statistics:** `212 UP045 [*] non-pep604-annotation-optional — 212 fixable`
- **Violation total:** 212 in 9 files. Classification: `app/` 189, `scripts/` 21, `tests/` 2.
- **File distribution:**

| Violations | Path |
|-----------:|------|
| 89 | `app/db/models/projection.py` |
| 61 | `app/db/models/afk.py` |
| 21 | `scripts/afk_e2e_test.py` |
| 17 | `app/db/models/ingest.py` |
| 12 | `app/core/schemas/identity.py` |
| 5 | `app/db/models/identity.py` |
| 3 | `app/api/health.py` |
| 2 | `app/db/models/reporting.py` |
| 2 | `tests/test_health.py` |

- **Safe-automation candidate:** **Yes — 100% autofixable, mechanical** (`--fix`). No semantics change under `py312`. Churn hotspot is `app/db/models/projection.py` (89) — should be sliced solo or with `afk.py` as a bounded cluster (see §6).
- **Hotspots:** `app/db/models/projection.py` and `app/db/models/afk.py` — very high density; review per-file diff even though mechanical.

### UP017 — `datetime-timezone-utc` (`timezone.utc` → `UTC`)

- **Command:** `ruff check --isolated --select UP017 --line-length 100 --target-version py312 --no-cache .`
- **Statistics:** `99 UP017 [*] datetime-timezone-utc — 99 fixable`
- **Violation total:** 99 in 45 files. Classification: `tests/` 75 violations, `app/` 14, `scripts/` 8, `alembic/` 2.
- **File distribution (top, full list via JSON):**

| Violations | Path |
|-----------:|------|
| 11 | `tests/integration/test_afk_dashboard_golden_dataset.py`, `tests/test_issue_557_usage_v12.py` |
| 8 | `tests/test_agent_runs.py` |
| 6 | `tests/test_change_request_summary.py` |
| 4 | `tests/test_ingest.py` |
| 3 | `scripts/validate_github_pr_path.py`, `tests/test_afk_outcomes_change_request_detail.py`, `tests/test_afk_outcomes_models.py`, `tests/test_read_path_parity_usage.py`, `tests/test_usage.py` |
| 2 | `alembic/versions/0035_observation_key_observed_via_snapshot_at.py`, `app/api/ingest.py`, `scripts/verify_helpers.py`, and 6 more tests |
| 1 | 20 further files (1 each) |

- **Safe-automation candidate:** **No — semantics-sensitive / intentionally retained.** 119 existing `noqa: UP017` directives (see §3) document `datetime.UTC` is 3.11+ and the codebase intentionally keeps `timezone.utc` for py39 importability (`afk_outcomes/models.py` header, etc.). Autofix would silently drop py39 compat. Treat as *do not fix* until `requires-python` is raised to `>=3.11` and the `noqa` justifications are revisited.
- **Hotspots / risk:** `tests/` dominates, but production `app/` 14 hits are spread; any bulk edit would touch core reconciliation/ingest paths and break the documented compat contract.

### E501 — `line-too-long`

- **Command:** `ruff check --isolated --select E501 --line-length 100 --target-version py312 --no-cache .`
- **Statistics:** `212 E501 line-too-long — 0 fixable`
- **Violation total:** 212 in 37 files. Classification: `app/` 107 violations, `tests/` 86, `scripts/` 14, `afk_outcomes/` 5.
- **File distribution (top 10):**

| Violations | Path |
|-----------:|------|
| 76 | `app/api/ingest.py` |
| 11 | `tests/integration/test_afk_run_convergence.py` |
| 10 | `tests/test_usage.py` |
| 9 | `app/core/afk_dashboard_daily.py`, `tests/test_agent_runs.py` |
| 8 | `scripts/afk_e2e_test.py`, `tests/test_afk_outcomes_closure_integration.py` |
| 7 | `app/core/schemas/usage.py`, `tests/test_afk_run_status_aggregation.py` |
| 6 | `tests/test_agent_runs_status_filter_pg.py`, `tests/test_ingest.py` |
| 5 | `app/api/admin_afk_reconcile.py`, `tests/test_api_afk_executions.py` |
| 4 | `tests/integration/test_afk_lifecycle_multi_stage.py`, `tests/integration/test_afk_outcomes_repository.py`, `tests/test_api_afk_runs.py` |

- **Safe-automation candidate:** **Not autofixable — manual formatting, mechanical but high churn.** Requires wrapping long lines (often long f-strings, chained calls, parametrized tests). Per-file edits are safe but diff-heavy; `app/api/ingest.py` alone is 76 violations and should be a dedicated slice. Use `ruff format` or editor wrapping; do not use `# noqa: E501` floods.
- **Hotspots:** `app/api/ingest.py` (76) is the clear hotspot — 36% of all E501. Next is test convergence/summary files. Many violations are in tests (86) — lower production risk.

## 5. Mechanical vs semantics-sensitive matrix

| Rule | Mechanical? | Autofix | Semantics risk | File churn | Notes |
|------|:-----------:|:-------:|---------------:|------------|-------|
| I001 | ✓ | `--fix` safe | None | Broad (90 files, 1 per file) | Smallest per-file diff; ideal first slice. |
| UP035 | ✓ | `--fix` safe | None (py312) | Broad (42 files, 1 per file) | Single-line import rewrites. |
| UP007 | ✓* | `--fix` safe (72/73) | Low — needs runtime `X | Y` support; CI is py312 so safe; 1 unsafe flagged | Medium (25 files, 3 per alembic) | Keep with UP045 if desired (both PEP604). |
| UP045 | ✓ | `--fix` safe | None (py312) | Narrow but dense (9 files, up to 89 per file) | Mechanical but slice by hotspot file. |
| E501 | ✓ | none | None (formatting) | Medium (37 files, top 76) | Manual wrap; hotspot `ingest.py`. |
| UP017 | ✗ | `--fix` would break compat | **High** — intentional py39 compat; 119 `noqa` already document retention | Broad (45 files) | *Do not fix* until `requires-python >=3.11`. |

## 6. Proposed safe order and bounded slices

**Principle:** mechanical + autofixable first, narrow high-density files in solo slices, broad 1-per-file rules batched, semantics-sensitive last (or never). Every slice should remove one `ignore` entry at a time and run `ruff check --select <RULE>` + `pytest` + `mypy` before merging.

**Recommended order (lowest risk → highest):**

1. **I001** — import sort. Slice as 2-3 batches: (a) `app/` + `afk_outcomes/` (15 files), (b) `alembic/` (32), (c) `tests/` + `scripts/` + `mcp` (balance). Each batch `ruff check --select I001 --fix . && ruff check --select I001 .` → drop `I001` from `ignore`.
2. **UP035** — deprecated typing imports. One slice or two: (a) `alembic/` batch (35 files), (b) `scripts/` + `afk_outcomes/` + `tests/` (7). All `--fix`.
3. **UP007** — PEP604 union. One slice: all 25 files (24 alembic one-liners + `app/api/usage.py`). Fix with `--fix`, verify the one unsafe flagged line manually.
4. **UP045** — PEP604 optional. Two slices: (a) hotspot `app/db/models/projection.py` (89) alone, (b) `app/db/models/afk.py` (61) + remaining 7 files. Both `--fix`.
5. **E501** — line length. Four bounded slices: (a) `app/api/ingest.py` (76) solo, (b) `app/core/` + `app/api/admin_*` + `afk_outcomes/` (remainder app), (c) `scripts/` (14), (d) `tests/` batched by 10 files. Manual wrap, no autofix; use `ruff format` preview per file.
6. **UP017** — **last, or retain indefinitely**. Only un-suppress after `requires-python >=3.11` and after auditing the 119 `noqa` compat justifications. If pursued, treat as a single mechanical slice gated on version bump, not a silent s/ `timezone.utc`/ `UTC`/ .

**Why this order:** I001/UP035 have zero semantics risk and smallest per-file diffs → validate CI wiring first. UP007/UP045 share PEP604 semantics and are co-located in `alembic/` vs `app/db/models/` → batch after imports are settled. E501 is non-autofixable and high churn → defer until mechanical rules are out of the way to avoid merge conflicts. UP017 is intentionally retained for compat → last or never.

**Small-file clusters for extensive rules:**

- E501 extensive (212): cluster as above — `ingest.py` solo, then app-core, then scripts, then tests. Keeps each PR <80 changed lines of logic.
- UP045 extensive (212 in 9 files): slice hotspot files solo to keep review diffs <100 lines.
- I001 extensive (99 in 90 files): despite 90 files, each change is 1 hunk (import block) — safe to batch by directory, 15-35 files per PR, each PR <50 lines moved.

**Verification per slice:** `ruff check --select <RULE> --no-cache .` → 0, `ruff check .` → still passes for remaining ignores, `mypy app/ afk_outcomes/` → `Success`, `pytest -q` subset for touched area.

## 7. Gate verification (unchanged)

No `pyproject.toml` or suppression edits in this slice — only `docs/quality/ruff-baseline-814.md` added.

```
$ ruff check .
All checks passed!

$ mypy app/ afk_outcomes/
Success: no issues found in 88 source files

$ git diff --stat
 docs/quality/ruff-baseline-814.md | (new)
```

## 8. Reproducible review commands

> Run from repo root on `4cb263eb83dbbfef6d5e96b39aa716d8f5edf2c6`, Python 3.12, `ruff 0.15.16`. All six are one-liners; no config mutation.

```bash
# Baseline (must be green before inventory)
ruff check . && mypy app/ afk_outcomes/

# Per-rule isolated baselines (exact evidence; no pyproject edit)
ruff check --isolated --select I001  --line-length 100 --target-version py312 --no-cache . --statistics
ruff check --isolated --select UP035 --line-length 100 --target-version py312 --no-cache . --statistics
ruff check --isolated --select UP007 --line-length 100 --target-version py312 --no-cache . --statistics
ruff check --isolated --select UP045 --line-length 100 --target-version py312 --no-cache . --statistics
ruff check --isolated --select UP017 --line-length 100 --target-version py312 --no-cache . --statistics
ruff check --isolated --select E501  --line-length 100 --target-version py312 --no-cache . --statistics

# File-precise JSON (for full file lists; replace <RULE> as above)
ruff check --isolated --select <RULE> --line-length 100 --target-version py312 --no-cache . --output-format json | python3 -c "
import json,sys,collections
d=json.load(sys.stdin)
print(f'Total {len(d)} in {len(set(x[\"filename\"] for x in d))} files')
from collections import Counter
c=Counter(x['filename'] for x in d)
for f,n in c.most_common(): print(f'{n:3d} {f}')
"

# Quick per-rule counts (respects pyproject file discovery; equivalent for UP*/E501)
ruff check --select I001  --statistics --no-cache .
ruff check --select UP035 --statistics --no-cache .
ruff check --select UP007 --statistics --no-cache .
ruff check --select UP045 --statistics --no-cache .
ruff check --select UP017 --statistics --no-cache .
ruff check --select E501  --statistics --no-cache .

# Narrow noqa inventory (existing justified exceptions)
grep -rn "noqa:" --include="*.py" . | cut -d: -f2- | grep -o "noqa: [^ ]*" | sort | uniq -c | sort -rn
grep -rn "noqa: UP017\|noqa: UP007\|noqa: E501" --include="*.py" . | head -n 50

# Fix-preview (do NOT commit in this slice; slices above use per-rule --fix)
ruff check --isolated --select I001  --line-length 100 --target-version py312 --no-cache . --fix --diff | head -n 200
ruff check --isolated --select UP035 --line-length 100 --target-version py312 --no-cache . --fix --diff | head -n 200
```

**Refreshing the baseline:** before opening any implementation slice, re-run the six `--isolated --statistics` lines on the current `HEAD` and compare to this report; mypy cleanup (#787-#793) and new alembic migrations will shift counts.

---

*Report generated from disposable isolated configs; no `pyproject.toml` ignore was edited, no `afk` label added, no live operations performed. Evidence is local/tooling-only and reproducible via §8.*
