# OpenCode Gateway — Domain Language

An observability service for headless OpenCode serve instances. Collects
telemetry, monitors health, and provides a REST API for observability data.

> **Refactor note (issue #207):** This project is being refactored from an
> execution control plane into an observability service. Execution-era
> subsystems (executor plugins, job scheduling, workspace lifecycle,
> policy engine) have been removed. The vocabulary below reflects the
> observability service identity. Future slices will add observability-
> specific concepts.

## Language

**Gateway**:
The observability service. Collects telemetry from Runner VMs, stores
metrics in Postgres, and exposes them through a REST API. It serves the
API, not the Aurora Glass frontend.
_Avoid_: Backend, server, controller

**Aurora Glass**:
The browser-based telemetry dashboard for Gateway observability data.
Consumes the Gateway API, but is not part of the Gateway service itself.
_Avoid_: Gateway UI, embedded dashboard

**OpenCode Serve**:
A long-running headless API process managed by systemd on the Runner VM.
Owns coding sessions, messages, diffs, and tool execution.
_Avoid_: opencode daemon, opencode service (in generic sense)

**Runner VM**:
A persistent virtual machine that hosts workspace directories and
systemd-managed opencode serve instances. Provides the native toolchain
for code editing tasks.
_Avoid_: Worker, node, agent

**Paperclip**:
An agent/work orchestration layer that coordinates agents, goals, task
assignment, governance, budgets, and higher-level workflows. Paperclip
can sit above the Gateway, calling the Gateway API to retrieve
observability data.
_Avoid_: Gateway, execution control plane

**External Session ID**:
The source system's native session identifier (e.g., OpenCode's `ses_*`
IDs from SQLite `session.id`). Stored in `sessions.external_session_id`.
Not a UUID.
_Avoid_: Session ID (ambiguous)

**Internal Session ID**:
The gateway's own UUID primary key (`sessions.id`). Generated at ingest
time when an external session ID is first seen.
_Avoid_: Session ID (ambiguous)

**Session Resolution**:
The process at ingest time of mapping an external session ID to an
internal gateway session UUID, scoped by
`(source_database_id, external_session_id)`. Creates a new internal
session row if not found; reuses existing if found.

**Session Context**:
Descriptive metadata about an OpenCode session read from the source
SQLite database, such as title, agent, external project ID, project
worktree, parent external session ID, workspace ID, model, and code-change
summary counts. Session Context explains what a session was doing;
usage records explain what model usage it consumed.
_Avoid_: Usage Record, Session aggregate

**Session Model**:
The LLM model identifier (e.g., `claude-sonnet-4-20250514`) recorded in a
Session Context row. Set by OpenCode at session start. Surfaces in the
agent-runs list Model column and the Session Context section of the detail
overlay. When absent, the column renders as `—`.

The Session Context row is the **canonical source** for "which model did a
session use". The `sessions` aggregate table deliberately carries no model
column — a second denormalized copy would drift from this source of truth.
The sessions endpoint (`SessionSummary`) therefore has no model field; if
one is ever needed it must be derived via LEFT JOIN to the Session Context
row, the same way the agent-runs list derives it.
_Avoid_: Session Model on the sessions aggregate, Session Model on a Usage Record

**Todo Snapshot**:
The latest observed set of OpenCode `todo` rows for an external session,
read from the source SQLite database and stored by the Gateway for agent
run reporting. Todo Snapshots summarize planned, in-progress, completed,
and cancelled work items; they are not event timelines.

**Agent Run Summary**:
A Gateway API/view model that answers what happened during an OpenCode
agent or subagent session using Session Context, Todo Snapshots, project
snapshots, parent/child session relationships, and usage aggregates. Date
range filters include runs whose activity window overlaps the selected
period, not only runs whose latest message falls inside it. The activity
window runs from `first_message_at` through `last_message_at`; a missing
`last_message_at` does not exclude a run that started before the selected
period ends. Date-only range boundaries are calendar-day boundaries: the
selected `To` date includes the full selected day. It is a summary view, not
a replay of OpenCode events or message parts.
_Avoid_: Event Timeline, transcript replay

**Agent Usage**:
An Aurora Glass aggregate view that groups observed token usage by the
recorded OpenCode agent identity. The set of rows is dynamic: every observed
agent may appear, with missing identities grouped as `unknown`. Agent Usage
uses the shared dashboard date range and aggregate filters. It summarizes
usage across runs; it is not an Agent Run list. Rows show a compact token
breakdown, estimated cost, and request count, ordered by total token usage
descending with agent name as the tie-breaker.
Agent grouping is resolved at read time, so usage first observed without
context may appear under `unknown` and move to the recorded agent after
Session Context is ingested.
Agent Usage is independently refreshable from other dashboard panels; a
failure leaves the rest of Aurora Glass usable and may preserve the last
successful panel data with a stale/error indication.
_Avoid_: fixed agent categories, Agent Run Summary

**Execution Transcript**:
The reconstructed, chronologically-ordered stream of message and part
records across a session and its descendant subagent sessions. The
observability answer to "what did this run actually do". It is an event
timeline, not an accounting summary. Exposed read-only via the
`/api/v1/execution` endpoints (ADR 0016).
_Avoid_: replay blob, transcript (in the usage-aggregate sense)

**Observed Message**:
A Gateway-owned row (`observed_messages`, migration 0029) projecting one
OpenCode `message` row: its identity, session linkage, role/agent/mode
metadata, cost/token facts, and parent linkage, with the full
`message.data` payload preserved verbatim (redacted) in a JSONB column.
Keyed by `(client_id, source_database_id, external_message_id)`.
_Avoid_: Usage Record (those are accounting facts, not transcript rows)

**Observed Part**:
A Gateway-owned row (`observed_parts`, migration 0029) projecting one
OpenCode `part` row: its identity, owning message and session, an
explicit Transcript Event Type, and the full `part.data` payload
preserved verbatim (redacted) in a JSONB column. A tool part is an
Observed Part whose event type is `tool`.
_Avoid_: event (ambiguous with the deferred OpenCode `event` table)

**Observed Tool Call**:
A normalized, Gateway-owned projection (`observed_tool_calls`, migration
0029) of the tool-call facts extracted from an Observed Part whose event
type is `tool`: tool name, status, and truncated input/output. It is a
derived query surface over `observed_parts`, not an independent source of
truth.
_Avoid_: tool call row (when the verbatim part payload is meant)

**Transcript Event Type**:
The explicit, first-class category of an Observed Part, normalized from
`part.data.type` (`text`, `reasoning`, `tool`, `step-start`,
`step-finish`, plus unknown future values). It is the transcript-slice
counterpart of the usage-slice token categories: a queryable dimension,
never an opaque blob.
_Avoid_: part type (when the normalized column is meant)

**Transcript Timeline**:
A unified, chronologically-ordered stream of Observed Parts across a
root session and its descendant subagent sessions, each event annotated
with its owning session and generation depth. It is the API view
(`GET /api/v1/execution/sessions/{session_id}/timeline`) that
reconstructs "what happened across the whole run".
_Avoid_: unified transcript (ambiguous), message timeline (messages only)

**Source-Created Ordering**:
The ordering rule behind "most recent" in Aurora Glass usage views: rows
are ordered by when the underlying activity happened at the source, not by
when the Gateway ingested it. The Records view sorts by
``source_created_at`` (``COALESCE(source_created_at_tz, reported_at)``,
the backend's default sort option; ``ingested_at`` is an explicit opt-in);
the Sessions and Agent Runs views order by the source-created
``last_message_at`` (``DESC``, nulls last for Agent Runs). A message
delivered late can therefore appear above earlier-ingested but genuinely
newer activity. _Avoid_: ingest-time ordering, "most recently ingested"

**Queued Agent Run**:
An Agent Run that is known to the Gateway but has not yet produced its first
observed message or usage activity. Before observed activity exists, date
range filtering anchors the run on its creation or enqueue timestamp; once
activity exists, normal Agent Run activity-window overlap applies.
_Avoid_: Pending session, empty run

**Run Status — Completed**:
A best-effort terminal status derived by the gateway from available
quiet/activity heuristics. Because OpenCode exposes no authoritative
terminal success signal, "completed" is not an upstream-proven success
proof — it means the session has recently fallen quiet (beyond the running
threshold but within the stale threshold), has no parent dependency, and
has recorded messages (see ``_compute_status``). Inactivity alone must
never be confused with upstream-proven success; it is a heuristic. Distinct
from Blocked (intentional wait), Stale (lost liveness), Timed Out (expired
budget), and terminal failures like Failed or Cancelled.

**Run Status — Running**:
A run that is actively executing with trusted, recent liveness evidence —
heartbeats, output, or other forward-progress signals.

**Run Status — Blocked**:
A run that is intentionally paused awaiting an external condition
(dependency, human input, resource). The inactivity is deliberate and
tracked, not presumed. A Blocked run may return to Running when the
condition resolves.

**Run Status — Stale**:
A run whose liveness is no longer trusted without any terminal signal
(success, error, cancellation, or timeout). The process or response stream
has gone silent, or its heartbeats cannot be verified. It occupies the
extended-quiet band between the stale threshold and the unknown threshold —
recent enough to not yet be "unknown", but quiet long enough that the
"completed"/"blocked" classification is no longer trusted. Stale is a valid
direct user-facing status label in observability contexts. It represents
a gap in observability, not a known termination reason. A stale run may
transition back to Running if it resumes producing output. Distinct from
Blocked (intentional tracked wait) and Timed Out (expired budget).

**Run Status — Timed Out**:
A terminal unsuccessful status. The run exceeded its configured maximum
duration, regardless of whether it was making progress or blocked.
Distinct from Stale (non-terminal, observability gap) and Blocked
(intentional tracked wait).

**Token Category**:
One of the distinct usage-token buckets reported by OpenCode: input tokens,
output tokens, cache read tokens, and cache write tokens. Gateway summaries
should preserve these categories instead of collapsing them into a single
ambiguous token total.
_Avoid_: Tokens when the category matters

**Active Tokens**:
The primary token total shown in Gateway summaries (e.g. Sessions table
and aggregate views), calculated as input tokens plus output tokens.
Active Tokens intentionally exclude cache read and cache write tokens so
cache activity does not obscure new model work.
_Avoid_: Total Tokens when cache categories are also visible

**Uncached/Output Tokens**:
The fresh model input and output tokens shown for one Aurora Glass summary
row. Aurora Glass displays these as `{input} in | {output} out` on a single
line, separated by a pipe.

Input and output are independent provider-reported counters. "Uncached" is
no longer displayed as a derived value — use the raw input token count.
_Avoid_: Active, Total Tokens, "uncached" derived value in display

**Cache Activity**:
The token activity related to prompt caching, represented by cache read
tokens and cache write tokens. Cache Activity should be displayed alongside
Active Tokens, not merged into Active Tokens.
_Avoid_: Cached Tokens when read/write direction matters

**Average Cache Read Per Call**:
Cache read tokens divided by the row's reliable call or message count. Used
only when a view explicitly asks for per-call normalization. It is not the
default compact Token Breakdown for Sessions or Agent Runs.
_Avoid_: Cache read total when a reliable denominator exists

**Cache-Hit Input**:
The cached prompt/input tokens reused by the model, derived from cache read
tokens. Aurora Glass displays this as `{cr} cache read` on the optional
third breakdown line. Cache-Hit Input is displayed alongside (not inside)
input/output.
_Avoid_: Cached output, cache hit output, nesting cache read inside input

**Cache-Write Input**:
The prompt/input tokens written into the provider cache, derived from cache
write tokens. Aurora Glass displays Cache-Write Input on the optional third
breakdown line, omitted when zero, appended to cache read when both present.
_Avoid_: Cache write output, write 0, showing cache line when both are zero

**Cumulative Cache Activity**:
Raw cache read and cache write token totals for a row. Compact Token
Breakdowns for Sessions and Agent Runs use the row's raw cache read value as
**Cache-Hit Input**, not an averaged value.
_Avoid_: Average cache read in compact row displays

**Session Cache-Write Total**:
The session-level aggregate of cache write tokens
(`sessions.total_cache_write_tokens`), incremented at ingest and
corrected from raw usage records when it drifts (see
`scripts/backfill_cache_write_tokens.py`). Raw usage records are
authoritative; the aggregate is the value to correct on disagreement.
_Avoid_: Cache write total without a defined session scope

**Token Breakdown**:
A compact per-title presentation of usage tokens for a session or run.
Aurora Glass summary rows display a flat two-line breakdown with an
optional cache line:

{total} total
{input} in | {output} out
{cache_hit} cache read + {cache_write} cache write

The cache line is shown only when at least one of cache_read or cache_write
is non-zero:
- Both zero → cache line omitted entirely
- Only cache_read > 0 → `{cr} cache read`
- Only cache_write > 0 → `{cw} cache write`
- Both > 0 → `{cr} cache read + {cw} cache write`

Where:
- `total = input + output + cache_read + cache_write`
- The four counters are independent provider-reported values, not a nested breakdown of input
- Cache values are siblings of input/output under total, not subsets of input

Token numbers use Aurora Glass compact number formatting (e.g. 66.1K, 1.0M).

_Avoid_: "in = uncached + cached" line, derived "uncached" value,
  three-line-always format, per-call averaging

**Agent Run Token Display**:
The per-run token presentation in the Agent Runs table. Uses the same
Token Breakdown format as Sessions — delegates to the shared
`fmtTokenBreakdownCompact` formatter. Displays:

{total} total
{input} in | {output} out
{cache_hit} cache read + {cache_write} cache write

Cache line follows the same conditional visibility rules as Token Breakdown.
No headline label (active/uncached/output), no per-call averaging.
_Avoid_: "active" or "uncached/output" headline, per-call averaging,
  one-line format, always-show-cache line

**Project Label**:
The human-readable project value shown in Aurora Glass wherever usage is
grouped by project. Resolved from source project metadata in this order:
display name, name, workspace directory basename, external project ID, then
`unknown`. Project Label is display-only and must not be treated as project
identity.
_Avoid_: Project ID when referring to a display label

**Canonical Client Name**:
The per-deployment identity label under which multiple per-workspace client
registrations are aggregated for reporting. Per-workspace clients — one
registration per AWX workspace — share a canonical name so their usage rolls
up under a single deployment identity; a client without a canonical name
reports under its own name. The canonical name is assigned manually per
client; the relationship between a workspace client's own name and its
canonical name is a deliberate mapping, not a derived transformation.
Aggregation applies ``COALESCE(canonical_name, name)`` at read time, so
changing a canonical name never requires a table recompute.
_Avoid_: Client alias, merged client

**Client Project Rollup**:
A pre-aggregated read-model of the canonical ``usage_events`` table keyed by
``(client_id, project_id, day)``, storing only additive token and cost totals
(input, output, cache read, cache write tokens plus estimated cost). It is a
derived convenience, not the source of truth: it is maintained in the same
atomic ingest transaction as the canonical event write, moved by the same
replay-merge deltas the reconciliation layer computes, and corrected against
``usage_events`` when it disagrees (ADR 0015). Backfilled by
``scripts/backfill_client_project_rollup.py``, which recomputes rows from
``usage_events`` with the same additive math. Session counts and model counts
are not part of the rollup and are computed from raw records.
_Avoid_: Aggregated table, rollup view, source of truth, raw-record-backed rollup

**Project Rollup Key**:
The stable project identifier used as the key of the Client Project Rollup
(``project_id`` in ``(client_id, project_id, day)``), distinct from the
volatile human-readable display label. The Project Label is resolved at read
time from source project metadata; keying on the label string would fragment
the table when labels change.
_Avoid_: Project Label, project name (when identity is meant)

**Hybrid Aggregates Read**:
The client-project usage aggregation strategy: the ``client,project``
dimension reads the pre-aggregated Client Project Rollup table, while every
other aggregate dimension (model, session, day, week, month) keeps scanning
raw ``usage_events``. ``COALESCE(canonical_name, name)`` is applied at read
time on both paths, so a canonical name change never requires a table
recompute.
_Avoid_: Rollup-only reads, rollup for every dimension

**Drilldown State**:
The user's current expanded/collapsed view within an Aurora Glass summary,
such as which Client rows are expanded in the Client / Project Usage
Breakdown. Auto-refresh may update values but should not discard Drilldown
State while the query context remains the same.
_Avoid_: Refresh state, table state

**Admin API Key**:
The `GATEWAY_API_KEY` environment variable. A master bearer token used by
the `ApiKeyMiddleware` to protect ALL non-`/health` routes. Also serves
as a bootstrap collector token when its SHA-256 hash is inserted into
`collector_credentials`.
_Avoid_: gateway API key, master token

**Collector Credential**:
A row in the `collector_credentials` table, owned by an OpenCode Client.
Stores a SHA-256 `token_hash` of the collector's bearer token. Validated
by the `require_collector_token` dependency on `/ingest`. A client may
have multiple credentials (tokens). Each credential tracks `last_used_at`
and supports revocation.
_Avoid_: collector token row, API key row

**Two-Layer Auth**:
The auth model protecting the Gateway: (1) `ApiKeyMiddleware` checks
every request (except `/health`) against `GATEWAY_API_KEY`;
(2) `require_collector_token` (on `/ingest` only) additionally looks up
the token hash in `collector_credentials`. A collector's bearer token
must pass BOTH layers — either by using the Admin API Key itself (with
its hash registered in `collector_credentials`), or by using a
provisioned collector token that also matches `GATEWAY_API_KEY`.

**Operator Token**:
The `GATEWAY_OPERATOR_TOKEN` environment variable. A dedicated operator
bearer token, DISTINCT from the Admin API Key (`GATEWAY_API_KEY`) and from
per-client Collector Credentials, that gates operator-only read surfaces
(delivery payload, DLQ) via the `require_operator_token` dependency. It is
transported in the dedicated `X-Operator-Token` header on operator-only read
requests — never `Authorization`, which carries the Admin API Key — so the
two credentials are distinct and both gates are satisfiable on the same
request. An empty operator token fails closed — no operator-only surface is
reachable (no broad read). Delivery payload and state trails are readable
**only** through the operator-gated Reporting Read API (issue #484, ADR 0021), which
is the sanctioned read path for those tables; every other route touching
them is write-only. The Admin API Key does not satisfy the operator gate;
the three credential types are never shared across pipelines.
_Avoid_: admin key, collector token (when the operator role is meant)

**Retention Tier**:
One of the configurable data-lifecycle buckets for the AFK outcome +
reporting read-model (issue #483, ADR 0022), declared on Settings and
env-driven via `GATEWAY_RETENTION_*`:

* **Aggregates** (`afk_runs`, `afk_run_sessions`) — indefinite (`0` days =
  never swept).
* **Metadata** (`engineering_events`, `delivery_log`,
  `delivery_state_trails`, `afk_run_entities`, `unresolved_correlations`)
  — 12 months (365 days).
* **Redacted payload storage** (`reporting_deliveries.payload` and the
  `engineering_events.payload` redacted projection) — 90 days.
* **DLQ operational max** (`afk.events-dlq`) — 30 days, never unbounded.

Retention boundaries use strict ordering: a row/message exactly at the
cutoff edge is retained; only strictly-older data expires; unknown-age data
(no timestamp) is never prematurely expired.
_Avoid_: a single monolithic retention window, retention without a tier

**Usage Record Consumer**:
A companion container that reads JSON-serialised usage records from the
``opencode-usage`` Kafka topic and POSTs each one to the Gateway's
``/ingest`` endpoint. Runs as a separate Kubernetes Deployment alongside
the Gateway — it is not part of the Gateway API process.
_Avoid_: Kafka consumer, ingestion bridge (in generic sense)

**Dead Letter Queue (DLQ)**:
The ``opencode-usage-dlq`` Kafka topic where the Usage Record Consumer
sends messages that cannot be processed — invalid payloads (Pydantic
validation failure) or requests that received a 4xx response from the
Gateway ingest endpoint. DLQ messages include the original payload and a
reason string describing the failure.

**DLQ Operational Max**:
The bound that keeps the AFK outcome DLQ topic (`afk.events-dlq`) from
growing unbounded (issue #483, ADR 0022). Every DLQ record is stamped with
`dead_lettered_at` and `max_age_days` at producer time; records strictly
older than `GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS` (default 30 days) are
**escalated** by the DLQ sweep (`python -m app.consumer.afk_consumer
--dlq-sweep`) to `afk.events-dlq-expired`, preserving the original payload
+ reason + `dead_lettered_at` + a deterministic `escalation_key` +
`escalation_reason` for operator resolution. Escalation records are
content-stable: the `escalation_key` is a SHA-256 natural key over the DLQ
record's own identity, so the same record always escalates to an identical
record (deduplicable by content). The sweep commits consumed offsets in
write mode — per partition at its first retained (not-yet-expired) record's
offset, or past the last consumed record when none are retained — so re-runs
do **not** re-escalate already-escalated records, while retained records are
re-examined on later runs until they age past the operational max; dry-run
never commits. Physical removal is enforced by the DLQ topic's Kafka
retention configured to the same max age. A record without a usable
`dead_lettered_at` has unknown age and is retained. "Never unbounded" is
enforced per-DLQ: the operational-max sweep covers only `afk.events-dlq`; the
reporting DLQ (`afk.events-reporting-dlq`, issue #479) has no operational-max
sweep and is out of #483's scope.
_Avoid_: unbounded DLQ growth, silently dropping poison messages

**Canonical Event**:
A row in the ``usage_events`` table (migration 0021) — the canonical
accounting event for one logical collector record, keyed by
``(canonical_source_identity_id, source_record_id)``. Written at ingest time
for genuinely-new records, corrected by a Replay Merge when a later delivery
carries authoritative non-null values, and never deleted by normal replay.
The usage query endpoints (``app/api/usage.py``) read from ``usage_events``;
the legacy ``opencode_usage_records`` table is still written but is no longer
the query source. The canonical row is selected per duplicate group as the
earliest ``first_ingested_at`` (lowest ``id`` tiebreaker).
_Avoid_: Usage Record (in canonical context), raw record, duplicate row

**Canonical Source Identity**:
A row in ``source_identities`` mapping a collector's source ID
(``collector_source_id``) to a client-owned identity UUID, resolved at ingest
time by ``resolve_canonical_identity()``. An identity resolved into a parent
links to it via ``canonical_parent_id`` and its records then attribute to the
parent. Overlapping identities are quarantined until resolved.
_Avoid_: Collector identity, source database identity (ambiguous)

**Source Identity Quarantine**:
An active row in ``source_identity_quarantine`` recording that a source
identity's records overlap an existing identity (``overlap_count`` shared
source record IDs). While a quarantine is active, the overlapping identity's
records route to the ``quarantined`` ingest outcome and no canonical event or
session aggregate change is made. Quarantines are listed via
``GET /admin/quarantined-identities`` and cleared via
``POST /admin/resolve-source-identity``, which links the identity to a
canonical parent and records the decision in ``source_identity_resolutions``.
_Avoid_: Blocked identity, suspended collector

**Ingest Attempt**:
A row in ``usage_ingest_attempts`` recording each delivery of a record
processed through the canonical layer — the original JSONB payload, the
resolved canonical source identity, the canonical event (when one exists), the
per-record outcome, and the optional ``replay_id``. Attempts are the audit
trail of replay-safe ingest: ``duplicate``, ``quarantined``, and ``conflict``
deliveries are recorded as attempts even though no canonical event changes.
_Avoid_: Ingest log line, delivery row

**Ingest Outcome**:
The per-record result status returned by ``/ingest``:
``accepted`` — a new canonical event was created;
``duplicate`` — idempotent replay, no event modification;
``updated`` — a Replay Merge reconciled the stored canonical event and
delta-adjusted session aggregates;
``quarantined`` — the source identity has an active quarantine or was newly
quarantined for overlap;
``conflict`` — the canonical event is owned by a different, unresolved
identity (cross-identity conflict);
``rejected`` — validation failure or internal error.
All outcomes are 2xx at batch level so the Usage Record Consumer commits
Kafka offsets; only invalid payloads and 4xx/5xx responses route to the DLQ.
_Avoid_: The legacy accepted/rejected/conflict-only vocabulary

**Replay Merge**:
The reconciliation rule applied at ingest when a canonical event already
exists for ``(canonical_source_identity_id, source_record_id)``
(``app/core/reconciliation.py``, ADR 0012). It is the canonical-event
counterpart of the legacy usage-record Replay Merge (ADR 0011). A losing
replay is neither re-appended (double-count) nor blindly overwritten:
- **Non-null collector values are authoritative.** A replay carrying a
  non-null value different from the stored event corrects the event toward
  the collector's latest observation and moves the owning session aggregate
  by the per-field delta (``new − old``) — the aggregate is delta-adjusted,
  never re-incremented, so replay cannot double-count.
- **Omitted/null collector values produce a zero delta (no erasure).** A
  replay that lacks a field can never erase a populated value; numeric zero
  is a valid observed value and is never treated as missing. Text enrichment
  (``provider``, ``mode``, ``finish_reason``) is COALESCE-filled without
  erasing.
- **Session totals are clamped to zero** so no negative total is ever
  written. ``reasoning_tokens`` deltas correct the event but are not applied
  to the session (the ``sessions`` table carries no reasoning aggregate).
- **Concurrent deliveries are serialised** with a transaction-scoped advisory
  lock (``pg_advisory_xact_lock``) covering the read-compute-write sequence;
  a second delivery blocks until the first commits, then re-reads
  (re-read-after-commit) and resolves to ``duplicate`` or ``updated``.
- Outcomes: ``duplicate`` when all deltas are zero (no UPDATE issued);
  ``updated`` when the event and/or session aggregate were adjusted. The
  legacy ``opencode_usage_records`` path keeps the fill-absent COALESCE rule
  of ADR 0011 unchanged.
_Avoid_: Overwrite-on-replay, replay append (double-counting), uncached/derived
merge on base totals

**AFK Run**:
The aggregate root of the AFK outcome read-model (``afk_runs`` table,
``afk_outcomes.models.AFKRun``). Represents one reconstructed unit of AFK
engineering work — the sessions that performed it, the engineering entities it
touched, the deterministic correlations linking them, and the resulting
EngineeringOutcome. Reconstructed by the CorrelationEngine from a session seed
plus a window of Engineering Entities/Events, and keyed by a newly-assigned
``afk_run_id`` ULID.
_Avoid_: Outcome record, engineering task (generic)

**afk_run_id**:
The Gateway-owned ULID primary key of an AFK Run (``afk_runs.afk_run_id``),
assigned at reconstruction time by the resolver. The run carries no
pre-existing identifier from the provider.
_Avoid_: Run ID (ambiguous — collides with agent-run session IDs)

**RunStatus**:
The lifecycle status of an AFK Run (``afk_outcomes.models.RunStatus``):
``running``, ``completed``, ``blocked``, ``stale``, ``timed_out``, ``failed``,
``cancelled``. Mirrors the Agent Run Run Status vocabulary but is a separate
enum stored on ``afk_runs.status`` — never conflate it with the agent-run
``_compute_status`` heuristic or with EngineeringOutcomeStatus.
_Avoid_: Reusing the Agent Run status heuristic, conflating with outcome status

**Engineering Entity**:
A provider-independent, stable reference to one engineering artifact
(``afk_outcomes.models.EngineeringEntity``): an issue, change_request, commit,
review, or merge_event. ``entity_id`` is the provider-scoped stable identifier
(e.g. ``issue:437``, ``change_request:442``); other fields are descriptive
metadata, never identity.
_Avoid_: Provider object, GitHub issue (provider-specific)

**Engineering Event**:
A timestamped observation about an Engineering Entity
(``afk_outcomes.models.EngineeringEvent``), e.g. ``opened``, ``closed``,
``merged``, ``review_submitted``. Consumed from the provider-events topic or
pulled by the provider adapters.
_Avoid_: Webhook payload (in the canonical sense), raw provider event

**change_request**:
The provider's pull/merge request, normalized as an Engineering Entity with
``entity_type = change_request`` (e.g. ``change_request:442``). The owning
change request anchors an AFK Run: the CorrelationEngine binds it by exact
title match and extracts its body's resolved/mentioned issue references into
the EngineeringOutcome. Its branch commits and reviews surface as lineage
links inheriting its confidence (see correlation_source /
owning_change_request_id).
_Avoid_: PR/MR (provider-specific), pull request (in generic sense)

**EngineeringOutcome**:
The resolved engineering result of an AFK Run (``EngineeringOutcome``):
``status`` (EngineeringOutcomeStatus: ``merged``/``closed``/``abandoned``/
``open``), ``change_request_ids``, ``resolved_issue_ids``, ``merge_event_id``,
``merged_at``. Aurora Glass renders ``open`` as "still open" and treats
``failed`` as a RunStatus, never an outcome status.
_Avoid_: Outcome status only, PR state

**Correlation**:
A deterministic link between an AFK Run and one Engineering Entity
(``afk_outcomes.models.Correlation``), produced by the CorrelationEngine.
Every derived link records ``correlation_method`` (the rule that produced it),
``correlation_confidence``, evidence with source identifiers, and
``resolver_version``. Persisted on ``afk_run_entities`` /
``unresolved_correlations``.
_Avoid_: Match, guess

**correlation_confidence**:
The 0.0–1.0 confidence of a derived link. Values ≥ 0.5 resolve to a
``resolved`` role; 0.1 is ``referenced``; 0.0 is ``noise``. Aurora Glass
renders it as a percentage (e.g. ``100%``, ``10%``).
_Avoid_: Score, probability

**correlation_method**:
The deterministic rule that produced a Correlation. The five rules run in
descending confidence order with first-lock-wins: ``explicit_run_id``,
``issue_reference``, ``branch_issue_reference``, ``commit_issue_reference``,
``temporal_inference``.
_Avoid_: Reason (generic), source

**resolver_version**:
The version of the CorrelationEngine that produced a derived link (currently
``"2"``), recorded on every Correlation, link, and UnresolvedCorrelation so
rule-semantics changes can be detected downstream.
_Avoid_: Schema version, migration version

**correlation_source**:
The provenance of an entity link on an AFK Run (``afk_run_entities.correlation_source``,
migration 0028): ``direct`` for links produced by the correlation rules
(Correlations and noise), or ``owning_change_request`` for **lineage links** —
commits and reviews carried on the owning change request's branch, which
inherit the owning change request's confidence instead of a fixed weak
inference. Surfaced on the AFK Outcomes REST API entity links and entity rows.
_Avoid_: Correlation method (the rule), data origin

**owning_change_request_id**:
The provider-scoped external id of the change request whose branch carries an
Engineering Entity (e.g. ``"442"`` for ``change_request:442``), set at fetch
time by the provider adapters for commits and reviews and persisted via
migration 0028. An entity whose ``owning_change_request_id`` matches the owning
change request surfaces as a lineage link inheriting the owning change
request's confidence. ``None`` for the owning change request itself and for
non-commit/review entity types.
_Avoid_: The full provider-scoped entity id (``change_request:442``), branch name

**Provisional Link**:
A derived link marked ``provisional`` — an entity link whose role is not
``resolved`` (i.e. ``referenced`` or ``noise``), or a session link marked
``inferred``. Provisional links are visibly distinguished in Aurora Glass and
never silently conflated with explicit resolved links.
_Avoid_: Uncertain link, best-effort link (ambiguous)

**Unresolved Correlation**:
A correlation the resolver could not deterministically establish
(``unresolved_correlations`` table): ``ambiguous`` (competing candidates with
no higher-confidence tie-breaker) or ``unmatched`` (no rule produced a link).
Never random-tiebroken. Exposed via the AFK Outcomes REST API with
``provisional=true``.
_Avoid_: Blocked link, pending match

**AFK Outcome Consumer**:
A companion Kafka consumer (``app/consumer/afk_consumer.py``) that reads the
external provider-events topic — ``engineering.events.normalized`` after the
topic split (ADR 0023), previously ``afk.events`` — in its OWN consumer group
(``opencode-outcomes`` — never the usage consumer's ``opencode-gateway``
group), maps message types to canonical Engineering Events, writes each
message in a single DB transaction (offset committed only after success), DLQs
poison messages, and runs scheduled bounded-window reconciliation reusing the
backfill engine for terminal states (merged/closed) the topic does not carry.
_Avoid_: Kafka consumer (generic), outcomes ingestion bridge

**AFK command** (alias: actionable event):
A derived event published to the Kafka topic ``afk.events`` that requests work
to be done ("do something"). Uses the EDA ``event_type`` vocabulary
(``label``, ``review_request``, ``developer_request``, ``review_verdict``,
``pr_mr_opened``, ``container_upgrade_requested``). Produced only when a
webhook triggers an AFK action; never carries raw webhook payloads.

**Normalized engineering lifecycle event** (alias: observable event):
An unconditional lifecycle observation published to the Kafka topic
``engineering.events.normalized`` ("something happened"). Produced for every
qualifying webhook regardless of whether it triggered an AFK action.

**Event fan-out rule**:
A single webhook may produce both an AFK command and a normalized engineering
lifecycle event, independently. An AFK-labelled issue produces two records
(one command + one observation); human issues/PRs/MRs produce one record
(observation only).

**linked_issues**:
The list of issue numbers extracted by the FastAPI EDA Gateway from a PR/MR
title and description using regex matching (e.g. "Implemented issues #503",
"Closes #494") and included in the normalized ``change_request.opened``
observation to capture the issue↔change-request relationship for correlation.
The regex pattern matches ``#\d+`` references in the title and description
body. Empty for PRs/MRs that reference no issues.

**engineering.events.normalized**:
The Kafka topic carrying normalized engineering lifecycle events
(observations), consumed by the AFK Outcome Consumer after the topic split
(ADR 0023).
_Avoid_: afk.events (that is the actionable-command topic)

**Normalized Provider Event**:
A schema-versioned, provider-agnostic event on the provider-events topic
emitted by the producer (`fast-api-eda-gateway`, issues #97–#102). It is the
**only** accepted wire contract for the AFK Outcome Consumer (issue #497).
The v1 envelope is nested: resource fields live in a ``resource`` object
(``type``, ``repository_url``, ``number``) and the payload reference lives in
a ``redacted_payload.reference`` object (``provider``, ``delivery_id``).  The
flat shape has been removed.  The event carries the producer's native
resource vocabulary (``issue``, ``pull_request``, ``merge_request``) — never
the outcome layer's canonical ``change_request`` vocabulary — plus
``event_type`` (always ``"normalized"``), a forwarded ``delivery_id``,
``action``, ``occurred_at``, ``ingested_at``, and ``actor``.  Actions are
constrained to the producer lifecycle allowlist: ``issue``
opened/edited/reopened/closed; ``pull_request`` opened/edited/reopened/closed/
merged; ``merge_request`` opened/updated/reopened/closed/merged.  ``edited``
(GitHub) and ``updated`` (GitLab) converge on the canonical ``updated`` event
type, with the source action retained as provenance.  Repository identity is
the normalized producer repository URL (``normalize_repository_url``).
_Avoid_: Legacy provider event (the flat ``ProviderEventMessage`` shape has been removed)

**Mapping Bridge**:
The AFK Outcome Consumer's ``map_provider_event`` / ``map_normalized_event``
that bridges a **Normalized Provider Event** into the outcome layer's
canonical vocabulary (ADR 0020, superseded by FastAPI EDA Gateway ADR 0005):
``issue`` → ``issue``; ``pull_request`` and ``merge_request`` →
``change_request``.  ``action`` maps to the canonical event-type suffix
(``edited``/``updated`` → ``updated``), and the result is validated against
the locked canonical vocabulary (``_CANONICAL_EVENT_TYPES``).  Validation
rejects unsupported schema versions, event types, resource types, actions,
invalid repository identities, and payload-reference mismatches — each with a
distinct DLQ reason.  The producer owns the normalized-event contract; the
consumer-authored flat ``ProviderEventMessage`` shape has been removed
(issue #497).
_Avoid_: Conflating ``pull_request``/``merge_request`` with ``change_request``

**AFK Backfill CLI**:
The operator CLI ``scripts/afk_backfill.py`` that pulls a bounded window of
engineering activity from a provider adapter, runs it through the
CorrelationEngine against Gateway sessions, and persists resolved runs
idempotently. ``--dry-run`` prints match/unmatched/ambiguous counts and
optional per-match evidence without writing. Backfill is CLI-only — the AFK
Outcomes REST API is strictly read-only.
_Avoid_: Backfill script (generic), reconciliation daemon

**AFK Outcomes REST API**:
The read-only API surface for the AFK outcome read-model
(``app/api/afk_outcomes.py``, prefix ``/api/v1/afk-outcomes``):
``GET /runs`` (filterable by repository, window, status, outcome, origin;
paginated), ``GET /runs/{afk_run_id}`` (full chain with per-link provenance),
``GET /entities``, ``GET /correlations``. Uses the ``{status, data, error}``
envelope and API-key auth. Never writes — backfill remains CLI-only.
_Avoid_: AFK API (generic), outcomes endpoint

**AFK Outcomes Tab**:
The Aurora Glass dashboard tab (top-nav "AFK Outcomes") that lists AFK Runs
and opens the chain detail overlay for one run. Follows the shared panel
conventions (freshness/stale-on-error retention, Token Breakdown, Active
Tokens).
_Avoid_: AFK panel (generic)

**Session Resource Reference**:
An explicit stable resource reference carried by one session's metadata
(``afk_outcomes.models.SessionResourceReference``): the full stable resource
identity (``provider``, ``repository``, ``resource_type``, ``resource_number``)
plus the session identity and the ``source_field`` that carried it. It is the
ONLY input to the exact association resolver. It carries no timestamps,
windows, or scores, so the resolver structurally cannot temporally or
heuristically infer a link from it.
_Avoid_: Usage record, heuristic hint, temporal window

**Exact Resource↔Session Association**:
A deterministic many-to-many link between one engineering resource and one
OpenCode session (``ResourceSessionAssociation``, migration 0034). One
resource may link to many sessions and one session may link to many
resources. Associations derive ONLY from explicit **Session Resource
Reference**s — never from temporal or heuristic inference — and every
association records its **Reference Source** (which session field carried the
link), so each link is provable and reproducible. Repeated identical
references converge to a single association (idempotent, no duplicates);
re-observation advances ``last_seen_at`` while the ``source_reference``
provenance stays write-once.
Associations deliberately carry no completion/finished claim (PRD
Implementation Decision 13).
_Avoid_: correlation, resolved/referenced link, run↔session link (that is the
AFK Run's provisional inferred attachment)

**Reference Source**:
The provenance recorded on every **Exact Resource↔Session Association**
(``afk_outcomes.models.ReferenceSource``): the name of the session metadata
field that carried the stable resource reference (``field``) and the value
found there (``detail``). Together they make a link reproducible — re-reading
that field yields the same resource. It is the association-path counterpart of
the correlation engine's ``correlation_method``/``evidence``, but records
*where the link came from in the session*, not a scoring rule.
_Avoid_: correlation_method, evidence (those belong to the correlation engine)

**Stable Resource Identity**:
The four-field identity of an engineering resource used to key exact
associations: ``(provider, repository, resource_type, resource_number)``.
``resource_type`` is the ``EntityType`` (issue, change_request, commit,
review, merge_event); ``resource_number`` is the provider-scoped external id
as an opaque string (issue/MR number, commit SHA, review id). It mirrors the
``EngineeringEntity`` identity (``entity_id = "<resource_type>:<resource_number>"``)
without collapsing onto the display metadata.
_Avoid_: resource number as an integer, entity_id string, project label

**Reporting Read API**:
The read-only API surface for the reporting read-model
(``app/api/reporting.py``, prefix ``/api/v1/reporting``, ADR 0021, issue
#484): ``GET /resources`` (paginated ingested resources filterable by any
subset of the stable resource identity — ``provider`` + ``repository_url`` +
``resource_type`` + ``resource_number``), ``GET /resources/detail`` (the
current aggregate plus the per-delivery State Trail plus session links for
one resource), and ``GET /session-links`` (provisional Reporting Session
Links). It is strictly read-only — the write path remains
``app/api/reporting_ingest.py`` (issue #479) — uses the ``{status, data,
error}`` envelope and API-key auth, and — because delivery payload and the
state trail are operator-only (ADR 0022) — additionally requires the
dedicated Operator Token (``GATEWAY_OPERATOR_TOKEN`` via
``require_operator_token``) on every route, on top of the Admin API Key
(fails closed when unprovisioned). It makes **no completion claims**: it
surfaces the resource's verbatim current payload and pipeline lifecycle
states and never derives or asserts a "completed"/"finished"/outcome state.
_Avoid_: Reporting endpoint (generic), completion/outcome report

**Resource Summary**:
The current aggregate for one stable reporting resource as surfaced by the
Reporting Read API: the verbatim current ``payload``, ``delivery_count``,
``last_delivery_id``, ``last_ingested_at``, and the composite ``resource_id``
key (``provider:repository_url:resource_type:resource_number``). Derived at
read time from the immutable ``reporting_deliveries`` rows until the
current-aggregate layer (#480) lands; the shape carries no completion/outcome
field.
_Avoid_: Resource report (generic), finished/outcome summary

**State Trail**:
The per-delivery lifecycle observations for one reporting resource
(``delivery_state_trails`` rows), surfaced chronologically by the Reporting
Read API as pipeline observations — ``received``, ``normalized``,
``published``, ``persisted``, ``rejected``, ``failed``. States describe the
delivery pipeline, never resource completion.
_Avoid_: Resource status, completion state

**Reporting Session Link**:
A session link surfaced by the Reporting Read API (``GET
/api/v1/reporting/session-links``; rows from ``afk_run_sessions``). Until
exact resource↔session correlation (#481) lands, every link is marked
``provisional=True`` with an empty ``source_references`` list — the Gateway
never fabricates a resource↔session link it cannot prove. When #481 lands,
exact links populate ``source_references`` and flip ``provisional=False``;
the response shape is forward-compatible.
_Avoid_: Exact link, proven session association

## Architecture Note

The Gateway uses a layered architecture:

- **app/api/** — REST endpoints
- **app/core/** — Configuration, auth, logging, factory
- **app/db/** — Postgres pool, migrations, ORM models
- **app/consumer/** — Kafka consumer bridge that reads usage records from Kafka and POSTs them to the Gateway ingest API (separate container), plus the AFK Outcome Consumer (separate container)
- **afk_outcomes/** — pure-domain AFK outcome package (models, serialization, correlation engine, provider adapters, repository Protocol) that imports nothing from `app`
- **scripts/** — operator CLIs, including the AFK Backfill CLI

Aurora Glass is related to the Gateway, but is not part of the Gateway's
service layers.

Additional layers will be added in future slices.

## Relationship with Paperclip

The Gateway does **not** replace Paperclip — they operate at different
layers. Paperclip coordinates agents and higher-level work. The Gateway
provides observability into the OpenCode infrastructure that Paperclip
manages.

## Relationships

- **Aurora Glass** consumes the **Gateway** API
- **Aurora Glass** is delivered as a separate frontend from the **Gateway** service
- **Aurora Glass** and the **Gateway** are intended to share one public origin even
  when deployed as separate containers
- An **OpenCode Client** owns **0..N Collector Credentials**, each with one token
- A **Collector Credential** belongs to exactly one **OpenCode Client**
- A **Session Context** belongs to one resolved **Internal Session ID** and is keyed by `(source_database_id, external_session_id)`
- **Session Context** is sent as a separate batch-level collection, not duplicated onto each **Usage Record**
- A **Todo Snapshot** belongs to one resolved **Internal Session ID** and is keyed by `(source_database_id, external_session_id, position)`
- An **Agent Run Summary** is composed by the **Gateway** from stored usage, context, project, todo, and hierarchy data
- An **Observed Message** belongs to one resolved **Internal Session ID** and is keyed by `(client_id, source_database_id, external_message_id)`
- An **Observed Part** belongs to one **Observed Message** and one resolved **Internal Session ID** and is keyed by `(client_id, source_database_id, external_part_id)`
- An **Observed Tool Call** is derived from one **Observed Part** whose event type is `tool` and is keyed by the same `(client_id, source_database_id, external_part_id)` as its source part
- An **Execution Transcript** is composed by the **Gateway** from **Observed Messages** and **Observed Parts** across a session and its descendant subagent sessions
- A **Transcript Timeline** unifies the **Observed Parts** of a root session and its descendants into one chronologically-ordered stream annotated with owning session and generation depth
- **Aurora Glass** presents **Agent Usage** as a dynamic aggregate grouped by recorded agent identity, using the shared dashboard date range and aggregate filters
- **Agent Usage** is distinct from the per-run **Agent Run Summary** view
- **Agent Usage** rows are ordered by total token usage descending, then agent name ascending
- **Agent Usage** uses the same compact **Token Breakdown** display as Sessions and Agent Run Summary rows
- **Agent Usage** resolves agent grouping at read time from the latest available **Session Context**
- **Agent Usage** failure is isolated from other dashboard panels and may preserve the last successful data with a stale/error indication
- **Aurora Glass** uses the same compact **Token Breakdown** vocabulary for Sessions and **Agent Run Summary** rows
- **Aurora Glass** orders usage views by **Source-Created Ordering** (Records: `sort_by=source_created_at`; Sessions and Agent Runs: `last_message_at DESC`), never by ingest time
- **Aurora Glass** treats an Agent Run or Session title as the meaningful scope for a **Token Breakdown** row
- **Aurora Glass** applies the shared dashboard date range to **Agent Run Summary** views unless an Agent Runs-specific date boundary is explicitly selected; each Agent Runs-specific boundary takes precedence for that side of the range, while unset boundaries inherit from the shared dashboard range
- **Aurora Glass** treats Agent Run status filters as additional narrowing filters on the effective date range; status filters do not define or alter the date range
- The **Admin API Key** MAY also serve as a **Collector Credential** when its hash is registered in `collector_credentials`
- The `ApiKeyMiddleware` runs before `require_collector_token` — a request must pass the **Admin API Key** check before **Collector Credential** lookup occurs
- A **Usage Record Consumer** reads from the ``opencode-usage`` Kafka topic
- A **Usage Record Consumer** POSTs to the Gateway's ``/ingest`` endpoint using a **Collector Credential**
- Unprocessable messages are sent to the **Dead Letter Queue (DLQ)** topic ``opencode-usage-dlq``
- A **Canonical Source Identity** belongs to exactly one **OpenCode Client** and is keyed by `(client_id, collector_source_id)`
- A **Canonical Event** belongs to one **Canonical Source Identity** and is keyed by `(canonical_source_identity_id, source_record_id)`
- An **Ingest Attempt** records each delivery processed through the canonical layer and references the owning **Canonical Event** when one exists
- A **Source Identity Quarantine** belongs to one **Canonical Source Identity**; while it is active the identity's records route to the `quarantined` **Ingest Outcome** with no canonical event or session aggregate change
- The usage query endpoints read from the **Canonical Event** table (`usage_events`), not `opencode_usage_records` (API contracts unchanged)
- A **Client Project Rollup** row is keyed by `(client_id, project_id, day)` and stores only additive token and cost totals, never session or model counts
- The client-project aggregate read path reads the **Client Project Rollup** (ADR 0015); all other aggregate dimensions scan **Canonical Event** rows (`usage_events`)
- Concurrent deliveries of the same **Canonical Event** are serialised with a transaction-scoped advisory lock (`pg_advisory_xact_lock`); a second delivery re-reads after the first commits
- A **Replay Merge** applies only to a duplicate **Usage Record** that passed the dedup identity check; a divergent duplicate goes to conflict instead (legacy `opencode_usage_records` path, ADR 0011)
- A **Replay Merge** fills only currently-NULL nullable enrichment fields on the stored **Usage Record** and never erases populated values (legacy `opencode_usage_records` path, ADR 0011)
- A **Replay Merge** that backfills ``cache_read_tokens``/``cache_write_tokens`` triggers a one-time repair of the owning session aggregate's derived enrichment totals (``total_cache_read_tokens``/``total_cache_write_tokens``), never its base totals (legacy `opencode_usage_records` path, ADR 0011)
- The **Replay Merge** non-erasing fill-absent principle also applies to **Session Context** and Project projections while preserving their snapshot semantics
- A **Canonical Event Replay Merge** (ADR 0012) corrects a stored **Canonical Event** toward the collector's latest non-null observation and moves the owning session aggregate by the per-field delta, never re-incrementing it
- A **Canonical Event Replay Merge** never erases a populated value: null/omitted collector values produce a zero delta, and text enrichment is COALESCE-filled
- An **AFK Run** is keyed by an **afk_run_id** ULID assigned at reconstruction time and carries one **RunStatus** and one optional **EngineeringOutcome**
- An **AFK Run** is reconstructed by the **CorrelationEngine** from a session seed plus a window of **Engineering Entities/Events**
- A **Correlation** links one **AFK Run** to one **Engineering Entity** and records **correlation_method**, **correlation_confidence**, evidence, and **resolver_version**
- An **Engineering Entity** is referenced by zero or more **Engineering Events** and may carry an entity link on zero or more **AFK Runs**
- A **change_request** anchors an **AFK Run**; its body's resolved/mentioned issue references become **resolved_issue_ids** / referenced links in the **EngineeringOutcome**
- A **change_request**'s branch commits and reviews surface as **lineage links** on the **AFK Run** (`correlation_source = "owning_change_request"`, `owning_change_request_id` = the owning change request's external id), inheriting the owning change request's correlation confidence
- An **Unresolved Correlation** belongs to exactly one **AFK Run** — `afk_run_id` is NOT NULL and part of the row identity (migration 0027), so the same entity may carry a separate unresolved row per run and evidence is never merged across runs — and is either `ambiguous` or `unmatched`
- An **AFK Outcome Consumer** reads from the external provider-events topic (`afk.events`) in its own consumer group (`opencode-outcomes`, never the usage consumer's `opencode-gateway` group)
- An **AFK Outcome Consumer** writes canonical **Engineering Events** to Postgres and reconciles terminal states via the **AFK Backfill CLI** engine
- A **Mapping Bridge** maps a **Normalized Provider Event** into the outcome layer's canonical vocabulary — `pull_request`/`merge_request` → `change_request`, `issue` → `issue` — while leaving the legacy ten-type mapping unchanged (ADR 0020)
- An **AFK Backfill CLI** run persists resolved **AFK Runs** idempotently and is the only write path for backfill — the **AFK Outcomes REST API** is strictly read-only
- The **AFK Outcomes REST API** reads from the AFK outcome tables (`afk_runs`, `afk_run_entities`, `afk_run_sessions`, `unresolved_correlations`) and is consumed by **Aurora Glass** (the **AFK Outcomes Tab**)
- The **AFK Outcomes Tab** in **Aurora Glass** renders **AFK Runs**, their **EngineeringOutcome**, per-link correlation provenance, and usage aggregates following the **Token Breakdown** / **Active Tokens** vocabulary
- An **Exact Resource↔Session Association** links one engineering resource (by **Stable Resource Identity**) to one OpenCode session and is keyed by `(provider, repository, resource_type, resource_number, external_session_id)`, written with `ON CONFLICT ... DO UPDATE SET last_seen_at = now()` so the same explicit reference never duplicates a link while `last_seen_at` tracks re-observation recency
- An **Exact Resource↔Session Association** is derived only from a **Session Resource Reference**; the `afk_outcomes.repository` `AsyncpgOutcomeRepository.save_associations` is the only writer, and no association is ever created from temporal or heuristic inference
- A **Retention Tier** groups the AFK/reporting data into aggregates (indefinite), metadata (12 months), redacted payload (90 days), and the **DLQ Operational Max** (30 days), each configurable via `GATEWAY_RETENTION_*` (ADR 0022)
- The **DLQ Operational Max** stamps every `afk.events-dlq` record with `dead_lettered_at` + `max_age_days` and escalates records strictly older than the max to `afk.events-dlq-expired` — never unbounded, never silently dropped
- An **Operator Token** gates operator-only read surfaces (delivery payload, DLQ) and is distinct from the **Admin API Key** and **Collector Credential** — no token is shared across pipelines
- The **Admin API Key** does not satisfy the operator-only gate (`require_operator_token`) — the three credential layers are disjoint
- Delivery payload and state trails are readable **only** through the operator-gated Reporting Read API (`require_operator_token` on `GET /api/v1/reporting/resources`, `/resources/detail`, `/session-links`); no other route reads `reporting_deliveries` / `delivery_state_trails` back out, and `delivery_log` / `engineering_events.payload` remain readable by no API route (ADR 0021, ADR 0022)
- The ingestion endpoint relies on the **Collector Credential** (Two-Layer Auth) — never the **Admin API Key** alone — and is not exposed to the public internet; producer webhook ingress on the EDA gateway side is unchanged
- The **Reporting Read API** exposes the reporting read-model — ingested resources with their current aggregates (`reporting_deliveries`), per-delivery **State Trails** (`delivery_state_trails`), and **provisional Reporting Session Links** — and is strictly read-only: the write path remains the reporting ingestion endpoint (`app/api/reporting_ingest.py`, issue #479). It is the **sanctioned read path** for delivery payload and the state trail: every route is additionally gated by the **Operator Token** (`require_operator_token`) on top of the **Admin API Key**, and no other route reads those tables back out (ADR 0021, ADR 0022)
- The **Reporting Read API** makes **no completion claims**: a **Resource Summary** carries the verbatim current payload and pipeline lifecycle states, never a derived "completed"/"finished"/outcome state
- A **Resource Summary** is keyed by the composite `resource_id` (`provider + repository_url + resource_type + resource_number`), the stable resource identity distinct from any human-readable label
- A **Reporting Session Link** is marked `provisional=True` with an empty `source_references` list until exact resource↔session correlation (#481) lands — the Gateway never fabricates a link it cannot prove

## Flagged Ambiguities

- "frontend layer inside the Gateway" was used to mean **Aurora Glass**.
  Resolved: **Aurora Glass** is a separate frontend that consumes the
  **Gateway** API.
- ``afk_events`` (underscore) is deprecated — the canonical spelling is
  ``afk.events`` (dot), confirmed against the live Strimzi cluster.
