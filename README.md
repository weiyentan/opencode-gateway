# OpenCode Gateway

*An observability service for headless OpenCode.*

OpenCode Gateway provides monitoring, telemetry collection, and health tracking for OpenCode serve instances. It ingests observations from Runner VMs, stores time-series metrics in PostgreSQL, and exposes them through a clean REST API. Platform engineers and agent orchestrators (like Paperclip) use the Gateway to monitor OpenCode deployments at scale.

> **Note:** This project has been refactored from an execution control plane into an observability service. Execution-era subsystems (executor plugins, job scheduling, workspace lifecycle) were removed in issue #207. Observability features (client registry, token auth, usage ingest, reporting API) were added in issues #208–#210.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/status-early--development-orange.svg" alt="Status: Early Development">
  <img src="https://img.shields.io/badge/framework-FastAPI-teal.svg" alt="FastAPI">
</p>

---

## Architecture Overview

The Gateway is built as layered concerns:

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **API Layer** | `app/api/` | REST endpoints: health, admin client CRUD, collector token management, usage ingest, collector cursor recovery, reporting (aggregates, records, sessions, agent runs), the read-only execution-transcript API (sessions, children, messages, parts, timeline, tool-calls), and the read-only AFK outcomes API (runs, entities, correlations). API key authentication from day one. Consistent JSON response envelope for all endpoints. |
| **Core Engine** | `app/core/` | Pydantic-based settings and config (`GATEWAY_` env prefix), application factory, logging with secret redaction, auth middleware, token generation/hashing, and Loki URL builder. |
| **Database Layer** | `app/db/` | asyncpg connection pool, SQLAlchemy ORM models for identity, ingest/observability domains, Alembic migrations, and advisory lock utilities. |
| **Consumer** | `app/consumer/` | Kafka consumer bridge — reads usage records from the `opencode-usage` topic and POSTs them to the Gateway's `/ingest` endpoint. Runs as a separate container (Kubernetes), not as part of the Gateway API process. Also hosts the live AFK outcome consumer (`app/consumer/afk_consumer.py`) that writes provider engineering events to Postgres. |

---

## Technology Stack

| Category | Choice | Notes |
|----------|--------|-------|
| **Runtime** | Python 3.12+ | Required for new typing features and asyncio improvements |
| **Framework** | FastAPI | Async-first, Pydantic-native, OpenAPI auto-generation |
| **Database** | PostgreSQL 15+ via `asyncpg` | Direct connection pool plus SQLAlchemy ORM for future models |
| **Migrations** | Alembic | Schema versioning — auto-applied at startup |
| **Validation** | Pydantic v2 + `pydantic-settings` | Configuration and boundary models |
| **Linting** | `ruff` | Replaces flake8, isort, pyupgrade. Selects: E, F, I, UP |
| **Type Checking** | `mypy` (strict mode) | Full strict checking; Python 3.12 target |
| **Frontend** | Vanilla HTML/CSS/JS + nginx | Aurora Glass dashboard — no build step, served by a separate nginx container. In Docker Compose, the frontend nginx is the sole browser entrypoint and proxies API requests to the Gateway. |
| **Testing** | `pytest` + `pytest-asyncio` | `asyncio_mode = auto` |
| **Streaming** | `aiokafka` | Kafka consumers for the usage-record bridge and the AFK outcome consumer; separate companion containers |

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

All configuration uses the `GATEWAY_` prefix and is loaded via `pydantic-settings` (case-insensitive, `.env` file, environment variables). Key configuration variables:

| Variable | Default | Description |
|----------|---------|-------------|
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
| `GATEWAY_OPERATION_TIMEOUT_MS` | `30000` | Default per-operation timeout budget in milliseconds. Read directly by the telemetry module (`app/core/telemetry.py`) and applied when an operation specifies no explicit budget |
| `GATEWAY_GRAFANA_BASE_URL` | `http://localhost:3000` | Base URL for Grafana (used to build Loki drill-down links in reporting API responses) |
| `GATEWAY_KAFKA_BROKERS` | `localhost:9092` | Kafka bootstrap brokers (comma-separated) — used by the consumer bridge |
| `GATEWAY_KAFKA_TOPIC` | `opencode-usage` | Kafka topic for usage records |
| `GATEWAY_KAFKA_DLQ_TOPIC` | `opencode-usage-dlq` | Dead-letter queue topic for unprocessable messages |
| `GATEWAY_CONSUMER_GROUP_ID` | `opencode-gateway` | Kafka consumer group ID |
| `GATEWAY_NORMALIZED_EVENTS_TOPIC` | `engineering.events.normalized` | Normalized provider-events topic the AFK outcome consumer subscribes to (issue #531; external; not created here) |
| `GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC` | `engineering.events.normalized.dlq` | Dead-letter queue topic for poison normalized provider-events messages |
| `GATEWAY_AFK_OUTCOMES_TOPIC` | `afk.events` | Compatibility-only (issue #531): legacy `afk.events` command topic. Settings still accepts it, but the AFK outcome consumer no longer reads it |
| `GATEWAY_AFK_OUTCOMES_DLQ_TOPIC` | `afk.events-dlq` | Compatibility-only (issue #531): legacy AFK outcome DLQ topic. Settings still accepts it, but the AFK outcome consumer no longer reads it |
| `GATEWAY_NORMALIZED_EVENTS_CONSUMER_GROUP_ID` | `opencode-normalized-events` | Kafka consumer group ID for the AFK outcome consumer (never shared with the usage consumer's `opencode-gateway` group) |
| `GATEWAY_AFK_OUTCOMES_PROVIDER` | `github` | Source provider for reconciliation windows (`github` or `gitlab`) |
| `GATEWAY_AFK_OUTCOMES_REPOSITORY` | *(empty)* | Full owner/repo (or group/project) name the AFK consumer reconciles |
| `GATEWAY_AFK_OUTCOMES_RECONCILE_CADENCE_SECONDS` | `3600` | Seconds between scheduled reconciliation windows |
| `GATEWAY_AFK_OUTCOMES_RECONCILE_WINDOW_SECONDS` | `86400` | Bounded reconciliation window size in seconds |
| `GATEWAY_AFK_OUTCOMES_MAX_RETRIES` | `5` | AFK outcome consumer reliability (issue #482): total persistence attempts before a message is DLQ'd (min 1 — 0 or negative is rejected) |
| `GATEWAY_AFK_OUTCOMES_INITIAL_BACKOFF_SECONDS` | `1.0` | Initial inter-attempt delay (seconds) for the bounded exponential backoff with jitter |
| `GATEWAY_AFK_OUTCOMES_MAX_BACKOFF_SECONDS` | `60.0` | Upper cap (seconds) on the exponential backoff delay |
| `GATEWAY_RETENTION_AFK_AGGREGATES_DAYS` | `0` | Retention tier (ADR 0022): aggregates (`afk_runs`, `afk_run_sessions`) — `0` = never swept (indefinite); min 0 — negatives are rejected |
| `GATEWAY_RETENTION_AFK_METADATA_DAYS` | `365` | Retention tier (ADR 0022): metadata — 12 months; min 0 (`0` = never swept) |
| `GATEWAY_RETENTION_AFK_PAYLOAD_DAYS` | `90` | Retention tier (ADR 0022): redacted payload storage; min 0 (`0` = never swept) |
| `GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS` | `30` | Retention tier (ADR 0022): DLQ operational max — strictly older records are escalated to `afk.events-dlq-expired`; min 0 (`0` = never swept) |
| `GATEWAY_BASE_URL` | `http://localhost:8000` | Gateway base URL (used by the consumer to POST to `/ingest`) |
| `GATEWAY_COLLECTOR_TOKEN` | | Collector bearer token for Gateway auth (used by the consumer) |
| `GATEWAY_OPERATOR_TOKEN` | *(empty)* | Operator-only read gate (ADR 0022). Presented in the dedicated `X-Operator-Token` header (never `Authorization`, which carries the Admin API Key). Distinct from `GATEWAY_API_KEY` and collector credentials; fails closed (403) when unset — no operator-only surface (delivery payload, DLQ) is reachable |
| `GATEWAY_TOOL_PAYLOAD_MAX_CHARS` | `4096` | Execution transcript (ADR 0016): per-field character cap for tool input/output payloads stored in `observed_tool_calls` (truncated at ingest; verbatim content stays in `observed_parts`) |
| `GATEWAY_PART_DATA_MAX_CHARS` | `65536` | Execution transcript (ADR 0016): verbatim character cap for `message`/`part` payloads stored in the `data` JSONB column (truncated at ingest with a `truncated` marker) |

> **Note:** The Gateway supports **graceful degradation** — if PostgreSQL is unreachable at startup, the app still starts and the health endpoint returns `"database": "disconnected"` instead of crashing.

> **Observability:** The Gateway emits structured timing log events — `request.completed` (per-request wall-clock duration, status code, endpoint, correlation ID), `operation.completed` (per-database-query/operation duration and success), and `operation.timeout` (deadline expiry, with the budget). Event data lives in structured `extra` fields, never in interpolated log strings, and contains no raw SQL or sensitive payload data. Each request receives a correlation ID propagated via the `X-Correlation-ID` request/response header; operation events within the request inherit it, which helps correlate latency in log aggregation tools.

### Run

**Development (standalone Gateway)** — starts the API server without the frontend container:

```bash
python -m app
```

**Production**:

```bash
uvicorn app.main:app
```

**Run tests**:

```bash
pytest tests/ -v
```

Performance profiling benchmarks (`tests/test_read_path_perf.py`) are marked `profiling` and excluded from the default run (`-m not profiling`). Run them explicitly with `pytest tests/test_read_path_perf.py -m profiling`. Baseline JSONs in `tests/fixtures/` are committed; set `REGENERATE_BASELINES=1` to force regeneration, and note profiling output is written to a gitignored `tests/fixtures/profiling-output/` directory.

### Verify

When running the Gateway standalone:

```bash
curl http://localhost:8000/health
```

Expected response (example):

```json
{"status":"ok","data":{"status":"ok","version":"0.1.0-dev","database":"connected","last_ingest_timestamp":null,"collectors":[],"source_databases":[]}}
```

**Dashboard:** When running with Docker Compose (see below), open [http://localhost:8080/](http://localhost:8080/) in a browser to view the **Aurora Glass** telemetry dashboard. It displays KPIs, model-mix charts, operational events, collector health, agent/LLM usage, and recent sessions — auto-refreshing every 30 seconds (client metadata is cached for 10 minutes). The frontend is served by a separate nginx container that proxies API requests to the Gateway.

---

## Running with Docker (Same-Origin Local Stack)

The Docker Compose stack runs Aurora Glass and the Gateway as separate containers behind a single browser origin. The frontend nginx is the sole entrypoint — it serves the Aurora Glass dashboard and proxies API requests to the Gateway.

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
| **kafka**   | `opencode-gateway-kafka`| —         | 9092          | Local KRaft Kafka broker (single node; internal — no host ports). Backstops both streaming consumers; the provider-events topic is external |
| **afk-outcomes-consumer** | `opencode-afk-outcomes-consumer` | — | — | AFK outcome consumer — reads the normalized provider-events topic (`engineering.events.normalized`) in its own group (`opencode-normalized-events`) and writes canonical engineering events to Postgres, plus scheduled reconciliation |

> **Same-origin architecture:** The frontend nginx serves static files at `/` and proxies `/api/*`, `/health`, `/admin/*`, `/docs` and `/openapi.json` to `http://gateway:8000`. This avoids CORS entirely — the browser talks to a single origin. The Gateway is not directly accessible from the host; all traffic flows through the frontend proxy.

---

## API Reference

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Application health check. Returns `status`, `version`, `database` connectivity, collector status (healthy/stale/unknown per credential), source-database health, and last-ingest timestamp. Graceful — always returns 200 even if the database is down. |

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

### Telemetry Ingest

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest` | Accept a batch of normalized usage records from a collector. First-delivery records create canonical events in `usage_events`; every delivery processed through the canonical layer is recorded as an ingest attempt (audit trail covering `accepted`, `duplicate`, `updated`, `quarantined`, and `conflict` outcomes). Per-record outcomes: `accepted` (new canonical event), `duplicate` (idempotent replay), `updated` (replay merged — event corrected and session aggregates delta-adjusted), `quarantined` (source identity quarantined or overlapping), `conflict` (canonical event owned by a different unresolved identity), `rejected` (validation failure or internal error). All outcomes are 2xx at batch level so the consumer commits Kafka offsets; invalid payloads and 4xx/5xx responses route to the DLQ. Optional replay metadata fields (`replay_id`, `replay_requested_start`, `replay_delivery_mode`) mark replay deliveries. Empty-batch heartbeats supported. Authenticated via collector bearer token. Schema v1.3 adds optional `messages` and `parts` batch collections (execution-transcript projections, ADR 0016); existing collections and per-record outcomes are unchanged. |

### Collector Cursor

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/cursor` | Return cursor state (last ingestion timestamp, record count, active status) for a source database. Called by collectors on startup to determine where to begin reading from the SQLite database. Authenticated via collector bearer token. Returns 404 for unknown source database IDs. |

### Usage Reporting

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/usage/aggregates` | Token/cost aggregates grouped by dimension (`client`, `model`, `session`, `day`, `week`, `month`, `project`, `agent` — comma-separated). Date-range filterable. |
| `GET` | `/api/v1/usage/records` | Paginated raw usage records. Supports filtering by `client_id`, `model`, `session_id`, date range, sorting, and pagination (`limit`/`offset`). Default sorting is by source-created message time (`sort_by=source_created_at`, `COALESCE(source_created_at_tz, reported_at)`), not ingest time. Includes `loki_search_url` for Grafana drill-down. |
| `GET` | `/api/v1/usage/sessions` | Session-level summaries with token/cost totals, message counts, Loki drill-down URLs, and `session_title` from Session Context. Paginated. |
| `GET` | `/api/v1/usage/agent-runs` | Paginated list of Agent Run Summaries with `session_title` and `model` enrichment from Session Context. |
| `GET` | `/api/v1/usage/agent-runs/{session_id}` | Detail view for a specific agent run, including `session_context` (title, model, code changes) and `todo_rows` (latest OpenCode todo snapshot) alongside usage data. |
| `GET` | `/api/v1/usage/records-with-context` | Paginated usage records enriched with `session_title`, `project_label`, and `agent`. Supports `group_by` aggregation by `project`, `agent`, `session`, or `model`. |

> **Note:** Usage query endpoints read from the canonical `usage_events` table
> (replay-safe accounting, migration 0021). API contracts are unchanged. The
> legacy `opencode_usage_records` table is still written at ingest but is no
> longer the query source.

### Reporting

Read-only endpoints exposing the reporting read-model (ADR 0021, issue #484)
— the first report of what the Gateway has ingested from the normalized-event
stream (`engineering.events.normalized`, the same external normalized
provider-events topic the AFK Outcome Consumer subscribes to; write path:
`app/api/reporting_ingest.py`, issue #479): ingested
resources with their current aggregate, the per-delivery state trail, and the
session links that can be provably linked to them. The surface is strictly
read-only and never derives a "completed"/"finished"/outcome state for a
resource (**no completion claims**) — it surfaces the resource's verbatim
current payload and its delivery lifecycle states (`received`, `normalized`,
`published`, `persisted`, `rejected`, `failed`) as pipeline observations.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/reporting/resources` | Paginated list of ingested resources. Filterable by any subset of the stable resource identity (`provider`, `repository_url`, `resource_type`, `resource_number`). Each item is a current aggregate: verbatim `payload`, `delivery_count`, `last_delivery_id`, `last_ingested_at`, plus the composite `resource_id` key. |
| `GET` | `/api/v1/reporting/resources/detail` | Full detail for one resource addressed by all four identity components (required query params): the current aggregate plus the per-delivery state trail (`delivery_state_trails`, chronological) plus `session_links`. 404 for an unknown resource identity. |
| `GET` | `/api/v1/reporting/session-links` | Paginated session links (`afk_run_sessions`), each surfaced as **provisional** (`provisional=True`) with an empty `source_references` list until exact resource↔session correlation (issue #481) lands. |

All responses use the `{status, data, error}` envelope and are protected by
the global API-key middleware, like every other non-`/health` route. These
endpoints additionally require the dedicated operator token
(`GATEWAY_OPERATOR_TOKEN`, via `require_operator_token`) **on top of** the
Admin API Key — delivery payload and state trails are an operator-only read
surface, and the gate fails closed when the operator token is unprovisioned.
The operator token is presented in the dedicated `X-Operator-Token` header
(never `Authorization`, which carries the Admin API Key), so the two
credentials can be distinct and both gates are satisfiable on the same
request. The reporting write path remains `app/api/reporting_ingest.py`; the
read router never writes.

### AFK Outcomes

Read-only endpoints exposing the AFK outcome read-model — AFK Runs reconstructed
from provider engineering activity, correlated against Gateway sessions. The
AFK outcome vocabulary is described in `CONTEXT.md` (AFK Run, `afk_run_id`,
RunStatus, EngineeringOutcome, EngineeringOutcomeStatus, change_request,
correlation_confidence, correlation_method, resolver_version,
correlation_source, owning_change_request_id, Provisional Link). Backfill
remains CLI-only (`scripts/afk_backfill.py`); these endpoints never write.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/afk-outcomes/runs` | Paginated list of AFK Runs. Filterable by `repository` (EXISTS subquery on `afk_run_entities`), window (`started_from`/`started_to`, `finished_from`/`finished_to`, `seen_from`/`seen_to` ISO-8601 bounds), `status` (RunStatus value), `outcome` (EngineeringOutcomeStatus value), and `origin` (provider). 400 on invalid enum/date values or inverted windows. Ordered by `last_seen_at DESC`. |
| `GET` | `/api/v1/afk-outcomes/runs/{afk_run_id}` | Full chain for one run: run aggregate, EngineeringOutcome, engineering entities grouped by type (issues, change_requests, reviews, commits, merge_events) each carrying correlation and lineage provenance (`correlation_method`, `correlation_confidence`, `evidence`, `resolver_version`, `correlation_source`, `owning_change_request_id`) and a `provisional` marker, linked sessions with usage/cost aggregates, distinct agents, and a run-level usage aggregate (Active Tokens = input + output; cache read/write as siblings). 404 for unknown `afk_run_id`. |
| `GET` | `/api/v1/afk-outcomes/entities` | Paginated engineering entities with their run links, correlation and lineage provenance (`correlation_method`, `correlation_confidence`, `evidence`, `resolver_version`, `correlation_source`, `owning_change_request_id`), `superseded_at` (superseded state surfaced, not hidden), and `provisional` marker. |
| `GET` | `/api/v1/afk-outcomes/correlations` | Paginated unresolved correlations with `method`, `correlation_confidence`, `evidence`, `resolver_version`, and `provisional=true`. Every row is attributed to a run — `afk_run_id` is NOT NULL (migration 0027) — and rows are unique per `(provider, repository, entity_type, external_id, afk_run_id, method)`, so the same entity can appear in separate rows per AFK run and evidence is never merged across runs. |

All responses use the `{status, data, error}` envelope and are protected by the
global API-key middleware, like every other non-`/health` route.

### Execution Transcript

Read-only endpoints exposing the execution-transcript observability slice
(ADR 0016): message- and part-level execution data ingested from the OpenCode
runtime's `message`/`part` SQLite tables. Transcripts are event timelines
(what a run did), kept distinct from usage accounting (how much it cost). The
transcript vocabulary is described in `CONTEXT.md` (Execution Transcript,
Observed Message, Observed Part, Observed Tool Call, Transcript Event Type,
Transcript Timeline). All endpoints are protected by the global API-key
middleware.

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
stable under concurrent ingest); `limit` defaults to 100 and is capped at 1000.
The children endpoint uses offset/limit pagination. Payloads are redacted
(secret-like keys) and truncated at ingest
(`GATEWAY_TOOL_PAYLOAD_MAX_CHARS` / `GATEWAY_PART_DATA_MAX_CHARS`), so the API
serves only the already-redacted, already-truncated store.

---

## Consumer Operations

### DLQ Sweep (issue #483)

The AFK outcome DLQ (`afk.events-dlq`) is bounded by an **operational max**
(`GATEWAY_RETENTION_DLQ_MAX_AGE_DAYS`, default 30 days). Every DLQ record is
stamped with `dead_lettered_at` and `max_age_days` at producer time; the DLQ
sweep escalates records **strictly older** than the max to the durable
`afk.events-dlq-expired` escalation topic (original payload + reason +
`dead_lettered_at` + deterministic `escalation_key` + `escalation_reason`),
so expired records are never silently lost. Escalation content is
content-stable: the `escalation_key` is a deterministic SHA-256 over the DLQ
record's own identity, so re-escalating the same record produces an
identical record (deduplicable by natural key). In write mode the sweep
commits consumed Kafka offsets after each chunk — for each partition at its
first retained (not-yet-expired) record's offset, or past the last consumed
record when the partition has no retained records — so re-runs do **not**
re-escalate already-escalated records, while retained records are re-examined
on later runs until they expire. Dry-run never commits. Physical removal
from the active DLQ is enforced by the topic's Kafka retention configured to
the same max age.

```bash
# Report the would-be-escalated records without publishing anything
python -m app.consumer.afk_consumer --dlq-sweep --dry-run

# Escalate expired records to afk.events-dlq-expired
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

### Operator Token (issue #483)

`GATEWAY_OPERATOR_TOKEN` gates operator-only read surfaces (delivery payload,
DLQ) via the `require_operator_token` dependency. It is a dedicated operator
bearer token, **distinct** from the Admin API Key (`GATEWAY_API_KEY`) and
from per-client Collector Credentials — the Admin API Key does not satisfy
the operator gate. It is transported in the dedicated `X-Operator-Token`
header (never `Authorization`, which carries the Admin API Key), so a client
presents the operator token alongside the Admin API Key and both gates are
satisfiable. An empty operator token **fails closed** (403): no operator-only
surface is reachable (no broad read). Delivery payload and state trails are
readable **only** through the operator-gated reporting read API (ADR 0021,
issue #484): `require_operator_token` is registered on
`GET /api/v1/reporting/resources`, `/resources/detail`, and
`/session-links`, and no other route reads `reporting_deliveries` /
`delivery_state_trails` back out. The delivery log,
`engineering_events.payload`, and DLQ records remain readable by no API
route (ADR 0022).

---

## Frontend Dashboard (Aurora Glass)

The Gateway ships with **Aurora Glass**, a browser-based telemetry dashboard that visualizes observability data collected from OpenCode Serve instances. It is a single-page application (SPA) built with vanilla HTML, CSS, and JavaScript.

### Access

**Docker Compose stack (recommended for local development):**

The frontend nginx container serves Aurora Glass and proxies API requests to the Gateway. Open the dashboard at:

```
http://localhost:8080/
```

The frontend is the sole browser entrypoint — the Gateway runs internally and is not directly accessible from the host.

### Dashboard Sections

The dashboard polls the Gateway REST API every 30 seconds (client metadata is cached for 10 minutes) and renders:

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
| **Agent Runs** | `/api/v1/usage/agent-runs` | Agent run table with session title, status, model, costs, and a detail overlay showing session context (title, model, code changes) and todo progress |
| **AFK Outcomes** | `/api/v1/afk-outcomes/runs`, `/api/v1/afk-outcomes/runs/{afk_run_id}` | AFK run list (RunStatus and EngineeringOutcomeStatus badges) whose rows open the chain detail overlay — issue → run → sessions → agents → tokens/cost → change_request → commits → review cycles → outcome — with per-link correlation provenance (method, confidence, evidence, resolver version) and visibly-marked provisional/inferred links. Sessions in the chain detail that carry a resolvable internal session ID are clickable and open the existing Agent Run detail overlay (the AFK chain overlay closes first); unresolved sessions stay non-clickable |

The dashboard uses the same authentication as the REST API — if the Gateway runs in production mode (`GATEWAY_ENV=production`) with an API key, the dashboard will need one. For local development, use `GATEWAY_ENV=development` to run without authentication.

---

## Database Migrations

Alembic is the **single source of truth** for the production database schema. The Gateway automatically runs migrations at startup — no manual steps are required.

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
│   │   ├── execution.py          # GET /api/v1/execution (sessions, children, messages, parts, timeline, tool-calls — read-only)
│   │   ├── usage.py              # GET aggregates, records, sessions
│   │   └── afk_outcomes.py       # GET /api/v1/afk-outcomes (runs, entities, correlations — read-only)
│   ├── consumer/
│   │   ├── __init__.py           # Module init, exports Consumer class
│   │   ├── consumer.py           # Kafka consumer bridge (separate container)
│   │   ├── afk_consumer.py       # Live AFK outcome consumer (own consumer group)
│   │   └── models.py             # Consumer-side Pydantic models for ingest payloads
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── auth.py               # API key + collector token middleware
│   │   ├── envelope.py           # Response envelope middleware
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   ├── identity.py           # Token generation/hashing + canonical source identity & quarantine resolution
│   │   ├── loki.py               # Grafana Explore URL builder
│   │   ├── logging.py            # RedactingFormatter
│   │   ├── reconciliation.py     # Canonical replay-merge deltas + historical duplicate reconciliation
│   │   ├── secrets.py            # Secret detection utilities
│   │   ├── telemetry.py          # Request timing middleware, operation/timeout helpers, structured timing log events
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── identity.py       # Pydantic schemas for clients, tokens & quarantined identities
│   │       ├── reconciliation.py # Pydantic schemas for historical reconciliation
│   │       ├── usage.py          # Pydantic schemas for usage reporting
│   │       ├── execution.py      # Pydantic schemas for the execution-transcript API
│   │       └── afk.py            # Pydantic schemas for AFK outcomes API responses
│   └── db/
│       ├── session.py            # DatabasePool (asyncpg wrapper)
│       ├── schema.py             # Schema management (delegates to Alembic)
│       ├── setup.py              # Migration runner + table validation
│       ├── lock.py               # Advisory locks
│       └── models/
│           ├── __init__.py
│           ├── base.py           # SQLAlchemy declarative base
│           ├── identity.py       # ORM models: OpenCodeClient, CollectorCredential
│           ├── ingest.py         # ORM models: SourceDatabase, Session, UsageRecord, IngestBatch, etc.
│           └── afk.py            # ORM models: AFKRun, EngineeringEntity, delivery log, unresolved correlations
├── afk_outcomes/                 # Pure-domain AFK outcome package (models, serialization, correlation engine, provider adapters, repository)
├── scripts/
│   ├── afk_backfill.py           # AFK outcome backfill/reconciliation CLI (--dry-run, --show-evidence)
│   └── rebuild_closure_projection.py  # Operator-only closure projection rebuild CLI (--since/--until, --confirm, --dry-run)
├── frontend/                     # Aurora Glass telemetry dashboard (HTML/CSS/JS SPA)
├── tests/                        # Foundation tests (more to be added)
├── docs/
│   ├── adr/                      # Architecture Decision Records
│   └── afk-outcome-validation.md # AFK reconstruction validation findings (#450)
├── alembic/                      # Alembic migrations
├── .env.example
├── docker-compose.yaml
├── Dockerfile
├── Dockerfile.afk-consumer       # AFK outcome consumer container (separate deployment)
├── k8s/                          # Kubernetes manifests (gateway + consumers)
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [0001](docs/adr/0001-separate-observation-tables.md) | Separate Observation Tables | Accepted |
| [0002](docs/adr/0002-executor-plugin-interface.md) | Executor Plugin Interface | Superseded (#207) |
| [0003](docs/adr/0003-postgres-port-allocation.md) | PostgreSQL Port Allocation | Superseded (#207) |
| [0004](docs/adr/0004-gateway-no-infra-secrets.md) | Gateway Never Holds Infrastructure Secrets | Accepted |
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
| [0015](docs/adr/0015-client-project-rollup-as-usage-events-read-model.md) | Client Project Rollup as a usage_events read-model | Accepted |
| [0016](docs/adr/0016-execution-transcript-observability.md) | Execution Transcript Observability | Accepted |
| [0017](docs/adr/0017-migration-0019-index-measurement.md) | Migration 0019 Index Keep/Drop Decisions (Measured) | Accepted |
| [0018](docs/adr/0018-reporting-delivery-write-semantics.md) | Reporting-Delivery Write Semantics | Accepted |
| [0019](docs/adr/0019-exact-resource-session-associations.md) | Exact Resource↔Session Associations | Accepted |
| [0020](docs/adr/0020-normalized-provider-event-mapping-bridge.md) | Normalized Provider Event Mapping Bridge | Superseded |
| [0021](docs/adr/0021-reporting-read-api.md) | Reporting Read API | Accepted |
| [0022](docs/adr/0022-retention-defaults-and-access-controls.md) | Retention Defaults and Access Controls | Accepted |
| [0023](docs/adr/0023-kafka-topic-split-commands-vs-observations.md) | Kafka Topic Split: afk.events vs engineering.events.normalized | Accepted |

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up the project, running tests, code style, and the pull request workflow.
