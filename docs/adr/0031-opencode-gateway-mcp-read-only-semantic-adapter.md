# 0031 — Expose OpenCode Gateway through a separate read-only semantic MCP adapter

Status: accepted — amended 2026-10-08 (v1.1 adds `list_change_requests`; nine tools)

## Context

OpenCode Gateway is an observability and reporting service. It already exposes
first-class HTTP APIs for AFK runs and outcomes, AWX execution bindings, usage and
cost aggregates, change-request detail, correlation quality, dashboard rollups, and
collector/service health.

MCP-capable clients such as ChatGPT, OpenCode, and Claude should be able to ask
human-oriented questions about that data without learning Gateway REST paths or
internal persistence details.

A one-to-one OpenAPI-to-MCP projection would expose implementation mechanics rather
than useful investigative capabilities. Conversely, allowing the MCP to query
Postgres/Kafka directly or reconstruct missing reports would create a second
reporting layer and undermine the Gateway's ownership boundary.

## Decision

Create a separate `opencode-gateway-mcp` adapter/repository.

The v1 adapter is read-only and communicates only with the published OpenCode Gateway
HTTP API using Gateway API-key authentication.

The MCP exposes semantic, question-oriented tools rather than mirroring every REST
endpoint. The v1 tool set (as amended 2026-10-08 — v1.1, nine tools) is:

1. `get_afk_activity_summary` — `GET /api/v1/afk/dashboard/summary`
2. `list_afk_runs` — `GET /api/v1/afk-outcomes/runs`
3. `get_afk_run_story` — `GET /api/v1/afk-outcomes/runs/{afk_run_id}` + `GET /api/v1/afk/executions/runs/{afk_run_id}` (only approved composite)
4. `get_model_usage` — `GET /api/v1/usage/aggregates?group_by=model`
5. `get_agent_usage` — `GET /api/v1/usage/aggregates?group_by=agent`
6. `list_change_requests` — `GET /api/v1/afk-outcomes/change-requests`
7. `get_change_request_story` — `GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}`
8. `get_gateway_health` — `GET /health`
9. `get_correlation_issues` — `GET /api/v1/afk-outcomes/correlations`

Tool results remain structured Gateway facts. Narrative interpretation belongs to the
calling model.

`get_afk_run_story` is the only approved composite v1 tool. It may combine the
first-class AFK run-detail endpoint with the first-class run-scoped AWX execution
history endpoint because both are keyed by the same explicit `afk_run_id`. It must
not infer additional relationships.

The adapter must not:

- connect directly to Postgres, Kafka, source/collector databases, or Gateway
  implementation modules;
- expose write, reconcile, ingest, provisioning, delete, or patch operations in v1;
- provide a generic arbitrary Gateway HTTP passthrough tool;
- silently crawl paginated APIs to emulate unsupported queries;
- invent correlations, reports, or lifecycle conclusions that the base Gateway API
  does not expose.

Natural-language time expressions are resolved by the MCP caller and passed as
explicit dates/timestamps. The MCP does not use implicit "today" semantics. If an
underlying daily rollup is UTC-calendar based, the MCP must preserve that limitation
rather than claiming timezone-local precision.

Configuration starts with:

- `OPENCODE_GATEWAY_URL`
- `OPENCODE_GATEWAY_API_KEY`

## Consequences

### Positive

- Gateway remains the single source of truth for observability/reporting semantics.
- MCP clients receive a small surface aligned with real user questions.
- Tool schemas stay stable even if internal Gateway storage changes.
- Authentication and transport are isolated in one adapter.
- The MCP can be reused by multiple clients without coupling those clients to
  FastAPI-specific details.
- Missing capabilities become visible Gateway API requirements rather than hidden
  MCP-side workarounds.

### Trade-offs

- Some useful questions remain unavailable until the Gateway adds a first-class API.
- The MCP may need small parameter translations, for example provider to the
  Gateway's run `origin` filter.
- A dedicated adapter adds another deployable component and its own compatibility
  tests.
- UTC-based daily rollups cannot be made truly timezone-local by the MCP without
  changing the Gateway reporting surface.

## Follow-up

When the dedicated `opencode-gateway-mcp` repository is created:

1. seed it with the MCP context document;
2. implement the nine read-only tools against the documented Gateway APIs;
3. add contract tests using representative Gateway responses;
4. keep Gateway/API enhancements separate from MCP implementation issues;
5. review any future write capability through a new ADR rather than extending v1 by
    default.

## Amendment 2026-10-08 — v1.1: add `list_change_requests`

**Status: accepted (amends the eight-tool v1 as shipped).**

PR [#770](https://github.com/weiyentan/opencode-gateway/pull/770)
(`feat/mcp-change-request-summary`) added `list_change_requests` backed by
`GET /api/v1/afk-outcomes/change-requests` — the same per-change-request
summary rows used by the Gateway frontend (provider, repository,
`external_id`, provider lifecycle state, `total_estimated_cost_usd`,
latest linked activity, execution counts, with explicit `limit`/`offset`
pagination and no silent crawling or cost reconstruction).

Code, tests, and CI (`opencode-gateway-mcp`, `mcp-publish.yml`) already assert
**nine** approved read-only tools. The original ADR text listed eight; this
amendment corrects the governing record to nine and adds the ninth catalogue
entry above. All other ADR 0031 constraints remain unchanged (read-only,
published Gateway API only, no direct Postgres/Kafka/AWX/collector access,
no generic passthrough, no silent crawling, no invented correlation, UTC
calendar and null-preservation rules).

History preserved: the initial acceptance enumerated eight tools (items 1–8
before `list_change_requests`); this amendment extends the approved set to
nine. No superseding ADR is required — ADR 0031 remains the governing
decision as amended.
