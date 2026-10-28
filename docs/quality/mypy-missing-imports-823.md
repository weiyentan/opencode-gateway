# Mypy missing-import baseline — issue #823

Parent: #806 — global mypy waiver audit  
Slice: #823 — `ignore_missing_imports` (parallel with #821/#822)  
Scope: `mypy app/ afk_outcomes/` (88 source files)  
Date: 2026-10-29  
Worktree: `tmp/issue-823-replace-missing-import-suppression`

## Baseline (matches #815 reproduction method)

Recorded on the runner used for this slice (CI pins Python 3.12; local runner is 3.9.25 but mypy `python_version = 3.12`):

```
python --version            # 3.9.25 (runner) — mypy python_version = 3.12 (pinned)
python -m mypy --version    # mypy 2.4.0 (compiled: yes)  [pip show mypy 1.19.1 in same env]
pip show asyncpg            # 0.31.0 — no py.typed, no types-asyncpg on PyPI
pip show aiokafka            # 0.12.0 — no py.typed, no types-aiokafka on PyPI
pip show fastapi             # 0.115.2 — ships py.typed
pip show pydantic            # 2.13.4 — ships py.typed
pip show sqlalchemy          # 2.0.x via installed deps — ships py.typed
pip show httpx               # 0.28.1 — ships py.typed
pip show starlette           # 0.38.6 — ships py.typed
pip show uvicorn             # 0.39.0 — ships py.typed
pip show pydantic-settings   # 2.11.0 — ships py.typed
pip show alembic             # 1.16.5 — ships py.typed
```

### Reproduction commands (exact)

```bash
# 1. Baseline diagnostic enumeration — temporary lift of global suppression
#    In pyproject.toml set [tool.mypy] ignore_missing_imports = false
#    then run:
python -m mypy app afk_outcomes --show-error-codes 2>&1 | tee /tmp/mypy-823-baseline.log

# Expected baseline diagnostics (28 errors in 25 files before fix):
#   - asyncpg:  Cannot find implementation or library stub  [import-not-found]  (22 sites)
#   - aiokafka: Skipping analyzing "aiokafka" / "aiokafka.errors" / "aiokafka.structs"
#               [import-untyped] (6 sites, 2 files)
# No other third-party import produces a missing-import diagnostic.
# All other deps (fastapi, pydantic, sqlalchemy, httpx, starlette, uvicorn,
# pydantic-settings, alembic, prometheus-fastapi-instrumentator) ship py.typed.

# 2. After targeted handling — verify green
python -m mypy app afk_outcomes --show-error-codes
# => Success: no issues found in 88 source files

# 3. Ruff gate
ruff check .

# 4. Unit / contract tests (no DB)
pytest -q --ignore=tests/integration
```

## Inventory — every third-party import in `app/` `afk_outcomes/`

| Package | Import sites | Inline typing / stubs | Mypy diagnostic when global lifteds | Decision |
|---------|--------------|----------------------|-------------------------------------|----------|
| `asyncpg` | 22 × `import asyncpg` (app/db/*, app/api/*, app/core/*, afk_outcomes/repository/*, scripts/afk_backfill) | No `py.typed`, no `types-asyncpg` on PyPI (pip index negative) | `[import-not-found]` | **Justified per-package `ignore_missing_imports = true`** — genuinely untyped C-extension driver. No upstream stubs at baseline; narrow adapter/protocol would be a cross-cutting runtime refactor out of scope for this config slice. Treated as typed-boundary: `asyncpg` is used only behind `app.db.session` pool helpers and `afk_outcomes.repository` data-access layer; call sites treat connections as DB-boundary, not business-logic types. Owner: DB/infra. |
| `aiokafka` | 6 × `aiokafka`, `aiokafka.errors`, `aiokafka.structs` (app/consumer/*) | No `py.typed`, no `types-aiokafka` on PyPI | `[import-untyped]` | **Justified per-package `ignore_missing_imports = true`** — genuinely untyped Kafka client. Same rationale as asyncpg; consumer bridge is isolated to `app/consumer/*` and typed at the `ConsumerRecord` boundary via local narrowing `Any -> typed` only where needed. Owner: platform/consumer. |
| `fastapi` | ~15 × `fastapi` | `py.typed` present | none | Fully checked — no override |
| `pydantic` (+ `pydantic-settings`) | ~20 × `pydantic*` | `py.typed` present | none | Fully checked |
| `sqlalchemy` | 6 × `sqlalchemy*` | `py.typed` present | none | Fully checked |
| `httpx` | 4 × `httpx` | `py.typed` present | none | Fully checked |
| `starlette` | 4 × `starlette.*` | `py.typed` via starlette | none | Fully checked |
| `uvicorn` | 1 × `uvicorn` | `py.typed` present | none | Fully checked |
| `alembic` | via `app/db/setup.py` lazy import only | `py.typed` + `.pyi` present | not in `app/` checked slice as Alembic code is excluded (`alembic/**/*` forbidden path) | Not in scope |
| Others (stdlib, `afk_outcomes.*` internal) | — | — | none | — |

## Chosen handling

**No blanket `Any`, no `follow_imports=skip`, no `ignore_errors`, no new global waivers.**

- Removed `ignore_missing_imports = true` from `[tool.mypy]` (global suppression).
- Added two *narrow* `[[tool.mypy.overrides]]` sections — one per genuinely untyped dep —
  covering the package and its submodules (`asyncpg.*`, `aiokafka.*`) with
  `ignore_missing_imports = true` and inline justification comments in `pyproject.toml`.
- All other imports remain strictly checked (`strict = true`, `python_version = 3.12`);
  `disable_error_code = ["type-arg", "unused-ignore"]` unchanged (owned by #821/#822).

## HITL / unresolved decisions

None — both untyped packages are justified per-package overrides with evidence
(no stubs on PyPI, no `py.typed`, stable at baseline). No contract or upgrade
decision required; no `Any` injection. If a future baseline upgrades either
package to a typed release, remove its override and re-run the reproduction
commands above. If stubs ever appear (`types-asyncpg` / `types-aiokafka`),
prefer installing the stub over the override.

Coordination: this slice touches `pyproject.toml` [tool.mypy] — sibling slices
#821 and #822 touch `disable_error_code` and `ignore_errors` overrides on the
same file. Coordinator merges via `git merge`; this slice adds only the two
`ignore_missing_imports` override blocks and removes the global line, so
conflicts are limited to the `[tool.mypy]` header comments.
