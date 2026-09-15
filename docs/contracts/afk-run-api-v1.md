# Canonical AFK Run API Contract (v1)

| | |
|---|---|
| **Issue** | #671 (contract definition — no implementation in this issue) |
| **Status** | Accepted — binding contract for #672 (list/detail), #673 (updates), #674 (deletion), #675 (compatibility tests) |
| **Implementation surface** | New canonical endpoints under `/api/v1/afk/runs` |
| **Compatibility constraint** | Existing `/api/v1/afk/executions` and `/api/v1/afk-outcomes` endpoints are unchanged |

This document is the **canonical resource contract for the AFK Run**. It defines
the endpoint paths, request and response shapes, mutable fields, lifecycle
validation rules, authorization requirements, orphan eligibility rules,
deletion behavior, and compatibility expectations for the existing
execution-scoped endpoints. Implementation issues (#672–#675) implement this
contract; where earlier routing hints in their digests suggest different file
or path hosting, this contract governs.

---

## 1. Scope and coexistence

Three API surfaces touch AFK runs. They are deliberately distinct and all
remain in place:

| Surface | Prefix | Role | Writes? |
|---|---|---|---|
| **Canonical AFK Run API** (this contract) | `/api/v1/afk/runs` | Resource-oriented read/update/delete of the AFK Run aggregate | PATCH, DELETE (orphan-only) |
| Execution-scoped API (issues #549, #589, #590, #626) | `/api/v1/afk/executions` | AWX execution bindings, lifecycle provisioning, change-request binding | POST (bindings, provisioning, binding) |
| AFK Outcomes REST API (issue #452) | `/api/v1/afk-outcomes` | Read-only observability projection (full chain, entities, correlations, change-request views) | No |

The canonical namespace is `/api/v1/afk/runs` — **not**
`/api/v1/afk/executions/runs` and **not** `/api/v1/afk-outcomes/runs`:

* `/api/v1/afk/executions/runs/{afk_run_id}` already exists (execution-binding
  history for a run, issue #589). A second `GET` handler on the same path is a
  route collision and would change an existing endpoint's response — forbidden.
* `/api/v1/afk-outcomes` is read-only by contract; hosting destructive
  operations there would break its guarantee.

**Implementation placement.** Implement the canonical endpoints on a dedicated
router module (recommended `app/api/afk_runs.py`) mounted at prefix
`/api/v1/afk` in `app/core/factory.py`. Do not add these routes to
`app/api/afk_executions.py` or `app/api/afk_outcomes.py`.

---

## 2. Resource identity

**`afk_run_id`** — the Gateway-owned ULID primary key of the AFK Run
(`afk_runs.afk_run_id`, `String(26)`), assigned at provisioning time
(`MonotonicULID`) or at backfill reconstruction. The run carries no
pre-existing identifier from the provider. It is the sole path identifier of
every canonical endpoint.

**Path-parameter validation (all canonical endpoints).** `afk_run_id` must
match the ULID shape (26 Crockford-base32 characters). A non-conforming value
is rejected with **`400 BAD_REQUEST`** before any database access — the same
deterministic-400 precedent as `_validate_awx_job_id` on the execution-binding
paths. A well-formed but unknown `afk_run_id` is **`404 NOT_FOUND`**.

**Sub-resource representations embedded in responses:**

* **Change request** — `{provider, repository, external_id}` (`null` while
  unbound). The three columns on `afk_runs` are all-set-or-all-None and carry
  the 1:1 lifecycle↔change-request invariant (partial unique index
  `uq_afk_runs_change_request_identity`).
* **Provider** — `github | gitlab`. **Trigger type** — `eda | manual |
  scheduled | backfill | recovery` (`null` for legacy reconstruction rows).

---

## 3. Lifecycle status vocabulary

`afk_runs.status` carries the **lifecycle status** — distinct from
`EngineeringOutcomeStatus` (merged/closed/abandoned/open, stored as
`outcome_status`), from the per-execution `ExecutionOutcome`, and from the
agent-run `_compute_status` heuristic. Never conflate them.

| Status | Meaning | Origin |
|---|---|---|
| `pending` | Provisioned, no execution bound yet (provisional start state) | `POST /api/v1/afk/executions/runs` |
| `running` | Execution in progress | Projection / lifecycle facts |
| `completed` | Terminal success | Projection / lifecycle facts |
| `failed` | Terminal failure | Projection / lifecycle facts |
| `cancelled` | Terminal cancellation | Projection / lifecycle facts |
| `blocked` | Intentionally paused awaiting an external condition | Operator correction (PATCH) |
| `stale` | Liveness no longer trusted; no terminal signal | Operator correction (PATCH) |
| `timed_out` | Terminal — exceeded maximum duration | Operator correction (PATCH) |

* `pending` is the provisional status (`PROVISIONAL_RUN_STATUS` in
  `afk_outcomes/models.py`) — it is deliberately **not** a `RunStatus` member.
* `blocked`, `stale`, and `timed_out` are `RunStatus` members
  (`afk_outcomes/models.py`) that enter `afk_runs.status` only through the
  canonical PATCH (§4.3). The status **filter** vocabulary therefore covers
  all eight values.
* Per ADR 0028 the owning change request anchors the lifecycle: a change-
  request lifecycle fact (merge/close) may supersede a manually patched
  status at the next reconciliation. **Facts outrank manual corrections** —
  a PATCH is a correction until the next authoritative fact arrives.
* **Writer scoping (resolves the PATCH-terminal vs ADR 0028 tension).** The
  §4.3 terminal freeze and ADR 0028 reconciliation govern **different
  writers** of `afk_runs.status`:
  * The terminal freeze (§4.3) binds **the canonical PATCH endpoint** — an
    operator correction can never rewrite terminal history through the API.
  * The ADR 0028 reconciliation writer (the backfill/reconciliation upsert,
    `AsyncpgOutcomeRepository.save` → `_upsert_run`) supersedes **any**
    stored status — including a manually patched terminal one — with the
    re-derived status whenever a change-request lifecycle fact is
    reconciled. No database trigger or constraint may enforce the terminal
    freeze, or this writer would break (see §4.3).

---

## 4. Endpoints

| Method | Path | Purpose | Authorization |
|---|---|---|---|
| `GET` | `/api/v1/afk/runs` | Paginated, filterable list | Admin API Key |
| `GET` | `/api/v1/afk/runs/{afk_run_id}` | Canonical detail (full chain) | Admin API Key |
| `PATCH` | `/api/v1/afk/runs/{afk_run_id}` | Guarded update (mutable fields only) | Admin API Key + AWX collector credential |
| `DELETE` | `/api/v1/afk/runs/{afk_run_id}` | Orphan-only deletion | Admin API Key + Operator Token |

**No `POST` in the canonical namespace (v1).** Creation remains exclusively
`POST /api/v1/afk/executions/runs` (provisioning, issue #589) — the
idempotency key (`provider + host + source_event_id`), batch provenance, and
recovery semantics live on that endpoint and are not duplicated here. A
`POST` to `/api/v1/afk/runs` is `405 Method Not Allowed`.

All responses use the shared `{status, data, error}` envelope
(`app/core/envelope.py`): success → `{"status": "ok", "data": …}`; failure →
`{"status": "error", "error": {"code": "<STABLE_CODE>", "message": "…"}}`.
`204 No Content` passes through the envelope unchanged (no body).

### 4.1 `GET /api/v1/afk/runs` — list

**Query parameters**

| Parameter | Type | Valid values | Semantics |
|---|---|---|---|
| `provider` | string, optional | `github`, `gitlab` | `afk_runs.provider` equality |
| `repository` | string, optional | normalized repository identity | Matches the run's bound repository (`afk_runs.repository`) **or** any linked entity's repository (`afk_run_entities.repository`) — reconstruction-era rows carry `NULL` repository but have entity links |
| `status` | string, optional | `pending`, `running`, `completed`, `blocked`, `stale`, `timed_out`, `failed`, `cancelled` | `afk_runs.status` equality |
| `outcome` | string, optional | `merged`, `closed`, `abandoned`, `open` | `afk_runs.outcome_status` equality |
| `has_change_request` | boolean, optional | `true`, `false` | `true` → bound (`change_request_provider IS NOT NULL`); `false` → unbound |
| `created_before` | ISO-8601 datetime, optional | — | `afk_runs.first_seen_at < value` |
| `limit` | int, optional | 1–1000, default 50 | Page size |
| `offset` | int, optional | ≥ 0, default 0 | Page offset |

Invalid enum values, unparseable datetimes, and out-of-range pagination raise
**`400 BAD_REQUEST`** (the `_require_enum_value` / `_parse_datetime`
convention of the outcomes API).

**Ordering.** `ORDER BY last_seen_at DESC NULLS LAST, afk_run_id ASC` —
activity recency (the Source-Created Ordering analogue used by the outcomes
list), with a deterministic tie-breaker.

**Response** — `PaginatedResponse[AFKRunSummary]`:
`{"items": […], "total": <int>, "limit": <int>, "offset": <int>}`.

**`AFKRunSummary`** — the outcomes `RunSummary` extended additively with the
lifecycle fields. The outcomes endpoints keep their exact current
`RunSummary` shape; only the canonical namespace carries the extension.

| Field | Type | Notes |
|---|---|---|
| `afk_run_id` | string (ULID) | |
| `provider` | string | `github` \| `gitlab` |
| `status` | string | §3 vocabulary |
| `title` | string \| null | |
| `repository` | string \| null | Normalized identity; `null` for legacy rows |
| `trigger_type` | string \| null | `eda` \| `manual` \| `scheduled` \| `backfill` \| `recovery` |
| `recovered_from_afk_run_id` | string \| null | Recovery predecessor |
| `change_request` | object \| null | `{provider, repository, external_id}` when bound; `null` when unbound |
| `started_at` / `finished_at` | datetime \| null | |
| `outcome_status` | string \| null | `merged` \| `closed` \| `abandoned` \| `open` |
| `first_seen_at` / `last_seen_at` | datetime \| null | |

### 4.2 `GET /api/v1/afk/runs/{afk_run_id}` — detail

**Response** — the full chain, reusing the outcomes `RunDetail` composition
(3 bounded queries, no N+1) with the run block replaced by `AFKRunSummary`:

| Field | Type | Notes |
|---|---|---|
| `run` | AFKRunSummary | The extended summary (§4.1) |
| `outcome` | object \| null | `EngineeringOutcome`: `{status, change_request_ids, resolved_issue_ids, merge_event_id, merged_at}` |
| `issues`, `change_requests`, `reviews`, `commits`, `merge_events` | arrays | `EntityLink` rows grouped by type, each with `role`, `correlation_method`, `correlation_confidence`, `evidence`, `correlation_source`, `owning_change_request_id`, `resolver_version`, and the computed `provisional` marker |
| `sessions` | array | `SessionLink` rows (internal/external session id, agent, per-session token categories, cost) |
| `agents` | array of string | Sorted distinct agents |
| `usage` | object | `UsageAggregate`: active/input/output/cache-read/cache-write tokens, estimated cost, message and session counts |

**Errors.** `400` malformed ULID; `404` unknown `afk_run_id`.

### 4.3 `PATCH /api/v1/afk/runs/{afk_run_id}` — guarded update

**Request** — `AFKRunUpdateRequest`, `ConfigDict(extra="forbid")`.

| Field | Type | Rules |
|---|---|---|
| `title` | string, optional | When supplied: non-empty, whitespace-trimmed, ≤ 1000 characters. Supplying `null` is **`422`** — omit the field to leave the value unchanged. |
| `status` | string, optional | One of the seven `RunStatus` values: `running`, `completed`, `blocked`, `stale`, `timed_out`, `failed`, `cancelled`. `pending` is **not** patchable (`422`) — it is the provisioning-assigned provisional state. |

**Mutable fields are exactly `{title, status}`.** Nothing else is patchable,
ever: `provider`, `afk_run_id`, `started_at`/`finished_at`,
`first_seen_at`/`last_seen_at`, `outcome`/`outcome_status`, the change-request
binding columns, `recovered_from_afk_run_id`, `trigger_type`, `repository`,
and `host` are immutable through this endpoint. PATCH never touches execution
bindings, entity/session links, unresolved correlations, or the change-request
binding.

**Body validation.** Unknown fields (`extra="forbid"`), wrong types, an
explicit `null` for `title`, and an empty body (neither field supplied) are
**`422 VALIDATION_ERROR`**.

**Lifecycle transition rules.** Evaluated against the current stored status
under a `SELECT … FOR UPDATE` row lock (serialized, replay-safe):

| Current status | Requested status | Result |
|---|---|---|
| any | same as current | `200` — idempotent no-op, no mutation (including terminal → same terminal) |
| `completed`, `failed`, `cancelled`, `timed_out` (terminal) | any different value | **`409 CONFLICT`** — through this endpoint, terminal history is never overwritten; a retry is a new lifecycle via `recovered_from_afk_run_id` |
| `pending`, `running`, `blocked`, `stale` (non-terminal) | any of the seven | `200` — applied (e.g. `pending → failed`, `blocked → running`, `running → stale`) |

Mirrors the execution-binding terminal rule (`running` never reappears after
a terminal outcome) and the domain language that `blocked`/`stale` may return
to `running`. A status value outside the seven-member vocabulary is `422`
(validation); a vocabulary member rejected only by the transition table is
`409` (conflict with lifecycle state).

**Scope of the terminal freeze.** The freeze is endpoint validation on the
PATCH writer — not a database-wide constraint. It stops an operator
correction from rewriting terminal history through the API, and nothing
else: the ADR 0028 reconciliation writer (`AsyncpgOutcomeRepository.save` →
`_upsert_run`, which upserts with `status = EXCLUDED.status`) supersedes any
stored status, including a manually patched terminal one, when a re-derived
change-request lifecycle fact is reconciled (§3 — facts outrank manual
corrections). Implementations (#673) must therefore enforce the freeze **in
the PATCH handler only** and must not add a trigger or database constraint
that would block the reconciliation writer.

**Authorization.** Admin API Key (`ApiKeyMiddleware`) **plus** the dedicated
AWX execution-binding collector credential — the same two-layer write gate as
`POST /api/v1/afk/executions`: `X-Collector-Token` resolving to a
non-revoked credential of the `awx-execution-bindings` client. A valid
credential of any other client is **`403 FORBIDDEN`**; missing/invalid
credentials are **`401 UNAUTHORIZED`**. Lifecycle correction is a pipeline
write on the same aggregate the AWX integration owns.

**Response.** `200` with the updated `AFKRunSummary`.

### 4.4 `DELETE /api/v1/afk/runs/{afk_run_id}` — orphan-only deletion

**Orphan eligibility (authoritative definition).** A run is orphan-eligible
if and only if **all three** hold:

1. **No execution bindings reference it** — zero rows in
   `execution_bindings WHERE afk_run_id = $1`. This guard is mandatory:
   `execution_bindings.afk_run_id` is `FOREIGN KEY … ON DELETE SET NULL`, so
   deleting a bound run would silently sever the bindings' run attribution —
   execution history must never become unattributed.
2. **No change request is bound** — `change_request_provider IS NULL` (all
   three columns are NULL). A bound run is owned, not an orphan: deleting it
   would erase the durable 1:1 change-request binding and silently break the
   `GET /api/v1/afk/executions/runs/by-change-request` identity continuation.
3. **No delivery provenance references it** — zero rows in
   `delivery_log WHERE afk_run_id = $1`. This guard is mandatory:
   `delivery_log.afk_run_id` is a nullable `FOREIGN KEY … ON DELETE NO
   ACTION` (migration 0026) with no detach path — a DELETE of a referenced
   run would fail with an FK violation surfacing as **`500`**, and delivery
   rows are immutable facts that are never deleted or unattributed by a run
   deletion (see "Never touched" below), so the guard converts that failure
   into a deterministic `409`. Delivery provenance is ownership: every run
   persisted by the backfill engine or the reconciliation upsert records a
   `delivery_log` row (`AsyncpgOutcomeRepository._log_delivery`), so
   reconstructed runs are durably owned and never orphans.

**Consequence.** In practice orphan deletion applies to provisioned runs
(`POST /api/v1/afk/executions/runs`) that never accumulated an execution
binding, a change-request binding, or any delivery record — provisioning
itself writes no `delivery_log` row. A backfill-reconstructed run always
fails check 3.

A run failing any check is **`409 CONFLICT`**; the error message names the
blocking reason (execution bindings present / change request bound / delivery
provenance present).

**Deletion scope — single transaction:**

| Table | Action | Mechanism |
|---|---|---|
| `afk_runs` | DELETE the row | Explicit |
| `afk_run_sessions` | DELETE | `ON DELETE CASCADE` |
| `afk_run_entities` | DELETE | `ON DELETE CASCADE` |
| `afk_run_delivery_batches` | DELETE (batch provenance dies with the run) | `ON DELETE CASCADE` |
| `unresolved_correlations` | DELETE | **No FK** — must be deleted explicitly by the same statement batch |
| successor `afk_runs.recovered_from_afk_run_id` | SET NULL | Self-FK `ON DELETE SET NULL` — deleting a recovery predecessor nulls the successor's pointer (documented effect, not an eligibility blocker) |

**Never touched:** `execution_bindings`, `sessions`,
`engineering_events`, `delivery_log`, `closure_links` / `closure_episodes` /
`closure_unresolved`, `resource_session_associations`. Immutable facts and
independent aggregates are never deleted by a run deletion. The orphan guard
is evaluated in the same transaction as the delete, under the locked row, so
a concurrent reference (an execution binding or a `delivery_log` row) turns
the delete into a `409` rather than a severed association or an FK violation.

**Authorization.** Admin API Key (`ApiKeyMiddleware`) **plus** the Operator
Token (`require_operator_token`, `X-Operator-Token` header — never
`Authorization`). Deletion is a destructive operator-only operation; the
Admin API Key does not satisfy the operator gate. `GATEWAY_OPERATOR_TOKEN`
unconfigured → **`403 FORBIDDEN`** (fail closed, no operator-only surface is
reachable); missing/invalid header → **`401 UNAUTHORIZED`**.

**Responses.** `204 No Content` (no body — the envelope passes `204` through
unchanged); `400` malformed ULID; `404` unknown `afk_run_id` (a repeated
DELETE is `404`, not a second `204`); `409` not orphan-eligible.

---

## 5. Error catalog

| Status | Code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed `afk_run_id` (non-ULID); invalid filter enum/datetime on the list endpoint |
| 401 | `UNAUTHORIZED` | Missing/invalid Admin API Key; missing/invalid `X-Operator-Token` (DELETE); missing/invalid collector credential (PATCH) |
| 403 | `FORBIDDEN` | Operator access not configured (DELETE — fail closed); collector credential not attributable to `awx-execution-bindings` (PATCH) |
| 404 | `NOT_FOUND` | Well-formed ULID with no `afk_runs` row |
| 409 | `CONFLICT` | PATCH: forbidden lifecycle transition; DELETE: not orphan-eligible (bindings present, change request bound, or delivery provenance present) |
| 422 | `VALIDATION_ERROR` | Request-body shape failures on PATCH (unknown fields, wrong types, `null` title, empty body, non-member status, oversized title) |
| 504 | `GATEWAY_TIMEOUT` | Request time budget exceeded (existing handler) |

---

## 6. Compatibility expectations

The introduction of the canonical AFK Run API is **additive**. The following
guarantees are binding on #672–#675:

**Must not change on `/api/v1/afk/executions`:**

1. `POST /api/v1/afk/executions` — execution-binding write: idempotent by AWX
   job identity (identical replay `200`, new insert `201`, conflicting data
   `409`, missing referenced lifecycle `404`); `afk_run_id` required for every
   new binding (issue #626, `422` otherwise); repository-URL normalization at
   the boundary; bounded/redacted failure metadata only.
2. `PATCH /api/v1/afk/executions/{awx_job_id}` — two-phase terminal
   transition (`running` → `completed`/`failed`/`cancelled`): serialized under
   `SELECT … FOR UPDATE`, non-erasing fill-ins, identical replay `200`,
   conflicting payload `409`, `running` never reappears after a terminal
   outcome. **It remains the only writer of execution outcomes.** The
   canonical run PATCH (§4.3) never mutates execution outcomes or bindings.
3. `POST /api/v1/afk/executions/runs` — remains the **only** creation path
   for AFK Run lifecycles (provisioning idempotency, batch provenance,
   recovery). The canonical namespace adds no `POST`.
4. `POST /api/v1/afk/executions/runs/{afk_run_id}/change-request` — remains
   the **only** writer of the change-request binding (1:1 invariant, `409` on
   conflict). The canonical PATCH/DELETE never mutate the binding columns.
5. `GET /api/v1/afk/executions/{awx_job_id}`, `GET /api/v1/afk/executions`,
   `GET /api/v1/afk/executions/runs/{afk_run_id}` (binding history), and
   `GET /api/v1/afk/executions/runs/by-change-request` — unchanged paths,
   response shapes, and Admin-API-Key-only read authorization.
6. Response envelope, authentication layers (disjoint credential types:
   Admin API Key / AWX collector credential / Operator Token), and
   failure-summary bounds and redaction on the execution-scoped endpoints.
7. **Deletion interplay** — the orphan-only rule exists precisely so that
   execution-scoped reads keep working across any canonical delete:
   `execution_bindings` rows always survive; binding↔run attribution is never
   severed; `delivery_log` rows always survive (a run they reference is not
   orphan-eligible — §4.4 check 3); a deleted (orphan) run never had
   bindings or delivery provenance, so
   `GET /api/v1/afk/executions/runs/{afk_run_id}` behavior for surviving runs
   is untouched.

**Must not change on `/api/v1/afk-outcomes`:** every endpoint stays strictly
read-only with its exact current response shapes (`RunSummary`, `RunDetail`,
`EntityLink`, …). The canonical list/detail reuse the same query composition
and schema shapes **additively** (the extended `AFKRunSummary` exists only in
the canonical namespace). The outcomes API gains no write operations — the
DELETE of §4.4 lives on the canonical router, not there.

**Coexistence strategy.** Namespace separation is the compatibility
mechanism: no existing path, method, status code, envelope, or response shape
changes; new capabilities appear only under `/api/v1/afk/runs`. Consumers of
the execution-scoped and outcomes APIs require no migration.
