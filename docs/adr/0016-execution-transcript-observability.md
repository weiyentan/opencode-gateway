# ADR 0016: Execution Transcript Observability — schema and API for `message` / `part` replay

## Status

Proposed

## Context

### The execution-observability gap

Issue #217 records that the Gateway today answers *summary* questions —
"how many tokens did this run consume, which model, what project, what
todo state" — from the usage aggregates (`usage_events`), the Client
Project Rollup, and the Gateway-owned projection tables introduced by
ADR 0008. It cannot answer *reconstruction* questions:

- What did a subagent actually do?
- Which tools were called, in what order, with what inputs and results?
- Why did an execution branch or fail?
- What was parent-coordinator work vs child-subagent work?
- What transcript led to a cost spike or unusual behaviour?

The underlying OpenCode runtime already stores this data in its local
SQLite database, in the `message` and `part` tables. The Gateway's
collector reads that SQLite database read-only, but nothing in the
current ingest surface collects or models `message` / `part` rows.

ADR 0008 explicitly deferred this surface: "The OpenCode `event` table,
full message transcripts, and `part` replay are out of scope for this
decision," and its Agent Run Summary section restates that "Detailed
event timelines, message transcripts, and part/tool-call replay remain
out of scope." This ADR picks up that deferred surface.

### Data available in the OpenCode runtime

Issue #217's runtime SQLite inspection established the concrete shape of
the source data:

**`message` table** (`id`, `session_id`, `time_created`, `time_updated`,
`data` JSON). Observed `message.data` fields: `role`, `agent`, `mode`,
`cost`, `tokens`, `parentID`, and path/cwd metadata. A recent 7-day
sample held 17,484 message rows (`assistant` 15,573, `user` 1,911).

**`part` table** (`id`, `message_id`, `session_id`, `time_created`,
`time_updated`, `data` JSON). This is the critical layer — `part.data`
captures step-by-step execution: prompt text, reasoning text, tool
calls, tool inputs, tool outputs, and step-start / step-finish markers.
Observed `part.data.type` values: `text`, `reasoning`, `tool`,
`step-start`, `step-finish`. Tool parts additionally carry tool name,
tool input, tool output, and tool status.

**Parent/child linkage.** The OpenCode `session` table links an
orchestrator session to child subagent sessions (e.g. a parent
`autonomous-coordinator` session delegating to `general`,
`git-workspace`, and `ansible-dev` children). The `part` rows across
those sessions are sufficient to reconstruct an end-to-end timeline.

### Why usage aggregates are insufficient

Usage records are per-message *accounting* facts (tokens, cost, model)
reconciled into canonical events and additive aggregates. They carry no
part-level event stream, no tool input/output, no reasoning text, and no
step markers. Reconstructing "what happened" from them is impossible:
there is no transcript to reconstruct. The execution transcript is a
different data model — an append-only, ordered event timeline with
parent/child structure — and must be stored, indexed, and exposed
separately.

### Terminology (new terms)

These terms are introduced by this ADR and follow the CONTEXT.md
glossary style. Existing glossary terms (Session Resolution, Internal
Session ID, External Session ID, Session Context, Canonical Event,
Client Project Rollup, Agent Run Summary) are used with their defined
meanings and are not redefined here.

**Execution Transcript**:
The reconstructed, chronologically-ordered stream of message and part
records across a session and its descendant subagent sessions. The
observability answer to "what did this run actually do". It is an event
timeline, not an accounting summary.
_Avoid_: replay blob, transcript (in the usage-aggregate sense)

**Observed Message**:
A Gateway-owned row (`observed_messages`) projecting one OpenCode
`message` row: its identity, session linkage, role/agent/mode metadata,
cost/token facts, and parent linkage, with the full `message.data`
payload preserved verbatim (redacted) in a JSONB column.
_Avoid_: Usage Record (those are accounting facts, not transcript rows)

**Observed Part**:
A Gateway-owned row (`observed_parts`) projecting one OpenCode `part`
row: its identity, owning message and session, an explicit event type,
and the full `part.data` payload preserved verbatim (redacted) in a
JSONB column. A tool part is an Observed Part whose event type is
`tool`.
_Avoid_: event (ambiguous with the deferred OpenCode `event` table)

**Observed Tool Call**:
A normalized, Gateway-owned projection (`observed_tool_calls`) of the
tool-call facts extracted from an Observed Part whose event type is
`tool`: tool name, status, and truncated input/output. It is a derived
query surface over `observed_parts`, not an independent source of truth.
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
with its owning session and generation depth. It is the API view that
reconstructs "what happened across the whole run".
_Avoid_: unified transcript (ambiguous), message timeline (messages only)

## Decision

The Gateway will add an **execution-transcript slice** that is
schematically, ingest-wise, and API-wise separate from the usage slice.
It reuses existing session identity and parent/child linkage, and
introduces three new append-oriented transcript tables plus one
normalized tool-call projection. No existing usage, ingest, or aggregate
contract changes.

### 1. Session identity and parent/child linkage are reused, not duplicated

A proposed fourth entity, `observed_sessions`, is **rejected**. The
Gateway already owns session identity and parent/child structure:

- `sessions` (the aggregate table) stores the resolved Internal Session
  ID keyed by `(source_database_id, external_session_id)`, plus an
  external `parent_session_id` string populated at Session Resolution.
- `opencode_session_contexts` (ADR 0008) stores both
  `parent_external_session_id` and a resolved internal
  `parent_session_id` UUID.

A third session table would duplicate this identity and create drift
between three copies of "which session is whose parent". The transcript
tables hang off `sessions.id` instead.

Parent/child linkage for the transcript is therefore modeled two ways,
redundantly and without duplication of identity:

- **Session-level** (authoritative for structure): the existing
  `opencode_session_contexts.parent_external_session_id` /
  `parent_session_id` pair.
- **Message-level** (reconstruction safety net): `observed_messages`
  carries a first-class `parent_external_session_id` column normalized
  from `message.data.parentID`, so parent/child structure survives a
  missing or late Session Context projection (the out-of-order case ADR
  0008 already flags).

### 2. Three transcript tables

All three tables follow the projection-table conventions established by
ADR 0008 / migration 0015: `client_id`, `source_database_id`, external
identity columns, resolved internal `session_id`, dual source
timestamps (raw millisecond `bigint` plus normalized `timestamptz`),
`first_seen_at` / `last_seen_at`, and a verbatim `data` JSONB column.
`session_id` references `sessions(id)`.

#### `observed_messages`

Keyed by `(client_id, source_database_id, external_message_id)`.

```text
id uuid pk
client_id uuid references opencode_clients(id)            not null
source_database_id uuid references source_databases(id)   not null
external_message_id text                                   not null
session_id uuid references sessions(id)                    nullable (resolved)
external_session_id text                                   not null
parent_external_session_id text                            nullable
role text                                                  not null
agent text                                                 nullable
mode text                                                  nullable
cost_usd numeric                                           nullable
input_tokens bigint                                        nullable
output_tokens bigint                                       nullable
source_created_at bigint                                   nullable (ms)
source_updated_at bigint                                   nullable (ms)
source_created_at_tz timestamptz                           nullable
source_updated_at_tz timestamptz                           nullable
first_seen_at timestamptz                                  not null
last_seen_at timestamptz                                   not null
data jsonb                                                 nullable (verbatim, redacted)
```

`role`, `agent`, `mode`, `cost_usd`, `input_tokens`, `output_tokens`,
and `parent_external_session_id` are promoted to columns because they
are filter dimensions (role, agent, mode), reconstruction linkage
(parent), or cheaply-queried facts (cost, tokens). `data` preserves the
full `message.data` verbatim — including `tokens`'s exact shape, the
path/cwd metadata, and any field not promoted — so nothing is lost.

#### `observed_parts`

Keyed by `(client_id, source_database_id, external_part_id)`.

```text
id uuid pk
client_id uuid references opencode_clients(id)            not null
source_database_id uuid references source_databases(id)   not null
external_part_id text                                      not null
message_id uuid references observed_messages(id)           nullable (resolved)
external_message_id text                                   not null
session_id uuid references sessions(id)                    nullable (resolved)
external_session_id text                                   not null
part_type text                                             not null
source_created_at bigint                                   nullable (ms)
source_updated_at bigint                                   nullable (ms)
source_created_at_tz timestamptz                           nullable
source_updated_at_tz timestamptz                           nullable
first_seen_at timestamptz                                  not null
last_seen_at timestamptz                                   not null
data jsonb                                                 nullable (verbatim, redacted)
```

`part_type` is the single promoted field — it is the Transcript Event
Type dimension that drives filtering and the tool-call projection. All
content (text, reasoning, tool input/output, step markers) stays in
`data`. Chronological ordering is `source_created_at` (the `part`
table's `time_created`), with `id` as a stable tiebreaker where source
timestamps collide.

#### `observed_tool_calls` (normalized tool-call projection)

Keyed by `(client_id, source_database_id, external_part_id)` — one row
per tool part.

```text
id uuid pk
client_id uuid references opencode_clients(id)            not null
source_database_id uuid references source_databases(id)   not null
part_id uuid references observed_parts(id)                 not null
external_part_id text                                      not null
message_id uuid references observed_messages(id)           nullable
session_id uuid references sessions(id)                    nullable (resolved)
external_session_id text                                   not null
tool_name text                                             not null
tool_status text                                           nullable
tool_input jsonb                                           nullable (truncated)
tool_output jsonb                                          nullable (truncated)
source_created_at bigint                                   nullable (ms)
source_updated_at bigint                                   nullable (ms)
source_created_at_tz timestamptz                           nullable
source_updated_at_tz timestamptz                           nullable
first_seen_at timestamptz                                  not null
last_seen_at timestamptz                                   not null
data jsonb                                                 nullable (verbatim, redacted)
```

`tool_name`, `tool_status`, `tool_input`, and `tool_output` are
normalized from the tool part's `part.data` payload. `tool_name` is the
filter dimension for "which tool". `tool_input` / `tool_output` are
stored *truncated* (see Redaction and Privacy) so the projection stays
bounded while the verbatim content remains in `observed_parts.data`.

### 3. JSONB vs first-class columns — the rule

A field is promoted to a first-class column when it is **queried as a
filter, a linkage key, or a cheap scalar fact**. Everything else — and
the *entire* raw payload — stays in the verbatim `data` JSONB column so
no source fidelity is lost. Concretely:

| Field | Location | Reason |
|---|---|---|
| `role`, `agent`, `mode` | columns | filter dimensions |
| `cost`, `tokens` | columns (`cost_usd`, `input_tokens`/`output_tokens`) + raw `data.tokens` | cheap facts; raw shape preserved |
| `parentID` | column (`parent_external_session_id`) + raw `data.parentID` | reconstruction linkage |
| path/cwd metadata | `data` only | heterogeneous, rarely filtered |
| `part.type` | column (`part_type`) | event-type dimension |
| text / reasoning / step markers | `data` only | large content, reconstruction-only |
| tool name / status / input / output | columns in `observed_tool_calls`; input/output truncated | tool-name/status filters; content stays verbatim in `observed_parts.data` |

### 4. Tool parts: embedded AND projected

A `part.type = tool` stays embedded in `observed_parts` (as an
`observed_parts` row with `part_type = 'tool'` and its full `data`),
**and** also produces one `observed_tool_calls` row. The two are written
in the same ingest transaction, so they can never diverge within a
single batch.

The sync rule mirrors ADR 0015's rollup reasoning: `observed_tool_calls`
is a *derived query surface*, not an independent source of truth. The
authoritative verbatim store is `observed_parts.data`; the projection's
normalized columns are extracted from it at ingest. A disagreement is
repaired by re-extraction — a backfill script recomputes
`observed_tool_calls` from `observed_parts` using the same extraction
logic as live ingest (the ADR 0015 backfill↔live-equivalence pattern).

### 5. Indexes

Required for transcript reconstruction and timeline queries:

```text
observed_messages
  UNIQUE (client_id, source_database_id, external_message_id)
  (session_id, source_created_at)            -- reconstruct a session's messages in order
  (agent)                                    -- filter by agent
  (role, source_created_at)                  -- filter by role within time

observed_parts
  UNIQUE (client_id, source_database_id, external_part_id)
  (session_id, source_created_at)            -- timeline within a session
  (message_id, source_created_at)            -- a message's parts in order
  (session_id, part_type, source_created_at) -- filter by event type within a session
  (part_type, source_created_at)             -- global event-type scans
  (source_created_at)                        -- global time-range scans

observed_tool_calls
  UNIQUE (client_id, source_database_id, external_part_id)
  (session_id, source_created_at)            -- a session's tool calls in order
  (tool_name, source_created_at)             -- filter by tool name
  (tool_status)                              -- filter by status (partial)
```

`source_created_at` composite indexes use the *normalized*
`source_created_at_tz` when the collector supplies it; otherwise they
fall back to the millisecond column via the same `COALESCE`-style
normalization already used for Source-Created Ordering.

### 6. Ingest surface

The collector reads the `message` and `part` SQLite tables read-only and
sends them as two new optional batch-level collections on the existing
`/ingest` request shape, alongside the ADR 0008 collections:

```text
records            (existing, usage)
session_contexts   (existing, ADR 0008)
projects           (existing, ADR 0008)
project_directories(existing, ADR 0008)
session_todos      (existing, ADR 0008)
messages           (new)
parts              (new)
```

Processing follows the established projection semantics:

- `observed_messages` and `observed_parts` use idempotent
  `INSERT … ON CONFLICT … DO UPDATE` on their unique keys, preserving
  `first_seen_at` and updating `last_seen_at`.
- `observed_tool_calls` rows are derived inside the same transaction
  from any `observed_parts` row whose `part_type = 'tool'`.
- Partial-success semantics are preserved: a malformed transcript item
  is counted in the projection-rejected tally and never blocks accepted
  usage records.
- Redaction and truncation (below) happen at ingest, before any value
  reaches the database, so the durable store never holds plaintext
  secrets or unbounded tool payloads.

## API Design

### Envelope

Transcript endpoints reuse the existing response envelope
(`app/core/envelope.py`, `ResponseEnvelopeMiddleware`): every successful
JSON response is `{status: "ok", data: <body>}` and failures are
`{status: "error", error: {code, message}}`. No new envelope. All
endpoints are protected by the existing `ApiKeyMiddleware` (all
non-`/health` routes).

### Endpoints

Mounted at `/api/v1/execution` (the usage router is at
`/api/v1/usage`; transcript endpoints are a sibling prefix, never a
child of the usage endpoints).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/execution/sessions/{session_id}` | Transcript session header: identity, parent/child linkage, counts, time window |
| `GET /api/v1/execution/sessions/{session_id}/children` | Child subagent sessions |
| `GET /api/v1/execution/sessions/{session_id}/messages` | A session's messages, chronologically |
| `GET /api/v1/execution/sessions/{session_id}/parts` | A session's part events, chronologically |
| `GET /api/v1/execution/sessions/{session_id}/timeline` | Unified timeline across parent + descendants |
| `GET /api/v1/execution/tool-calls` | Global tool-call query |

Request/response shapes (Pydantic, following `app/core/schemas/usage.py`):

- **Session header** (`GET …/sessions/{id}`):

  ```json
  {
    "id": "<internal uuid>",
    "external_session_id": "ses_…",
    "agent": "autonomous-coordinator",
    "parent_session_id": "ses_… (external, null for root)",
    "parent_internal_id": "<uuid, null for root>",
    "child_session_ids": ["<uuid>…"],
    "message_count": 214,
    "part_count": 1403,
    "tool_call_count": 87,
    "first_part_at": "2026-08-01T00:00:00Z",
    "last_part_at": "2026-08-01T00:04:12Z"
  }
  ```

- **Messages** (`GET …/messages`, `GET …/parts`): a page of
  `ObservedMessage` / `ObservedPart` rows. `ObservedPart` exposes the
  promoted columns (`id`, `part_type`, `source_created_at_tz`,
  `external_part_id`, `message_id`, `session_id`) plus the `data`
  payload. `ObservedMessage` exposes `id`, `role`, `agent`, `mode`,
  `cost_usd`, token columns, `parent_external_session_id`, timestamps,
  and `data`.

- **Tool calls** (`GET …/tool-calls`): a page of `ObservedToolCall`
  rows (`id`, `session_id`, `tool_name`, `tool_status`, `tool_input`,
  `tool_output`, `source_created_at_tz`).

- **Timeline** (`GET …/timeline`): a page of timeline events, each a
  part event annotated with its owning session:

  ```json
  {
    "part_id": "<uuid>",
    "session_id": "<uuid>",
    "external_session_id": "ses_…",
    "agent": "git-workspace",
    "depth": 1,
    "part_type": "tool",
    "source_created_at_tz": "2026-08-01T00:01:03Z",
    "data": { … }
  }
  ```

  `depth` is the generation relative to the requested root (`0` for the
  root, `1` for direct children, etc.), so a consumer can render
  parent-coordinator work distinctly from child-subagent work.

### Pagination

The existing list endpoints paginate with offset/limit via the generic
`PaginatedResponse` (`items`, `total`, `limit`, `offset` in
`app/core/schemas/usage.py`). Note that `app/api/cursor.py` is the
collector's **ingestion cursor** (`GET /cursor`), not a response
pagination cursor — its name is unrelated to pagination and must not be
conflated with it.

Transcripts are append-only and monotonically ordered by
`source_created_at`, which makes deep-offset pagination both unstable
(concurrent ingest shifts pages) and expensive (large `OFFSET` scans).
Transcript endpoints therefore use **keyset (cursor) pagination**:

- Response page shape:

  ```json
  {
    "items": [ … ],
    "next_cursor": "base64(sort_key=…&id=…)",   // null on last page
    "has_more": true
  }
  ```

- The cursor encodes the last row's `(source_created_at, id)`. The next
  page re-issues the same filter with `after=<cursor>` and orders by
  `(source_created_at, id)`. This is stable under concurrent ingest
  (new events append after the cursor, never reshuffling earlier pages)
  and uses the `(session_id, source_created_at)` / `(source_created_at)`
  indexes directly.
- `limit` is bounded (default 100, max 1000) as today.

### Unified timeline

Yes — `GET …/sessions/{session_id}/timeline` is the unified endpoint. It
walks the parent/child tree rooted at `session_id`, collects the part
events of every descendant (bounded by an optional `max_depth`, default
all generations), and returns a single chronologically-ordered stream
annotated with `session_id`, `agent`, and `depth`. Implementation is a
recursive CTE over the parent linkage (`opencode_session_contexts`
`parent_session_id`, falling back to `observed_messages`
`parent_external_session_id`) joined to `observed_parts`, ordered by
`source_created_at`, paginated by the same keyset cursor.

### Truncation and redaction at the API boundary

- Tool inputs/outputs are **truncated at ingest** to a configurable cap
  (default `GATEWAY_TOOL_PAYLOAD_MAX_CHARS = 4096` per field), so
  `observed_tool_calls.tool_input` / `tool_output` are bounded. The
  verbatim content is retained in `observed_parts.data`, itself
  truncated to a larger verbatim cap (default `GATEWAY_PART_DATA_MAX_CHARS
  = 65536`) with a `truncated` marker so readers know content was cut.
- Text and reasoning part content is truncated at the same verbatim cap;
  step markers are small and stored whole.
- Secret-like keys anywhere in a part or message payload are redacted at
  ingest via the existing `redact_dict` helper (`app/core/secrets.py`,
  ADR 0004) before the JSONB is written.

### Filter parameters

| Parameter | Applies to | Meaning |
|---|---|---|
| `session_id` | tool-calls | restrict to one session |
| `agent` | messages, tool-calls, timeline | restrict to an agent |
| `part_type` | parts | restrict to an event type (`text`, `tool`, …) |
| `tool_name` | parts, tool-calls | restrict to a tool |
| `tool_status` | tool-calls | restrict by status (e.g. success/error) |
| `role` | messages | restrict by role |
| `from` / `to` | all list endpoints | time range over `source_created_at` |

## Separation of Concerns

This slice is deliberately parallel to, and never a modification of, the
usage/aggregate slice:

- **Transcripts are event timelines; usage is accounting.** The usage
  slice (`usage_events`, `opencode_usage_records`, the Client Project
  Rollup, and the `sessions` aggregate) answers "how much" — tokens,
  cost, model mix, project rollups, session summaries. The transcript
  slice answers "what happened" — the ordered message/part event stream
  with tool calls, reasoning, and parent/child structure. The two never
  merge into one table.
- **No change to existing ingest/usage contracts.** The new `messages`
  and `parts` collections are additive and optional. `records`,
  `session_contexts`, `projects`, `project_directories`, and
  `session_todos` processing is untouched. Existing usage endpoints,
  their response models, and the Canonical Event replay semantics (ADR
  0012) are unchanged.
- **New ingest surface stays separate.** Transcript rows are written to
  the new tables only; they never touch `usage_events`, the rollup, or
  the session aggregates. A transcript row is an append of an observed
  source fact, not an accounting event — it has no token/cost delta
  semantics and is not subject to the Canonical Event Replay Merge.
- **Shared identity, separate facts.** The transcript tables share
  session identity (`sessions.id`) and the parent/child linkage with the
  usage slice, but every transcript fact lives in its own table. Sharing
  identity does not couple the two slices' query or mutation paths.

## Redaction and Privacy

Tool inputs/outputs and part content are the highest-risk surface — they
can contain source code, environment snippets, and credentials. Handling:

- **Ingest-time secret redaction.** Every part and message payload passes
  through `redact_dict` (`app/core/secrets.py`) before persistence, so
  keys matching `token`, `password`, `secret`, `credential`, `apikey`,
  `auth`, `key`, etc. (case/underscore-insensitive) are replaced with
  `***`. The redacted form is what is *stored* — plaintext secrets are
  never written to the durable transcript store.
- **Truncation limits.** Tool input/output fields are truncated at
  ingest (`GATEWAY_TOOL_PAYLOAD_MAX_CHARS`, default 4096); full part
  payloads are truncated at a larger verbatim cap
  (`GATEWAY_PART_DATA_MAX_CHARS`, default 65536) with a `truncated`
  marker. Both are configurable and default to conservative values.
- **No redaction at read time.** Redaction is an ingest concern; the API
  serves only the already-redacted, already-truncated store. This keeps
  the read path cheap and guarantees the same privacy guarantees
  regardless of caller.
- **Retention recommendation.** Transcript data is higher-volume and
  lower-longevity than accounting data. Recommend a separate retention
  policy (configurable, default e.g. 90 days for `observed_parts` /
  `observed_tool_calls`, longer for `observed_messages`) enforced by a
  maintenance job — accounting aggregates (usage events, rollup) retain
  their existing, longer retention. Retention is a recommendation here,
  not an implementation in this ADR.

## Rationale

- **Model the event type explicitly** (`part_type`) so consumers filter
  by `tool` vs `reasoning` vs `step-start` without inspecting JSONB, and
  so the tool-call projection has a stable extraction trigger.
- **Keep the verbatim payload** (`data` JSONB) so no source fidelity is
  lost and future OpenCode part shapes remain readable without migration;
  promotion to columns is limited to what is actually filtered or linked.
- **Reuse session identity** rather than a fourth session table to avoid
  three-way identity drift, while capturing `parent_external_session_id`
  on messages as a reconstruction safety net against out-of-order context
  projections.
- **Embed *and* project tool parts** to get both verbatim reconstruction
  (`observed_parts.data`) and cheap tool-name/status queries
  (`observed_tool_calls`), kept consistent by same-transaction writes and
  a backfill-equivalent recompute.
- **Keyset pagination** because transcripts are append-only and
  monotonically ordered; it is stable under concurrent ingest and avoids
  deep-offset scans.
- **Separate the slice** so the usage accounting surface's replay/dedup
  semantics (ADR 0011, ADR 0012, ADR 0015) are never burdened by
  event-timeline data, and vice versa.

## Consequences

### Positive

- The Gateway can reconstruct a full end-to-end timeline across a parent
  session and its subagent children, including tool calls, reasoning, and
  step markers — the #217 "what did a subagent actually do" gap is
  closed.
- Event types, tool names, agent, and time ranges are queryable via
  indexes without scanning opaque JSONB.
- Privacy is enforced at ingest, so the durable store and every API read
  are redacted and truncated by construction.
- No existing usage, ingest, or aggregate contract changes; the slice is
  additive and optional, so a collector that never emits `messages` /
  `parts` behaves exactly as before.
- Tool-call projection is recomputable from `observed_parts`, so a
  projection bug is a backfill, not a data loss.

### Negative

- Ingest surface grows by two more optional batch collections, and the
  collector must now read two additional SQLite tables.
- Transcript volume is far higher than accounting volume; storage and
  index cost are dominated by `observed_parts` and demand the retention
  policy.
- Three new tables plus a projection increase migration and maintenance
  surface.
- Parent/child tree walking for the unified timeline is a recursive
  query; it needs the `max_depth` bound and index discipline to stay
  cheap on deeply nested runs.
- Redaction is heuristic (key-name based); a secret stored under a
  non-matching key name would not be caught by `redact_dict` alone.

## Alternatives Considered

**Store everything in one opaque JSONB blob (a single `transcripts`
table with a `data` column).** Rejected: it violates the explicit
constraint to model part/event types rather than hide them in a blob,
forces full payload reads for any filter, and cannot support the
`(session_id, part_type, source_created_at)` timeline indexes without
generated columns.

**Reuse the legacy `opencode_usage_records` path (fold parts into usage
records).** Rejected: usage records are per-message accounting facts
with token/cost replay semantics; parts are a separate, larger, ordered
event stream with no accounting meaning. Folding them in would corrupt
the accounting model and force transcript reads through the replay-merge
machinery.

**Event-sourcing-style single append-only `execution_events` table**
(one polymorphic table for messages, parts, and tool calls). Rejected
for v1: a single table would need nullable per-type columns or JSONB
discrimination, blunting the per-type indexes and forcing every
reconstruction query to scan all event types. Three typed tables give
clean identity, typed indexes, and clearer ingest idempotency.

**A fourth `observed_sessions` table** (as the issue floated). Rejected
(Decision 1): session identity and parent/child linkage already live in
`sessions` + `opencode_session_contexts`; a fourth copy invites drift
for no query benefit.

**Offset/limit pagination for transcripts (reuse `PaginatedResponse`
as-is).** Rejected for the timeline/parts paths: deep offsets are
unstable under concurrent ingest and expensive on large transcripts.
Keyset cursors are adopted instead; the existing `PaginatedResponse`
remains for the smaller count-bounded header/children endpoints.

## Breaking Into Implementation Issues

The proposal is actionable as sequential, independently-shippable
slices:

1. **Migration** (`alembic/versions/0026_add_execution_transcript_tables.py`):
   create `observed_messages`, `observed_parts`, `observed_tool_calls`
   with the columns, unique keys, and indexes specified above.
2. **Collector read** (collector change): read `message` and `part`
   SQLite tables and emit `messages` / `parts` batch collections,
   redacting and truncating payloads before send.
3. **Ingest processing** (Gateway): add `_process_message` /
   `_process_part` projection handlers with idempotent upsert, session
   resolution, redaction, truncation, and same-transaction
   `observed_tool_calls` extraction; wire partial-success counts.
4. **API — session/children/messages/parts** (`app/api/execution.py`):
   header, children, messages, and parts endpoints with keyset
   pagination and filters.
5. **API — tool-calls + unified timeline**: the global tool-call query
   and the recursive parent+descendant timeline endpoint.
6. **Backfill/reconciliation script** (`scripts/backfill_tool_calls.py`):
   recompute `observed_tool_calls` from `observed_parts` with the same
   extraction logic (ADR 0015 equivalence test).
7. **Frontend (Aurora Glass)** — separate from the Gateway service: a
   transcript detail view and timeline renderer consuming
   `/api/v1/execution/*`, distinct from the aggregate usage views.
8. **Retention job + config**: retention enforcement for transcript
   tables and the `GATEWAY_TOOL_PAYLOAD_MAX_CHARS` /
   `GATEWAY_PART_DATA_MAX_CHARS` settings.

Each slice maps to one AFK implementation issue; slices 2–3 and 4–5 are
the natural vertical-slice pairings.
