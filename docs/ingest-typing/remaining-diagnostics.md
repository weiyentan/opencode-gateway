# Ingest typing — remaining strict-mypy diagnostics after slice #829

> **Slice:** #829 (parent #790) — collector heartbeat, request validation and per-record ingest outcomes.
> **Baseline:** [`docs/ingest-typing-baseline.md`](../ingest-typing-baseline.md) (#824) — the 13-error inventory this slice starts from.
> **Status:** The shipped `[[tool.mypy.overrides]]` `ignore_errors = true` entry for `app.api.ingest` in `pyproject.toml` **stays in place**. This slice only eliminates the diagnostics it owns; the remaining seven are owned by later slices.

## 1 — What this slice typed

`app/api/ingest.py` previously described the return value of
`_record_canonical_event()` as `dict[str, uuid.UUID | None]`, although the dict
also carries text (`status`, `reason`). That single annotation produced all six
`dict-item`/`assignment` errors in the **record validation / partial responses**
bucket (#8–#13 of the baseline).

The slice replaces it with a `TypedDict`:

```python
class _CanonicalEventResult(TypedDict):
    event_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    status: NotRequired[str]
    reason: NotRequired[str | None]
```

`status`/`reason` remain optional because the early "model/session row missing"
returns carry only the identifiers — that runtime behaviour is unchanged. The
ingest handler's `auth` dependency parameter is also annotated precisely as
`dict[str, str]` (matching `require_collector_token`'s return type); this is an
annotation-only change and does not touch Two-Layer Auth or token handling.

No request/response model, status code, per-record error string, heartbeat
semantic, quarantine/conflict branch, `Any`, or `type: ignore` was added or
changed.

## 2 — Reproducing the isolated check

`mypy-strict-ingest.ini` at the repository root is exactly the shipped
`[tool.mypy]` configuration **minus** the `app.api.ingest` override. It is a
verification aid, not a CI gate. Keep it in sync with `pyproject.toml`.

```bash
python -m mypy --config-file mypy-strict-ingest.ini app/api/ingest.py
# expected after #829: Found 7 errors in 1 file (checked 1 source file)
```

The repository-wide gate still runs the shipped configuration:

```bash
python -m mypy app/ afk_outcomes/   # Success: no issues found
ruff check .
pytest tests/test_ingest.py tests/test_ingest_typing.py
```

`tests/test_ingest_typing.py` runs the isolated check and fails if any
diagnostic outside the documented later-slice allowlist below appears —
including a regression in collector auth, heartbeat, request validation or the
per-record result path.

## 3 — Diagnostics remaining after #829 (7)

Line numbers are from the post-#829 tree and shift as later slices edit the
module; the error code + message pair is the stable identity.

| # | Line (post-#829) | Code | Behaviour bucket | Owning slice |
|---|---|---|---|---|
| 1 | 561 | `call-overload` | Enrichment — cache-token delta cast (`int(object)`) | #831 |
| 2 | 563 | `call-overload` | Enrichment — cache-token delta cast (`int(object)`) | #831 |
| 3 | 656 | `no-any-return` | First-write-wins persistence — `_upsert_model` `row["id"]` (`Any` → `UUID`) | #830 |
| 4 | 731 | `no-any-return` | First-write-wins persistence — `_resolve_session` `row["id"]` (`Any` → `UUID`) | #830 |
| 5 | 1536 | `assignment` | Enrichment — observed-message JSONB serialization narrowing | #831 |
| 6 | 1640 | `assignment` | Enrichment — observed-part JSONB serialization narrowing | #831 |
| 7 | 2229 | `no-redef` | First-write-wins persistence — duplicate `new_values` binding in replay-merge branches | #830 |

Counts by bucket: **persistence 3** (#830) and **enrichment 4** (#831).
**Collector authentication / heartbeat / request validation / per-record
result: 0** — the path owned by #829.

## 4 — When the shipped exemption is removed

The `app.api.ingest` entry can only be deleted from `pyproject.toml` by the
final ingestion slice (#831), after:

1. This slice is merged and the isolated check reports only the seven
   diagnostics above.
2. #830 and #831 land, and the isolated check reports `Success: no issues`.
3. `mypy app/ afk_outcomes/`, `ruff check .`, and the ingest test suites stay
   green **without** new ignores, broad `Any`, or module-wide suppressions.
4. No `type: ignore` is introduced to paper over a diagnostic — each error is
   resolved by narrowing, `TypedDict`, or `cast`.

Until then the exemption stays: the shipped CI gate is green while the
isolated check keeps the typed slice independently verifiable.

## 5 — Behavioural regression coverage (reused)

No new endpoint test was required — the four required scenarios already exist
and are re-run by this slice's validation:

| Scenario | Tests (`tests/test_ingest.py`) |
|---|---|
| Authorised vs rejected collectors | `TestUnauthenticated::test_no_auth_header_returns_401`, `TestUnauthenticated::test_invalid_token_returns_401` |
| Empty-batch heartbeat | `TestEmptyBatchHeartbeat::test_empty_batch_heartbeat`, `TestProjectionBackwardCompat::test_empty_batch_with_no_projections_returns_200` |
| Partial success (mixed valid/invalid) | `TestMalformedRecord`, `TestProjectionPartialFailure`, `TestC6PerRecordExceptionHandling` |
| Conflicts / first-write-wins | `TestDivergentDuplicate`, `TestCanonicalDuplicateDetection`, `TestReplayMergeNoOverwrite` |
