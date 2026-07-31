# ADR 0008: Gateway-owned OpenCode projection tables

## Status

Accepted

## Context

The Gateway ingests usage telemetry from OpenCode SQLite source databases
through read-only collectors. The initial ingest pipeline focused on
per-message usage records: model, provider, tokens, cost, timestamps, and
session identity resolution.

That usage model is not enough to answer operational questions such as
"what happened during this agent run?" Operators need session-level and
project-level context around the usage data: session title, agent,
parent/child session relationships, project/worktree details, code-change
summary counts, and the latest OpenCode todo state.

OpenCode already stores much of this data in its local SQLite database in
tables such as `session`, `project`, `project_directory`, and `todo`.
The collector must continue treating that SQLite database as read-only.
The Gateway therefore needs its own durable representation of selected
OpenCode source facts.

Alternatives considered:

1. **Write enrichment back into OpenCode SQLite** — populate OpenCode's
   existing nullable fields such as `session.metadata`, `summary_diffs`,
   `project.name`, and `project.commands`.
2. **Add many columns to existing Gateway tables** — store session context
   and project context directly on `sessions` or `opencode_usage_records`.
3. **Store precomputed agent run summaries** — denormalize all context,
   todos, project details, and usage totals into an `agent_run_summaries`
   table at ingest time.
4. **Gateway-owned projection tables** — collect selected OpenCode source
   tables read-only, store normalized Gateway-owned projections, and
   compute agent run summaries on read.

## Decision

Use Gateway-owned projection tables (option 4).

The collector will remain read-only against OpenCode SQLite. It may read
additional source tables and fields, then send them as separate batch-level
collections in the existing ingest request shape:

```text
records
session_contexts
projects
project_directories
session_todos
```

The Gateway will store those collections in normalized projection tables:

```text
opencode_session_contexts
opencode_source_projects
opencode_project_directories
opencode_session_todos
```

The Gateway will compute Agent Run Summary responses on read by joining
usage aggregates, session context, project snapshots, todo snapshots, and
parent/child session relationships. It will not store a separate v1
`agent_run_summaries` table.

The OpenCode `event` table, full message transcripts, and `part` replay
are out of scope for this decision.

## Rationale

- **Collector safety**: collectors keep using read-only SQLite access and
  never mutate OpenCode-owned state.
- **Ownership clarity**: OpenCode owns its SQLite schema and contents;
  Gateway owns its Postgres projections and API models.
- **Normalized storage**: session context, projects, directories, and
  todos have different identity and update rules. Separate tables avoid
  overloading `sessions` or `opencode_usage_records`.
- **Evolvability**: Agent Run Summary is an API/view model. Computing it
  on read lets the response evolve without repeatedly migrating a
  denormalized summary table.
- **Operational usefulness**: collecting `session`, `project`,
  `project_directory`, and `todo` data is enough to answer summary-level
  "what happened?" questions without streaming the large OpenCode `event`
  table.
- **Partial rollout**: projection collections are optional and
  schema-aware. Missing optional source tables or columns should not block
  core usage ingestion.

## Consequences

Positive:

- Gateway can answer agent-run questions without writing to OpenCode
  SQLite.
- Usage records stay focused on per-message usage telemetry.
- Projection tables can use their own idempotency, upsert, and snapshot
  rules.
- Frontend views can show session title, agent, project/worktree,
  parent/child runs, todo progress, file-change counts, cost, and tokens.

Negative:

- Ingest request handling becomes more complex because it must process
  several independent batch-level collections.
- Gateway must handle partial projection payloads and out-of-order data,
  such as a session context arriving before its parent session has been
  resolved.
- Computed-on-read summaries require joins across several tables.
- Projection schemas must track selected OpenCode SQLite schema changes
  over time.

## Projection Semantics

### Session Context

`opencode_session_contexts` stores the latest observed descriptive
snapshot for an OpenCode external session, keyed by:

```text
(source_database_id, external_session_id)
```

Session Context is sent as a separate batch-level collection, not
duplicated onto every usage record.

The table should store both source identifiers and resolved Gateway
identifiers where useful:

```text
external_session_id text not null
session_id uuid references sessions(id)
parent_external_session_id text null
parent_session_id uuid references sessions(id)
external_project_id text null
source_project_id uuid references opencode_source_projects(id)
```

Session Context uses last-write-wins upsert semantics. The Gateway
preserves `first_seen_at` and updates `last_seen_at` when a newer snapshot
is observed.

### Project Snapshot

`opencode_source_projects` stores the latest observed source project row,
keyed by:

```text
(source_database_id, external_project_id)
```

`worktree` is stored as mutable descriptive data, not as identity.

`opencode_project_directories` stores the latest observed directory set for
a project, keyed by source database, external project ID, and directory.
When a project snapshot is processed, project directories are replaced per
project so stale cleaned-up directories do not remain visible.

### Todo Snapshot

`opencode_session_todos` stores the latest observed OpenCode todo rows for
an external session. Todo snapshots are keyed by:

```text
(source_database_id, external_session_id, position)
```

When todo data for a session is processed, the Gateway replaces all todo
rows for that session with the latest snapshot. This treats todos as
current state, not an event history.

### Source Timestamps

Projection tables should preserve both raw source millisecond timestamps
and normalized Postgres timestamps where useful:

```text
source_time_created_ms bigint
source_time_updated_ms bigint
created_at timestamptz
updated_at timestamptz
first_seen_at timestamptz
last_seen_at timestamptz
```

Raw millisecond values preserve OpenCode fidelity. Normalized timestamps
support Gateway filtering, querying, and frontend display.

## Ingest Rules

- Projection collections are batch-level arrays.
- The collector de-duplicates session contexts by external session ID
  within a batch.
- The collector sends project snapshots only for projects referenced by
  sessions in the current usage batch.
- The collector sends todo snapshots only for sessions represented in the
  current usage batch.
- Projection processing has independent partial-success semantics. A bad
  todo or project snapshot must not block accepted usage records.
- Optional projection collection is schema-aware. Missing optional source
  tables or fields should produce partial projections, not exclusion of the
  entire source database.

## Agent Run Summary

Agent Run Summary is computed on read. It is not stored as a v1 table.

The summary API/view model may derive fields such as:

```text
title
status
agent
model
project/worktree
parent run
child run count
todo completed count
todo total count
files changed
summary additions
summary deletions
token totals
cost totals
last updated
```

The initial status is inferred from todo state and session recency, not
from an OpenCode-native status column. Detailed event timelines, message
transcripts, and part/tool-call replay remain out of scope.

## Alternatives Considered

**Writing to OpenCode SQLite** was rejected because the collector must be
read-only and OpenCode owns that schema. Custom writes risk future
OpenCode schema changes, overwrites, or unclear ownership.

**Adding all fields to `sessions` or `opencode_usage_records`** was
rejected because usage records are per-message facts and sessions are usage
aggregates. Project directories and todo snapshots have separate identity
and replacement semantics.

**Precomputing `agent_run_summaries` at ingest time** was rejected for v1
because it creates stale denormalized data and couples API shape to ingest
storage. Read-time computation keeps the normalized facts authoritative.

**Collecting the OpenCode `event` table** was rejected for this round
because it is a large event log requiring a distinct cursor and product
surface. The immediate goal is a summary-level answer to "what happened
during this agent run?", not a full event timeline or replay.
