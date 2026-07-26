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

**Todo Snapshot**:
The latest observed set of OpenCode `todo` rows for an external session,
read from the source SQLite database and stored by the Gateway for agent
run reporting. Todo Snapshots summarize planned, in-progress, completed,
and cancelled work items; they are not event timelines.

**Agent Run Summary**:
A Gateway API/view model that answers what happened during an OpenCode
agent or subagent session using Session Context, Todo Snapshots, project
snapshots, parent/child session relationships, and usage aggregates. It is
a summary view, not a replay of OpenCode events or message parts.
_Avoid_: Event Timeline, transcript replay

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

## Architecture Note

The Gateway uses a layered architecture:

- **app/api/** — REST endpoints
- **app/core/** — Configuration, auth, logging, factory
- **app/db/** — Postgres pool, migrations, ORM models

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
- The **Admin API Key** MAY also serve as a **Collector Credential** when its hash is registered in `collector_credentials`
- The `ApiKeyMiddleware` runs before `require_collector_token` — a request must pass the **Admin API Key** check before **Collector Credential** lookup occurs

## Flagged Ambiguities

- "frontend layer inside the Gateway" was used to mean **Aurora Glass**.
  Resolved: **Aurora Glass** is a separate frontend that consumes the
  **Gateway** API.
