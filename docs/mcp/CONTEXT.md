# OpenCode Gateway MCP — Context

## Purpose

Define the context and v1 capability boundary for a dedicated `opencode-gateway-mcp`
adapter that lets MCP clients such as ChatGPT, OpenCode, and Claude interrogate the
OpenCode Gateway using question-oriented, read-only tools.

This document is staged in `opencode-gateway` because the dedicated MCP repository
does not exist yet. When that repository is created, this document should seed its
root `CONTEXT.md`.

## Problem

OpenCode Gateway already exposes rich observability and AFK reporting APIs, but an
LLM should not need to know REST paths, internal table layouts, Postgres schemas, or
Kafka topics to answer operational questions.

The MCP should expose the questions a human actually asks while keeping the Gateway
as the source of truth.

The governing flow is:

```text
question -> information required -> existing Gateway API -> MCP capability -> answer
```

The MCP is not a second reporting service and must not manufacture capabilities the
Gateway API does not expose cleanly.

## Architecture

```text
ChatGPT / OpenCode / Claude
          |
          | MCP
          v
+-----------------------------+
| opencode-gateway-mcp        |
|                             |
| semantic read tools         |
| filter/time normalization   |
| HTTP client + API key       |
| response schemas            |
+-------------+---------------+
              |
              | HTTPS + API key
              v
+-----------------------------+
| OpenCode Gateway FastAPI    |
+-------------+---------------+
              |
       existing Gateway
       storage/projections
```

The MCP communicates only with the published OpenCode Gateway HTTP API.

It does not connect directly to Postgres, Kafka, collector databases, or Gateway
Python modules.

## v1 user questions

### Models, usage, and cost

The MCP must support questions such as:

- What models were used over this period?
- How many sessions, tokens, and records did each model account for?
- How much did each model cost?
- How much did AFK cost over this period?
- How much did this specific AFK run cost?

### AFK runs

The MCP must support:

- What happened in this AFK run?
- How many AFK runs happened over this period?
- Show me recent AFK runs for this repository.
- Show me AFK runs by provider or outcome.
- Which issues, PRs/MRs, reviews, commits, agents, and sessions were linked to this run?
- What did this run cost?

### AWX executions

The MCP must support:

- How many AFK AWX executions happened over this period?
- How many completed, failed, or were cancelled?
- What AWX executions happened for this AFK run?
- Did an execution fail and then retry?

### Agents and sessions

The MCP must support:

- Which agents were used over this period?
- Which agents accounted for the most usage or cost?
- Which agents and sessions were involved in this AFK run?
- How many sessions were involved?

### Change requests

The MCP must support:

- What happened with this PR/MR from AFK's perspective?
- Which AFK runs were linked to this PR/MR?
- Which AWX executions and sessions were linked?
- Did it merge, close, or remain open?
- What execution/session cost was associated with it?

### Gateway and collector health

The MCP must support:

- Is OpenCode Gateway healthy?
- Is the database connected?
- When was the most recent ingest?
- Are the remote collectors healthy?
- Which collectors or source databases are stale or unknown?
- When did a collector last report?
- How many records has a collector ingested?

### Correlation quality

The MCP must support:

- Are there unresolved AFK correlations?
- Which correlations are ambiguous or unmatched?
- Are there links whose reconstruction should not be trusted as deterministic?

### Repository-scoped activity

The MCP must support questions such as:

- How many AFK runs did this repository have today or this week?
- How many AWX executions did it produce?
- How many succeeded, failed, or were cancelled?
- How much did AFK cost for this repository?
- Show me the recent or merged AFK runs for this repository.

## v1 MCP tool set

The v1 surface is intentionally small. Tools represent investigative actions rather
than mirroring REST endpoints one-for-one.

### 1. `get_afk_activity_summary`

Purpose:
Return AFK rollup activity for a date range, optionally scoped to provider and
repository.

Inputs:

- `from_date`
- `to_date`
- optional `provider`
- optional `repository`
- `interval` = `daily` or `monthly`

Backed by:

- `GET /api/v1/afk/dashboard/summary`

Expected data includes runs started, execution counts, execution outcomes, sessions,
tokens, and estimated cost.

### 2. `list_afk_runs`

Purpose:
Find AFK runs using the Gateway's existing run filters.

Inputs may include:

- repository
- provider/origin
- started, finished, or seen time window
- outcome
- limit
- offset

Backed by:

- `GET /api/v1/afk-outcomes/runs`

The MCP may translate the user-facing `provider` concept to the Gateway's
`origin` filter but must not invent additional filtering semantics.

### 3. `get_afk_run_story`

Purpose:
Return the authoritative evidence needed to answer "What happened in this AFK run?"

Input:

- `afk_run_id`

Backed by:

- `GET /api/v1/afk-outcomes/runs/{afk_run_id}`
- `GET /api/v1/afk/executions/runs/{afk_run_id}`

This is the one approved v1 composite capability. It combines two first-class,
run-scoped Gateway surfaces. It must not infer additional relationships.

The result can include issues, change requests, reviews, commits, merge events,
sessions, agents, usage/cost, and the complete AWX execution-attempt history.

### 4. `get_model_usage`

Purpose:
Answer model mix, token, cache, session, and cost questions for a period.

Inputs:

- `start_date`
- `end_date`
- optional Gateway-supported filters such as `client_id` or `model`

Backed by:

- `GET /api/v1/usage/aggregates?group_by=model`

### 5. `get_agent_usage`

Purpose:
Answer agent usage and cost questions for a period.

Inputs:

- `start_date`
- `end_date`
- only filters supported by the underlying aggregate API

Backed by:

- `GET /api/v1/usage/aggregates?group_by=agent`

### 6. `list_change_requests`

Purpose:
Return the same per-change-request summary rows used by the Gateway frontend
from `GET /api/v1/afk-outcomes/change-requests`. Each row preserves provider,
repository, PR/MR external_id, provider lifecycle state,
`total_estimated_cost_usd`, latest linked activity, and execution counts.

Inputs:

- optional `provider` = `github` or `gitlab`
- optional `repository`
- optional `provider_state` = `open` / `closed` / `merged`
- optional `activity_from`
- optional `activity_to`
- `limit`
- `offset`

Backed by:

- `GET /api/v1/afk-outcomes/change-requests`

Supports explicit `limit`/`offset` pagination without silent crawling. Cost is
never recalculated in the MCP; the Gateway-owned value is returned verbatim,
including null when unavailable.

### 7. `get_change_request_story`

Purpose:
Return AFK evidence for one GitHub PR or GitLab MR.

Inputs:

- `provider`
- `repository`
- `external_number`

Backed by:

- `GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}`

The response may include provider state, merge time, AFK runs, AWX executions,
sessions, usage/cost, and timeline/provenance.

### 8. `get_gateway_health`

Purpose:
Return service and ingestion health.

Inputs:

- none

Backed by:

- `GET /health`

The response includes Gateway status/version, database connectivity, most recent
ingest, remote collector health, and source-database health.

### 9. `get_correlation_issues`

Purpose:
Expose unresolved correlation quality problems.

Inputs:

- optional `reason` = `ambiguous` or `unmatched`
- `limit`
- `offset`

Backed by:

- `GET /api/v1/afk-outcomes/correlations`

### 10. `list_sessions`

Purpose:
List live and historical OpenCode agent sessions — the same paginated Agent Run
list used by Aurora Glass. Answers "which sessions are running or recently ran,
and what did they do" without claiming a proven live OS/tmux process probe.

Inputs:

- optional `client_id` (UUID string)
- optional `from_date` (ISO-8601 — filter sessions last active on or after this date)
- optional `to_date` (ISO-8601 — filter sessions last active on or before this date)
- optional `agent` (exact match)
- optional `external_project_id` (exact match on `project_id` — the stable project
  identity, not a repository string; no `repository` filter exists)
- optional `status` = `running` / `stale` / `completed` / `blocked` / `unknown`
- `limit` = 1–1000 (default 50)
- `offset` >= 0

Backed by:

- `GET /api/v1/usage/agent-runs`

Each row preserves internal Gateway UUID `id`, external `ses_*` ID, title, computed
`status`/`currentStatus`, agent, `project_id`/`project_label`/`workspace_id`,
`model` (from Session Context, may be null), `last_updated_at`, `child_run_count`,
token fields (`total_input_tokens`, `total_output_tokens`, `total_cache_read_tokens`,
`total_cache_write_tokens`, `total_reasoning_tokens`), `message_count`,
`total_estimated_cost_usd` (nullable), and todo/code-change summaries. Pagination
and nulls are preserved as the Gateway reports them; no silent crawling is
performed.

**Status is computed, not proven.** `status` / `currentStatus` is the
Gateway-computed activity heuristic derived on read from `last_message_at`,
`message_count`, and `parent_session_id` against configurable quiet (15 min),
stale (2 h), and unknown (48 h) thresholds — not a proven live OS or tmux
process check. In order: `unknown` when no messages / no `last_message_at`;
`running` when `age < quiet`; `completed` when `quiet ≤ age < stale` and no
parent; `blocked` when `quiet ≤ age < stale` and has parent; `stale` when
`stale ≤ age < unknown` (observability gap, not a known termination); `unknown`
when `age ≥ unknown`. Boundaries are strict (exactly at a threshold falls to the
next bucket). A `running` result therefore means "recent Gateway activity within
the quiet window", not "a confirmed live process"; a `stale` result is not a
terminal failure. Callers that need proven liveness must use a Runner VM
process probe, not this list.

## Response philosophy

MCP tools return structured Gateway facts, not pre-written narrative answers.

The MCP is responsible for:

- authentication
- HTTP transport
- validation
- translating a small number of semantic parameter names to existing API parameters
- stable MCP schemas
- preserving Gateway provenance and null/unavailable values
- clear error reporting

The calling model is responsible for:

- deciding which tool answers the user's question
- combining returned structured facts into prose
- explaining evidence and limitations
- resolving natural-language time expressions before the tool call

The MCP must not hide a Gateway 4xx/5xx response as an empty successful result.

## Time semantics

Natural language such as "today", "yesterday", "overnight", and "this week" is
caller context, not MCP business logic.

The caller resolves those expressions into explicit dates/timestamps using the
user's local context and passes those values to the MCP.

The MCP must not rely on an implicit Gateway "today" default.

Where an underlying Gateway daily rollup is UTC-calendar based, the MCP must not
claim that the result is an exact timezone-local calendar day if the API cannot
provide that precision. True timezone-aware rollups are a Gateway API enhancement,
not MCP-side reconstruction.

## Authentication and configuration

Initial configuration is intentionally small:

- `OPENCODE_GATEWAY_URL`
- `OPENCODE_GATEWAY_API_KEY`

Credentials must not be returned through MCP tool results or logs.

## Pagination

Pagination remains explicit.

The MCP must not silently crawl thousands of Gateway rows to emulate a missing query
capability. Tool schemas should expose sensible `limit` / `offset` controls where
the Gateway does.

## v1 non-goals

The following are explicitly out of scope:

- write/admin operations
- reconciliation actions
- AFK run patch/delete operations
- ingest operations
- provisioning or triggering AWX work
- direct Postgres queries
- direct Kafka access
- direct collector-database access
- importing Gateway implementation modules
- generic `call_gateway_endpoint(method, path, body)` passthrough
- automatic global crawling to construct unsupported reports
- new correlation inference performed inside the MCP

## Parked API gaps

These questions are useful but do not currently have a sufficiently clean base API
surface for v1:

- Which model was used inside one specific AFK run, when that relationship is not
  directly exposed by the run API?
- Give me every individual AWX execution across an arbitrary global date/time range.
- Given an issue number alone, find its AFK run through a dedicated first-class issue
  lookup.
- Produce a broad cross-domain executive narrative when the required facts would need
  MCP-side reconstruction rather than a first-class Gateway query.
- Exact timezone-local daily rollups where the underlying Gateway rollup is UTC-day
  based.

If these become important, the Gateway API should gain the capability first. The MCP
can then expose it.

## Suggested implementation shape

A future dedicated repository can start with:

```text
src/opencode_gateway_mcp/
  server.py
  config.py
  client.py
  tools/
    activity.py
    afk_runs.py
    usage.py
    change_requests.py
    health.py
    correlations.py
  models/
    ...
tests/
```

`client.py` owns Gateway HTTP/API-key concerns. Tool modules own MCP schemas and
mapping to the existing Gateway API.

## v1 acceptance boundary

v1 is successful when an MCP client can answer the accepted question catalogue using
the ten read-only semantic tools without direct database access and without
inventing relationships the Gateway has not exposed.

When the dedicated `opencode-gateway-mcp` repository is created, copy this context
there and treat the MCP repository as the implementation owner.
