# OpenCode Gateway

*An observability and lifecycle-recording service for OpenCode and AFK automation.*

OpenCode Gateway records what OpenCode sessions and AFK automation actually
do. Collectors feed usage and execution-transcript observations into
reporting APIs consumed by Aurora Glass; provider webhook observations
(relayed as normalized lifecycle events) lead to Gateway-owned lifecycle
records that tie an AFK Run to its AWX execution attempts, OpenCode
sessions, change-request outcomes, and cost. Platform engineers and agent
orchestrators (like Paperclip) use the Gateway to observe OpenCode and AFK
activity at scale.

> **Domain language:** the canonical vocabulary used here — Gateway,
> Aurora Glass, OpenCode Serve, Runner VM, AFK Run, `afk_run_id`,
> `awx_job_id`, `external_session_id`, `source_event_id`, `change_request`,
> and the status vocabularies — is defined precisely in
> [CONTEXT.md](CONTEXT.md). Read it before extending the API, schema, or
> documentation. Detailed persistence, correlation, and schema semantics
> live in the [ADRs](docs/adr/) and are linked rather than duplicated here.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/status-early--development-orange.svg" alt="Status: Early Development">
  <img src="https://img.shields.io/badge/framework-FastAPI-teal.svg" alt="FastAPI">
</p>

---

## What the Gateway is

The Gateway has two connected responsibilities:

1. **OpenCode observability** — OpenCode clients (collectors) running on
   Runner VMs feed token usage, session context, todo snapshots, and
   message/part execution-transcript projections into the ingest API. The
   Gateway resolves identities, stores canonical accounting events and
   observed-message/part rows, and serves them back through the usage and
   execution-transcript reporting APIs to Aurora Glass.

2. **AFK lifecycle observability** — normalized engineering lifecycle
   events (webhook observations of issues, change requests, commits, and
   reviews) are consumed and recorded as immutable facts. The Gateway owns
   one **AFK Run** per logical lifecycle, records each **AWX execution
   binding** (one AWX job per attempt), attributes OpenCode sessions,
   derives change-request outcomes, and reports cost.

### System boundary — what the Gateway does *not* do

The Gateway is a **recording** service. It records identities,
relationships, attempts, status projections, outcomes, and costs. It does
**not** execute work:

| The Gateway records | The Gateway does **not** do |
|---------------------|-----------------------------|
| `afk_run_id` — one logical AFK lifecycle (provisioned via the recording API when a webhook batch arrives) | **Receive provider webhooks** — an external EDA gateway receives webhooks and produces normalized events to Kafka; the Gateway consumes the normalized stream and records the resulting lifecycle |
| `awx_job_id` — one execution attempt (AWX execution binding) | **Launch AWX jobs** — AWX is launched externally; it reports its job lifecycle back to the Gateway's recording API |
| `external_session_id` — one OpenCode session | **Execute OpenCode** — OpenCode Serve runs on Runner VMs; the Gateway never runs or schedules coding sessions |
| `source_event_id` — provider webhook delivery provenance | **Schedule work** — there is no job scheduler, policy engine, or orchestration loop in the Gateway |
| `change_request` — the canonical GitHub PR / GitLab MR identity | **Mutate provider resources** — the Gateway never opens, closes, merges, or labels provider issues/PRs/MRs |
| Execution outcomes, AFK Run status projections, engineering outcomes | |

In short: producers (collectors, AWX, the EDA gateway) report **to** the
Gateway; Aurora Glass and orchestrators read **from** it. Nothing upstream
is driven by the Gateway.

---

## Architecture Overview

The Gateway is built as layered concerns:

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **API Layer** | `app/api/` | REST endpoints grouped by concern: health, admin client/credential CRUD, usage ingest and cursor recovery, usage reporting (aggregates, records, sessions, agent runs), execution-transcript reporting (sessions, children, messages, parts, timeline, tool-calls), AFK run + execution-binding + change-request recording, AFK outcome reporting (runs, entities, correlations, change-request summaries), closure relationships, and operator-gated reporting reads. |
| **Core Engine** | `app/core/` | Pydantic settings (`GATEWAY_` env prefix), application factory, logging with secret redaction, auth middleware (API key, collector credentials, operator token), envelope middleware, telemetry, identity/secret helpers. |
| **Database Layer** | `app/db/` | asyncpg connection pool, SQLAlchemy ORM models, Alembic migrations (auto-applied at startup), advisory-lock utilities. |
| **AFK Domain** | `afk_outcomes/` | Pure-domain AFK outcome package (models, serialization, correlation engine, closure-episode projector, run-status policy, provider adapters, repository Protocol) — imports nothing from `app`. |
| **Consumers** | `app/consumer/` | Kafka consumer bridge (reads `opencode-usage`, POSTs to `/ingest`) and the AFK Outcome Consumer (reads `engineering.events.normalized`, writes canonical engineering events). Separate companion containers, not part of the Gateway API process. |
| **Frontend** | `frontend/` | **Aurora Glass** — a separate browser-based telemetry dashboard that consumes the Gateway API. Delivered as its own container (see ADR 0005). |

### Two flows, one store

```
OPENCODE OBSERVABILITY FLOW

  Runner VMs / OpenCode Serve
      │  (collectors read OpenCode SQLite: usage records, session
      │   context, todo snapshots, message/part transcripts)
      ▼
  /ingest  ──────────────►  usage_events (canonical accounting)
      │                       observed_messages / observed_parts
      │                       sessions aggregates
      ▼
  Usage + Execution-Transcript APIs  ──►  Aurora Glass / Paperclip
```

```
AFK LIFECYCLE OBSERVABILITY FLOW

  provider webhook ──► EDA gateway (external) ──► engineering.events.normalized (Kafka)
        │                     │                            │
        │                     │                            ▼
        │                     │              AFK Outcome Consumer ──► engineering_events
        │                     │                                    (immutable facts)
        │                     │
        │                     │
        │   EDA gateway records:                 AWX (external) records:
        │     • POST .../executions/runs           • POST  .../afk/executions
        │       (provision the AFK Run)              (running + terminal outcomes)
        │     • POST .../runs/{id}/change-request  • PATCH .../executions/{awx_job_id}
        │       (bind the change request)            (running → terminal transition)
        └───────────────────┬──────────────────────────┘
                            ▼
  afk_runs ─ one row per afk_run_id, the logical lifecycle ──► execution_bindings
  (provisional lifecycle)                                        (one row per awx_job_id / attempt)
        │
        ├── afk_run_sessions    (external_session_id attribution to the run)
        └── change-request binding (provider + repository + PR/MR number)
        │
        ▼
  AFK Outcomes + Change-Request + Closure APIs  ──►  Aurora Glass
  (RunStatus projection, engineering outcome, closure relationships, cost)
```

The EDA gateway drives the recording calls at lifecycle start and at
change-request binding; AWX drives the execution-binding calls at job start
and completion (all write paths use the dedicated `awx-execution-bindings`
credential). The AFK Outcome Consumer's fact ingestion is independent of
both — it records what the provider observed.

The two flows meet at the **OpenCode session**: the AFK path attributes the
`external_session_id`s observed during an AWX execution to the owning
`afk_run_id`, and both paths' cost reporting aggregates the same canonical
usage events.

### End-to-end AFK lifecycle

One logical AFK lifecycle, end to end:

```text
1. A provider webhook for an AFK-labelled issue arrives at the EDA gateway,
   which publishes a normalized engineering lifecycle event.

2. The AFK Outcome Consumer records the fact in engineering_events. The
   EDA gateway then provisions the Gateway-owned AFK Run (calling the
   Gateway's recording API with the `awx-execution-bindings` credential):
       POST /api/v1/afk/executions/runs
       → 201 { afk_run_id: <ULID>, status: "pending" }
   The run is keyed on provider + host + source_event_id (batch provenance
   preserved as deliveries). No AWX job exists yet.

3. AWX launches the job externally and reports the attempt at start
   (AWX itself calls the Gateway's recording API):
       POST /api/v1/afk/executions
       { awx_job_id, afk_run_id, outcome: "running", source_event_id, ... }
       → 201 execution binding (idempotent by awx_job_id)

4. When the run finishes, AWX reports the terminal outcome on the same
   binding:
       PATCH /api/v1/afk/executions/{awx_job_id}
       { outcome: "completed" | "failed" | "cancelled",
         external_session_id(s), finished_at, ... }
   The transition records the OpenCode session(s) the execution ran in
   (non-erasing fill-in when they were unknown at start).
   A failed run may be retried: a new awx_job_id attaches a new binding to
   the same afk_run_id (failed-then-successful history is preserved).

5. Once the change request exists, it is bound explicitly (also by the EDA
   gateway, calling the Gateway's recording API):
       POST /api/v1/afk/executions/runs/{afk_run_id}/change-request
   (1:1 lifecycle ↔ change_request invariant; idempotent, conflicts → 409.)

6. The AFK Run's execution status is projected transactionally from its
   bindings (ADR 0027); the engineering outcome and cost are read back
   through the AFK Outcomes API together with sessions and provenance.
```

See [ADR 0026](docs/adr/0026-afk-run-id-database-relationships.md) for the
full identity model and cardinalities (1 `afk_run_id` → N AWX jobs, N
sessions, N entities, 0..1 change request).

---

## Technology Stack

| Category | Choice | Notes |
|----------|--------|-------|
| **Runtime** | Python 3.12+ | Required for new typing features and asyncio improvements |
| **Framework** | FastAPI | Async-first, Pydantic-native, OpenAPI auto-generation |
| **Database** | PostgreSQL 15+ via `asyncpg` | Direct connection pool plus SQLAlchemy ORM |
| **Migrations** | Alembic | Schema versioning — auto-applied at startup |
| **Validation** | Pydantic v2 + `pydantic-settings` | Configuration and boundary models |
| **Linting** | `ruff` | Selects: E, F, I, UP (scoped ignores, see `pyproject.toml`) |
| **Type Checking** | `mypy` (strict mode) | Full strict checking; Python 3.12 target |
| **Frontend** | Vanilla HTML/CSS/JS + nginx | Aurora Glass dashboard — no build step, served by a separate nginx container. In Docker Compose, the frontend nginx is the sole browser entrypoint and proxies API requests to the Gateway. |
| **Testing** | `pytest` + `pytest-asyncio` | `asyncio_mode = auto` |
| **Streaming** | `aiokafka` | Kafka consumers for the usage-record bridge and the AFK Outcome Consumer; separate companion containers |

---

## Getting Started

### Prerequisites

- **Python 3.12** or later
- **PostgreSQL 15** or later (or Docker for a local Postgres instance)
- `pip` or `uv` for package installation

### Installation

```bash
git clone <repo-url>
cd opencode-gateway
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

### Configuration

Copy the example environment file and adjust values for your environment:

```bash
cp .env.example .env
```

All configuration uses the `GATEWAY_` prefix and is loaded via
`pydantic-settings` (case-insensitive, `.env` file, environment variables).
Key configuration variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_ENV` | `production` | `production` (API key required) or `development` (no key needed locally) |
| `GATEWAY_API_KEY` | *(empty)* | **Admin API Key** — master bearer token protecting all non-`/health` routes (`Authorization: Bearer <key>`) |
| `GATEWAY_ALLOW_INSECURE_AUTH` | `false` | Explicit insecure-auth opt-in for production (loud warning) |
| `GATEWAY_HOST` | `0.0.0.0` | Server bind address |
| `GATEWAY_PORT` | `8000` | Server port |
| `GATEWAY_DATABASE_HOST` | `localhost` | PostgreSQL host |
| `GATEWAY_DATABASE_PORT` | `5432` | PostgreSQL port |
| `GATEWAY_DATABASE_NAME` | `opencode_gateway` | Database name |
| `GATEWAY_DATABASE_USER` | `opencode` | Database user |
| `GATEWAY_DATABASE_PASSWORD` | *(empty)* | Database password |
| `GATEWAY_DATABASE_MIN_CONNECTIONS` | `2` | asyncpg pool minimum size |
| `GATEWAY_DATABASE_MAX_CONNECTIONS` | `10` | asyncpg pool maximum size |
| `GATEWAY_DATABASE_CONNECTION_TIMEOUT` | `30` | Connection timeout in seconds |
| `GATEWAY_DATABASE_MAX_INACTIVE_CONNECTION_LIFETIME` | `1800` | Max lifetime (seconds) of an inactive connection before asyncpg closes it |
| `GATEWAY_DATABASE_TIMEOUT_SECONDS` | `5` | Per-query timeout budget in seconds |
| `GATEWAY_STATUS_COMPUTATION_TIMEOUT_SECONDS` | `2` | `_compute_status` timeout budget in seconds |
| `GATEWAY_QUIET_THRESHOLD_MINUTES` | `15` | Agent run status: last message within this window → `running` |
| `GATEWAY_STALE_THRESHOLD_HOURS` | `2` | Agent run status: quiet beyond this (but within the unknown threshold) → `stale` |
| `GATEWAY_UNKNOWN_THRESHOLD_HOURS` | `48` | Agent run status: quiet beyond this → `unknown` |
| `GATEWAY_TOTAL_REQUEST_TIMEOUT_SECONDS` | `20` | Endpoint total request timeout budget in seconds |
| `GATEWAY_OPERATION_TIMEOUT_MS` | `30000` | Default per-operation timeout budget (milliseconds). Read directly by the telemetry module (`app/core/telemetry.py`) and applied when an operation specifies no explicit budget |
| `GATEWAY_GRAFANA_BASE_URL` | `http://localhost:3000` | Base URL for Grafana (used to build Loki drill-down links in reporting API responses) |
| `GATEWAY_KAFKA_BROKERS` | `localhost:9092` | Kafka bootstrap brokers (comma-separated) — used by the consumer bridge |
| `GATEWAY_KAFKA_TOPIC` | `opencode-usage` | Kafka topic for usage records |
| `GATEWAY_KAFKA_DLQ_TOPIC` | `opencode-usage-dlq` | Dead-letter queue topic for unprocessable messages |
| `GATEWAY_CONSUMER_GROUP_ID` | `opencode-gateway` | Kafka consumer group ID (usage consumer) |
| `GATEWAY_BASE_URL` | `http://localhost:8000` | Gateway base URL (used by the consumer to POST to `/ingest`) |
| `GATEWAY_COLLECTOR_TOKEN` | | Collector bearer token for Gateway auth (used by the consumer) |
| `GATEWAY_NORMALIZED_EVENTS_TOPIC` | `engineering.events.normalized` | Normalized provider-events topic the AFK Outcome Consumer subscribes to (external; not created here) |
| `GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC` | `engineering.events.normalized.dlq` | Dead-letter queue topic for poison normalized provider-events messages |
| `GATEWAY_NORMALIZED_EVENTS_CONSUMER_GROUP_ID` | `opencode-normalized-events` | Kafka consumer group ID for the AFK Outcome Consumer (never shared with the usage consumer's `opencode-gateway` group) |
| `GATEWAY_AFK_OUTCOMES_TOPIC` | `afk.events` | Compatibility-only (ADR 0023): legacy `afk.events` command topic. Settings still accepts it, but the AFK Outcome Consumer no longer reads it (never consumed — retention of `afk.events` is a Kafka-side concern) |
| `GATEWAY_AFK_OUTCOMES_DLQ_TOPIC` | `afk.events-dlq` | Compatibility-only: legacy AFK outcome DLQ topic (never consumed by the current consumer path) |
| `GATEWAY_AFK_OUTCOMES_PROVIDER` | `github` | Source provider for bounded reconciliation windows (`github` or `gitlab`) |
| `GATEWAY_AFK_OUTCOMES_REPOSITORY` | *(empty)* | Full owner/repo (or group/project) name the AFK consumer/backfill reconciles against. Required when the AFK Outcome Consumer is enabled (fails fast at startup) |
| `GATEWAY_AFK_OUTCOMES_CONSUMER_ENABLED` | `false` | Whether this process runs the AFK Outcome Consumer / backfill path (the read-only Gateway API process does not need it) |
| `GATEWAY_AFK_OUTCOMES_RECONCILE_CADENCE_SECONDS` | `3600` | Cadence for the preserved manual/archival reconciliation path (`_reconcile_loop`); not scheduled by the consumer's Kafka-only startup |
| `GATEWAY_AFK_OUTCOMES_RECONCILE_WINDOW_SECONDS` | `86400` | Bounded reconciliation window size in seconds (backfill / archival path) |
| `GATEWAY_AFK_OUTCOMES_MAX_RETRIES` | `5` | AFK Outcome Consumer reliability: total persistence attempts before a message is DLQ'd (min 1 — 0 or negative is rejected) |
| `GATEWAY_AFK_OUTCOMES_INITIAL_BACKOFF_SECONDS` | `1.0` | Initial inter-attempt delay (seconds) for the bounded exponential backoff with jitter |
| `GATEWAY_AFK_OUTCOMES_MAX_BACKOFF_SECONDS` | `60.0` | Upper cap (seconds) on the exponential backoff delay |
| `GATEWAY_AFK_IMPLEMENTATION_JOB_TEMPLATE_IDS` | *(empty)* | Comma-separated AWX job-template IDs for develop-loop executions; lets the change-request detail classify execution purpose as `implementation` |
| `GATEWAY_AFK_REVIEW_JOB_TEMPLATE_IDS` | *(empty)* | Comma-separated AWX job-template IDs for review executions; purpose classification `review` |
| `GATEWAY_OPERATOR_TOKEN` | *(empty)* | **Operator Token** — gates operator-only read surfaces (delivery payload, state trails). Presented in the dedicated `X-Operator-Token` header (never `Authorization`). Distinct from `GATEWAY_API_KEY` and collector credentials; fails closed (403) when unset |
| `GATEWAY_TOOL_PAYLOAD_MAX_CHARS` | `4096` | Execution transcript (ADR 0016): per-field character cap for tool input/output payloads stored in `observed_tool_calls` (truncated at ingest; verbatim content stays in `observed_parts`) |
| `GATEWAY_PART_DATA_MAX_CHARS` | `65536` | Execution transcript (ADR 0016): verbatim character cap for `message`/`part` payloads stored in the `data` JSONB column (truncated at ingest with a `truncated` marker) |
| `GATEWAY_TRANSCRIPT_RETENTION_MESSAGES_DAYS` | `365` | Retention window for `observed_messages` (enforced by `scripts/retention_transcripts.py`) |
| `GATEWAY_TRANSCRIPT_RETENTION_PARTS_DAYS` | `90` | Retention window for `observed_parts` |
| `GATEWAY_TRANSCRIPT_RETENTION_TOOL_CALLS_DAYS` | `90` | Retention window for `observed_tool_calls` |
| `GATEWAY_RETENTION_AFK_AGGREGATES_DAYS` | `0` | Retention tier (ADR 0022): aggregates (`afk_runs`, `afk_run_sessions`) — `0` = never swept (indefinite); min 0 — negatives are rejected |
| `GATEWAY_RETENTION_AFK_METADATA_DAYS` | `365` | Retention tier (ADR 0022): metadata — 12 months; min 0 (`0` = never swept) |
| `GATEWAY_RETENTION_AFK_PAYLOAD_DAYS` | `90` | Retention tier (ADR 0022): redacted payload storage; min 0 (`0` = never swept) |
| `GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS` | `30` | Retention tier (ADR 0022): DLQ operational max — records strictly older than this on `engineering.events.normalized.dlq` are escalated by the DLQ sweep (`--dlq-sweep`) to `engineering.events.normalized.dlq-expired`; min 0 (`0` = never swept) |
| `GATEWAY_ACTIVE_TOKENS_DEPRECATION_SUNSET` | `2026-11-20T00:00:00+00:00` | Sunset instant for the deprecated `active_tokens` field (`input + output`). While the current server instant is strictly before this instant, every usage query response carries `Deprecation: active_tokens; sunset=<ISO-8601>`. Set a past date to end the 90-day window (header stops being emitted) |

> **Note:** The Gateway supports **graceful degradation** — if PostgreSQL is
> unreachable at startup, the app still starts and the health endpoint
> returns `"database": "disconnected"` instead of crashing.

> **Observability:** The Gateway emits structured timing log events —
> `request.completed` (per-request wall-clock duration, status code,
> endpoint, correlation ID), `operation.completed` (per-database-query/
> operation duration and success), and `operation.timeout` (deadline
> expiry, with the budget). Event data lives in structured `extra` fields,
> never in interpolated log strings, and contains no raw SQL or sensitive
> payload data. Each request receives a correlation ID propagated via the
> `X-Correlation-ID` request/response header; operation events within the
> request inherit it, which helps correlate latency in log aggregation
> tools.

### Run

**Development (standalone Gateway)** — starts the API server without the
frontend container:

```bash
GATEWAY_ENV=development python -m app
```

**Production**:

```bash
uvicorn app.main:app
```

**Run tests**:

```bash
pytest tests/ -v
```

Performance profiling benchmarks (`tests/test_read_path_perf.py`) are
marked `profiling` and excluded from the default run (`-m not profiling`).
Run them explicitly with `pytest tests/test_read_path_perf.py -m profiling`.
Baseline JSONs in `tests/fixtures/` are committed; set
`REGENERATE_BASELINES=1` to force regeneration, and note profiling output
is written to a gitignored `tests/fixtures/profiling-output/` directory.

### Verify

When running the Gateway standalone in development mode:

```bash
curl http://localhost:8000/health
```

Expected response (example):

```json
{"status":"ok","data":{"status":"ok","version":"0.1.0-dev","database":"connected","last_ingest_timestamp":null,"collectors":[],"source_databases":[]}}
```

Interactive API docs are served at `/docs` (OpenAPI at `/openapi.json`).
When running with Docker Compose they are reachable through the frontend
proxy at `http://localhost:8080/docs`.

> **A note on environments:** with `GATEWAY_ENV=development` no API key is
> required (local convenience). In `production`, `GATEWAY_API_KEY` is
> mandatory and every non-`/health` route rejects requests without
> `Authorization: Bearer <api-key>`.

**Dashboard:** When running with Docker Compose (see below), open
[http://localhost:8080/](http://localhost:8080/) in a browser to view the
**Aurora Glass** telemetry dashboard. It displays KPIs, model-mix charts,
operational events, collector health, agent/LLM usage, recent sessions, and
AFK outcomes — auto-refreshing every 30 seconds (client metadata is cached
for 10 minutes). The frontend is served by a separate nginx container that
proxies API requests to the Gateway.

---

## Running with Docker (Same-Origin Local Stack)

The Docker Compose stack runs Aurora Glass and the Gateway as separate
containers behind a single browser origin. The frontend nginx is the sole
entrypoint — it serves the Aurora Glass dashboard and proxies API requests
to the Gateway.

```bash
cp .env.example .env
docker compose up -d
curl -f http://localhost:8080/health    # proxied to gateway by frontend nginx
```

### Services

| Service     | Container               | Host Port | Internal Port | Description                                            |
|-------------|-------------------------|-----------|---------------|--------------------------------------------------------|
| **frontend**| `opencode-frontend`     | 8080      | 80            | Aurora Glass dashboard + nginx reverse proxy for API   |
| **gateway** | `opencode-gateway`      | —         | 8000          | FastAPI application (internal — no host ports)         |
| **postgres**| `opencode-gateway-db`   | 5432      | 5432          | PostgreSQL 15 (Alpine) with persistent volume          |
| **kafka**   | `opencode-gateway-kafka`| —         | 9092          | Local KRaft Kafka broker (single node; internal — no host ports). Provides the broker the streaming consumers read from; the provider-events topic is external (produced elsewhere, not created here) |
| **afk-outcomes-consumer** | `opencode-afk-outcomes-consumer` | — | — | AFK Outcome Consumer — reads the normalized provider-events topic (`engineering.events.normalized`) in its own group (`opencode-normalized-events`) and writes canonical engineering events to Postgres (Kafka-only startup; terminal states are converged by the operator-invoked AFK Backfill CLI) |

> **Same-origin architecture:** The frontend nginx serves static files at
> `/` and proxies `/api/*`, `/health`, `/admin/*`, `/docs` and
> `/openapi.json` to `http://gateway:8000`. This avoids CORS entirely — the
> browser talks to a single origin. The Gateway is not directly accessible
> from the host; all traffic flows through the frontend proxy.

---

## Authentication

The Gateway uses a layered credential model. Three credential types are
never shared across pipelines (ADR 0022, CONTEXT.md):

| Credential | Env / store | Transport | Gates |
|------------|-------------|-----------|-------|
| **Admin API Key** | `GATEWAY_API_KEY` | `Authorization: Bearer <key>` | **Every** non-`/health` route, via `ApiKeyMiddleware` (layer 1 of Two-Layer Auth) |
| **Collector Credential** | `collector_credentials` rows (SHA-256 `token_hash`), owned by an OpenCode Client | `Authorization: Bearer <token>` | The `/ingest` and `/cursor` collector paths, the reporting-ingest write path, and the AFK execution-binding write path — via `require_collector_token` (layer 2) |
| **Operator Token** | `GATEWAY_OPERATOR_TOKEN` | `X-Operator-Token` header (never `Authorization`) | Operator-only read surfaces: `GET /api/v1/reporting/resources`, `/resources/detail`, `/session-links`, via `require_operator_token`. Fails closed when unset |

**Two-Layer Auth** on `/ingest`: (1) the bearer token must match the Admin
API Key at the middleware; (2) the SHA-256 hash of the same token must be a
non-revoked `collector_credentials` row. A collector therefore uses either
the Admin API Key itself (with its hash registered as a bootstrap
credential) or a provisioned collector token that also satisfies
`GATEWAY_API_KEY` (see [ADR 0007](docs/adr/0007-two-layer-collector-auth.md)).

**The dedicated `awx-execution-bindings` client** (ADR 0024): the AFK
execution-binding and lifecycle write paths — `POST /api/v1/afk/executions`,
`PATCH /api/v1/afk/executions/{awx_job_id}`,
`POST /api/v1/afk/executions/runs`, and
`POST /api/v1/afk/executions/runs/{afk_run_id}/change-request` — accept
**only** collector credentials attributable to the dedicated integration
client named `awx-execution-bindings` (module constant
`AWX_EXECUTION_BINDING_CLIENT_NAME`). The request must pass both existing
layers — the bearer token must match the Admin API Key at the middleware
*and* its SHA-256 hash must be a non-revoked `collector_credentials` row
owned by that client (register the Admin API Key's hash as a credential of
the client, the standard bootstrap pattern). A valid credential owned by
any other client (including the usage collector's `opencode-collector`) is
rejected with `403`. The execution-binding **read** endpoints remain
protected by the global API-key middleware alone.

**The Operator Token** does not replace the Admin API Key: a request to an
operator-only surface must pass both gates, with the Admin API Key on
`Authorization` and the operator token on `X-Operator-Token`. The Admin API
Key does **not** satisfy the operator gate.

Client and credential administration happens through the admin API below
(`POST /admin/clients`, `POST /admin/clients/{id}/tokens`).

---

## API Reference

All responses use the `{status, data, error}` envelope. Success responses
are `{"status": "ok", "data": ...}`; errors are
`{"status": "error", "error": {"code": ..., "message": ...}}`. Every route
except `/health` is protected by the Admin API Key; the write and
operator-only routes add the gates described under
[Authentication](#authentication).

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Application health check. Returns `status`, `version`, `database` connectivity, collector status (healthy/stale/unknown per credential), source-database health, and last-ingest timestamp. Exempt from API-key auth. Graceful — always returns 200 even if the database is down. |

### Admin — Client Registry

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/clients` | Register a new OpenCode client (name + optional description). |
| `GET` | `/admin/clients` | List all registered clients. |
| `GET` | `/admin/clients/{id}` | Get a client by ID, including its credential tokens (metadata only). |
| `PATCH` | `/admin/clients/{id}` | Update a client (supplied fields only). |
| `DELETE` | `/admin/clients/{id}` | Soft-delete a client (sets `is_active=false`). |

### Admin — Token Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/clients/{id}/tokens` | Provision a new collector bearer token. **The raw token is returned once.** |
| `GET` | `/admin/clients/{id}/tokens` | List credential tokens for a client — metadata only, no raw tokens. |
| `POST` | `/admin/clients/{id}/tokens/{token_id}/revoke` | Revoke a collector credential token immediately. |

### Admin — Replay-Safe Usage Accounting

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/quarantined-identities` | List active (uncleared) quarantined source identities, paginated (`limit`/`offset`, default `limit=50`). Optionally narrows to one client via `client_id`. |
| `POST` | `/admin/resolve-source-identity` | Resolve a quarantined source identity into a canonical parent. Body: `quarantine_id`, `resolving_identity_id`, optional `reason`. Returns 404 for an unknown quarantine, 400 if the quarantine is already cleared or the resolving identity does not exist. |
| `POST` | `/admin/reconcile-historical-duplicates` | Reconcile duplicate canonical events in `usage_events`. Body: `dry_run` (required), optional `client_id`, `date_from`, `date_to`. `dry_run: true` scans duplicate `source_record_id` groups and returns a preview without writing; `dry_run: false` removes non-canonical rows (earliest `first_ingested_at`, lowest `id` tiebreaker), preserves them as ingest-attempt history, and rebuilds affected session aggregates. Serialised per client with an advisory lock. |

### Telemetry Ingest (OpenCode observability write path)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest` | Accept a batch of normalized usage records from a collector. First-delivery records create canonical events in `usage_events`; every delivery processed through the canonical layer is recorded as an ingest attempt (audit trail covering `accepted`, `duplicate`, `updated`, `quarantined`, and `conflict` outcomes). Per-record outcomes: `accepted` (new canonical event), `duplicate` (idempotent replay), `updated` (replay merged — event corrected and session aggregates delta-adjusted), `quarantined` (source identity quarantined or overlapping), `conflict` (canonical event owned by a different unresolved identity), `rejected` (validation failure or internal error). All outcomes are 2xx at batch level so the consumer commits Kafka offsets; invalid payloads and 4xx/5xx responses route to the DLQ. Batch-level collections (v1.2+) carry Session Contexts, projects, and Todo Snapshots; schema v1.3 adds optional `messages` and `parts` collections (execution-transcript projections, ADR 0016). Optional replay metadata fields (`replay_id`, `replay_requested_start`, `replay_delivery_mode`) mark replay deliveries. Empty-batch heartbeats supported. Authenticated via collector bearer token. |

### Collector Cursor

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/cursor` | Return cursor state (last ingestion timestamp, record count, active status) for a source database. Called by collectors on startup to determine where to begin reading from the SQLite database. Authenticated via collector bearer token. Returns 404 for unknown source database IDs. |

### Usage Reporting (OpenCode observability read path)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/usage/aggregates` | Token/cost aggregates grouped by dimension (`client`, `model`, `session`, `day`, `week`, `month`, `project`, `agent` — comma-separated). Date-range filterable. Each row exposes `cache_hit_ratio` (`cache_read / (input + cache_read)`, rounded to 4 decimals; `null` when no input) and `provider_breakdown` (per-provider record counts, `unknown` for null/empty) alongside a deprecated `active_tokens` (`input + output`) computed field. |
| `GET` | `/api/v1/usage/records` | Paginated raw usage records. Supports filtering by `client_id`, `model`, `session_id`, date range, sorting, and pagination (`limit`/`offset`). Default sorting is by source-created message time (`sort_by=source_created_at`, `COALESCE(source_created_at_tz, reported_at)`), not ingest time. Includes `provider` (null → JSON `null`), raw token fields `cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens`, the deprecated `active_tokens` computed field, and `loki_search_url` for Grafana drill-down. |
| `GET` | `/api/v1/usage/sessions` | Session-level summaries with token/cost totals, message counts, Loki drill-down URLs, and `session_title` from Session Context. Paginated. Surfaces `total_reasoning_tokens` (read-time sum from `usage_events`; the `sessions` table carries no reasoning aggregate — ADR 0012) and `primary_provider` (most frequent provider by record count, alphabetical tie-break; `null` when none observed), plus the deprecated `active_tokens` computed field. |
| `GET` | `/api/v1/usage/agent-runs` | Paginated list of Agent Run Summaries with `session_title` and `model` enrichment from Session Context (model derived from the Session Context row — the canonical source). Surfaces `total_reasoning_tokens`, `primary_provider`, and the deprecated `active_tokens` computed field. |
| `GET` | `/api/v1/usage/agent-runs/{session_id}` | Detail view for a specific agent run, including `session_context` (title, model, code changes) and `todo_rows` (latest OpenCode todo snapshot) alongside usage data. |
| `GET` | `/api/v1/usage/records-with-context` | Paginated usage records enriched with `session_title`, `project_label`, and `agent`. Supports `group_by` aggregation by `project`, `agent`, `session`, or `model`. Exposes the deprecated `active_tokens` computed field alongside the raw token fields. |

> **Note:** Usage query endpoints read from the canonical `usage_events`
> table (replay-safe accounting, migration 0021). API contracts are
> unchanged. The legacy `opencode_usage_records` table is still written at
> ingest but is no longer the query source.

> **Deprecation:** The `active_tokens` field (`input + output`) on every
> usage query row is deprecated in favour of the raw token fields
> (`cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens`). While
> the current server instant is strictly before
> `GATEWAY_ACTIVE_TOKENS_DEPRECATION_SUNSET` (default `2026-11-20T00:00:00+00:00`),
> every `/api/v1/usage/*` response carries
> `Deprecation: active_tokens; sunset=<ISO-8601>`. Exactly at or after the
> sunset the header is omitted. The ingest path also normalises empty-string
> `provider`/`mode`/`finish_reason` values to `NULL` (null provider
> serialises as JSON `null`; aggregates group null/empty under `unknown`).

### Execution Transcript (OpenCode observability read path)

Read-only endpoints exposing the execution-transcript observability slice
(ADR 0016): message- and part-level execution data ingested from the
OpenCode runtime's `message`/`part` SQLite tables. Transcripts are event
timelines (what a run did), kept distinct from usage accounting (how much
it cost). The transcript vocabulary is described in `CONTEXT.md` (Execution
Transcript, Observed Message, Observed Part, Observed Tool Call, Transcript
Event Type, Transcript Timeline).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/execution/sessions/{session_id}` | Transcript session header: identity, parent/child linkage, counts, and time window. 404 for an unknown internal session ID. |
| `GET` | `/api/v1/execution/sessions/{session_id}/children` | Direct child subagent sessions (offset/limit paginated). |
| `GET` | `/api/v1/execution/sessions/{session_id}/messages` | A session's Observed Messages, chronologically, keyset-paginated. Filterable by `agent`, `role`, and `from`/`to` time range. |
| `GET` | `/api/v1/execution/sessions/{session_id}/parts` | A session's Observed Parts, chronologically, keyset-paginated. Filterable by `part_type` (Transcript Event Type), `tool_name`, and `from`/`to`. |
| `GET` | `/api/v1/execution/sessions/{session_id}/timeline` | Unified Transcript Timeline across the root session and its descendant subagent sessions, each event annotated with owning session, agent, and generation `depth`. Optional `max_depth` bound. |
| `GET` | `/api/v1/execution/tool-calls` | Global Observed Tool Call query. Filterable by `session_id`, `agent`, `tool_name`, `tool_status`, and `from`/`to`. |

The messages, parts, timeline, and tool-calls endpoints use keyset (cursor)
pagination via `after=<cursor>` (append-only `source_created_at` ordering,
stable under concurrent ingest); `limit` defaults to 100 and is capped at
1000. The children endpoint uses offset/limit pagination. Payloads are
redacted (secret-like keys) and truncated at ingest
(`GATEWAY_TOOL_PAYLOAD_MAX_CHARS` / `GATEWAY_PART_DATA_MAX_CHARS`), so the
API serves only the already-redacted, already-truncated store.

### AFK Lifecycle Recording (write path)

Endpoints that record the Gateway-owned AFK lifecycle. The write paths use
the dedicated `awx-execution-bindings` collector credential (ADR 0024); the
read paths are protected by the Admin API Key alone. Identity semantics are
defined in ADR 0026 (`afk_run_id` = logical lifecycle, `awx_job_id` = one
execution attempt, `external_session_id` = one OpenCode session,
`source_event_id` = provider webhook delivery provenance, `change_request`
= the lifecycle's single optional PR/MR identity).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/afk/executions/runs` | Provision one provisional AFK Run lifecycle at webhook ingress. Body carries the source provenance (`provider` + `host` + `source_event_id`), the provider-qualified repository identity (normalized at the boundary), `trigger_type` (`eda`/`manual`/`scheduled`/`backfill`/`recovery`), an optional title, an optional ordered `deliveries` batch (first delivery stored as `first_delivery_id`, every identity kept as batch provenance; non-erasing), and an optional `recovered_from_afk_run_id` (required when `trigger_type=recovery`; provisions a recovery lifecycle without mutating its predecessor — unknown predecessor → `404`). Idempotent on `provider + host + source_event_id` (partial unique index `uq_afk_runs_provisioning_key`, migration 0039): new key → `201` (status `pending`), identical replay → `200`, conflicting replay → `409`. |
| `POST` | `/api/v1/afk/executions` | Persist one AWX execution binding. Two-phase lifecycle: `outcome="running"` provisions the execution at AWX start, attached to the pre-provisioned `afk_run_id` (change request and sessions optional, still unknown); terminal outcomes (`completed`/`failed`/`cancelled`) may be persisted directly — failed/cancelled persist without a change request or a session, while a direct-terminal `completed` must carry both the change request and the resolved session(s). Session attribution is the deduplicated, order-preserving `external_session_ids` collection (the legacy singular `external_session_id` normalizes into it; the first entry is the primary session). `source_event_id` is required when `trigger_type=eda`. Idempotent by `awx_job_id`: identical replay → `200` (no mutation), conflicting data → `409`. A new `awx_job_id` for the same resource/run is a valid retry. `afk_run_id` is required for every new binding (unknown run → `404`). Never stores raw tokens, stdout, prompts, or arbitrary AWX payloads (bounded, redacted failure metadata only). |
| `PATCH` | `/api/v1/afk/executions/{awx_job_id}` | Transition one binding from `running` to a terminal outcome (`completed`/`failed`/`cancelled`). Idempotent identical replay → `200`; conflicting payload → `409` (history is never overwritten); unknown AWX job → `404`. `resource` and the session fields (`external_session_id` / `external_session_ids`) are non-erasing fill-ins for identities that only became known at completion — an omitted field never erases a stored value; a `completed` outcome never carries failure metadata and must end with both a change-request identity and a resolved session (repository-enforced after merge). |
| `POST` | `/api/v1/afk/executions/runs/{afk_run_id}/change-request` | Bind one change request to a lifecycle (the 1:1 lifecycle ↔ change_request invariant, available before review processing and independent of the correlation engine). Idempotent per lifecycle; a different change request on the same run, or a change request already owned by another run → `409`; unknown lifecycle → `404`. |
| `GET` | `/api/v1/afk/executions/runs/by-change-request` | Resolve a provider-qualified change-request identity (`provider` + `repository` + `external_id`) to its owning `afk_run_id` via the explicit durable binding on `afk_runs`. Read-only (Admin API Key only). `400` invalid identity, `404` unknown/unbound, `409` impossible ownership conflict. Follow-up GitHub PR / GitLab MR webhooks use this to continue the same lifecycle. |
| `GET` | `/api/v1/afk/executions` | List all execution bindings for one provider resource (`provider`, `repository_url`, `entity_type=change_request`, `entity_number` required). Returns the full failed-to-successful execution history in deterministic order (earliest first). |
| `GET` | `/api/v1/afk/executions/{awx_job_id}` | Return one execution binding by AWX job ID (or 404). Exposes the approved execution metadata: AWX job identity, gateway-assigned `binding_id`, canonical `change_request` resource (nullable), `external_session_id`/`external_session_ids` session attribution (empty for bindings with no resolved session), `afk_run_id` (null for legacy rows), `trigger_type`, `source_event_id`, branch, title, timestamps, outcome, and bounded failure metadata. |

### AFK Outcomes (read path)

Read-only endpoints exposing the AFK outcome read-model — the
Gateway-owned AFK Runs recorded through the lifecycle/execution-binding
path above, plus runs reconstructed from provider engineering activity by
the backfill/correlation engine, all with per-link provenance. The AFK
outcome vocabulary is described in `CONTEXT.md` (AFK Run, `afk_run_id`,
RunStatus, EngineeringOutcome, EngineeringOutcomeStatus, change_request,
correlation_confidence, correlation_method, resolver_version,
correlation_source, owning_change_request_id, Provisional Link). Backfill
remains CLI-only (`scripts/afk_backfill.py`); these endpoints never write.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/afk-outcomes/runs` | Paginated list of AFK Runs. Filterable by `repository`, window (`started_from`/`started_to`, `finished_from`/`finished_to`, `seen_from`/`seen_to` ISO-8601 bounds), `status` (RunStatus value: `running`/`completed`/`blocked`/`stale`/`timed_out`/`failed`/`cancelled`), `outcome` (EngineeringOutcomeStatus value: `merged`/`closed`/`abandoned`/`open`), and `origin` (provider). 400 on invalid enum/date values or inverted windows. Ordered by `last_seen_at DESC`. |
| `GET` | `/api/v1/afk-outcomes/runs/{afk_run_id}` | Full chain for one run: run aggregate, EngineeringOutcome, engineering entities grouped by type (issues, change_requests, reviews, commits, merge_events) each carrying correlation and lineage provenance (`correlation_method`, `correlation_confidence`, `evidence`, `resolver_version`, `correlation_source`, `owning_change_request_id`) and a `provisional` marker, linked sessions with usage/cost aggregates, `parent_session_id` for the nested session tree, distinct agents, and a run-level usage aggregate (Active Tokens = input + output; cache read/write as siblings). 404 for unknown `afk_run_id`. |
| `GET` | `/api/v1/afk-outcomes/entities` | Paginated engineering entities with their run links, correlation and lineage provenance, `superseded_at` (superseded state surfaced, not hidden), and `provisional` marker. |
| `GET` | `/api/v1/afk-outcomes/correlations` | Paginated unresolved correlations with `method`, `correlation_confidence`, `evidence`, `resolver_version`, and `provisional=true`. Every row is attributed to a run — `afk_run_id` is NOT NULL (migration 0027) — and rows are unique per `(provider, repository, entity_type, external_id, afk_run_id, method)`, so the same entity can appear in separate rows per AFK run and evidence is never merged across runs. |
| `GET` | `/api/v1/afk-outcomes/change-requests` | Paginated change-request summaries — one row per provider/repository/change-request identity. Each row aggregates provider state (derived from observed facts), AFK automation state, total estimated USD cost (`null` when no cost telemetry is available — never zero), latest linked activity, and AWX execution counts. Filterable by `provider`, `repository`, `provider_state` (`merged`/`closed`/`open`), `automation_state` (`pending`/`running`/`completed`/`failed`/`cancelled`), and an activity window. Executions without a durable change-request identity are excluded from the row universe and never contribute counts. |
| `GET` | `/api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}` | Provider-scoped change-request detail: summary block (provider state, AFK automation state, merge/freshness enrichment, aggregate cost), linked AFK runs with link provenance, ordered AWX execution bindings (purpose, per-execution session telemetry, cost, duration, failure metadata), deduplicated linked sessions, aggregate usage/cost, and the optional provenance timeline. Strictly read-only — reads stored facts/projections only and makes no provider API calls. `400` invalid identity, `404` unknown to every durable source. |

### Closure Relationships (read path)

Read-only endpoints exposing the closure-episode projection (migration
0036; vocabulary in CONTEXT.md): the derived
change-request→issue closure relationship from immutable
`engineering_events` facts — **without claiming provider-authoritative
causation**. The API records observed facts and inferred attribution; every
response exposes `derived_at` (last successful recompute) and
`resolver_version`. Strictly read-only — reads only the DB
projection/unresolved rows and makes no provider API calls.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/closure-relationships/issues/current` | The current issue→change-request answer keyed by the issue endpoint identity (`provider`, `repository`, `external_id`): the current episode with its status, the single-candidate attribution or the unmatched/ambiguous marker, and evidence links. 404 when no closure episode exists for the issue. |
| `GET` | `/api/v1/closure-relationships/issues/episodes` | The auditable episode/evidence history for one issue — every immutable episode including `superseded` (never hidden), with endpoint identities, declaration/revocation snapshots, `resolver_version`, and status. Filterable by `status` and `closed_from`/`closed_to`. |
| `GET` | `/api/v1/closure-relationships/change-requests/issues` | Reverse lookup: the issues one change request references and/or declares closing, paginated. Filterable by `kind` (`references` / `declares_closure`). |

### Reporting (operator-gated read path)

Read-only endpoints exposing the reporting read-model (ADR 0021): ingested
resources with their current aggregate, the per-delivery state trail, and
the session links that can be provably linked to them (write path:
`app/api/reporting_ingest.py`). The surface is strictly read-only and never
derives a "completed"/"finished"/outcome state for a resource (**no
completion claims**) — it surfaces the resource's verbatim current payload
and its delivery lifecycle states (`received`, `normalized`, `published`,
`persisted`, `rejected`, `failed`) as pipeline observations.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/reporting/resources` | Paginated list of ingested resources. Filterable by any subset of the stable resource identity (`provider`, `repository_url`, `resource_type`, `resource_number`). Each item is a current aggregate: verbatim `payload`, `delivery_count`, `last_delivery_id`, `last_ingested_at`, plus the composite `resource_id` key. |
| `GET` | `/api/v1/reporting/resources/detail` | Full detail for one resource addressed by all four identity components (required query params): the current aggregate plus the per-delivery state trail (`delivery_state_trails`, chronological) plus `session_links`. 404 for an unknown resource identity. |
| `GET` | `/api/v1/reporting/session-links` | Paginated session links (`afk_run_sessions`), each surfaced as **provisional** (`provisional=True`) with an empty `source_references` list — the Gateway never fabricates a resource↔session link it cannot prove. (Exact links will populate `source_references` once exact resource↔session correlation ships.) |

These endpoints are protected by the global API-key middleware **and**
additionally require the dedicated operator token (`GATEWAY_OPERATOR_TOKEN`,
via `require_operator_token`) — delivery payload and state trails are an
operator-only read surface, and the gate fails closed when the operator
token is unprovisioned. The operator token is presented in the dedicated
`X-Operator-Token` header (never `Authorization`, which carries the Admin
API Key), so the two credentials can be distinct and both gates are
satisfiable on the same request. The reporting write path remains
`POST /api/v1/reporting/ingest/deliveries` (collector credential); the read
router never writes.

---

## Consumer Operations

### AFK Outcome Consumer

The AFK Outcome Consumer (`app/consumer/afk_consumer.py`, separate
container) reads the external provider-events topic
`engineering.events.normalized` in its **own** consumer group
(`opencode-normalized-events` — never the usage consumer's
`opencode-gateway` group), maps message types to canonical Engineering
Events via the Mapping Bridge, writes each message in a single DB
transaction (offset committed only after success), and DLQs poison
messages. Startup is Kafka-only — it makes no provider API calls and runs
no scheduled reconciliation. Terminal states (merged/closed) the topic does
not carry are converged by the operator-invoked AFK Backfill CLI over a
bounded window (the consumer's `_reconcile_loop`/`_reconcile_once` methods
are preserved for archival/manual use only).
Topic-split semantics are defined in [ADR 0023](docs/adr/0023-kafka-topic-split-commands-vs-observations.md).

### DLQ Sweep

The AFK Outcome Consumer's DLQ (`engineering.events.normalized.dlq`,
`GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC`) is bounded by an **operational max**
(`GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS`, default 30 days). Every DLQ record is
stamped with `dead_lettered_at` and `max_age_days` at producer time; the DLQ
sweep escalates records **strictly older** than the max to the durable
`engineering.events.normalized.dlq-expired` escalation topic (original
payload + reason + `dead_lettered_at` + deterministic `escalation_key` +
`escalation_reason`), so expired records are never silently lost.
Escalation content is content-stable: the `escalation_key` is a
deterministic SHA-256 over the DLQ record's own identity, so re-escalating
the same record produces an identical record (deduplicable by natural key).
In write mode the sweep commits consumed Kafka offsets after each chunk —
for each partition at its first retained (not-yet-expired) record's offset,
or past the last consumed record when the partition has no retained records
— so re-runs do **not** re-escalate already-escalated records, while
retained records are re-examined on later runs until they expire. Dry-run
never commits. Physical removal from the active DLQ is enforced by the
topic's Kafka retention configured to the same max age.

```bash
# Report the would-be-escalated records without publishing anything
python -m app.consumer.afk_consumer --dlq-sweep --dry-run

# Escalate expired records to engineering.events.normalized.dlq-expired
python -m app.consumer.afk_consumer --dlq-sweep
```

Flags:

| Flag | Description |
|------|-------------|
| `--batch-size N` | Records per consume batch (default 100) |
| `--limit N` | Process at most N records (bounded runs) |
| `--dry-run` | Report the would-be-escalated records without publishing anything |

Boundary semantics mirror the transcript retention job (ADR 0016): a record
exactly at the max-age edge is retained (strict `>`); only strictly older
records escalate; a record without a usable `dead_lettered_at` has unknown
age and is retained (never prematurely expired).

### Retention (AFK / reporting)

Retention is tiered (ADR 0022): aggregates (`afk_runs`, `afk_run_sessions`)
are indefinite (`0` days = never swept); metadata (`engineering_events`,
`delivery_log`, `delivery_state_trails`, `afk_run_entities`,
`unresolved_correlations`) defaults to 12 months; redacted payload storage
(`reporting_deliveries.payload`, `engineering_events.payload`) defaults to
90 days; the AFK Outcome Consumer's DLQ is bounded by the DLQ operational
max described above. Each tier is configurable via `GATEWAY_RETENTION_*`.
Retention boundaries use strict ordering — a row exactly at the cutoff edge
is retained; only strictly-older data expires; unknown-age data is never
prematurely expired.

### Transcript retention

Transcript tables (`observed_messages`, `observed_parts`,
`observed_tool_calls`) are higher-volume and lower-longevity than
accounting data. A scheduled job enforces the per-table windows
(`GATEWAY_TRANSCRIPT_RETENTION_*`; parts and tool calls default to 90 days,
messages to 365 days):

```bash
python scripts/retention_transcripts.py --dry-run   # report only
python scripts/retention_transcripts.py             # delete in bounded batches
```

### Closure-projection rebuild

The closure-episode projection is DB-local and rebuildable from the
immutable `engineering_events` facts. To force a full or bounded rebuild
(after a projector version bump, or to repair temporary staleness):

```bash
python scripts/rebuild_closure_projection.py --dry-run   # report only
python scripts/rebuild_closure_projection.py --confirm   # full rebuild
```

---

## Frontend Dashboard (Aurora Glass)

The Gateway ships with **Aurora Glass**, a browser-based telemetry dashboard
that visualizes observability data collected from OpenCode Serve instances
and AFK automation. It is a single-page application (SPA) built with
vanilla HTML, CSS, and JavaScript, delivered as a separate container that
consumes the Gateway API (ADR 0005) — it is not part of the Gateway service
itself.

### Access

**Docker Compose stack (recommended for local development):**

The frontend nginx container serves Aurora Glass and proxies API requests
to the Gateway. Open the dashboard at:

```
http://localhost:8080/
```

The frontend is the sole browser entrypoint — the Gateway runs internally
and is not directly accessible from the host.

### Dashboard Sections

The dashboard polls the Gateway REST API every 30 seconds (client metadata
is cached for 10 minutes) and renders:

| Section | Data Source | Description |
|---------|-------------|-------------|
| **KPI Cards** | `/health`, `/api/v1/usage/aggregates` | Active tokens, estimated cost, session count, healthy collectors, source databases |
| **Model Mix** | `/api/v1/usage/aggregates?group_by=model` | Token/cost breakdown by LLM model |
| **Operational Events** | Recent usage records | Real-time feed of incoming telemetry events |
| **Collector Distribution** | `/admin/clients` | Collector status overview (healthy/stale/unknown) |
| **Collectors Table** | `/admin/clients` + health data | Per-collector name, status, last ingest, sessions, tokens, cost |
| **Agents & LLMs** | `/api/v1/usage/records` | Per-client model usage with request counts and cost |
| **Agent Usage** | `/api/v1/usage/aggregates?group_by=agent` | Dynamic per-agent aggregate rows (token breakdown, estimated cost, request count), grouped by recorded agent identity with missing identities as `unknown`, ordered by total token usage descending |
| **Recent Sessions** | `/api/v1/usage/sessions` | Client, session title, model, token/cost totals, duration, and status |
| **Agent Runs** | `/api/v1/usage/agent-runs` | Agent run table with session title, status, model, provider, project/worktree, todo/files, costs, compact tokens, cache read/write, reasoning, last updated, and children. Detail overlay includes a Token Breakdown section (input/output/cache read/cache write/reasoning, cache hit ratio, provider) alongside session context (title, model, code changes) and todo progress |
| **AFK Outcomes** | `/api/v1/afk-outcomes/runs`, `/api/v1/afk-outcomes/runs/{afk_run_id}`, `/api/v1/afk-outcomes/change-requests` | Repository-first workflow: **Repository Summary** (normalized GitHub/GitLab list with provider badges, AFK activity counts, date-range scoping) → **Change Request List** (all PRs/MRs in the period with provider-specific PR/MR labels under the canonical `change_request` vocabulary, AFK-linked highlighting, AFK-only filter) → **Change Request Provenance Timeline** (chronological develop/review executions with phase, timestamps, status/outcome, duration, AWX job IDs, OpenCode session IDs, usage/cost, merge state vs issue-closure state, RunStatus distinct from EngineeringOutcomeStatus) with **nested session relationships** (root vs child via `parent_session_id`, deep-link to the Agent Run detail overlay, explicit missing/unresolved markers) and **relationship certainty** (resolved vs provisional/inferred distinction, confidence/method/evidence/resolver_version, dedicated Unresolved Relationships view for `ambiguous`/`unmatched`/`parked` with provenance). All views use deterministic `frontend/fixtures/*` GitHub/GitLab parity fixtures for offline regression (`node frontend/tests/test_pure_functions.js`), plus an opt-in live-provider E2E harness (`scripts/afk_e2e_test.py`, `docs/afk-e2e-validation.md`). Sessions in the chain/provenance detail that carry a resolvable internal session ID are clickable and open the existing Agent Run detail overlay; unresolved sessions stay non-clickable |

The dashboard uses the same authentication as the REST API — if the Gateway
runs in production mode (`GATEWAY_ENV=production`) with an API key, the
dashboard will need one. For local development, use
`GATEWAY_ENV=development` to run without authentication.

---

## Database Migrations

Alembic is the **single source of truth** for the production database
schema. The Gateway automatically runs migrations at startup — no manual
steps are required.

```bash
# Apply all pending migrations
alembic upgrade head

# View current revision
alembic current

# Generate a new migration
alembic revision --autogenerate -m "description of change"

# Roll back one migration
alembic downgrade -1
```

---

## Project Structure

```
opencode-gateway/
├── app/
│   ├── __init__.py
│   ├── __main__.py               # Dev entry point (python -m app)
│   ├── main.py                   # Production entry point (uvicorn)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py             # GET /health endpoint
│   │   ├── admin_clients.py      # Admin CRUD for clients + tokens
│   │   ├── admin_quarantines.py  # GET /admin/quarantined-identities
│   │   ├── admin_reconcile.py    # POST /admin/reconcile-historical-duplicates
│   │   ├── admin_resolve_source_identity.py  # POST /admin/resolve-source-identity
│   │   ├── cursor.py             # GET /cursor collector cursor endpoint
│   │   ├── ingest.py             # POST /ingest telemetry endpoint
│   │   ├── usage.py              # GET /api/v1/usage (aggregates, records, sessions, agent-runs)
│   │   ├── execution.py          # GET /api/v1/execution (sessions, children, messages, parts, timeline, tool-calls — read-only)
│   │   ├── afk_outcomes.py       # GET /api/v1/afk-outcomes (runs, entities, correlations, change-requests — read-only)
│   │   ├── afk_executions.py     # /api/v1/afk/executions (AFK lifecycle + AWX execution-binding recording)
│   │   ├── closure_relationships.py  # GET /api/v1/closure-relationships (issues/current, issues/episodes, change-requests/issues)
│   │   ├── reporting.py          # GET /api/v1/reporting (operator-gated read API)
│   │   └── reporting_ingest.py   # POST /api/v1/reporting/ingest/deliveries
│   ├── consumer/
│   │   ├── __init__.py           # Module init, exports Consumer class
│   │   ├── consumer.py           # Usage-record Kafka consumer bridge (separate container)
│   │   ├── afk_consumer.py       # AFK Outcome Consumer (own consumer group; --dlq-sweep)
│   │   └── models.py             # Consumer-side Pydantic models for ingest payloads
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── auth.py               # ApiKeyMiddleware + collector-token/operator-token deps
│   │   ├── envelope.py           # Response envelope middleware + exception handlers
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   ├── identity.py           # Token generation/hashing + canonical source identity & quarantine resolution
│   │   ├── repository.py         # Repository-URL normalization helper
│   │   ├── loki.py               # Grafana Explore URL builder
│   │   ├── logging.py            # RedactingFormatter
│   │   ├── metrics.py            # Process-wide metrics registry
│   │   ├── reconciliation.py     # Canonical replay-merge deltas + historical duplicate reconciliation
│   │   ├── reporting_aggregates.py  # Reporting aggregate enrichment
│   │   ├── secrets.py            # Secret detection utilities
│   │   ├── telemetry.py          # Request timing middleware, operation/timeout helpers
│   │   ├── timeouts.py           # Request/database timeout helpers
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── identity.py       # Pydantic schemas for clients, tokens & quarantined identities
│   │       ├── reconciliation.py # Pydantic schemas for historical reconciliation
│   │       ├── usage.py          # Pydantic schemas for usage reporting
│   │       ├── execution.py      # Pydantic schemas for the execution-transcript API
│   │       ├── afk.py            # Pydantic schemas for AFK outcomes API responses
│   │       ├── afk_lifecycle.py  # Schemas for provisional AFK run lifecycle + change-request binding
│   │       ├── execution_binding.py  # Schemas for AWX execution bindings
│   │       ├── closure_relationships.py  # Schemas for closure-relationship API responses
│   │       └── reporting.py      # Pydantic schemas for the reporting API
│   └── db/
│       ├── session.py            # DatabasePool (asyncpg wrapper)
│       ├── schema.py             # Schema management (delegates to Alembic)
│       ├── setup.py              # Migration runner + table validation
│       ├── lock.py               # Advisory locks
│       └── models/
│           ├── __init__.py
│           ├── base.py           # SQLAlchemy declarative base
│           ├── identity.py       # ORM models: OpenCodeClient, CollectorCredential
│           ├── ingest.py         # ORM models: SourceDatabase, Session, UsageRecord, etc.
│           ├── afk.py            # ORM models: AFKRun, EngineeringEntity, execution bindings, delivery log
│           ├── projection.py     # ORM models: transcript / closure projections
│           └── reporting.py      # ORM models: reporting deliveries / state trails
├── afk_outcomes/                 # Pure-domain AFK outcome package
│   ├── models.py                 # Domain models, enums, resolver versions
│   ├── repository.py             # AsyncpgOutcomeRepository (facts, runs, bindings, associations, projection)
│   ├── correlation.py            # CorrelationEngine
│   ├── closure_episodes.py       # Closure-episode projector (pure domain)
│   ├── associations.py           # Exact resource↔session association resolver
│   ├── run_status.py             # Transactional AFK run status policy
│   ├── serialization.py          # ULID helpers + serialization
│   ├── interfaces.py             # Repository Protocols
│   └── providers/                # GitHub / GitLab provider adapters
├── scripts/
│   ├── afk_backfill.py           # AFK outcome backfill/reconciliation CLI (--dry-run, --show-evidence)
│   ├── afk_e2e_test.py           # Opt-in GitHub/GitLab lifecycle E2E harness (see docs/afk-e2e-validation.md)
│   ├── rebuild_closure_projection.py  # Closure projection rebuild CLI (--since/--until, --confirm, --dry-run)
│   ├── retention_transcripts.py  # Transcript retention job (--dry-run, --limit, --batch-size)
│   ├── backfill_usage_events.py  # usage_events backfill/repair CLI
│   ├── backfill_cache_write_tokens.py  # Session cache-write aggregate correction CLI
│   ├── backfill_client_project_rollup.py # Client-project rollup recompute CLI
│   ├── backfill_tool_calls.py    # Observed-tool-call backfill CLI
│   ├── seed.py                   # Local seed helpers
│   └── validate_github_pr_path.py  # GitHub PR path validation helper
├── frontend/                     # Aurora Glass telemetry dashboard (HTML/CSS/JS SPA)
│   ├── fixtures/                 # Deterministic GitHub/GitLab AFK fixtures
│   ├── adapters/                 # Change-request view adapters
│   └── tests/                    # VM-sandbox regression coverage (node frontend/tests/test_pure_functions.js)
├── tests/                        # Backend test suite (unit + integration; see pyproject.toml)
├── docs/
│   ├── adr/                      # Architecture Decision Records (see index below)
│   ├── afk-outcome-validation.md # AFK reconstruction validation findings
│   ├── afk-outcome-contract-validation.md
│   ├── afk-e2e-validation.md     # Opt-in GitHub/GitLab lifecycle E2E operator guide
│   ├── prd/                      # PRDs (AFK run creation, AWX execution bindings, replay-safe accounting, …)
│   └── contracts/normalized-event-v1   # Producer-owned normalized-event contract artifacts
├── alembic/                      # Alembic migrations
├── k8s/                          # Kubernetes manifests (gateway + usage consumer + AFK consumer)
├── .env.example
├── docker-compose.yaml           # Same-origin local stack (frontend + gateway + postgres + kafka + consumer)
├── docker-compose.test.yml
├── docker-compose.smoke.yml
├── Dockerfile
├── Dockerfile.consumer           # Usage-record consumer container
├── Dockerfile.afk-consumer       # AFK Outcome Consumer container (separate deployment)
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Further Reading

| Document | Purpose |
|----------|---------|
| [CONTEXT.md](CONTEXT.md) | Canonical domain vocabulary for the observability + AFK lifecycle-recording service |
| [docs/adr/](docs/adr/) | Architecture Decision Records — persistence, correlation, and schema semantics |
| [docs/afk-outcome-validation.md](docs/afk-outcome-validation.md) | AFK reconstruction validation findings |
| [docs/afk-outcome-contract-validation.md](docs/afk-outcome-contract-validation.md) | AFK outcome contract validation |
| [docs/afk-e2e-validation.md](docs/afk-e2e-validation.md) | Opt-in GitHub/GitLab lifecycle E2E operator guide |
| [docs/prd/](docs/prd/) | PRDs behind the implemented slices (AFK run creation, AWX execution bindings, replay-safe usage accounting, …) |
| [docs/contracts/normalized-event-v1](docs/contracts/normalized-event-v1) | Producer-owned normalized-event contract (pinned copy) |

## Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [0001](docs/adr/0001-separate-observation-tables.md) | Separate Observation Tables Per Domain Entity | Accepted |
| [0002](docs/adr/0002-executor-plugin-interface.md) | Executor Plugin Interface Design | Superseded (#207) |
| [0003](docs/adr/0003-postgres-port-allocation.md) | Port Allocation in Postgres | Superseded (#207) |
| [0004](docs/adr/0004-gateway-no-infra-secrets.md) | Gateway Holds No Infrastructure Secrets | Accepted |
| [0005](docs/adr/0005-separate-aurora-glass-from-gateway-service.md) | Separate Aurora Glass from Gateway Service | Accepted |
| [0006](docs/adr/0006-session-identity-resolution.md) | Session Identity Resolution | Accepted |
| [0007](docs/adr/0007-two-layer-collector-auth.md) | Two-Layer Collector Auth | Accepted |
| [0008](docs/adr/0008-gateway-owned-opencode-projection-tables.md) | Gateway-Owned OpenCode Projection Tables | Accepted |
| [0009](docs/adr/0009-explicit-replay-corrections.md) | Explicit Replay Corrections | Accepted |
| [0010](docs/adr/0010-backend-computed-run-status.md) | Backend-Computed Agent Run Status | Accepted |
| [0011](docs/adr/0011-replay-merge-semantics.md) | Replay Merge Semantics | Accepted |
| [0012](docs/adr/0012-canonical-event-replay-merge.md) | Canonical Event Replay Merge | Accepted |
| [0013](docs/adr/0013-session-currentstatus-heuristic.md) | Session currentStatus Heuristic | Accepted |
| [0014](docs/adr/0014-canonical-client-name-and-rollup.md) | Canonical Client Name and Client-Project Rollup | Accepted |
| [0015](docs/adr/0015-client-project-rollup-as-usage-events-read-model.md) | Client Project Rollup as a `usage_events` read-model | Accepted |
| [0016](docs/adr/0016-execution-transcript-observability.md) | Execution Transcript Observability | Accepted |
| [0017](docs/adr/0017-migration-0019-index-measurement.md) | Migration 0019 Index Keep/Drop Decisions (Measured) | Accepted |
| [0018](docs/adr/0018-reporting-delivery-write-semantics.md) | Reporting-Delivery Write Semantics | Accepted |
| [0019](docs/adr/0019-exact-resource-session-associations.md) | Exact Resource↔Session Associations | Accepted |
| [0020](docs/adr/0020-normalized-provider-event-mapping-bridge.md) | Normalized Provider Event Mapping Bridge | Superseded (FastAPI EDA Gateway ADR 0005) |
| [0021](docs/adr/0021-reporting-read-api.md) | Reporting Read API | Accepted |
| [0022](docs/adr/0022-retention-defaults-and-access-controls.md) | Retention Defaults and Access Controls | Accepted |
| [0023](docs/adr/0023-kafka-topic-split-commands-vs-observations.md) | Kafka Topic Split: afk.events vs engineering.events.normalized | Accepted |
| [0024](docs/adr/0024-awx-execution-binding-history.md) | Preserve AWX Execution Binding History | Accepted |
| [0026](docs/adr/0026-afk-run-id-database-relationships.md) | AFK Run ID Database Relationships | Accepted |
| [0027](docs/adr/0027-transactional-afk-run-status-projection.md) | Project AFK Run Status Transactionally from AWX Executions | Accepted |

Detailed schema, correlation, and database semantics live in the ADRs and
in `CONTEXT.md`; this README deliberately links them rather than duplicating
them.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up the project, running tests, code style, and the pull request workflow.
