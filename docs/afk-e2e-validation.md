# AFK Lifecycle E2E Validation — GitHub & GitLab (Opt-In)

**Issue**: #578 — AFK Outcomes: Opt-In GitHub and GitLab Lifecycle E2E
**Parent**: #570 — PRD: AFK Outcomes Frontend
**Slice layer**: HITL validation harness (explicitly opt-in)
**Harness**: `scripts/afk_e2e_test.py`

> **HITL slice.** This harness depends on explicit environment credentials,
> external provider resources, AWX availability, cleanup permissions, and an
> agreed operational test environment. It is **never** part of the default
> frontend test command, `pytest`, or CI.

---

## 1. Purpose

The harness drives one full AFK lifecycle per provider — GitHub and/or
GitLab — through **dedicated disposable repositories/projects** created solely
for the run, and records evidence for every stage the AFK Outcomes PRD calls
out:

| Evidence category | Source |
|---|---|
| repository | disposable repo/project created by the harness |
| issue | AFK-labelled issue (the AFK trigger) |
| change request | PR (GitHub) / MR (GitLab) opened by the develop loop |
| AWX jobs | Gateway execution bindings (`GET /api/v1/afk/executions`) |
| OpenCode sessions | AFK run detail session links + binding session ids |
| usage/cost | AFK run detail usage aggregate (token categories, estimated cost) |
| merge | harness-driven merge + provider merge state |
| closure | issue closure (provider auto-close or harness fallback) + closure-episode projection |

The same scenario and the same evidence schema run against both providers, so
the harness validates **equivalent user-visible behavior for GitHub and
GitLab**.

## 2. Opt-in guarantee

- The harness is a standalone script. It is run **only** by an explicit
  command:
  ```bash
  python scripts/afk_e2e_test.py --provider both
  ```
- The default frontend test command is `node frontend/tests/test_pure_functions.js`
  (see `.github/workflows/ci.yml`). It never invokes the harness.
- `pytest` discovery is scoped to `tests/` (`pyproject.toml`), so the harness
  is never collected; the harness's deterministic unit tests
  (`tests/test_afk_e2e_harness.py`) run with mocks and require **no
  credentials, network, AWX, or provider resources**.
- The default evidence directory (`.status/afk-e2e-evidence`) is git-ignored.

## 3. Operational prerequisites (HITL)

The harness assumes the operational environment is already wired end-to-end
for the target org/group:

1. The **FastAPI EDA Gateway (producer)** observes the target GitHub org /
   GitLab group and publishes normalized engineering lifecycle events and AFK
   commands.
2. **AWX job templates** exist for the develop and review executions and are
   triggered by the EDA rules for the target org/group.
3. The **AFK Outcome Consumer** is consuming
   `engineering.events.normalized` and writing facts + the closure projection.
4. The **Gateway** is reachable with an Admin API Key that can read
   `/api/v1/afk-outcomes`, `/api/v1/closure-relationships`, and
   `/api/v1/afk/executions`.
5. The AWX integration writes **execution bindings** through the dedicated
   `awx-execution-bindings` collector credential (issue #550) — the harness
   only *reads* them back.
6. Provider tokens with permission to **create and delete** repositories in
   the target org/group, create issues, post reviews, and merge change
   requests.

The harness itself never calls AWX, Kafka, or the OpenCode runners — it
drives the provider side of the lifecycle and polls the Gateway read APIs for
the rest.

## 4. Configuration (environment variables)

Secrets are supplied **only** through environment variables and are never
hardcoded, logged, or printed.

| Variable | Required for | Purpose |
|---|---|---|
| `AFK_E2E_GITHUB_TOKEN` | github | GitHub PAT (repo scope) |
| `AFK_E2E_GITHUB_ORG` | github | Org where disposable repos are created |
| `AFK_E2E_GITLAB_TOKEN` | gitlab | GitLab PAT (`api` scope) |
| `AFK_E2E_GITLAB_GROUP` | gitlab | Group path for disposable projects (subgroups `group/sub`) |
| `AFK_E2E_GATEWAY_API_KEY` | always | Admin API Key for Gateway read APIs |
| `AFK_E2E_OPERATOR_TOKEN` | optional | Operator token (`X-Operator-Token`) for reporting reads |
| `AFK_E2E_GATEWAY_BASE_URL` | optional | default `http://localhost:8000` |
| `AFK_E2E_GITHUB_API_BASE` | optional | default `https://api.github.com` (self-hosted GitHub) |
| `AFK_E2E_GITLAB_API_BASE` | optional | default `https://gitlab.com/api/v4` (self-hosted GitLab) |
| `AFK_E2E_AFK_LABEL` | optional | issue label used as the AFK trigger marker (default `afk`) |
| `AFK_E2E_POLL_INTERVAL_SECONDS` | optional | default `15` |
| `AFK_E2E_POLL_TIMEOUT_SECONDS` | optional | per-phase bound (default `1800`) |
| `AFK_E2E_EVIDENCE_DIR` | optional | default `.status/afk-e2e-evidence` |
| `AFK_E2E_REPO_PREFIX` / `AFK_E2E_REPO_SUFFIX` | optional | disposable repo naming (suffix random by default) |

**Missing credentials fail closed** — the harness prints the *names* of the
missing variables and exits with code `2`. No secret value is ever echoed.

## 5. Usage

```bash
# Validate configuration and connectivity without creating anything:
python scripts/afk_e2e_test.py --dry-run --provider both

# Full lifecycle against disposable GitHub + GitLab resources:
python scripts/afk_e2e_test.py --provider both

# One provider only:
python scripts/afk_e2e_test.py --provider github
python scripts/afk_e2e_test.py --provider gitlab

# Keep the disposable resources for post-mortem (skips cleanup):
python scripts/afk_e2e_test.py --provider github --keep-repos
```

Flags:

- `--provider {github,gitlab,both}` (default `both`)
- `--dry-run` — preflight only (credential + org + Gateway reachability)
- `--keep-repos` — skip cleanup; evidence notes the resources were kept
- `--evidence-dir PATH`, `--poll-interval S`, `--poll-timeout S` — overrides

Exit codes: `0` all checks passed, `1` at least one check failed (with
evidence + diagnostics), `2` configuration error.

## 6. Scenario walkthrough

Per provider the harness:

1. **disposable-repo** — creates a private repo `afk-e2e-<provider>-<suffix>`
   in the configured org/group and records its normalized repository identity.
2. **afk-trigger** — creates an `afk`-labelled issue. The issue *is* the AFK
   trigger: the EDA gateway observes it and launches the AWX develop job.
3. **develop-execution** — polls the provider (bounded) for a change request
   referencing the issue (`#N` in title/body, or the issue number in the
   branch), i.e. the develop loop's PR/MR.
4. **awx-execution-bindings** — polls the Gateway execution-bindings read API
   for bindings of the change request (AWX job ids, outcomes, session ids).
5. **review-request** — posts a changes-requested review (the review trigger).
6. **fix-re-review** — polls for a new commit after the review (the develop
   loop's fix), then posts an approving re-review.
7. **merge** — merges the change request via the provider API.
8. **issue-closure** — polls for the issue closing after the merge (provider
   auto-close from the closing keyword). If the provider does not auto-close
   within the bounded window, the harness closes the issue itself and records
   `mechanism: harness` — never silently pretending.
9. **gateway-afk-run** — polls the Gateway for the reconstructed AFK run
   containing the change request, then records sessions and usage/cost.
10. **closure-projection** — polls the closure-relationships read API for the
    issue's episode (status, attribution, `resolver_version`, `derived_at`).

Then **cleanup** deletes the disposable repository (reported clearly on
failure).

## 7. Bounded polling and diagnostics

Every asynchronous condition (develop change request, fix commits, bindings,
AFK run, closure episode) polls at `AFK_E2E_POLL_INTERVAL_SECONDS` up to
`AFK_E2E_POLL_TIMEOUT_SECONDS`. Each tick logs attempts + elapsed time; on
timeout the step fails with the timeout, attempt count, and a diagnostic
"observed" note explaining what was (not) seen — e.g. *"no change request
referencing the issue appeared — is the EDA gateway / AWX develop loop wired
for this org/group?"*. A failed step stops the scenario (later evidence
categories are recorded as `not_attempted`, never fabricated), but cleanup
still runs.

## 8. Evidence

Written under the evidence directory (per run):

- `summary.json` — overall verdict, `secrets_provided` (presence flags only),
  the eight check categories with full detail, ordered step log, and the
  cross-provider evidence-equivalence note.
- `steps.jsonl` — one line per step, flushed immediately so partial runs
  still leave evidence (e.g. a cleanup failure after a pass).

Secrets are redacted before any value is written: diagnostic strings and
evidence detail replace secret values with `***`, and only presence flags are
stored. The provider/Gateway URLs and resource identifiers in evidence are
not secret.

## 9. Cleanup

Cleanup is attempted at the end of every live run. Deletion failures are
recorded in the summary and printed with the resource slug so an operator can
finish cleanup manually; they never crash the harness. `--keep-repos` skips
deletion and records that the resources were kept.

## 10. Testing the harness itself

The harness's deterministic behaviors are covered by
`tests/test_afk_e2e_harness.py` (no credentials, no network):

- configuration failures list variable names and never echo secrets;
- redaction, bounded polling (success + timeout + tick reporting);
- evidence recording (categories, redaction, presence flags);
- GitHub/GitLab client URL shapes and header conventions (mock transport);
- Gateway envelope unwrapping and auth headers;
- the full scenario through fake in-memory provider/Gateway implementations —
  happy path and a bounded-timeout failure path that still cleans up.

```bash
python -m pytest tests/test_afk_e2e_harness.py -v
```
