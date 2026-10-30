# Ingest typing — remaining strict-mypy diagnostics after slice #831 (gate restored)

> **Slice:** #831 (parent #790) — nested enrichment typing (session context / project / directory / todo / message / part JSON + canonical text enrichment) and full ingestion gate restoration.
> **Baseline:** [`docs/ingest-typing-baseline.md`](../ingest-typing-baseline.md) (#824) — the 13-error inventory.
> **Previous slice:** #830 eliminated persistence diagnostics, leaving 4 enrichment.
> **Status:** The shipped `[[tool.mypy.overrides]]` `ignore_errors = true` entry for `app.api.ingest` in `pyproject.toml` **has been removed** (12 entries remain). The entire ingestion module is now strictly checked by the mandatory CI gate.

## 1 — What this slice typed

Four enrichment diagnostics from the isolated check:

* `call-overload` on `int(fillable_columns["cache_read_tokens"])` / `int(fillable_columns["cache_write_tokens"])` (`dict[str, object]` → `int`): replaced `int(object)` with a typed `isinstance` guard (`if isinstance(v, int): cr_delta = v; elif isinstance(v, str): cr_delta = int(v)`) preserving `0` as valid, no `type: ignore`, no behavioural change to `sessions.total_cache_*_tokens` repair delta handling. Zero deltas remain no-ops; `>0` still increments aggregates.
* `assignment` on `_serialize_jsonb(data)` reusing the same `data` name for `dict|None` then `str|None` (two sites: observed_messages / observed_parts): split into distinct bindings (`redacted_msg_data` → `data_json: str | None` and `redacted_part_data` → `part_data_json: str | None`), both threaded to the subsequent `INSERT` and tool-call `INSERT`. Redaction (`_redact_and_truncate_payload`) and bounded-payload truncation semantics are unchanged.

No new `type: ignore`, broad `Any`, module-wide exclusion, or schema migration. The `type: ignore[arg-type]` comments on the `int(object)` lines were removed.

## 2 — Reproducing the check

The isolated config `mypy-strict-ingest.ini` is now equivalent to the shipped configuration — both omit `app.api.ingest` from the override list (12 entries remain: `app.core.reconciliation`, `afk_outcomes.*` (6), `app.api.usage`, `app.core.factory`, `app.core.identity`, `app.consumer.afk_consumer`). Either command must report success:

```bash
# Shipped gate (mandatory CI)
python -m mypy app/ afk_outcomes/   # Success: no issues found in 88 source files
# Isolated verification (now 0 errors, gate restored)
python -m mypy --config-file mypy-strict-ingest.ini app/api/ingest.py
# => Success: no issues found in 1 source file
python -m mypy --config-file mypy-strict-ingest.ini app/ afk_outcomes/
# => Success: no issues found in 88 source files
ruff check .
pytest tests/test_ingest.py tests/test_ingest_typing.py
```

`tests/test_ingest_typing.py` now asserts **0** isolated errors — `DOCUMENTED_REMAINING` is empty. Any new diagnostic, including enrichment, persistence, or collector/validation, fails the test. The shipped gate itself also rejects synthetic errors:

```bash
# Proof the gate genuinely checks the module (e.g. inject `_x: int = "bad"` into app/api/ingest.py)
python -m mypy app/ afk_outcomes/   # => error: Incompatible types (assignment)  -- gate is live
```

## 3 — Diagnostics remaining after #831 (0)

| # | Code | Behaviour bucket | Owning slice |
|---|---|---|---|
| — | — | — | — |

Counts by bucket: **collector authentication / heartbeat / request validation / per-record result 0** (#829), **first-write-wins persistence 0** (#830), **enrichment 0** (#831). The 13-error baseline is fully eliminated.

## 4 — When the shipped exemption was removed (satisfied)

The `app.api.ingest` entry was deleted from `pyproject.toml` only after:

1. #829, #830 and the four enrichment diagnostics were isolated (4 remaining).
2. #831 landed and the isolated check reports `Success: no issues`.
3. `mypy app/ afk_outcomes/`, `ruff check .`, and the ingest test suites stay green without new ignores, broad `Any`, or module-wide suppressions.
4. No `type: ignore` was introduced — each error was resolved by narrowing or serialization split.

The exemption stayed until this slice; the shipped CI gate was green throughout the sequence while the isolated check kept each slice independently verifiable. The override list retains exactly the 12 other entries, coordinated with #788-793 and #821-823 (no concurrent stale-list development — this slice rebased onto `ai/feat/issues-824-829-830-831` @ `0b270b2`).

## 5 — Behavioural regression coverage (reused)

No new endpoint test was required — enrichment and replay correctness are covered by the existing ingest suite and the disposable-Postgres replay regression; the five required scenarios already exist and are re-run by this slice's validation:

| Scenario | Tests (`tests/test_ingest.py`) |
|---|---|
| Authorised vs rejected collectors | `TestUnauthenticated::test_no_auth_header_returns_401`, `TestUnauthenticated::test_invalid_token_returns_401` |
| Empty-batch heartbeat | `TestEmptyBatchHeartbeat::test_empty_batch_heartbeat`, `TestProjectionBackwardCompat::test_empty_batch_with_no_projections_returns_200` |
| Partial success (mixed valid/invalid) | `TestMalformedRecord`, `TestProjectionPartialFailure`, `TestC6PerRecordExceptionHandling` |
| Duplicate / replay — first-write-wins | `TestDuplicateBatchIdempotent`, `TestCanonicalDuplicateDetection`, `TestF2WinnerTransactionAtomicity`, `TestReplayMergeFillAbsent`, `TestReplayMergeNoOverwrite` |
| Nested enrichment (session contexts / project_directories / session_todos / messages / parts with bounded payloads + redaction) | `TestSessionTodoReplace`, transcript-adjacent `_process_message`/`_process_part` paths, `TestProjectionIndependentPaths`, `TestProjectionPartialFailure` (projections produce same facts/linkage; bounded payload + redaction preserved; replay/mixed partial-success API status unchanged) |
| Rollup / dual-write compat | `tests/test_client_project_rollup_ingest.py`, `TestClientProjectRollup*` (rollup delta-adjusted, clamped) |
