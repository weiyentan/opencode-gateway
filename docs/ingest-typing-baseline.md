# Ingest typing baseline — strict-mypy inventory for `app.api.ingest`

> **Slice:** #824 (parent #790) — Baseline strict-mypy errors for ingestion and heartbeat contracts.  
> **Status:** Diagnostic inventory only — no runtime or exemption change. The shipped `pyproject.toml` `[[tool.mypy.overrides]]` entry for `app.api.ingest` remains `ignore_errors = true`; all counts below are from an **isolated temporary config** that disables only that one override.

## 1 — Reproducibility

| Field | Value |
|---|---|
| Commit SHA | `4cb263eb83dbbfef6d5e96b39aa716d8f5edf2c6` (worktree branch `tmp/issue-824-baseline-strict-mypy-errors` from `ai/feat/issues-824-829-830-831` @ `origin/master`) |
| Python | `3.12.14` (`/home/svc_admin_ansible/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/bin/python`, also `/home/svc_admin_ansible/.local/bin/python3.12`) |
| mypy | `1.20.2` via `uv run` (CI range `mypy>=1.13,<2.0`); system `mypy 2.4.0` produces identical 13-error output on this file |
| ruff | `0.17.3` (`ruff 0.15.16` also available system-wide) |
| Date | 2026-10-29 |
| Checked inputs | `app/api/ingest.py` strict, all other `ignore_errors` overrides retained |

### Isolated configuration (only `app.api.ingest` exemption disabled)

```ini
[mypy]
python_version = 3.12
strict = true
ignore_missing_imports = true
disable_error_code = type-arg, unused-ignore

[mypy-app.core.reconciliation]
ignore_errors = True
[mypy-afk_outcomes.closure_episodes]
ignore_errors = True
[mypy-afk_outcomes.repository]
ignore_errors = True
[mypy-afk_outcomes.repository.crud]
ignore_errors = True
[mypy-afk_outcomes.repository.executions]
ignore_errors = True
[mypy-afk_outcomes.repository.lifecycle]
ignore_errors = True
[mypy-afk_outcomes.repository.closure_projection]
ignore_errors = True
[mypy-afk_outcomes.providers.gitlab]
ignore_errors = True
[mypy-app.api.usage]
ignore_errors = True
[mypy-app.core.factory]
ignore_errors = True
[mypy-app.core.identity]
ignore_errors = True
[mypy-app.consumer.afk_consumer]
ignore_errors = True
# app.api.ingest intentionally NOT ignored
```

Saved for reproduction as `/tmp/mypy_isolated_ingest.ini` in the worktree session. Equivalent `pyproject.toml` delta is: copy `[[tool.mypy.overrides]]` and remove the `app.api.ingest` entry.

### Commands (reproduce)

```bash
# Shipped exemptions — must stay green
uv run python -m mypy app/ afk_outcomes/        # => Success: no issues found in 88 source files
uv run ruff check .
uv run python -m pytest tests/test_ingest.py -q # => 155 passed

# Isolated strict check (only ingest exemption lifted)
uv run python -m mypy --config-file /tmp/mypy_isolated_ingest.ini app/api/ingest.py
uv run python -m mypy --config-file /tmp/mypy_isolated_ingest.ini app/ afk_outcomes/
# Both => Found 13 errors in 1 file (checked 1 / 88 source files)
# Counts below exclude unrelated module waivers — only app.api.ingest is surfaced.
```

## 2 — Error inventory (13 errors, all in `app.api.ingest`)

| # | Line | Code | Behaviour bucket | Observable effect if mis-typed | Why bucket |
|---|---|---|---|---|---|
| 1 | 542 | `call-overload` | **enrichment** | `_apply_replay_merge` computes `cr_delta = int(fillable_columns["…"])` where the dict value is `object`; a wrong cast could silently apply 0/raise and skip the `sessions.total_cache_read_tokens` repair | Cache-token enrichment repair after fill-absent replay merge |
| 2 | 544 | `call-overload` | **enrichment** | Same as #1 for `cw_delta` / `total_cache_write_tokens` | Same |
| 3 | 637 | `no-any-return` | **first-write-wins persistence** | `_upsert_model` returns `row["id"]` typed `Any` from `asyncpg` — no validation, persistence or deduplication change; pure annotation gap on `UUID` return | Winner-path model upsert inside dedup transaction |
| 4 | 712 | `no-any-return` | **first-write-wins persistence** | `_resolve_session` returns `row["id"]` typed `Any` — same class as #3; session aggregate creation is unaffected at runtime | Session resolution / `INSERT … ON CONFLICT … RETURNING id` |
| 5 | 1517 | `assignment` | **enrichment** | `data = _serialize_jsonb(data)` narrows `str \| None` into `dict \| None` variable; heartbeat/validation unaffected, only execution-transcript `observed_messages.data` serialization | Observed message projection (enrichment/projection) |
| 6 | 1621 | `assignment` | **enrichment** | Same pattern for `observed_parts` / `observed_tool_calls` payload path | Observed part projection |
| 7 | 2210 | `no-redef` | **first-write-wins persistence** | `new_values` redefined in the same function scope across the cost-only vs general replay-merge branches; shadowed binding could mask the wrong delta set under `pg_advisory_xact_lock` | Canonical replay-merge delta construction (winner/loser discrimination) |
| 8 | 2268 | `dict-item` | **record validation / partial responses** | `result: dict[str, UUID \| None]` initializer puts `"status": outcome_str` (`str`) into a `UUID \| None` slot — response shaping typo; runtime still returns JSON `str` | Per-record `IngestRecordResult` assembly (`_record_canonical_event` return dict) |
| 9 | 2271 | `assignment` | **record validation / partial responses** | `result["reason"] = reason` assigns `str` into `UUID \| None` slot — same dict-typing bug as #8; partial-success `reason` string | Same |
| 10 | 2525 | `assignment` | **record validation / partial responses** | `result.status = canonical_status` where `canonical_status` is inferred `UUID` from the loosely-typed `canonical: dict[str, UUID \| None]` but `IngestResponse.status` is `str`; partial-success status mapping | Handler: cost-only divergence → canonical routing (partial-success path) |
| 11 | 2527 | `assignment` | **record validation / partial responses** | `result.reason = canonical["reason"]` `UUID \| None` → `str \| None` — same dict typing | Same |
| 12 | 2558 | `assignment` | **record validation / partial responses** | Same as #10 on the divergence-backfill branch | Handler: legacy fallback → canonical backfill (partial-success path) |
| 13 | 2560 | `assignment` | **record validation / partial responses** | Same as #11 on that branch | Same |

### Counts by affected ingestion behaviour

| Behaviour | Errors | Notes |
|---|---|---|
| **Collector authentication / heartbeat** | **0** | Empty-batch heartbeat (`POST /ingest` with `records: []`) and Two-Layer Auth (`require_collector_token` + `ApiKeyMiddleware`) carry no strict errors. Verified by `TestEmptyBatchHeartbeat` and `TestUnauthenticated`. |
| **Record validation / partial responses** | **6** | #8–#13 — all on the loosely-typed `canonical` result dict and `IngestRecordResult` status/reason wiring; partial-success semantics (per-record `accepted/conflict/rejected/quarantined` + `reason`) is the surface at risk. |
| **First-write-wins persistence** | **3** | #3, #4, #7 — `UUID` return + `new_values` redefinition in the atomic dedup / advisory-lock replay path; directly guards `usage_events` winner selection and session aggregate increments, not a new contract. |
| **Enrichment** | **4** | #1, #2, #5, #6 — cache-token delta casts + JSONB serialization narrowing on `observed_messages`/`observed_parts` projections; touches `fill-absent COALESCE` and `sessions.total_cache_*_tokens` repair, not base token totals. |
| **Total** | **13** | Excludes all unrelated waivers (`app.core.reconciliation`, `afk_outcomes.*`, `app.api.usage`, `app.core.factory`, `app.core.identity`, `app.consumer.afk_consumer` remain `ignore_errors`). |

Unrelated module waivers are **not** counted — the shipped `pyproject.toml` lists 13 ignored modules; only `app.api.ingest` was lifted for this inventory.

## 3 — Regression validation against existing fixtures

### Suite run

```
uv run python -m pytest tests/test_ingest.py -q  # 155 passed
uv run python -m mypy app/ afk_outcomes/          # Success: no issues found in 88 source files
uv run ruff check .                               # All checks passed!
```

No production DB/K8s access, no schema migration, no runtime adjustment — all I/O is mocked via `AsyncMock` `fetchrow`/`execute` side-effect sequences (see `tests/test_ingest.py: _new_record_side_effect`, `_handler_routing_side_effect_items`).

### Behaviour → tests mapping

| Behaviour | Representative test classes / cases (`tests/test_ingest.py` unless noted) | What they prove | Result |
|---|---|---|---|
| **Collector heartbeat** (empty `records: []` → 200, `accepted_count=0`, `rejected_count=0`, batch + `source_databases.last_seen_at` still written) | `TestEmptyBatchHeartbeat::test_empty_batch_heartbeat`, `TestProjectionBackwardCompat::test_empty_batch_with_no_projections_returns_200`, `TestSourceDatabaseUpsert::*`, `TestHealthExtended::test_health_includes_collectors_and_source_dbs` (collector `last_heartbeat` via ingest activity) | Heartbeat is a valid liveness signal distinct from data-bearing ingest; `last_heartbeat` surfaced on `GET /health` client-level `Remote Collector Health` row | ✅ 155-pass suite covers |
| **Collector authentication** (Two-Layer Auth, `ApiKeyMiddleware` + `require_collector_token`) | `TestUnauthenticated::test_no_auth_header_returns_401`, `TestUnauthenticated::test_invalid_token_returns_401` | Ingest rejects without collector credential; credential identity is not the monitored collector health identity (coverage via heartbeat tests) | ✅ |
| **Duplicate / replay — first-write-wins (idempotent accept)** | `TestDuplicateBatchIdempotent::test_duplicate_batch_accepted_idempotently`, `TestDuplicateBatchIdempotent::test_v12_duplicate_uses_effective_cached_tokens`, `TestIdempotencyWithSessionResolution`, `TestConcurrentIdenticalRecords`, `TestF2WinnerTransactionAtomicity`, `TestCanonicalDuplicateDetection` (qua `TestDuplicateBatchIdempotent` mirror), `TestReplayMergeFillAbsent`, `TestReplayMergeNoOverwrite`, `TestReplayMergeWhitespaceNormalization` | Atomic `INSERT … ON CONFLICT … DO NOTHING RETURNING id` inside `async with conn.transaction()` decides winner; identical duplicate returns `accepted` with optional `enrichment applied`, no double-count of session aggregates; enrichment fill is `FOR UPDATE` + transaction-scoped | ✅ |
| **Divergent duplicate → conflict** | `TestDivergentDuplicate::test_divergent_duplicate_returns_conflict`, `TestCostDedupExactEquality::*` (exact `Decimal.normalize()` equality), `TestReplayMergeNoOverwrite` | Token/cost mismatch (including sub-`$0.0001` deltas) → `conflict` with `Divergent duplicate` reason; cost-only divergence carries `COST_ONLY_DIVERGENCE_REASON` for canonical routing | ✅ |
| **Partial-success** (batch `200` with per-record `accepted/conflict/rejected/quarantined`; one bad record never blocks others) | `TestMalformedRecord::test_pydantic_rejects_non_numeric_tokens`, `TestMalformedRecord::test_negative_tokens_rejected`, `TestProjectionPartialFailure::test_usage_records_accepted_when_projections_fail`, `TestProjectionPartialFailure::test_some_projections_fail_others_succeed`, `TestProjectionIndependentPaths`, `TestC6PerRecordExceptionHandling`, `TestQuarantinedIdentity::*`, `TestC3WhitespaceDirectoryDedup`, `TestValidationDetailLogging::*` | Bad `IngestRecord` → per-record `rejected` with verbatim `Negative token value` / `Non-numeric…`; projection item failure → `projection_rejected_count` only; batch stays `200` so Usage Record Consumer commits Kafka offset; DLQ only on Pydantic/4xx | ✅ |
| **Enrichment (fill-absent COALESCE, whitespace normalize, cache repair)** | `TestReplayMergeFillAbsent`, `TestReplayMergeNoOverwrite`, `TestReplayMergeWhitespaceNormalization`, `TestReplayEnrichmentSessionAggregateRepair`, `TestSessionCacheTokensUpdate::*`, `TestCostOnlyCanonicalRouting::*` (`tests/test_replay_cost_regression.py`), `Test380SessionIdNullFallback` | `provider/mode/finish_reason` are non-erasing fill-absent; `reasoning_tokens/cache_*` backfill repairs `sessions.total_cache_*` once; `estimated_cost_usd` goes via canonical cost-only path, not legacy overwrite | ✅ |

### Unsafe or uncovered changes — explicit identification

| Change | Verdict | Reason |
|---|---|---|
| Narrowing `result: dict[str, UUID \| None]` to a `TypedDict` without touching `IngestResponse.status: str` | **Unsafe to do naïvely** | Errors #8–#13 stem from a single loosely-typed dict carrying both `UUID` (`event_id`/`attempt_id`) and `str` (`status`/`reason`). Fixing only one side (e.g. `result["status"] = outcome_str` assignment) without introducing `TypedDict`/`NotRequired` and updating all `canonical.get(...)` consumers reintroduces a different strict error. Must be one atomic typing slice. |
| Adding `int()` narrowing for `fillable_columns["…"]` with `isinstance` guard | **Safe in isolation** but must preserve `0` vs `None` semantics | Cache-token repair intentionally treats `0` as valid (ADR 0011) — a guard that conflates falsy `0` with missing would suppress a legitimate repair. Any guard must be `is not None` + `int()`; regression `TestReplayEnrichmentSessionAggregateRepair` proves this. |
| Annotating `_upsert_model` / `_resolve_session` `row["id"]` with `cast(UUID, …)` | **Safe** | No behavioural change — `asyncpg` returns `Any`; `cast` only satisfies `no-any-return`. Covered by `TestModelUpsert` / `TestResolveSessionCreatesNewRow` etc. |
| `_serialize_jsonb` retype on `data` | **Safe** | Separate local variable (`serialized = _serialize_jsonb(data)`) resolves the `str \| None` → `dict` mismatch without touching payload shape. Covered by `TestSessionTodoReplace` / transcript tests exercising `observed_messages`/`observed_parts` insert paths. |
| Renaming `new_values` second binding | **Safe if scoped** | `no-redef` at line 2210 is two `new_values: dict[str, object]` in distinct conditional branches but same function scope. Renaming or narrowing scope (extract branch to helper) is mechanical; risk is only shadowing, not persistence logic. Prove by running `TestCanonicalDuplicateDetection` + `TestCostOnlyCanonicalRouting`. |
| **Uncovered**: concurrent duplicate with advisory-lock interleaving + cost-only divergence in the **same** batch | **Gap — no single test exercises both in one batch** | Existing suites cover each path separately (`TestF2WinnerTransactionAtomicity` for concurrency, `TestCostOnlyCanonicalRouting` for cost-only). A combined bounded-window regression (two records in one batch, one cost-only divergence + one concurrent duplicate) is not present; adding it is out of scope for this baseline but noted as unsafe to refactor without. |
| **Uncovered**: heartbeat + projection arrays in one empty-records batch with a projection failure | **Minor gap** | `TestEmptyBatchHeartbeat` and `TestProjectionPartialFailure` are separate; the cross of “empty `records: []` + projection `failure`” has no explicit case. Behaviour is well-specified (batch row still written, counts stay `0/0`), but no test asserts both together. |

No existing test fails due to this baseline — the inventory is docs-only and the 155-case ingest suite passes both before and after.

## 4 — Bounded safe implementation order

Fixes are ordered so each step is **mechanically verifiable** and independently covered by the tests above; no step changes request validation, collector auth, empty-batch heartbeat, duplicate detection, replay-safe first-write-wins, partial-success responses, or status codes.

| Order | Slice | Fixes | Files touched | Proving tests (must stay green) |
|---|---|---|---|---|
| 1 | **Typed result dict** | Replace `result: dict[str, UUID \| None]` and the similar `canonical: dict[str, UUID \| None]` ad-hoc dict with a `TypedDict` (e.g. `CanonicalResult` with `event_id: UUID \| None`, `attempt_id: UUID \| None`, `status: str`, `reason: str \| None` via `NotRequired`) | `app/api/ingest.py` (one typing-only edit) | `TestDuplicateBatchIdempotent`, `TestDivergentDuplicate`, `TestQuarantinedIdentity`, `TestProjectionPartialFailure` (all read `response.json()["data"]["results"][*].status/reason`) |
| 2 | **Any-return narrowing** | `cast(UUID, row["id"])` or `row["id"]: UUID` annotation in `_upsert_model` and `_resolve_session` | `app/api/ingest.py` | `TestModelUpsert`, `TestResolveSessionCreatesNewRow`, `TestResolveSessionReturnsExisting`, `TestDifferentSourceDbSameExternalId` |
| 3 | **Payload serialization variable split** | `serialized = _serialize_jsonb(data)` then use `serialized` in the subsequent `INSERT` (two sites: observed messages / observed parts) | `app/api/ingest.py` | Transcript-adjacent tests exercising `_process_message` / `_process_part` plus `TestProjectionIndependentPaths` |
| 4 | **Cache-token delta typed guard** | `value = fillable_columns["cache_read_tokens"]; assert isinstance(value, int)` or `value is not None` guard before `int(value)` (replace `type: ignore[arg-type]` at lines 542/544) | `app/api/ingest.py` | `TestReplayEnrichmentSessionAggregateRepair`, `TestSessionCacheTokensUpdate`, `TestSessionCacheTokenBackfill` |
| 5 | **no-redef scope fix** | Rename second `new_values` or extract the `apply_replay_merge` branch into `_build_replay_merge_values(record, effective_cached_tokens)` | `app/api/ingest.py` | `TestCanonicalDuplicateDetection`, `TestCostOnlyCanonicalRouting`, `TestReplayMerge*` |

Each slice is additive and can be landed separately; none introduces new dependencies, new public API, or schema change. Total diff stays within a T2-appropriate 1-file, 5-slice sequence.

## 5 — When the shipped exemption can be removed

The `[[tool.mypy.overrides]]` entry for `app.api.ingest` can be deleted **only after**:

1. All five slices above land and `uv run python -m mypy --config-file /tmp/mypy_isolated_ingest.ini app/api/ingest.py` reports `Success: no issues`.
2. The same command against the full codebase (`app/ afk_outcomes/`) also reports success (the isolated config is exactly the shipped config minus one entry — equivalence is proven by step 3).
3. `uv run python -m pytest tests/test_ingest.py -q` (155 passed) plus `uv run ruff check .` remain green — the behavioural contract (Two-Layer Auth, empty-batch heartbeat, `first-write-wins`, partial-success) is unchanged.
4. No `type: ignore` is added to paper over errors — each error is resolved by narrowing or `TypedDict`/`cast`, not suppression.

Until then the exemption stays; this document is the gate.

## 6 — Validation (recorded runs)

```
# Shipped config — required CI gates
$ uv run python -m mypy app/ afk_outcomes/
Success: no issues found in 88 source files

$ uv run ruff check .
All checks passed!

$ uv run python -m pytest tests/test_ingest.py -q
155 passed in 11.08s

# Isolated strict check — diagnostic only (not a CI gate until exemption removal)
$ uv run python -m mypy --config-file /tmp/mypy_isolated_ingest.ini app/ afk_outcomes/
app/api/ingest.py:542: error: No overload variant of "int" matches argument type "object"  [call-overload]
...
Found 13 errors in 1 file (checked 88 source files)
```

## 7 — Raw isolated output (for audit)

```
app/api/ingest.py:542: error: No overload variant of "int" matches argument type "object"  [call-overload]
app/api/ingest.py:544: error: No overload variant of "int" matches argument type "object"  [call-overload]
app/api/ingest.py:637: error: Returning Any from function declared to return "UUID"  [no-any-return]
app/api/ingest.py:712: error: Returning Any from function declared to return "UUID"  [no-any-return]
app/api/ingest.py:1517: error: Incompatible types in assignment (expression has type "str | None", variable has type "dict[Any, Any] | None")  [assignment]
app/api/ingest.py:1621: error: Incompatible types in assignment (expression has type "str | None", variable has type "dict[Any, Any] | None")  [assignment]
app/api/ingest.py:2210: error: Name "new_values" already defined on line 2180  [no-redef]
app/api/ingest.py:2268: error: Dict entry 2 has incompatible type "str": "str"; expected "str": "UUID | None"  [dict-item]
app/api/ingest.py:2271: error: Incompatible types in assignment (expression has type "str", target has type "UUID | None")  [assignment]
app/api/ingest.py:2525: error: Incompatible types in assignment (expression has type "UUID", variable has type "str")  [assignment]
app/api/ingest.py:2527: error: Incompatible types in assignment (expression has type "UUID | None", variable has type "str | None")  [assignment]
app/api/ingest.py:2558: error: Incompatible types in assignment (expression has type "UUID", variable has type "str")  [assignment]
app/api/ingest.py:2560: error: Incompatible types in assignment (expression has type "UUID | None", variable has type "str | None")  [assignment]
Found 13 errors in 1 file (checked 1 source file)
```

---

*No production DB access, schema changes, or runtime adjustments were made to produce this baseline. No `pyproject.toml` exemption was lifted; no `app/api/ingest.py` contract was changed.*
