# ADR 0010: Backend-computed agent run status

## Status

Accepted

## Context

The Gateway API returns an `AgentRunSummary` view model originally containing
a single `status` field — a derived label such as `running`, `stale`,
`completed`, `blocked`, or `unknown`. This status is not stored in a database
column; it is computed on read from session facts (message count, timestamps,
parent relationship) using a heuristic with quiet-threshold, stale-threshold,
and unknown-threshold constants.

At the time of this decision, the backend implements status derivation twice:

1. A Python function `_compute_status()` used for individual run detail.
2. A SQL `CASE` expression (`_status_case_expression()`) used for paginated
   list queries so the database can filter by computed status server-side.

The frontend (Aurora Glass) receives the already-computed status string and
maps it to CSS badge classes via a `statusBadgeClass()` function. It performs
no independent status derivation.

A contributor new to the codebase might reasonably consider moving status
derivation to the frontend — for example, to reduce API payload size, to
enable offline display, or because the derivation appears simple enough to
duplicate in JavaScript. Without a recorded decision, this boundary is
implicit and subject to drift.

### Revised contract: splitting persisted and derived status

After initial implementation, a gap was identified: the `status` field in the
API response was being overwritten by the computed value, destroying the raw
persisted status that consumers and operators relied on for debugging and
backward-compatible filtering. The API contract was revised to introduce a
separation of concerns:

- **`status`**: The first-generation computed status, retained for backward
  compatibility with existing API consumers.
- **`currentStatus`**: The authoritative derived/computed status produced by
  the backend heuristics (`_compute_status` / `_status_case_expression`).
  This is the canonical run-status label that the rest of the system should
  consume.

**Note:** There is no raw persisted status column in the sessions table.
Both `status` and `currentStatus` are computed on read from session facts.
The two fields exist so consumers can migrate from the legacy `status` field
to the canonical `currentStatus` without a breaking change.

The UI badge rendering uses `currentStatus` (falling back to `status` for
backward compatibility with older API responses that predate the two-field
contract); `status` is available in the payload for legacy consumers.

## Decision

Agent run status MUST be computed by the Gateway backend (API layer) and
included in the API response. The frontend MUST NOT independently derive
or re-derive status from raw session fields; it renders the status value it
receives and owns only the presentation mapping (e.g., badge colour, icon).

Both the Python helper and the SQL `CASE` expression in the API layer are
acceptable implementations, provided they remain consistent with each other.
If the derivation logic is ever centralised, it belongs in the backend, not
in the frontend.

### API contract: two status fields

The API exposes **two distinct fields** to separate concerns:

1. **`status`** — The first-generation computed status, retained for
   backward compatibility. May be deprecated in a future version.
2. **`currentStatus`** — The authoritative derived/computed status produced
   by the backend heuristics (`_compute_status` / `_status_case_expression`).
   This is the canonical run-status label. All UI badge rendering SHOULD
   use this field, falling back to `status` for responses that lack it.

Both fields contain the same derived value in the current implementation.
The frontend badge function (`statusBadgeClass`) prefers `currentStatus`,
falling back to `status` for backward compatibility. The `status` field
remains in the payload strictly for legacy-consumer compatibility.

## Rationale

- **Single source of truth.** One derivation, one set of threshold constants,
  one place to tune. Duplicating heuristic logic in JavaScript creates
  unavoidable drift when thresholds or rules change.

- **Server-side filtering.** The API supports filtering agent runs by
  computed status (`?status=running`). This requires the database to compute
  status at query time — a capability the frontend cannot provide. Moving
  derivation to the frontend would either break server-side filtering or
  require the backend to compute status anyway, defeating the purpose.

- **API contract clarity.** The Gateway API contract explicitly includes
  computed fields (`status`, `child_run_count`) that are "never stored in
  a single column but calculated from surrounding rows." Documenting this
  boundary protects consumers from relying on raw fields to re-derive status.

- **Auditability.** The full status derivation rules live in one place
  (`_compute_status` docstring and `_status_case_expression` in
  `app/api/usage.py`). A frontend developer inspecting the API response
  can read the docstring to understand what each status means.

## Consequences

Positive:

- Derivation logic remains consistent between list queries and detail views.
- Threshold adjustments (quiet threshold, stale threshold, unknown threshold)
  are a single backend change with no frontend deployment dependency.
- The frontend code stays simple: `statusBadgeClass` maps `currentStatus`
  to CSS, nothing more.
- The two-field contract provides a migration path from legacy `status` to
  canonical `currentStatus` without a breaking change.
- The `stale` status adds fidelity to the observability model: sessions with
  a recent-but-inactive gap are distinguished from confidently-completed
  sessions, reducing false "completed" classifications.

Negative:

- The frontend cannot display a useful status while offline or before the
  API responds — it must wait for the computed field.
- The API response size now includes two status strings per run (`status`
  + `currentStatus`). The cost is negligible for short enum values but
  worth noting for large paginated responses.
- If a consumer wants status computed with custom thresholds, they must
  request a backend change rather than adjusting locally.
- Two fields in the response create a risk of consumers using the wrong
  field (`status` instead of `currentStatus`). Mitigated by marking
  `status` as deprecated for UI purposes in API documentation.

## Alternatives Considered

**Frontend computes status from raw API fields** was rejected because it
duplicates heuristic logic, breaks server-side status filtering, and
introduces a second place where threshold constants must be kept in sync.

**Both backend and frontend compute status** was rejected because it
creates ambiguity about which derivation is authoritative and invites drift.

**Precompute status into a database column at ingest time** was deferred.
The current read-time derivation is simple and cheap. A stored column would
be appropriate only if status derivation becomes expensive (e.g., requires
joins across many tables) or if status needs to be indexed at scale.
