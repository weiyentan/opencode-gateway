# Ingest typing — remaining strict-mypy diagnostics after slice #830

> **Slice:** #830 (parent #790) — replay-safe persistence (canonical event, replay-merge, session/model resolution).
> **Baseline:** [`docs/ingest-typing-baseline.md`](../ingest-typing-baseline.md) (#824) — the 13-error inventory this slice starts from.
> **Previous slice:** #829 eliminated the 6 record-validation / partial-response diagnostics, leaving 7.
> **Status:** The shipped `[[tool.mypy.overrides]]` `ignore_errors = true` entry for `app.api.ingest` in `pyproject.toml` **stays in place**. This slice eliminates the 3 persistence diagnostics it owns; the remaining 4 enrichment diagnostics are owned by #831.

## 1 — What this slice typed

Three persistence diagnostics from the isolated check:

* `no-any-return` on `_upsert_model` (`row["id"]` from `asyncpg.fetchrow` typed `Any` → `UUID`): fixed with `cast(uuid.UUID, row["id"])` — annotation-only, no behavioural change to model upsert / first-write-wins winner path.
* `no-any-return` on `_resolve_session` (`row["id"]` from `INSERT … ON CONFLICT … RETURNING id`): same `cast` fix — annotation-only, session aggregate `INSERT … ON CONFLICT … DO UPDATE … RETURNING` contract unchanged.
* `no-redef` on duplicate `new_values` binding in the canonical replay-merge branches (cost-only vs general): the first binding inside the cost-only branch was renamed to `cost_only_values` so the general branch's `new_values` no longer shadows it in the same function scope. Logic under `pg_advisory_xact_lock` is identical; only the binding name changes.

Required imports added: `from typing import cast` (alongside existing `TypedDict`). No new `type: ignore`, broad `Any`, module-wide exclusion, or schema migration.

## 2 — Reproducing the isolated check

`mypy-strict-ingest.ini` at the repository root is exactly the shipped
`[tool.mypy]` configuration **minus** the `app.api.ingest` override. It is a
verification aid, not a CI gate. Keep it in sync with `pyproject.toml`.

```bash
python -m mypy --config-file mypy-strict-ingest.ini app/api/ingest.py
# expected after #830: Found 4 errors in 1 file (checked 1 source file)
```

The repository-wide gate still runs the shipped configuration:

```bash
python -m mypy app/ afk_outcomes/   # Success: no issues found
ruff check .
pytest tests/test_ingest.py tests/test_ingest_typing.py
```

`tests/test_ingest_typing.py` runs the isolated check and fails if any
diagnostic outside the documented #831 allowlist below appears —
including a regression in collector auth, heartbeat, request validation,
per-record result, or the persistence path just completed.

## 3 — Diagnostics remaining after #830 (4 — enrichment only)

Line numbers are from the post-#830 tree and shift as later slices edit the
module; the error code + message pair is the stable identity.

| # | Line (post-#830) | Code | Behaviour bucket | Owning slice |
|---|---|---|---|---|
| 1 | 566 | `call-overload` | Enrichment — cache-token delta cast (`int(object)`) | #831 |
| 2 | 568 | `call-overload` | Enrichment — cache-token delta cast (`int(object)`) | #831 |
| 3 | 1541 | `assignment` | Enrichment — observed-message JSONB serialization narrowing | #831 |
| 4 | 1645 | `assignment` | Enrichment — observed-part JSONB serialization narrowing | #831 |

Counts by bucket: **persistence 0** (fixed by #830) and **enrichment 4** (#831).
**Collector authentication / heartbeat / request validation / per-record
result: 0** — the path owned by #829. **First-write-wins persistence: 0** —
the path owned by #830.

## 4 — When the shipped exemption is removed

The `app.api.ingest` entry can only be deleted from `pyproject.toml` by the
final ingestion slice (#831), after:

1. #829 and this slice are merged and the isolated check reports only the four
   enrichment diagnostics above.
2. #831 lands, and the isolated check reports `Success: no issues`.
3. `mypy app/ afk_outcomes/`, `ruff check .`, and the ingest test suites stay
   green **without** new ignores, broad `Any`, or module-wide suppressions.
4. No `type: ignore` is introduced to paper over a diagnostic — each error is
   resolved by narrowing, `TypedDict`, or `cast`.

Until then the exemption stays: the shipped CI gate is green while the
isolated check keeps the typed slice independently verifiable.

## 5 — Behavioural regression coverage (reused)

No new endpoint test was required — persistence is covered by the existing
ingest suite and the disposable-Postgres replay regression; the five required
scenarios already exist and are re-run by this slice's validation:

| Scenario | Tests (`tests/test_ingest.py`) |
|---|---|
| Authorised vs rejected collectors | `TestUnauthenticated::test_no_auth_header_returns_401`, `TestUnauthenticated::test_invalid_token_returns_401` |
| Empty-batch heartbeat | `TestEmptyBatchHeartbeat::test_empty_batch_heartbeat`, `TestProjectionBackwardCompat::test_empty_batch_with_no_projections_returns_200` |
| Partial success (mixed valid/invalid) | `TestMalformedRecord`, `TestProjectionPartialFailure`, `TestC6PerRecordExceptionHandling` |
| Duplicate / replay — first-write-wins | `TestDuplicateBatchIdempotent`, `TestCanonicalDuplicateDetection`, `TestF2WinnerTransactionAtomicity`, `TestReplayMergeFillAbsent`, `TestReplayMergeNoOverwrite` |
| Rollup / dual-write compat | `tests/test_client_project_rollup_ingest.py`, `TestClientProjectRollup*` (rollup delta-adjusted, clamped) |
