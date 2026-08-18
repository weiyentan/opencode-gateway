# AFK Reconstruction Validation — opencode-gateway (#450)

**Issue**: #450 — Validate AFK reconstruction on opencode-gateway
**Parent**: #454 — PRD: AFK Outcome Observability (tracking issue)
**Slice layer**: L1 validation milestone (builds on #444–#449)
**Validator**: `code-editor-senior` (T3)
**Date**: 2026-08-14
**Verdict**: **PASS** — the known-real #437–440 / #442 cluster reconstructs as ONE
AFK Run with a merged EngineeringOutcome, a newly-assigned ULID `afk_run_id`, and
explainable evidence on every link. Two correlation/fixture observations were
filed as new issues rather than applied silently (see §7).

---

## 1. Summary

The AFK outcome pipeline (domain models #444, deterministic `CorrelationEngine`
#445, GitHub adapter #446, repository #448, backfill CLI #449) was validated
against the known-real `weiyentan/opencode-gateway` cluster that shipped issues
#437–440 as one consolidated develop-loop change request, PR #442.

The cluster reconstructs correctly as **one AFK Run**:

- `afk_run_id` = `01KZX9M4G80000000000000000` — a newly-assigned 26-character
  ULID (the resolver assigns it at reconstruction time; the run carries no
  pre-existing id).
- `status` = `completed`; the run is **reconstructed** by the resolver from a
  session seed plus the window of engineering entities/events (origin =
  reconstructed — the run is built by the resolver, not observed as a live
  provider object).
- `outcome.status` = `merged`; `change_request_ids` = `["change_request:442"]`;
  `resolved_issue_ids` = `["issue:437", "issue:438", "issue:439", "issue:440"]`;
  `merge_event_id` = `"merge_event:442"`; `merged_at` = `2026-08-13T10:10:29Z`.
- **0 unresolved** — no ambiguity and no unmatched outcome. The deliberate noise
  (change_request #441, issue #436) is present but correctly un-correlated.

Every derived link carries `correlation_method` (the `method` field),
`correlation_confidence`, `evidence` with source identifiers, and
`resolver_version = "2"`.

## 2. Scope & method

The validation had to be meaningful without a live `GITHUB_TOKEN` or a running
Postgres (neither is available in this environment — see §8). It was performed
in three mutually-reinforcing steps:

1. **Real-history inspection** — the actual `weiyentan/opencode-gateway` git
   history (commit `337c011` and its branch) and the live GitHub API were
   cross-checked against the committed fixtures.
2. **Engine reconstruction on the committed real-data fixtures** — the
   `CorrelationEngine` was driven directly over
   `tests/fixtures/afk_outcomes/github/raw_payload.json` (which represents the
   #437–440/#442 cluster) with a deterministic ULID source, and the output was
   inspected link-by-link.
3. **Live GitHub cross-check** — `gh api` was used to confirm PR #442 and issues
   #436–440 against the source of truth (read-only; no token handling added).

The machine-checkable form of the acceptance criteria is locked in
`tests/test_afk_outcomes_validation.py` (see §5).

## 3. The known-real cluster (verified against real history)

The cluster is real history in this repository. Verified facts:

| Artifact | Real value (git / GitHub API) |
|---|---|
| Change request | PR #442 "Develop-Loop: Consolidated run — Implemented issues #437, #438, #439, #440" |
| Branch | `ai/feat/issues-437-438-439-440` |
| Merge | `merged = true`, `merged_at = 2026-08-13T10:10:29Z` |
| Resolved issues | #437, #438, #439, #440 (body: `Closes #437` … `Closes #440`) |
| Merge commit | `337c0116775c51abf03d90e73a9afdcee0aef01a`, parent `9d38b66` (the #441 merge) |
| Issue #436 | Real, open, **not** resolved by #442 — "PRD: Add dynamic Agent Usage breakdown to Aurora Glass dashboard" |
| Issue close times | #437 closed `…10:10:31Z`, #440 closed `…10:10:32Z` (GitHub auto-close ~1–3 s after merge) |

The real PR #442 body references #436 in its Summary header
("…implemented the following issues (**PRD #436** — Agent Usage dashboard)"),
which the resolver's mention path surfaces as a `referenced` (0.1) link — the
same semantic role the committed fixture encodes with a different phrase
("Follow-up tracked in #436"). See §6 for the fixture-fidelity notes.

## 4. Reconstruction results (engine output over the fixture)

Resolved `ResolutionResult` for the #442 cluster (deterministic ULID source
`SequenceULID(1786615829000)`, `resolver_version = "2"`, `unresolved = []`):

### 4.1 Correlations (6 — all explainable)

| entity_id | method | confidence | evidence (kind, source) |
|---|---|---|---|
| `change_request:442` | `issue_reference` | 1.0 | `title_match` ← `change_request:442` (exact title) |
| `issue:437` | `issue_reference` | 1.0 | `issue_reference` ← `change_request:442` (`resolves #437`) |
| `issue:438` | `issue_reference` | 1.0 | `issue_reference` ← `change_request:442` (`resolves #438`) |
| `issue:439` | `issue_reference` | 1.0 | `issue_reference` ← `change_request:442` (`resolves #439`) |
| `issue:440` | `issue_reference` | 1.0 | `issue_reference` ← `change_request:442` (`resolves #440`) |
| `issue:436` | `issue_reference` | 0.1 | `issue_reference` ← `change_request:442` (`mentioned #436`) |

### 4.2 Entity links (13)

`resolved` (confidence ≥ 0.5): `change_request:442`, `issue:437`, `issue:438`,
`issue:439`, `issue:440`.
`referenced` (confidence 0.1): `issue:436`.
`noise` (confidence 0.0): `change_request:441`, `commit:d4e5f6a7…` (the commit
message "apply #441 review feedback" references #441, which is outside the run's
known set `{437,438,439,440,442}`).

Since issue #456, the owning change request's branch commits and reviews are
also surfaced as links: four commits (`a1b2c3d…`, `b2c3d4e…`, `c3d4e5f…`,
`337c011…`) and the review (`review:481234`) are lineage links that inherit the
owning change request's confidence (1.0) and carry
`correlation_source = "owning_change_request"`. Direct links (the correlations
and noise above) carry `correlation_source = "direct"`.

### 4.3 Session link (1)

`session_id = 1f9c3a6e-0000-4000-8000-000000000001`,
`external_session_id = ses_01J4T2P0000000000000000000`,
`inferred = true`, `method = temporal_overlap`, `resolver_version = "2"`.

### 4.4 Outcome

`status = merged`, `change_request_ids = ["change_request:442"]`,
`resolved_issue_ids = ["issue:437","issue:438","issue:439","issue:440"]`,
`merge_event_id = "merge_event:442"`, `merged_at = 2026-08-13T10:10:29Z`.

## 5. What matched cleanly

- **One run, no fragmentation.** The owning change request is anchored by exact
  title match (`issue_reference` rule), and its body's explicit `resolves #N`
  references bind all four issues at confidence 1.0 — multi-issue binding is
  treated as a single run, not as ambiguity (locked by #445's design).
- **Merged outcome reconstructed.** The `merge_event:442` is matched to the
  owning change request by number and surfaced as `outcome.merge_event_id` /
  `merged_at`, producing `EngineeringOutcomeStatus.MERGED`.
- **Noise is present but un-correlated.** `change_request:441` (a genuinely
  unrelated merged PR in the same window) and the wrong-issue commit
  `d4e5f6a7…` are surfaced with `role = "noise"`, `correlation_confidence = 0.0`,
  never forced into the outcome.
- **Every link is explainable.** All six correlations carry `method`,
  `correlation_confidence`, `evidence` with `source_entity_id`, and
  `resolver_version = "2"`; all thirteen entity links and the session link carry
  `resolver_version`; the session link additionally carries its `method`.
- **Determinism.** Two runs over the same fixture produce byte-identical
  canonical JSON (asserted by `test_golden_determinism_byte_identical` and the
  new validation test).
- **Real-body compatibility.** The resolver's `resolves?|closes?|fixes?` matcher
  accepts the real body's `Closes #437`…`Closes #440` exactly as it accepts the
  fixture's `Resolves #437`…`Resolves #440` (verified directly against the live
  PR body — both yield `resolved = {437,438,439,440}`).

## 6. What was ambiguous / unmatched, and fixture fidelity

**Nothing was ambiguous or unmatched** — `unresolved = []` for the cluster.
However, the validation surfaced two fidelity observations about the committed
fixtures that matter for interpreting the result:

1. **`raw_payload.json` is a synthetic but semantically-faithful representative
   of the real cluster, not byte-exact GitHub data.** Differences verified
   against the live API:
   - PR #442 body wording — fixture `"Resolves #437, resolves #438, …"` +
     `"Follow-up tracked in #436"`; real body `"Closes #437"`…`"Closes #440"` +
     `"PRD #436 — Agent Usage dashboard"` in the Summary header. **Semantically
     identical** for the resolver (both resolve 437–440 and mention #436), so the
     reconstruction result is unchanged.
   - Issue #436 title — fixture `"Refactor dashboard date-range state into a
     shared hook"`; real `"PRD: Add dynamic Agent Usage breakdown to Aurora Glass
     dashboard"`. Title is descriptive metadata, never identity, so it does not
     affect correlation (identity is `entity_id = "issue:436"`).
   - Commit SHAs/messages and the review event are synthetic — the real branch
     commits are `63af10f` (#437), `e32cfc0` (#438), `e90b188` (#439), `7b71342`
     (#440), `95b75a9` (docs), `a7877c0` (review), and PR #442 itself has **no
     reviews** in the GitHub API (the fixture's `gatekeeper-bot` approved review
     is invented). These affect only which supporting entities are *carried*; the
     correlation *links* (change_request + issues) are unaffected.
   - Issue close timestamps — fixture uses the merge instant (`10:10:29Z`) for all
     four issues; real auto-close is 1–3 s later (`10:10:31Z`/`10:10:32Z`).

   **Implication**: the fixture validates the *engine's behavior on a
   representative window*; a live backfill over real GitHub data would produce the
   same `resolved`/`referenced` links (confirmed by the real-body check) but would
   carry the real commit SHAs and would not invent a #442 review. Full live-data
   confirmation is tracked as a follow-up issue (see §7.3).

2. **Stale golden fixture (`golden_run.json`) encodes an obsolete correlation
   vocabulary.** `tests/test_afk_outcomes_fixtures.py` builds the cluster with a
   hand-rolled builder that emits the *pre-#445* vocabulary
   (`change_request_merged`, `issue_resolved`, `issue_mention` methods;
   `branch_name`, `issue_mention` evidence kinds; a session link with
   `inferred = false, method = null`). The shipped engine (#445) emits
   `issue_reference`/`branch_issue_reference`/`commit_issue_reference`/
   `temporal_inference` methods and `title_match`/`issue_reference`/
   `branch_reference`/`commit_reference`/`temporal_overlap` evidence, with
   session links `inferred = true`. Two committed golden fixtures
   (`golden_run.json` vs `golden_resolution.json`) therefore describe the *same*
   cluster with *different* vocabularies. Filed as issue #455 (§7.1); left unchanged
   here to respect the validation-only constraint.

## 7. Correlation-rule tuning observations (filed as new issues)

No correlation rule was modified in this slice. Three observations were filed as
new issues with rationale:

1. **Fixture drift** — reconcile `golden_run.json` + the `build_run` builder with
   the shipped `CorrelationEngine` vocabulary (or retire them). See §6.2.
   Filed as **#455**.
2. **Link completeness (owning-branch commits & reviews)** — the engine links the
   owning change request and its resolved/referenced issues, but **commits and
   review events on the owning change request's branch are carried as entities
   and never linked** (`resolved`/`referenced`). `commit_issue_reference` binds
   *issues* (via commit messages), not commits; the consolidation commit `337c011`
   and its per-issue commits `63af10f`…`7b71342` therefore have no entity link.
   Whether the run should surface its own branch commits and review as links is an
   open design question for the correlation engine (rule-tuning candidate).
   Filed as **#456**. **Implemented**: commits and reviews on the owning change
   request's branch now surface as lineage links inheriting the owning change
   request's confidence, with `correlation_source = "owning_change_request"`;
   `owning_change_request_id` provenance is populated by the adapters and persisted
   via migration 0028.
3. **Live-data backfill follow-up** — complete the validation with a real
   (non-dry-run) backfill over `weiyentan/opencode-gateway`, `--since
   2026-06-01`, using `GITHUB_TOKEN` + the docker-compose Postgres (port 5433);
   confirm dry-run counts match stored rows and re-runs converge idempotently.
   Filed as **#457**.

## 8. Environment constraints & reproducibility

- **Python 3.9.25** (repo declares `requires-python = ">=3.12"`); all added code
  uses the 3.9-safe `from __future__ import annotations` pattern.
- **No `GITHUB_TOKEN`** in the environment and **no Postgres on port 5433**, so a
  live `python scripts/afk_backfill.py …` run was not possible. The validation was
  performed against the committed fixtures (which originate from the real history)
  plus a read-only `gh api` cross-check; the live-run step is the follow-up issue.
- Reproducible commands (run from the repo root):
  ```sh
  # Engine reconstruction over the known-real fixture:
  python -m pytest tests/test_afk_outcomes_validation.py -q
  # Full AFK outcome suite:
  python -m pytest tests/test_afk_outcomes_*.py -q
  # Live backfill (requires GITHUB_TOKEN + Postgres 5433):
  python scripts/afk_backfill.py --provider github \
      --repository weiyentan/opencode-gateway \
      --since 2026-06-01T00:00:00Z --until 2026-08-14T23:59:59Z \
      --dry-run --show-evidence
  ```

## 9. Files changed / added

- `docs/afk-outcome-validation.md` — this findings report (primary deliverable).
- `tests/test_afk_outcomes_validation.py` — machine-checkable assertions of the
  acceptance criteria over the known-real cluster.
- New issues filed on `weiyentan/opencode-gateway` (see §7).

No changes to `afk_outcomes/*`, `app/*`, migrations, or `scripts/afk_backfill.py`
(validation-only slice, per the task contract).

---

# Live GitLab MR Observation & AWX Command Path Validation — issue #516

**Issue**: #516 — Validate GitLab MR observation and AWX command path
**Parent**: #511 (validation pair with #515, the GitHub PR equivalent)
**Validator**: `code-editor-senior` (T3)
**Date**: 2026-08-18
**Verdict**: **PASS (qualified)** — the live GitLab MR open→command→review→close
path works end to end: one disposable non-draft MR produced exactly one
`pr_mr_opened` command, one `merge_request.opened` observation with the GitLab
event UUID in its provenance, exactly one AWX review job (posted its review
and its verdict round-tripped through Kafka), and a clean `merge_request.closed`
observation on close-without-merge, with **no** container-upgrade command.
Two acceptance criteria are **blocked by the in-flight #512–#514 topic-split
rollout**, not by this path: the observation is still published to `afk.events`
instead of `engineering.events.normalized` (the producer switch #513 is not
deployed yet), and the `engineering_events` row could not be verified because
the `opencode-outcomes` consumer group holds no committed offsets (the consumer
leg of the cutover is mid-flight). Evidence for every verified criterion is
recorded in §2–§3 with exact offsets and delivery IDs.

## 1. Method

This validation ran against the **live** stack, not fixtures:

- GitLab API (gitlab.com) as `wyautomation` — a member of
  `AUTHORIZED_ISSUERS=weiyentan,wyautomation` on the deployed EDA gateway, and
  Developer on `openclaw/openclaw_ansible_playbooks` (the disposable-MR host,
  chosen because its MR webhook path was proven live earlier the same day by
  MR #123).
- Strimzi Kafka at `192.168.1.105:9094` — direct consumer reads of
  `afk.events` / `engineering.events.normalized` / DLQ topics and of the
  consumer-group committed offsets (`ansible-eda-afk-trigger`,
  `opencode-outcomes`).
- GitLab MR notes API — the AWX "MR Review Runner" job posts its review as a
  comment (`AGENT: Reviewer`), which is the observable completion signal for
  the AWX leg (direct AAP API job listing requires a controller token that is
  not provisioned in this environment).

Sequence: create branch → add one file via the repository API → open
non-draft MR → observe Kafka → wait for the review job → close without
merging → observe Kafka → sweep for container-upgrade commands.

## 2. Disposable MR and open-path evidence

MR `!124` on `prometheus-build-repository/sourcecontrollayout/openclaw/
openclaw_ansible_playbooks`:
`test: issue-516 disposable MR — GitLab MR observation validation`
(web_url `.../merge_requests/124`; author `wyautomation`; draft=False;
no `afk` label; source branch `val/issue-516-mr-obs-20260818052055`).

| Criterion | Result | Evidence |
|---|---|---|
| MR opened by authorized issuer, no afk label | ✅ | MR iid 124, author wyautomation, draft=False, opened 05:21:29.708Z via API |
| GitLab event UUID recorded & in normalized provenance | ✅ | `delivery_id = e6e1b140-cf66-47dd-9be2-c234f75413ed` (open) — the producer forwards GitLab's `X-Gitlab-Event-UUID` header verbatim as the envelope `delivery_id`, and it appears in both the envelope and `redacted_payload.reference.delivery_id` (self-consistent reference). Direct GitLab "Recent deliveries" cross-check requires Maintainer+ on the project (403 for the validator) — format and code-path verified instead (`main.py`: `delivery_id=event_uuid`) |
| One `merge_request.opened` observation | ✅ (topic deviation, see §4.1) | `afk.events` p4 off792, ts 05:21:30.430Z: nested v1 envelope, `provider=gitlab`, `resource.type=merge_request`, `resource.number=124`, `action=opened`, `actor=wyautomation`, `occurred_at=2026-08-18T05:21:29.708Z`, `ingested_at=…05:21:30.391Z`, redacted reference matches envelope |
| One `pr_mr_opened` command | ✅ | `afk.events` p2 off463, ts 05:21:30.393Z: `event_type=pr_mr_opened`, 10-field schema, `forge=gitlab`, `pr_number=124`, `created_by=wyautomation`, correct source/target branches and title |
| Exactly one AWX review job launches & completes | ✅ | EDA rulebook consumed the command (`ansible-eda-afk-trigger` committed offsets advanced past p2 off463 → 465 and p4 off792 → 795); the "MR Review Runner" job posted exactly **one** `AGENT: Reviewer` comment on MR 124 at 05:25:32.931Z (approve), and its Note Hook produced one `review_verdict approve` on `afk.events` (p2 off464, 05:25:34.316Z) — the verdict round-trip is the job's completion signal |
| No raw webhook body persisted | ✅ | Wire level: the captured Kafka messages carry only `redacted_payload.reference` (provider + delivery_id) — no title/description/diff content. Producer redacts at the ingest boundary (`_redact_and_record`) before any routing; the consumer mapping tests pin that only `payload_ref` (never the body) reaches the canonical event |

## 3. Close-path evidence (close without merging, no container-upgrade)

MR 124 was closed via `state_event=close` at 05:27:28.351Z — never merged.

| Criterion | Result | Evidence |
|---|---|---|
| Normalized close observation persisted | ✅ | `afk.events` p4 off793, ts 05:27:28.926Z: `merge_request` `number=124`, `action=closed`, `delivery_id=f7142cca-2781-43c8-97a8-f43efd355a9e`, `actor=wyautomation`, `occurred_at=2026-08-18T05:27:28.020Z`. (GitLab also fires an update webhook on the close transition → an additional `action=updated` observation, p4 off794 — expected GitLab behavior, both actions are in the producer lifecycle allowlist) |
| No container-upgrade command during cleanup | ✅ | Full sweep of `afk.events` across the 05:20–05:35Z window found **zero** `container_upgrade_requested` events; the merge path (which produces that command) was never taken |

## 4. Findings (two criteria blocked by the in-flight topic-split rollout)

### 4.1 `engineering.events.normalized` is empty — producer switch not deployed

The `merge_request.opened`/`closed` observations were published to
`afk.events`, and `engineering.events.normalized` holds **zero** messages
(all six partitions at offset 0). The deployed producer image is still the
pre-split revision (`fast-api-eda-gateway:60433d49`), whose
`normalized_event_producer` defaults to `afk.events`; the split revision
(`200aae50`, which adds `NORMALIZED_EVENTS_TOPIC`) is pinned in the
GitOps repo's working tree but the deployment manifest still references the
old image, so the #513 producer switch has not reached the cluster. The
acceptance criterion "observation published to `engineering.events.normalized`"
therefore **fails under current deployment state** and must be re-checked
after #513 lands — the observation itself is produced and well-formed, only
its destination topic is stale.

### 4.2 `engineering_events` row not verifiable — consumer group has no committed offsets

The gateway's AFK outcome consumer group `opencode-outcomes` shows **no
committed offsets** on `afk.events`, `engineering.events.normalized`, or the
DLQ topics (control check: the `opencode-gateway` usage group on
`opencode.usage.v1` returns committed offsets, so the measurement is valid;
the `ansible-eda-afk-trigger` group likewise shows healthy advancing offsets).
The consumer commits only after a successful transactional DB write, so the
zero-commit state means the consumer→Postgres leg is not currently processing
(or is subscribed to the still-empty new topic). This is the expected mid-cutover
gap of the #512–#514 sequence (ADR 0023: consumer on the new topic before the
producer switch), but it blocks direct verification of the "exactly one matching
`engineering_events` row" criterion from this environment, which also has no
direct Postgres access (CNPG is cluster-internal; no API route reads
`engineering_events` by design, ADR 0021/0022). The DLQ topics are empty, so
no delivery was rejected — the row is *expected* to exist for the two observed
normalized events once the consumer leg is healthy; re-verification is a
follow-up once #514 completes.

## 5. Reproducibility

Disposable MR created and closed entirely via the GitLab API (no local git
push): branch from `main` head `89ec915f`, one-commit add of `VALIDATION_516.md`,
`POST /projects/:id/merge_requests`, then `PUT …/merge_requests/124` with
`state_event=close`. Kafka observation used a plain `kafka-python` consumer
(no group, `enable_auto_commit=False`) reading `afk.events` from the end
offsets, then re-scanning partition history for the MR's resource number.

Test suites run per the task contract:
- `python -m pytest tests/test_afk_outcomes_*.py -q` — **134 passed**
- `python -m pytest tests/test_producer_to_gateway_contract_matrix.py -q` — **130 passed**
- `python -m pytest tests/integration/test_afk_consumer.py -v -m integration` — **12 skipped** (no local Postgres 5433; skip-if-unreachable path, pre-existing)
- `python -m pytest tests/test_afk_consumer.py -q` — **collection error on this host**: Python 3.9 cannot import `datetime.UTC` (3.11+); the repo declares `requires-python = ">=3.12"`. Pre-existing environment limitation, not a regression (worktree otherwise clean).

## 6. Files changed

- `docs/afk-outcome-validation.md` — this findings section (primary deliverable).

No changes to `afk_outcomes/*`, `app/*`, migrations, or `scripts/afk_backfill.py`
(validation-only slice, per the task contract). The disposable MR was closed
without merging; its source branch `val/issue-516-mr-obs-20260818052055` was
left in place (deleting it would fire a push webhook — unnecessary noise).
