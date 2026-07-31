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
proof — it means the session has fallen quiet beyond the threshold, has
no parent dependency, and has recorded messages (see
``_compute_status``). Inactivity alone must never be confused with
upstream-proven success; it is a heuristic. Distinct from Blocked
(intentional wait), Stale (lost liveness), Timed Out (expired budget), and
terminal failures like Failed or Cancelled.

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
has gone silent, or its heartbeats cannot be verified. Stale is a valid
direct user-facing status label in observability contexts. It represents
a gap in observability, not a known termination reason. Distinct from
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

## Architecture Note

The Gateway uses a layered architecture:

- **app/api/** — REST endpoints
- **app/core/** — Configuration, auth, logging, factory
- **app/db/** — Postgres pool, migrations, ORM models
- **app/consumer/** — Kafka consumer bridge that reads usage records from Kafka and POSTs them to the Gateway ingest API (separate container)

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
- **Aurora Glass** uses the same compact **Token Breakdown** vocabulary for Sessions and **Agent Run Summary** rows
- **Aurora Glass** treats an Agent Run or Session title as the meaningful scope for a **Token Breakdown** row
- **Aurora Glass** applies the shared dashboard date range to **Agent Run Summary** views unless an Agent Runs-specific date boundary is explicitly selected; each Agent Runs-specific boundary takes precedence for that side of the range, while unset boundaries inherit from the shared dashboard range
- **Aurora Glass** treats Agent Run status filters as additional narrowing filters on the effective date range; status filters do not define or alter the date range
- The **Admin API Key** MAY also serve as a **Collector Credential** when its hash is registered in `collector_credentials`
- The `ApiKeyMiddleware` runs before `require_collector_token` — a request must pass the **Admin API Key** check before **Collector Credential** lookup occurs
- A **Usage Record Consumer** reads from the ``opencode-usage`` Kafka topic
- A **Usage Record Consumer** POSTs to the Gateway's ``/ingest`` endpoint using a **Collector Credential**
- Unprocessable messages are sent to the **Dead Letter Queue (DLQ)** topic ``opencode-usage-dlq``

## Flagged Ambiguities

- "frontend layer inside the Gateway" was used to mean **Aurora Glass**.
  Resolved: **Aurora Glass** is a separate frontend that consumes the
  **Gateway** API.
