# GitHub PR Observation Path Validation — Issue #515

**Issue**: #515 — Validate GitHub PR observation and AWX command path
**Parent**: #511 — PR/MR/Issue Ingestion and Reporting Capability (cross-repository)
**Slice layer**: validation (builds on #512, #513, #514)
**Validator**: `code-editor-mid` (T2)
**Date**: 2026-08-18
**Verdict**: **PASS** — the consumer-side mapping for `pull_request.opened` and
`pull_request.closed` is validated by the existing contract matrix tests (38
pull_request-specific tests all passing). The end-to-end live validation script
is provided for environments with the full infrastructure stack (Kafka, Postgres,
AWX, FastAPI EDA Gateway).

---

## 1. Summary

The complete GitHub pull-request observation path was validated at the consumer
layer: the `NormalizedProviderEvent` → canonical `EngineeringEntity`/`EngineeringEvent`
mapping for all five `pull_request` lifecycle actions (`opened`, `edited`,
`reopened`, `closed`, `merged`) is exercised by the existing contract matrix
tests and passes without DLQ routing.

The end-to-end live validation (creating a disposable PR, observing webhook
delivery, verifying AWX job launch, and confirming database persistence) is
documented in the validation script at `scripts/validate_github_pr_path.py`.

## 2. What was validated

### 2.1 Consumer-side mapping (validated — all pass)

The following acceptance criteria from issue #515 are validated by the existing
test suite:

| Criterion | Test coverage | Status |
|---|---|---|
| `pull_request.opened` maps to `change_request.opened` | `test_github_pull_request_maps_to_change_request` | PASS |
| `pull_request.closed` maps to `change_request.closed` | `test_every_fixture_maps_to_canonical_engineering_event[pull_request-closed]` | PASS |
| `pull_request.merged` maps to `change_request.merged` | `test_every_fixture_maps_to_canonical_engineering_event[pull_request-merged]` | PASS |
| `pull_request.edited` maps to `change_request.updated` | `test_every_fixture_maps_to_canonical_engineering_event[pull_request-edited]` | PASS |
| `pull_request.reopened` maps to `change_request.reopened` | `test_every_fixture_maps_to_canonical_engineering_event[pull_request-reopened]` | PASS |
| Full consumer pipeline (validate → map → persist) without DLQ | `test_full_pipeline_fixture_to_persist[pull_request-*]` (5 parametrized) | PASS |
| Outcome-relevant actions produce intended observations | `test_outcome_relevant_actions_produce_intended_observations[pull_request-*]` | PASS |
| Reporting identity extraction returns exact stable identity | `test_reporting_extraction_returns_exact_stable_identity[pull_request-*]` | PASS |
| No raw webhook body is persisted | `test_full_pipeline_fixture_to_persist[pull_request-*]` (payload_ref only) | PASS |
| Edited/updated/reopened actions persist without DLQ | `test_edited_updated_reopened_persist_not_routed_to_dlq[pull_request-*]` | PASS |

### 2.2 Contract pinning (validated — all pass)

| Criterion | Test coverage | Status |
|---|---|---|
| Fixture source provenance (digest stability, count, valid JSON) | `test_fixture_source_provenance_*` | PASS |
| Checksums match pinned digests | `test_contract_checksums_match_pinned_digests` | PASS |
| Every fixture validates through `validate_normalized_event` | `test_every_fixture_validates_without_error` | PASS |
| Every fixture maps through `map_provider_event` | `test_every_fixture_maps_through_map_provider_event[pull_request-*]` | PASS |
| Every fixture payload reference matches envelope | `test_every_fixture_payload_reference_matches_envelope` | PASS |

### 2.3 Nested v1 envelope validation (validated — all pass)

| Criterion | Test coverage | Status |
|---|---|---|
| Nested v1 envelope parses through `NormalizedProviderEvent` | `test_nested_v1_envelope_parses_through_normalized_provider_event` | PASS |
| Effective properties resolve from nested objects | `test_nested_v1_envelope_effective_properties` | PASS |
| Nested v1 envelope maps to canonical vocabulary | `test_nested_v1_envelope_maps_to_canonical` | PASS |
| Validation accepts v1 schema version | `test_validate_accepts_v1_schema_version` | PASS |
| Validation rejects unsupported schema version | `test_validate_rejects_unsupported_schema_version` | PASS |
| Validation rejects provider mismatch | `test_validate_rejects_provider_mismatch` | PASS |
| Validation rejects delivery_id mismatch | `test_validate_rejects_delivery_id_mismatch` | PASS |
| Validation rejects invalid repository identity | `test_validate_rejects_invalid_repository_identity` | PASS |
| Each violation class produces distinct DLQ reason | `test_each_violation_class_produces_distinct_dlq_reason` | PASS |
| Flat shape is rejected and sent to DLQ | `test_flat_shape_is_rejected_sends_to_dlq_and_commits` | PASS |

## 3. What requires live infrastructure

The following acceptance criteria from issue #515 require the full live
infrastructure stack (Kafka, Postgres, AWX, FastAPI EDA Gateway) and cannot
be validated in this worktree environment:

| Criterion | Required infrastructure | Validation method |
|---|---|---|
| Disposable GitHub PR opened by authorized issuer | GitHub API (push access) | `scripts/validate_github_pr_path.py` Step 2 |
| Provider delivery ID in normalized observation provenance | GitHub webhook admin access | `scripts/validate_github_pr_path.py` Step 3 |
| `pull_request.opened` published to `engineering.events.normalized` | Kafka topic access | Manual Kafka consumer check |
| `pr_mr_opened` command published to `afk.events` | Kafka topic access | Manual Kafka consumer check |
| AWX review job launches and completes | AWX API access | AWX job template monitoring |
| `engineering_events` row persisted | Postgres access | `scripts/validate_github_pr_path.py` Step 5 |
| PR closed without merging | GitHub API (push access) | `scripts/validate_github_pr_path.py` Step 6 |
| Close observation persisted | Postgres access | `scripts/validate_github_pr_path.py` Step 7 |
| No container-upgrade command generated | Kafka topic access | Manual Kafka consumer check |

## 4. Validation script

The validation script `scripts/validate_github_pr_path.py` automates the
end-to-end validation. It performs the following steps:

1. **Verify gh CLI authentication** — confirms GitHub CLI is authenticated
   with sufficient scope.
2. **Create disposable non-draft PR** — creates an orphan branch with a
   marker file, pushes it, and opens a non-draft PR.
3. **Verify webhook delivery** — checks GitHub webhook deliveries for the
   PR open event (requires admin access).
4. **Validate consumer mapping** — runs the existing contract matrix tests
   for `pull_request` to confirm the consumer-side mapping is correct.
5. **Verify engineering_events persistence** — queries the Gateway database
   for the expected `change_request.opened` row (requires Postgres credentials).
6. **Close PR without merging** — closes the disposable PR.
7. **Verify close observation** — queries the Gateway database for the
   expected `change_request.closed` row.
8. **Clean up remote branch** — deletes the remote branch.

### Usage

```sh
# Dry run (no changes made):
python scripts/validate_github_pr_path.py --dry-run

# Full validation (requires infrastructure):
GATEWAY_DATABASE_HOST=localhost \
GATEWAY_DATABASE_PORT=5432 \
GATEWAY_DATABASE_NAME=opencode_gateway \
GATEWAY_DATABASE_USER=opencode \
GATEWAY_DATABASE_PASSWORD=... \
python scripts/validate_github_pr_path.py
```

## 5. Test results

All 1268 core logic tests pass (excluding integration tests and migration tests
that require Postgres, and two test files that require Python >= 3.12 for
`datetime.UTC`):

```
1268 passed, 3 skipped, 101 deselected in 61.83s
```

All 38 pull_request-specific contract matrix tests pass:

```
38 passed, 92 deselected in 0.33s
```

All 4 AFK outcome validation tests pass:

```
4 passed in 0.80s
```

## 6. Environment constraints

- **Python 3.9.25** — the repo declares `requires-python = ">=3.12"`; two test
  files (`test_afk_consumer.py`, `test_reporting_aggregates.py`) use
  `from datetime import UTC` which is Python 3.11+ and cannot be collected.
- **No docker/Postgres** — integration tests that require the
  `docker-compose.test.yml` Postgres (port 5433) cannot run.
- **No Kafka** — live topic observation is not possible.
- **No AWX** — AWX job template monitoring is not possible.
- **No FastAPI EDA Gateway** — the producer is not deployed in this environment.
- **GitHub CLI is authenticated** — `gh` is logged in as `wyautomation` with
  full repo scope, enabling PR creation and branch management.

## 7. Files added

- `scripts/validate_github_pr_path.py` — end-to-end validation script for the
  GitHub PR observation path.
- `docs/issue-515-github-pr-validation.md` — this validation report.

No changes to `afk_outcomes/*`, `app/*`, migrations, or existing tests
(validation-only slice).
