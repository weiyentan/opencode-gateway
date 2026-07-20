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
| **API Layer** | `app/api/` | REST endpoints: health, admin client CRUD, collector token management, usage ingest, and reporting (aggregates, records, sessions). API key authentication from day one. Consistent JSON response envelope for all endpoints. |
| **Core Engine** | `app/core/` | Pydantic-based settings and config (`GATEWAY_` env prefix), application factory, logging with secret redaction, auth middleware, token generation/hashing, and Loki URL builder. |
| **Database Layer** | `app/db/` | asyncpg connection pool, SQLAlchemy ORM models for identity, ingest/observability domains, Alembic migrations, and advisory lock utilities. |

Additional layers can be added as the observability service grows.

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
| **Frontend** | Vanilla HTML/CSS/JS | Aurora Glass dashboard — no build step, served at `/` via Starlette `StaticFiles` |
| **Testing** | `pytest` + `pytest-asyncio` | `asyncio_mode = auto` |

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
| `GATEWAY_GRAFANA_BASE_URL` | `http://localhost:3000` | Base URL for Grafana (used to build Loki drill-down links in reporting API responses) |
| `GATEWAY_STATIC_DIR` | `frontend` | Path to the Aurora Glass dashboard static files directory |

> **Note:** The Gateway supports **graceful degradation** — if PostgreSQL is unreachable at startup, the app still starts and the health endpoint returns `"database": "disconnected"` instead of crashing.

### Run

**Development** (with auto-reload):

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

### Verify

```bash
curl http://localhost:8000/health
```

Expected response (example):

```json
{"status":"ok","version":"0.1.0-dev","database":"connected","last_ingest_timestamp":null,"collectors":[],"source_databases":[]}
```

**Dashboard:** Open [http://localhost:8000/](http://localhost:8000/) in a browser to view the **Aurora Glass** telemetry dashboard. It displays KPIs, model-mix charts, live events, collector health, agent/LLM usage, and recent sessions — all auto-refreshing every 30 seconds. No build step is required; the frontend is served directly by the Gateway as static files.

---

## Running with Docker

```bash
cp .env.example .env
docker compose up -d
curl -f http://localhost:8000/health
```

### Services

| Service     | Container               | Port | Description                                       |
|-------------|-------------------------|------|---------------------------------------------------|
| **gateway** | `opencode-gateway`      | 8000 | FastAPI application (built from this `Dockerfile`) |
| **postgres**| `opencode-gateway-db`   | 5432 | PostgreSQL 15 (Alpine) with persistent volume     |

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

### Telemetry Ingest

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest` | Accept a batch of normalized usage records from a collector. Uses first-write-wins idempotency, supports partial-success semantics (per-record accepted/rejected/conflict), and empty-batch heartbeats. Authenticated via collector bearer token. |

### Usage Reporting

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/usage/aggregates` | Token/cost aggregates grouped by dimension (`client`, `model`, `session`, `day`, `week`, `month` — comma-separated). Date-range filterable. |
| `GET` | `/api/v1/usage/records` | Paginated raw usage records. Supports filtering by `client_id`, `model`, `session_id`, date range, sorting, and pagination (`limit`/`offset`). Includes `loki_search_url` for Grafana drill-down. |
| `GET` | `/api/v1/usage/sessions` | Session-level summaries with token/cost totals, message counts, and Loki drill-down URLs. Paginated. |

---

## Frontend Dashboard (Aurora Glass)

The Gateway ships with **Aurora Glass**, a browser-based telemetry dashboard that visualizes observability data collected from OpenCode Serve instances. It is a single-page application (SPA) built with vanilla HTML, CSS, and JavaScript.

### Access

Once the Gateway is running, open the dashboard at:

```
http://localhost:8000/
```

No separate build step or server is required — the Gateway serves the static files automatically.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_STATIC_DIR` | `frontend` | Path to the directory containing the Aurora Glass SPA assets. Relative to the working directory. Point this to a custom dashboard build if needed. |

### Dashboard Sections

The dashboard polls the Gateway REST API every 30 seconds and renders:

| Section | Data Source | Description |
|---------|-------------|-------------|
| **KPI Cards** | `/health`, `/api/v1/usage/aggregates` | Total tokens, estimated cost, session count, healthy collectors, source databases |
| **Model Mix** | `/api/v1/usage/aggregates?group_by=model` | Token/cost breakdown by LLM model |
| **Live Events** | Recent usage records | Real-time feed of incoming telemetry events |
| **Collector Distribution** | `/admin/clients` | Collector status overview (healthy/stale/unknown) |
| **Collectors Table** | `/admin/clients` + health data | Per-collector name, status, last ingest, sessions, tokens, cost |
| **Agents & LLMs** | `/api/v1/usage/records` | Per-client model usage with request counts and cost |
| **Recent Sessions** | `/api/v1/usage/sessions` | Client, model, token/cost totals, duration, and status |

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
│   ├── main.py                   # Production entry point (uvicorn) + static file mount
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py             # GET /health endpoint
│   │   ├── admin_clients.py      # Admin CRUD for clients + tokens
│   │   ├── ingest.py             # POST /ingest telemetry endpoint
│   │   └── usage.py              # GET aggregates, records, sessions
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── auth.py               # API key + collector token middleware
│   │   ├── envelope.py           # Response envelope middleware
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   ├── identity.py           # Token generation & SHA-256 hashing
│   │   ├── loki.py               # Grafana Explore URL builder
│   │   ├── logging.py            # RedactingFormatter
│   │   ├── secrets.py            # Secret detection utilities
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── identity.py       # Pydantic schemas for clients & tokens
│   │       └── usage.py          # Pydantic schemas for usage reporting
│   └── db/
│       ├── session.py            # DatabasePool (asyncpg wrapper)
│       ├── schema.py             # Schema management (delegates to Alembic)
│       ├── setup.py              # Migration runner + table validation
│       ├── lock.py               # Advisory locks
│       └── models/
│           ├── __init__.py
│           ├── base.py           # SQLAlchemy declarative base
│           ├── identity.py       # ORM models: OpenCodeClient, CollectorCredential
│           └── ingest.py         # ORM models: SourceDatabase, Session, UsageRecord, IngestBatch, etc.
├── frontend/                     # Aurora Glass telemetry dashboard (HTML/CSS/JS SPA)
├── tests/                        # Foundation tests (more to be added)
├── docs/
│   └── adr/                      # Architecture Decision Records
├── alembic/                      # Alembic migrations
├── .env.example
├── docker-compose.yaml
├── Dockerfile
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

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up the project, running tests, code style, and the pull request workflow.
