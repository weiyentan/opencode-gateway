# Mypy Diagnostic Waiver Inventory — Issue #815

> **Parent:** #806 — Audit and restore globally disabled mypy diagnostics after module cleanup  
> **Slice:** #815 — Measure global mypy diagnostic waivers against the Python 3.12 CI baseline (research/verification, no shipped config changes)  
> **Date:** 2026-10-29  
> **Commit SHA:** `4cb263eb83dbbfef6d5e96b39aa716d8f5edf2c6` (branch `tmp/issue-815-measure-global-mypy-diagnostic-waivers-against-python-3-12-ci`, merge-base `4cb263e` — post #787)  
> **Worktree:** `/tmp/opencode/workspace/weiyentan/opencode-gateway-12977/.worktrees/issue-815-measure-global-mypy-diagnostic-waivers-against-python-3-12-ci`

---

## 1. Baseline and Reproducibility

### 1.1 Python / Tool Versions (CI baseline)

| Tool | Version | Source |
|------|---------|--------|
| Python | 3.12.14 (CPython, Clang 22.1.3) | `python --version` on worktree / CI `setup-python@v5` with `python-version: "3.12"` |
| mypy | 1.19.1 (compiled: yes) | `mypy --version` |
| ruff | 0.15.16 | `ruff --version` |
| asyncpg | 0.31.0 | `requirements.txt` + `pip show asyncpg` |
| aiokafka | 0.12.0 | `requirements.txt` + `pip show aiokafka` |
| fastapi | 0.115.2 | `pip freeze` |
| pydantic | 2.13.4 | `pip freeze` |
| sqlalchemy[asyncio] | 2.x (via `requirements.txt`: `sqlalchemy[asyncio]>=2.0.0,<3.0.0`) |  |
| No type-stub packages installed | `pip freeze` shows no installed `asyncpg-stubs` or `types-*` packages | `pip show types-asyncpg` → not found (that package does not exist); `asyncpg-stubs` exists on PyPI but is **not installed** (see §3) |

Both `asyncpg` and `aiokafka` ship **no** `py.typed` marker, so mypy does not treat their bundled files as typed (asyncpg includes a few internal `.pyi` files, but no public `py.typed` marker) and reports `import-untyped` / “Skipping analyzing … missing library stubs” (`uv pip` / `pip show -f`).

### 1.2 Shipped mypy Configuration (unchanged in this slice)

`pyproject.toml` `[tool.mypy]` as checked in at `4cb263e`:

```toml
[tool.mypy]
python_version = "3.12"
strict = true
ignore_missing_imports = true
disable_error_code = ["type-arg", "unused-ignore"]

[[tool.mypy.overrides]]
module = [
  "app.core.reconciliation",
  "afk_outcomes.closure_episodes",
  "afk_outcomes.repository",
  "afk_outcomes.repository.crud",
  "afk_outcomes.repository.executions",
  "afk_outcomes.repository.lifecycle",
  "afk_outcomes.repository.closure_projection",
  "afk_outcomes.providers.gitlab",
  "app.api.usage",
  "app.api.ingest",
  "app.core.factory",
  "app.core.identity",
  "app.consumer.afk_consumer",
]
ignore_errors = true
```

- 13 `ignore_errors` modules remain after #787 (`app.core.logging` was removed from the list by `eab2096`). Pre-#787 the list had 14; issue #787 restored strict checking for logging/secret redaction.
- Global waivers: `ignore_missing_imports = true`, `disable_error_code = ["type-arg", "unused-ignore"]`.
- Shipped gate: `mypy app/ afk_outcomes/` and `ruff check .` are **green** (0 errors, see §5).

> **Constraint honored:** This inventory did **not** edit `pyproject.toml`. All measurements use disposable temporary mypy INIs that replicate the shipped `ignore_errors` overrides and lift each global waiver independently.

### 1.3 Precise Repeatable Commands

All runs target `mypy app/ afk_outcomes/` under the Python 3.12 CI dependency set (`pip install -r requirements.txt && pip install -e .` as in `.github/workflows/ci.yml`).

Helper — create a disposable INI that mirrors shipped overrides but lifts selected waivers. Example for lifting `unused-ignore` only:

```bash
# Worktree root
WORKDIR=$(pwd)

# Write a disposable INI that replicates shipped ignore_errors and lifts one waiver
cat > /tmp/mypy.tmp.ini <<'INI'
[mypy]
python_version = 3.12
strict = true
ignore_missing_imports = true
disable_error_code = type-arg

[mypy-app.core.reconciliation]
ignore_errors = true
[mypy-afk_outcomes.closure_episodes]
ignore_errors = true
[mypy-afk_outcomes.repository]
ignore_errors = true
[mypy-afk_outcomes.repository.crud]
ignore_errors = true
[mypy-afk_outcomes.repository.executions]
ignore_errors = true
[mypy-afk_outcomes.repository.lifecycle]
ignore_errors = true
[mypy-afk_outcomes.repository.closure_projection]
ignore_errors = true
[mypy-afk_outcomes.providers.gitlab]
ignore_errors = true
[mypy-app.api.usage]
ignore_errors = true
[mypy-app.api.ingest]
ignore_errors = true
[mypy-app.core.factory]
ignore_errors = true
[mypy-app.core.identity]
ignore_errors = true
[mypy-app.consumer.afk_consumer]
ignore_errors = true
INI

mypy --config-file /tmp/mypy.tmp.ini app/ afk_outcomes/
```

Independently lift each waiver (three isolated runs):

| Waiver lifted | Temp INI delta vs shipped | Command |
|---------------|---------------------------|---------|
| `unused-ignore` only | `disable_error_code = type-arg` (remove `unused-ignore`) + `ignore_missing_imports = true` | `mypy --config-file /tmp/mypy.tmp.ini app/ afk_outcomes/` |
| `type-arg` only | `disable_error_code = unused-ignore` (remove `type-arg`) + `ignore_missing_imports = true` | same with the other `disable_error_code` line |
| `ignore_missing_imports` only | `ignore_missing_imports = false` + `disable_error_code = type-arg, unused-ignore` | same with `ignore_missing_imports = false` |
| All three | `ignore_missing_imports = false` + no `disable_error_code` line | `disable_error_code` omitted |

For “measure again after #787–#793” projection, rerun the same four INIs **without** the 13 `[mypy-*] ignore_errors = true` sections (fully strict). The delta between with-overrides and without-overrides is the hidden debt in the active `ignore_errors` modules.

Shipped gate verification:

```bash
mypy app/ afk_outcomes/          # shipped config, should be: Success: no issues found in 88 source files
ruff check .                      # shipped config, should be: All checks passed!
python -m pytest -q -m "not integration"   # relevant unit tests green
```

All outputs below were produced at `4cb263e` with the disposable INIs above; raw logs are retained in `/tmp/mypy_*.txt` during reproduction.

---

## 2. Diagnostic Counts by Rule and Module

### 2.1 With current `ignore_errors` active (eligible strict modules only)

The 13 `ignore_errors` modules are **suppressed**; only the 75 strict-scoped source files (`88 total − 13 ignored`) are measured.

| Rule (error code) | Global waiver | Errors | Files affected | Notes |
|-------------------|---------------|--------|----------------|-------|
| `unused-ignore` | `disable_error_code = ["unused-ignore"]` | **5** | 4 files | All in eligible strict modules; 0 in `ignore_errors` modules |
| `type-arg` | `disable_error_code = ["type-arg"]` | **43** | 13 files | All in eligible strict modules |
| `import-untyped` (`ignore_missing_imports`) | `ignore_missing_imports = true` | **28** | 25 files | 24 `app/` strict modules + `scripts/afk_backfill.py` (followed via import); `asyncpg` 23 errors, `aiokafka` 5 errors |
| **All three lifted together** | `false` + no disables | **76** | 34 files | 5 + 43 + 28 = 76; no overlap beyond distinct codes; reproduced as `Found 76 errors in 34 files (checked 88 source files)` |

Shipped baseline with all waivers active: **0 errors** (88 files, strict, overrides active).

#### 2.1.1 `unused-ignore` — 5 errors in 4 strict modules

```
afk_outcomes/run_status.py:75   [unused-ignore]  # type: ignore[no-any-return] is now unused
afk_outcomes/run_status.py:83   [unused-ignore]
app/core/schemas/afk.py:279     [unused-ignore]  # type: ignore[assignment] on AFKRunSummary
app/consumer/consumer.py:187    [unused-ignore]  # type: ignore[union-attr] on __aiter__
app/core/envelope.py:80         [unused-ignore]  # type: ignore[union-attr] on body_iterator
```

All four modules are **eligible strict** (none in the 13 `ignore_errors` list; `app.consumer.consumer` is distinct from `app.consumer.afk_consumer` which is ignored).

#### 2.1.2 `type-arg` — 43 errors in 13 strict modules

| File | Count | Typical pattern |
|------|-------|-----------------|
| `app/db/models/projection.py` | 11 | `dict`/`dict` bare in ORM models |
| `app/db/models/afk.py` | 8 | `dict`, `list` bare |
| `app/core/schemas/execution.py` | 7 | `dict`/`list` bare in schemas |
| `app/api/afk_executions.py` | 5 | `auth: dict = Depends(...)` |
| `app/db/models/reporting.py` | 4 | `dict` bare |
| `app/api/afk_outcomes.py` | 1 | `list` bare |
| `app/api/afk_runs.py` | 1 | `dict` bare |
| `app/api/admin_clients.py` | 1 | `list` bare |
| `app/api/admin_quarantines.py` | 1 | `list` bare |
| `app/api/cursor.py` | 1 | `dict` bare |
| `app/api/health.py` | 1 | `dict[str, dict]` bare inner |
| `app/api/reporting_ingest.py` | 1 | `dict` bare |
| `app/core/schemas/reporting.py` | 1 | `dict` bare |

All 13 are eligible strict modules. No `type-arg` errors were observed in the 13 `ignore_errors` modules when overrides are active.

#### 2.1.3 `import-untyped` — 28 errors in 25 files

All `import-untyped` errors map to two external packages that lack stubs:

- `asyncpg` — 23 errors across 23 files (every direct `import asyncpg` in strict code, including `scripts/afk_backfill.py`, which mypy follows via imports)
- `aiokafka` — 5 errors across 2 files (`app/consumer/consumer.py` imports `aiokafka`, `aiokafka.errors`, `aiokafka.structs`; `app/consumer/dlq_sweep.py` imports `aiokafka`, `aiokafka.structs`)

Full file list (strict only):

```
app/db/lock.py
app/core/usage_dashboard_daily.py
app/core/afk_dashboard_daily.py
app/core/reporting_aggregates.py
app/consumer/dlq_sweep.py          (2: aiokafka, aiokafka.structs)
app/consumer/consumer.py           (3: aiokafka, aiokafka.errors, aiokafka.structs)
scripts/afk_backfill.py            (imported via analysis; not a target but reported)
app/db/setup.py
app/db/schema.py
app/db/session.py
app/core/auth.py
app/api/execution.py
app/api/closure_relationships.py
app/api/afk_outcomes.py
app/api/afk_dashboard_summary.py
app/api/admin_resolve_source_identity.py
app/api/admin_reconcile.py
app/api/admin_quarantines.py
app/api/admin_clients.py
app/api/admin_afk_reconcile.py
app/api/reporting_ingest.py
app/api/reporting.py
app/api/cursor.py
app/api/afk_executions.py
app/api/afk_runs.py
```

Of the 25 files, 24 are `app/` modules in the strict target set. `scripts/afk_backfill.py` appears because mypy follows imports from `app/` even though the target is `app/ afk_outcomes/`; its `asyncpg` error is included in the counts above, and it is **not** an `ignore_errors` module. It is a candidate for a scoped override if `scripts/` is later type-checked.

### 2.2 Distinguishing `ignore_errors` modules (#787–#793 series)

Current shipped `ignore_errors` (13 modules) and their status:

| Module | Still `ignore_errors = true` at `4cb263e` | Notes |
|--------|-------------------------------------------|-------|
| `app.core.reconciliation` | Yes | #787–#793 pending |
| `afk_outcomes.closure_episodes` | Yes | #787–#793 pending |
| `afk_outcomes.repository` | Yes | — |
| `afk_outcomes.repository.crud` | Yes | — |
| `afk_outcomes.repository.executions` | Yes | — |
| `afk_outcomes.repository.lifecycle` | Yes | — |
| `afk_outcomes.repository.closure_projection` | Yes | — |
| `afk_outcomes.providers.gitlab` | Yes | — |
| `app.api.usage` | Yes | — |
| `app.api.ingest` | Yes | — |
| `app.core.factory` | Yes | — |
| `app.core.identity` | Yes | — |
| `app.consumer.afk_consumer` | Yes | — |
| `app.core.logging` | **No** (removed by #787) | Demonstrates the cleanup pattern; now strict and green |

**Key finding:** When `ignore_errors` is active, **none** of the 76 waiver errors above fall inside those 13 modules — they are masked, but the strict surface is still sizable (76 waiver errors). The `ignore_errors` modules themselves hide significant non-waiver debt (see §2.3) plus additional waiver debt that will surface when they become strict.

### 2.3 Measure again after those issues merge (projection — without `ignore_errors`)

Rerunning the same four disposable INIs **without** any `[mypy-*] ignore_errors = true` sections (simulating post-#787–#793 fully-strict) yields:

| Run | Total mypy errors | Waiver-specific delta vs baseline |
|-----|-------------------|-----------------------------------|
| Shipped baseline (no waivers lifted, no `ignore_errors`) | **122** | 0 — this is the non-waiver strict debt hidden in the 13 modules |
| `unused-ignore` lifted, no `ignore_errors` | **131** | +9 `unused-ignore` total (5 strict + 4 hidden in `ignore_errors`) |
| `type-arg` lifted, no `ignore_errors` | **197** | +75 `type-arg` total (43 strict + 32 hidden) |
| `import-untyped` lifted, no `ignore_errors` | **161** | +39 `import-untyped` total (28 strict + 11 hidden) |
| All waivers lifted, no `ignore_errors` | **245** | 122 baseline + 123 waiver (76 strict + 47 hidden) |

**Hidden waiver debt that will surface when the 13 modules become strict (47 errors):**
- `unused-ignore` hidden: **4** (in `afk_outcomes/closure_episodes.py`, `app/api/ingest.py`, `app/api/usage.py`, `app/consumer/afk_consumer.py` etc. — see no-ignore logs)
- `type-arg` hidden: **32** (e.g., `afk_outcomes/repository/lifecycle.py`, `app/api/ingest.py`, `app/api/usage.py`, `afk_outcomes/repository/closure_projection.py`)
- `import-untyped` hidden: **11** (e.g., `app/core/identity.py:19`, `app/core/reconciliation.py:54`, `afk_outcomes/repository/*:15–17`, `app/api/ingest.py:18`, `app/api/usage.py:19`, `app/consumer/afk_consumer.py:45`)

**Total waiver liability after cleanup:** **123 errors** (76 strict + 47 hidden).  
**Total strict debt after cleanup with all waivers lifted:** **245 errors** (122 non-waiver + 123 waiver).

The 122 non-waiver baseline (without any waiver lifted, without `ignore_errors`) is dominated by `attr-defined` (79), `arg-type` (11), `no-any-return` (7), etc., concentrated in `afk_outcomes/repository/crud.py` (21), `afk_outcomes/repository/executions.py` (34), `afk_outcomes/repository/lifecycle.py` (20), `app/api/ingest.py` (14), `app/api/usage.py` (5), `app/core/identity.py` (3), `afk_outcomes/closure_episodes.py` (2), etc. These are the existing type errors tracked for #787–#793.

**Re-measure guidance:** After each of #787–#793 merges (each module removed from `ignore_errors`), rerun the same disposable INIs. The strict-only counts (§2.1) will grow by exactly the hidden deltas for that module. When all 13 are clean, the projected 123 waiver errors are the steady-state lift cost.

---

## 3. Per-External-Package Stub/Override Analysis

Two external packages trigger `import-untyped` when `ignore_missing_imports = false`. No other external imports (fastapi, pydantic, sqlalchemy, alembic, httpx, uvicorn, etc.) are missing stubs in the CI set — they either ship `py.typed` or have `types-*` in the environment.

| Package | Version | `py.typed`? | `types-*` on PyPI? | Upstream stubs? | Resolution without unsound `Any` | Recommended action |
|---------|---------|-------------|---------------------|-----------------|----------------------------------|--------------------|
| `asyncpg` | 0.31.0 | **No** (checked installed distribution, no `py.typed`) | `types-asyncpg` **does not exist**; **`asyncpg-stubs` exists** on PyPI — latest **0.32.0**, with the **0.31.x** line tracking `asyncpg` 0.31.x (e.g. `asyncpg-stubs==0.31.3` requires `asyncpg>=0.31,<0.32`) and declaring Python 3.12 support | **No upstream/typeshed stubs**; `asyncpg-stubs` is **third-party** (Development Status: **Beta**, GitHub `bryanforbes/asyncpg-stubs`) and a PEP 561 stub-only package | **Package stubs (preferred)**, **scoped override**, or **local stub**: install `asyncpg-stubs` pinned to a compatible 0.31.x release (trial result below); per-module `[[tool.mypy.overrides]]` with `ignore_missing_imports = true` for the asyncpg-importing modules, or a local `stubs/asyncpg/__init__.pyi` with `mypy_path`, remain fallbacks; contributing `py.typed` upstream is still ideal but out of scope for this slice. | **Adopt `asyncpg-stubs` (pinned compatible) as the package-stub path; the global `ignore_missing_imports` hold can be reconsidered for `asyncpg` once the stub-induced call-site errors are fixed (see trial below).** Short-term, keep global `true`; when lifting, install `asyncpg-stubs>=0.31,<0.32`, fix the surfaced call-site errors, and keep scoped overrides/local stubs only for `aiokafka` (no stub package exists). |
| `aiokafka` (+ `aiokafka.errors`, `aiokafka.structs`) | 0.12.0 | **No** (`aiokafka` distribution has no `py.typed`, no `.pyi`) | `types-aiokafka` **does not exist**; `aiokafka-stubs` **does not exist** either (both PyPI lookups → 404) | **No** | **Scoped override** or **local stubs**: same as `asyncpg`, but no package-stub option exists for `aiokafka`. Because `aiokafka` is used in only 2 strict files + 1 ignored file, a scoped override is low-noise. | Keep global `ignore_missing_imports = true` until local stubs (or a scoped per-importer override) cover `aiokafka`; this is the only remaining blocker after `asyncpg-stubs` is adopted. |

**`asyncpg-stubs` trial (reproduced):** Extracting `asyncpg-stubs==0.31.3` and adding it to mypy's search path (`pip install --no-deps --target /tmp/asyncpg_stubs_0313 asyncpg-stubs==0.31.3`; `PYTHONPATH=/tmp/asyncpg_stubs_0313 mypy --config-file /tmp/mypy.import.ini app/ afk_outcomes/`) removes all **23** `asyncpg` `import-untyped` errors (28 errors in 25 files → 19 errors in 7 files). The 5 `aiokafka` errors remain, and **14 stricter-typing errors** surface at `asyncpg` call sites: `create_pool` keyword overloads (`app/db/session.py`), `PoolConnectionProxy` vs `Connection` (`app/db/session.py`, `scripts/afk_backfill.py`), and optional `Record` results from `fetchrow` (`app/api/admin_clients.py`, `app/api/admin_resolve_source_identity.py`, `app/api/afk_outcomes.py`). The package-stub path is therefore viable but not zero-cost; those call-site fixes belong with the waiver-lift PR.

**No `Any`-unsound workaround is needed:** `asyncpg` can be covered by the third-party **`asyncpg-stubs` package** (pinned to 0.31.x), and both packages can alternatively be handled with **scoped `ignore_missing_imports`** (per-module overrides) or **local stub files**; none of these introduces `Any` into the checked code. The remaining `aiokafka` gap has no package-stub option (`types-aiokafka` and `aiokafka-stubs` are both 404), so it needs local stubs or a scoped override. A global `ignore_missing_imports = false` without stubs/overrides would be correct but noisy (28 → 39 errors after cleanup), so the hold is intentional.

**Other packages:** Verified that `sqlalchemy[asyncio]`, `fastapi`, `pydantic`, `asyncio`, `typing`, etc., cause no `import-untyped` under `false` — their stubs or `py.typed` are present.

---

## 4. Proposed Sequencing and Follow-Up Thresholds

### 4.1 Sequencing (respects #787–#793 ordering)

The global waivers must **not** be lifted until the 13 `ignore_errors` modules are clean, otherwise waiver noise mixes with baseline debt. Proposed order:

1. **Complete #787–#793 first** — remove modules from `[[tool.mypy.overrides]] ignore_errors` one by one (as #787 did for `app.core.logging`). Each merge re-runs the disposable INIs; strict-only waiver counts will increment by ~47 hidden errors total, confirming the projection (§2.3). No shipped waiver changes in this phase.

2. **Then address `unused-ignore` (lowest cost, highest hygiene):** 5 strict + 4 hidden = **9 total** `unused-ignore` errors. Each is a dead `# type: ignore` comment. Fix by deleting the now-unneeded comment (4 in strict, 4 in ignored modules after they become strict). Small PR, no typing risk.

3. **Then `type-arg` (medium cost, cosmetic):** 43 strict + 32 hidden = **75 total** `type-arg` errors. Each is a bare `dict`/`list` without `[...]` args. Fix by adding precise args where trivial (`dict[str, Any]`, `list[str]`, etc.) or `dict[str, object]` for mixed JSON; no logic change. Batched PR(s) scoped to `app/db/models/`, `app/core/schemas/`, `app/api/`.

4. **Finally `ignore_missing_imports` (requires stub provisioning):** 28 strict + 11 hidden = **39 total** `import-untyped` errors, all `asyncpg`/`aiokafka`. Do **not** flip global `false` until stubs are in place. For `asyncpg`, **`asyncpg-stubs` is a viable package-stub path**: pin the 0.31.x line (`asyncpg-stubs>=0.31,<0.32`) so it tracks `asyncpg` 0.31.0, install it, and fix the ~14 stricter-typing call-site errors it surfaces (§3); the global hold can then be reconsidered for `asyncpg`. For `aiokafka`, no stub package exists, so add minimal local stubs and/or a per-importer scoped override. Once both are covered, flip the global waiver. This avoids unsound `Any`.

This sequencing follows the parent #806 principle: *global diagnostics are restored only after module-scoped debt is gone*, and `import-untyped` is last because it requires stub provisioning (`asyncpg-stubs` for `asyncpg`, local stubs or scoped overrides for `aiokafka`).

### 4.2 Exact Follow-Up Thresholds (gates for future slices)

Each threshold is a **maximum** error count for the corresponding disposable INI run (with current `ignore_errors` still active unless noted). The gate fails if the count is exceeded.

| Slice | Disposable INI | Threshold (strict-only, with current `ignore_errors`) | Threshold (projected, without `ignore_errors` — after #787–#793) | Gate command |
|-------|----------------|--------------------------------------------------------|-------------------------------------------------------------------|--------------|
| `unused-ignore` | `ignore_missing_imports=true`, `disable_error_code=type-arg` (lift `unused-ignore`) | **≤5** `unused-ignore` errors (0 in `ignore_errors` modules) | **≤9** `unused-ignore` total | `mypy --config-file /tmp/mypy.unused.ini app/ afk_outcomes/ 2>&1 \| grep -c "\[unused-ignore\]"` should be 0 after the slice lands |
| `type-arg` | `ignore_missing_imports=true`, `disable_error_code=unused-ignore` | **≤43** `type-arg` errors | **≤75** `type-arg` total | `grep -c "\[type-arg\]"` → 0 after |
| `import-untyped` | `ignore_missing_imports=false`, `disable_error_code=type-arg, unused-ignore` | **≤28** `import-untyped` errors | **≤39** `import-untyped` total | `grep -c "\[import-untyped\]"` → 0 after global flip (or stubs) |
| All waivers together | `ignore_missing_imports=false`, no disables | **≤76** total waiver errors | **≤123** waiver total; **≤245** grand total (waiver + 122 baseline) | Combined `grep -c " error:"` |
| Baseline (no waivers lifted, strict) | `ignore_missing_imports=true`, `disable_error_code=type-arg, unused-ignore`, no `ignore_errors` | — | **≤122** non-waiver errors (the current hidden debt) | This is the #787–#793 burn-down gauge; should trend to 0 as modules land |

**Policy:**
- **Do not edit `pyproject.toml` waivers in this slice.** The thresholds above are *measurement* thresholds for disposable INIs.
- After #787–#793, each waiver-removal PR must patch `pyproject.toml` (remove the relevant `disable_error_code` entry or set `ignore_missing_imports = false`) **and** fix all corresponding errors in that run (counts → 0). Do not partially lift (e.g., removing one code while leaving others disabled) without a bounded PR.
- If a future measurement exceeds its threshold, the PR is out-of-bounds — split or hold.

### 4.3 Reviewable PR Evidence Checklist (for follow-up PRs)

Each follow-up PR that lifts a waiver should attach:
- Disposable INI used, `mypy --version`, commit SHA, and `pip freeze | grep -E "mypy|asyncpg|aiokafka"` output
- Before/after `mypy --config-file <tmp.ini> app/ afk_outcomes/` logs (strict-only)
- Confirmation that `mypy app/ afk_outcomes/` and `ruff check .` with **shipped** config are still green

---

## 5. Shipped Configuration Still Green

Verified at `4cb263e` with disposable tooling only (no `pyproject.toml` edits):

```bash
$ mypy app/ afk_outcomes/
Success: no issues found in 88 source files

$ ruff check .
All checks passed!

$ python -m pytest -q -m "not integration"  # relevant unit tests
# (run in CI; local sample:  collection succeeds, no regressions)
```

No runtime, schema, deployment, or CI gate changes were made in this slice. The only files created are under `docs/quality/` and disposable `/tmp/mypy.*.ini`.

---

## 6. Appendix — Raw Counts and Methodology Notes

- Mypy is invoked as `mypy --config-file <disposable INI> app/ afk_outcomes/` (88 files). `scripts/afk_backfill.py` appears in `import-untyped` results because mypy follows imports from `app/`; it is excluded from the `ignore_errors` scope but noted as a candidate for a future scoped override if `scripts/` is type-checked.
- Error code extraction: `grep "\[.*\]"` on `mypy` output; counts use `grep -c " error:"`.
- `ignore_errors` modules were verified by reading `pyproject.toml` at `4cb263e` and by the post-#787 delta (`app.core.logging` removed).
- Dependency metadata via `pip show` / `pip freeze` and `pyproject.toml` `requirements.txt` at the same SHA.
- All disposable configs were verified to reproduce the shipped baseline (0 errors) when they replicate `ignore_missing_imports=true`, `disable_error_code=type-arg, unused-ignore`, and the 13 overrides — proving the Δ is solely the lifted waiver.

---

*This report is the bounded evidence deliverable for #815. It proposes no runtime or CI gate changes and intentionally holds `ignore_missing_imports` until stub provisioning is in place (`asyncpg-stubs` exists and is viable; `aiokafka` still requires local stubs or a scoped override). See §4 for sequencing and exact gated thresholds.*
