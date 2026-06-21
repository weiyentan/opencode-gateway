# OpenCode Gateway

*A portable execution control plane for running OpenCode as a safe, observable, API-driven coding backend.*

OpenCode Gateway fills the orchestration gap that OpenCode itself does not address. It coordinates the full lifecycle of headless coding sessions — submitting jobs, managing Runner VMs via executor plugins, tracking state in PostgreSQL, and surfacing results through a clean REST API. Platform engineers and agent orchestrators (like Paperclip) use the Gateway to run OpenCode at scale within automation pipelines, without managing infrastructure directly.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/status-early--development-orange.svg" alt="Status: Early Development">
  <img src="https://img.shields.io/badge/framework-FastAPI-teal.svg" alt="FastAPI">
</p>

---

## Architecture Overview

The Gateway is built as four layered concerns, each in its own package:

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **API Layer** | `app/api/` | REST endpoints for jobs, runners, workspaces, observations, and approvals. API key authentication from day one. Consistent JSON response envelope for all endpoints. |
| **Core Engine** | `app/core/` | Pydantic-based settings and config (`GATEWAY_` env prefix), policy module for pre-flight checks (disk pressure, runner health, concurrent job limits), and a background scheduler for periodic cleanup and observation polling. |
| **Executor Plugin Interface** | `app/executors/` | Abstract async base class defining seven methods (`create_workspace`, `start_opencode`, `stop_opencode`, `restart_opencode`, `collect_state`, `cleanup_workspace`, `cancel_job`), typed Pydantic models, and a registry (`EXECUTOR_REGISTRY`) mapping executor type names to implementation classes. The factory (`factory.py`) resolves the active executor from the `GATEWAY_EXECUTOR_TYPE` config via the registry. MVPs: **local executor** (default, shipping), **AWX executor** (shipping). Design documented in [ADR 0002](docs/adr/0002-executor-plugin-interface.md). |
| **OpenCode Serve Client** | `app/opencode/` | `httpx`-based wrapper for the OpenCode Serve REST API: health checks, session CRUD, task submission, diff retrieval, and abort. |

### Interaction Flow

> **Submit job** → Gateway selects a runner (by explicit pinning, label match, or automatic load-balancing) → validates runner health via the policy module → creates a job record in PostgreSQL → delegates workspace creation to the executor plugin → executor starts OpenCode Serve on an allocated port → Gateway sends the coding task via the OpenCode client → OpenCode produces a diff → Gateway records the result → caller polls or retrieves the diff.

Two critical security boundaries:

- **Gateway never reaches into a Runner VM directly.** All infrastructure actions flow through the executor plugin.
- **Gateway never holds infrastructure secrets.** The executor (AWX) owns SSH keys and credentials; the Gateway authenticates to AWX via API token only.

---

## Domain Model

Precise terminology keeps the system navigable. These terms are canonical — avoid the alternatives listed below.

| Term | Description | Avoid |
|------|-------------|-------|
| **Gateway** | Main API and state engine. Receives job requests, tracks state in Postgres, delegates infrastructure actions to executor plugins, and calls OpenCode Serve for coding sessions. | Backend, server, controller |
| **Executor Plugin** | Abstraction layer that performs infrastructure actions (create workspace, start/stop OpenCode, collect state, clean up). MVP default is AWX; other backends can be added later. | Provider, driver, adapter |
| **OpenCode Serve** | Long-running headless API process managed by systemd on the Runner VM. Owns coding sessions, messages, diffs, and tool execution. | opencode daemon, opencode service |
| **Runner VM** | Persistent virtual machine that hosts workspace directories and systemd-managed OpenCode Serve instances. Provides the native toolchain for code editing tasks. | Worker, node, agent |
| **Job** | Unit of work submitted to the Gateway. Maps to one coding task executed against one workspace via one OpenCode session. | Task, run, request |
| **Workspace** | Directory on the Runner VM containing a cloned repository and related artifacts. Created per-job, cleaned up according to policy. | Project directory, working directory, sandbox |
| **Paperclip** | Agent/work orchestration layer that coordinates agents, goals, task assignment, governance, budgets, and higher-level workflows. Sits *above* the Gateway, calling it for coding execution. | Gateway, execution control plane |

### Key Relationships

- A **Job** targets one **Workspace** on one **Runner VM**.
- A **Workspace** is served by one **OpenCode Serve** instance.
- An **Executor Plugin** performs infrastructure actions on the **Runner VM**.
- The **Gateway** coordinates all of the above and is the only component callers interact with.
- **Paperclip** coordinates agents and can call the Gateway to execute coding work.
- The Gateway does **not** replace Paperclip — they operate at different layers.

For example dialogues and deeper discussion, see [CONTEXT.md](CONTEXT.md).

---

## Technology Stack

| Category | Choice | Notes |
|----------|--------|-------|
| **Runtime** | Python 3.12+ | Required for new typing features and asyncio improvements |
| **Framework** | FastAPI | Async-first, Pydantic-native, OpenAPI auto-generation |
| **Database** | PostgreSQL 15+ via `asyncpg` | Direct connection pool (asyncpg) plus SQLAlchemy ORM models for observability tables and Alembic for schema migrations |
| **Validation** | Pydantic v2 + `pydantic-settings` | All boundary models and configuration use Pydantic |
| **HTTP Client** | `httpx` | Async client for the OpenCode Serve API |
| **Linting** | `ruff` | Replaces flake8, isort, pyupgrade. Selects: E, F, I, UP |
| **Type Checking** | `mypy` (strict mode) | Full strict checking; Python 3.12 target |
| **Testing** | `pytest` + `pytest-asyncio` | `asyncio_mode = auto`; TestClient via httpx |
| **Executor (MVP)** | AWX (Ansible Automation Platform) | Production executor; local shell executor for development |

---

## Getting Started

### Prerequisites

- **Python 3.12** or later
- **PostgreSQL 15** or later (or Docker for a local Postgres instance)
- `pip` or `uv` for package installation
- (Optional) AWX instance for production executor workloads

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

### Database Initialization and Migrations

Alembic is the **single source of truth** for the production database schema. All tables are created and evolved through versioned migration scripts in `alembic/versions/`.

The Gateway automatically runs migrations at startup — no manual steps are required in development or production. If any required table is missing after migration, startup fails with a clear error message naming the missing tables.

The 10 required tables are:

| Table | Migration | Purpose |
|-------|-----------|---------|
| `gateway_jobs` | 0000 | Core job lifecycle tracking |
| `workspaces` | 0000 | Per-job workspace directories |
| `job_events` | 0000 | State transition audit trail |
| `approvals` | 0000 | Human approval records |
| `runners` | 0001 | Runner VM registry |
| `runner_events` | 0006 | Runner status change audit trail |
| `runner_observations` | 0001 | Runner telemetry snapshots |
| `workspace_observations` | 0001 | Per-workspace resource metrics |
| `opencode_instance_observations` | 0001 | Per-instance opencode serve state |
| `webhooks` | 0002 | Webhook endpoint configuration |

#### Manual migration (optional)

For troubleshooting or CI pipelines, you can run migrations manually:

```bash
# Apply all pending migrations
alembic upgrade head

# View current revision
alembic current

# Generate a new migration after changing SQLAlchemy models
alembic revision --autogenerate -m "description of change"

# Roll back one migration
alembic downgrade -1
```

The legacy `app/db/schema.sql` file is kept as a **static reference** for tests and documentation. Do not edit it manually — it is auto-generated from the migration chain.

#### Fresh database setup

```bash
# 1. Create the database
createdb opencode_gateway

# 2. Run migrations (or just start the Gateway — it runs them automatically)
alembic upgrade head

# 3. (Optional) Seed with sample data
python scripts/seed.py --runners 3 --workspaces 3 --jobs 5 --observations 10
```

### Configuration

Copy the example environment file and adjust values for your environment:

```bash
cp .env.example .env
```

All configuration uses the `GATEWAY_` prefix and is loaded via `pydantic-settings` (case-insensitive, `.env` file, environment variables). The full set of configuration variables:

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
| `GATEWAY_EXECUTOR_TYPE` | `local` | Executor plugin type (`local`, `awx`, etc.) — looked up in `EXECUTOR_REGISTRY` |
| `GATEWAY_DATABASE_CONNECTION_TIMEOUT` | `30` | Connection timeout in seconds |
| `GATEWAY_CLEANUP_INTERVAL_SECONDS` | `900` | Background cleanup scheduler tick interval in seconds (default: 15 minutes) |
| `GATEWAY_CLEANUP_BATCH_SIZE` | `10` | Maximum number of expired workspaces cleaned per scheduler tick |
| `GATEWAY_CLEANUP_SUCCESS_RETENTION_HOURS` | `72` | Retention period in hours for successfully completed workspaces before they are eligible for cleanup |
| `GATEWAY_CLEANUP_FAILURE_RETENTION_HOURS` | `168` | Retention period in hours for failed workspaces before they are eligible for cleanup |
| `GATEWAY_DISK_THRESHOLD_PERCENT` | `80.0` | Maximum disk-usage percentage allowed on a runner VM before the policy engine rejects new jobs (0–100) |
| `GATEWAY_MEMORY_THRESHOLD_PERCENT` | `85.0` | Maximum memory-usage percentage allowed on a runner VM before the policy engine rejects new jobs (0–100) |
| `GATEWAY_STALENESS_SECONDS` | `600` | Maximum age in seconds of the last telemetry sample; runners with older data are treated as UNKNOWN |

> **Note:** The Gateway supports **graceful degradation** — if PostgreSQL is unreachable at startup, the app still starts and the health endpoint returns `"database": "disconnected"` instead of crashing. This is by design.

### AWX Executor Configuration

When `GATEWAY_EXECUTOR_TYPE` is set to `awx`, the Gateway uses [AWX](https://github.com/ansible/awx) (Ansible Automation Platform) as the executor plugin to manage workspace lifecycle on Runner VMs. The following environment variables configure the AWX connection:

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_AWX_BASE_URL` | *(empty)* | Base URL of the AWX instance (e.g. `https://awx.example.com`) |
| `GATEWAY_AWX_TOKEN` | *(empty)* | AWX API Bearer token for authentication |
| `GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID` | `0` | AWX job template ID for workspace creation (maps to `gateway-create-workspace` in AWX) |
| `GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID` | `0` | AWX job template ID for OpenCode start/stop/restart (maps to `gateway-opencode-lifecycle`) |
| `GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID` | `0` | AWX job template ID for workspace teardown and state collection (maps to `gateway-workspace-teardown`) |
| `GATEWAY_AWX_POLL_INTERVAL_SECONDS` | `5` | Seconds between poll retries when waiting for AWX job completion |
| `GATEWAY_AWX_TIMEOUT_SECONDS` | `300` | Maximum seconds to wait for an AWX job to complete |

To switch from the default `local` executor to the AWX executor, set the following in your `.env` file:

```bash
# Switch executor type to AWX
GATEWAY_EXECUTOR_TYPE=awx

# AWX connection and authentication
GATEWAY_AWX_BASE_URL=https://awx.example.com
GATEWAY_AWX_TOKEN=your-awx-api-token

# Job template IDs (replace with your actual AWX template IDs)
GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID=10
GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID=20
GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID=30
```

All three template IDs are required and must be non-zero. If any template ID is missing or zero, the Gateway raises a `ValueError` at startup.

> **Template contract:** The exact AWX job template structure (expected `extra_vars`, artifact outputs, and playbook contracts) is defined in the [GitLab issue #82](https://gitlab.com/opencode/gateway/-/issues/82) under the "AWX Template Contract" section. Refer to that issue when creating or updating the corresponding AWX job templates.

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

Expected response:

```json
{"status": "ok", "data": {"status": "ok", "version": "0.1.0-dev", "database": "connected"}}
```

If the database is unreachable, the response still returns HTTP 200 but reports `"database": "disconnected"`.

> **Docker support** is available — see [Running with Docker](#running-with-docker) below for container-based setup.

---

## Running with Docker

The Gateway can be run entirely in containers using Docker Compose — no local Python or PostgreSQL installation required.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (Engine 20.10+)
- [Docker Compose](https://docs.docker.com/compose/) (v2+)

### Quick start

```bash
# Clone and enter the repository
git clone <repo-url>
cd opencode-gateway

# Optionally create a .env file (defaults shown in .env.example work out of the box)
cp .env.example .env

# Build the image and start both services
docker compose up -d

# Verify the Gateway is running
curl -f http://localhost:8000/health
```

Expected response:

```json
{"status":"ok","data":{"status":"ok","version":"0.1.0-dev","database":"connected"}}
```

### Services

| Service     | Container               | Port | Description                                       |
|-------------|-------------------------|------|---------------------------------------------------|
| **gateway** | `opencode-gateway`      | 8000 | FastAPI application (built from this `Dockerfile`) |
| **postgres**| `opencode-gateway-db`   | 5432 | PostgreSQL 15 (Alpine) with persistent volume     |

### Configuration

All Gateway configuration uses the `GATEWAY_` prefix and can be set via:

1. A `.env` file in the project root (loaded by both `docker compose` and `pydantic-settings`).
2. Directly in the `environment` block of `docker-compose.yaml`.

The PostgreSQL container is configured with the `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` variables — all documented in `.env.example` with secure defaults for local development.

> **Production note:** Change `POSTGRES_PASSWORD` before deploying. The bundled `.env.example` uses `opencode` as the password — suitable only for local development.

### Useful commands

```bash
# View logs
docker compose logs -f gateway

# Rebuild after code changes
docker compose up -d --build

# Stop everything
docker compose down

# Stop and remove the database volume (destroys all data)
docker compose down -v
```

---

## API Reference

### Existing Endpoints

These endpoints are implemented and tested.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Application health check. Returns `status`, `version`, and `database` connectivity (`"connected"` or `"disconnected"`). Graceful — always returns 200 even if the database is down. |
| `POST` | `/jobs/{id}/approve` | Approve a job in `needs_approval` state, transitioning it to `pending` for further processing |
| `POST` | `/jobs/{id}/reject` | Reject a job in `needs_approval` state, transitioning it to `rejected` |
| `GET` | `/jobs/{id}/events` | Return event history for a job, including approval/rejection events and abort events (with previous_status) |
| `GET` | `/workspaces` | List all workspaces, optionally filtered by `runner_id` and/or `status` (cleanup_status). Sorted by `created_at` descending. |
| `GET` | `/workspaces/{id}` | Retrieve a single workspace by its ID. |
| `POST` | `/workspaces/{id}/pin` | Toggle the pinned flag on a workspace. Pinned workspaces are excluded from automatic cleanup policies. |
| `POST` | `/workspaces/{id}/cleanup` | Trigger cleanup of a workspace via the executor plugin. Transitions to `cleaning` status, uses a per-workspace PG advisory lock to serialise concurrent cleanup requests. |
| `GET` | `/jobs/{job_id}/diff` | Retrieve the stored diff for a completed job. Returns 200 with the diff payload, 409 if the job is still running, or 404 if the job or its diff does not exist. |
| `POST` | `/jobs/{id}/abort` | Abort a pending or running job. Transitions through `aborting` to `aborted`, with optional OpenCode session abort and executor cleanup. Returns 503 if the OpenCode session is unreachable (job stays in `aborting`). |

> **Job lifecycle extension:** The approval gate feature introduces two new job statuses — `needs_approval` (job is paused awaiting a decision) and `rejected` (decision was negative). The abort feature introduces two additional statuses — `aborting` (abort in progress, OpenCode session being terminated) and `aborted` (final state after abort). These complement the existing statuses (`pending`, `running`, `completed`, `failed`).

| `POST` | `/jobs` | Submit a coding job. Accepts optional `runner_id` (pin to a specific runner) and `labels` (filter runners by label key). Auto-selects a healthy runner via load-balancing when neither is provided. Returns the final job state (201) or a policy violation (503). |
| `GET` | `/jobs/{id}` | Retrieve a job's status, result, and diff by its ID. |
| `POST` | `/observations` | Ingest a runner heartbeat observation — upserts the runner record, stores runner-level resource metrics (disk, memory, load), workspace snapshots, and OpenCode Serve instance status. Returns 201 on success. |
| `GET` | `/runners` | List all registered Runner VMs with their latest observation summary (disk, memory, load, observed_at). Ordered by creation date descending. |
| `GET` | `/runners/{id}` | Retrieve a single runner by UUID with full observation history (last 50 workspace observations + last 50 OpenCode instance observations) and derived policy status (HEALTHY, BLOCKED_DISK_PRESSURE, BLOCKED_MEMORY_PRESSURE, UNKNOWN, OFFLINE, MAINTENANCE, ONLINE). Returns 404 if not found. |
| `POST` | `/runners/{id}/status` | Manually set a runner's status to `offline`, `online`, or `maintenance`. Validates the state machine transition, logs the change to `job_events`. Returns 404 if runner not found, 422 for invalid transitions. |

### Planned Endpoints

These endpoints are defined in the [PRD](docs/prd/opencode-gateway.md) but not yet implemented. Status: **planned**.

*(None — all MVP endpoints are now implemented.)*


---

## Project Status

**As of June 2026**

| Issue | Title | Status |
|-------|-------|--------|
| #1 | Product Requirements Document | ✅ Complete |
| #2 | Gateway skeleton — FastAPI app factory, Postgres pool, health endpoint | ✅ Complete |
| #3 | Runner registration and observation ingestion | ✅ Complete |
| #4 | Job submission and tracking with local executor | ✅ Complete |
| #5 | OpenCode client protocol and HTTP implementation | ✅ Complete |
| #6 | Workspace lifecycle management | ✅ Complete |
| #7 | Job diff retrieval via OpenCode client | ✅ Complete |
| #8 | Job abort via OpenCode client | ✅ Complete |
| #9 | Pre-flight policy: disk pressure guardrails | ✅ Complete |
| #10 | AWX executor plugin | ✅ Complete |
| #11 | Approval gates for risky operations | 🔄 In Progress |
| #12 | Background cleanup scheduler | ✅ Complete |
| #13 | Paperclip integration adapter | 🔄 Planned |
| #14 | Gateway container image and docker-compose setup | ✅ Complete |
| #88 | Policy check reordering (policy before workspace creation) | ✅ Complete |
| #92 | Runner selection & job pinning (explicit, label-based, auto load-balancing) | ✅ Complete |
| #96 | Runner status management (manual offline/online/maintenance transitions) | ✅ Complete |
| #97 | Test infrastructure (integration tests, seed script, compose override) | ✅ Complete |


### Dependency DAG

```
#2 (foundation) → #3 (observations) → #9 (policy) → #88 (policy reorder)
                → #4 (jobs + executors) → #92 (runner selection)
                                        → #6 (workspaces) → #12 (cleanup scheduler)
                                        → #10 (AWX executor)
                                        → #11 (approvals)
                → #5 (OpenCode client) → #7 (diff), #8 (abort)
                → #14 (Docker)
                → #96 (runner status)
                → #97 (test infrastructure)
                                        → #13 (Paperclip adapter)
```

**Critical path**: #2 → #4 → #6 → #12 (or #2 → #4 → #10 if AWX is available early).

Total estimated effort: **7.5–12 hours** wall-clock across 18 issues. See the [full effort estimate](docs/issues/2026-06-04-effort-estimate.md) for per-issue breakdowns and risk analysis.

---

## Development

### Project Structure

```
opencode-gateway/
├── app/
│   ├── __init__.py               # Package init
│   ├── __main__.py               # Dev entry point (python -m app)
│   ├── main.py                   # Production entry point (uvicorn)
│   ├── api/
│   │   ├── __init__.py           # Router stubs
│   │   ├── health.py             # GET /health endpoint
│   │   └── workspaces.py        # Workspace endpoints (list, get, pin, cleanup)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   └── factory.py            # create_app() FastAPI factory
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py            # DatabasePool (asyncpg wrapper)
│   │   └── models/               # SQLAlchemy ORM models
│   │       ├── __init__.py
│   │       ├── base.py           # DeclarativeBase with naming convention
│   │       └── runner.py         # Runner, RunnerObservation, WorkspaceObservation, OpenCodeInstanceObservation
│   ├── executors/
│   │   ├── __init__.py           # ExecutorPlugin ABC, EXECUTOR_REGISTRY, model exports
│   │   ├── factory.py            # get_executor() — config-driven registry lookup
│   │   ├── local.py              # LocalExecutor (default, shell-based)
│   │   ├── models.py             # Pydantic request/response models
│   │   └── awx/
│   │       ├── __init__.py       # AWX executor package exports
│   │       ├── client.py         # AWXApiClient — httpx REST API client
│   │       ├── exceptions.py     # AWX exception hierarchy
│   │       └── plugin.py         # AWXExecutorPlugin — lifecycle implementation
│   ├── policy/
│   │   ├── __init__.py           # Exports ObservationBasedPolicy, PolicyViolation, PreflightPolicy
│   │   ├── base.py               # PreflightPolicy protocol + PolicyViolation exception
│   │   └── observation.py        # ObservationBasedPolicy — disk/memory/staleness guardrails
│   └── opencode/
│       ├── __init__.py           # Package init, exports OpenCodeServeClient and custom exceptions
│       ├── protocol.py           # OpenCodeClientProtocol ABC and Pydantic response models
│       └── serve_client.py       # httpx-based OpenCode Serve REST API client
│   ├── scheduler/
│   │   ├── __init__.py           # Scheduler package
│   │   ├── cleaner.py            # CleanupScheduler — background workspace cleanup
│   │   └── engine.py             # Scheduler engine base class
├── tests/
│   ├── __init__.py
│   ├── test_app_factory.py       # Application factory lifecycle tests
│   ├── test_awx_client.py        # AWXApiClient unit tests
│   ├── test_config.py            # Settings defaults, env overrides, .env loading
│   ├── test_db_pool.py           # DatabasePool connect/acquire/release/close
│   ├── test_entry_points.py      # main.py exports app, title matches
│   ├── test_executor_loader.py   # Executor registry and factory resolution
│   ├── test_executors.py         # Executor plugin interface and models
│   ├── test_executors_awx.py     # AWXExecutorPlugin unit tests
│   ├── test_executors_awx_mocktransport.py # AWXExecutorPlugin mock transport tests
│   ├── test_health.py            # Health endpoint: connected, disconnected, broken
│   ├── test_job_model.py         # Job Pydantic models
│   ├── test_jobs.py              # Job API endpoints
│   ├── test_local_executor.py    # LocalExecutor implementation
│   ├── test_schema.py            # Database schema migration tests
│   ├── test_serve_client.py      # OpenCode Serve httpx client
│   ├── test_workspace_lifecycle.py # Workspace pin/cleanup lifecycle
│   ├── test_workspace_model.py   # Workspace Pydantic models
│   ├── test_workspaces.py        # Workspace list/get API endpoints
│   ├── test_clients/
│   │   ├── __init__.py
│   │   ├── test_awx_client_mocktransport.py # AWXApiClient mock transport tests
│   │   └── test_serve_client_comprehensive.py # OpenCode Serve client comprehensive tests
│   └── ...                       # Future: integration, e2e tests
├── docs/
│   ├── adr/                      # Architecture Decision Records (4 ADRs)
│   ├── prd/                      # Product Requirements Document
│   └── issues/                   # Planning and effort estimates
├── .env.example                  # Environment variable template
├── .gitignore
├── .opencode-workflow.yaml
├── alembic.ini                   # Alembic configuration
├── alembic/                      # Alembic migrations
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_add_runners_and_observations.py
├── CONTEXT.md                    # Domain language glossary
├── pyproject.toml                # Project metadata, pytest, ruff, mypy config
├── requirements.txt              # Runtime and dev dependencies
└── README.md
```

### Running Tests

```bash
pytest tests/ -v                 # All tests (650+ tests across 28 files)
pytest tests/ -v -k "db"         # Database-related tests only (requires Postgres)
ruff check .                     # Linting (E, F, I, UP rules)
mypy app/ tests/                 # Type checking (strict mode)
```

### Key Development Patterns

- **Application factory pattern** — `create_app()` builds FastAPI with injectable startup/shutdown hooks. Tests inject mock callbacks to verify lifecycle ordering without side effects.
- **Graceful degradation** — The app starts and serves health checks even without PostgreSQL. The pool is set to `None` with a warning log; endpoints that need the database will fail at request time rather than at startup.
- **Async-first** — All I/O uses `async`/`await`: `asyncpg` for database, `httpx` for HTTP clients, `asyncio` for background scheduling.
- **Pydantic at every boundary** — Settings use `pydantic-settings`, API responses use Pydantic models, and the executor plugin interface uses typed Pydantic request/response objects.
- **Dependency injection** — FastAPI `Depends()` for database sessions (`get_session`) and settings (`get_settings`), making test overrides trivial.
- **Stub-first development** — Packages start as stubs with docstrings describing future contents, enabling incremental implementation. The `executors/` package grew from a stub into a full plugin registry, factory, and local executor; the `opencode/` package is now fully implemented with protocol abstractions, an httpx client, and custom exceptions.
- **ADR-driven decisions** — Every significant architectural choice is documented as an ADR before code is written.

---

## Architecture Decision Records

| ADR | Title | Summary |
|-----|-------|---------|
| [0001](docs/adr/0001-separate-observation-tables.md) | Separate Observation Tables | Observation data is stored in domain-specific tables (`runner_observations`, `workspace_observations`, `opencode_instance_observations`) with composite indexes optimized for time-range queries, rather than a single polymorphic table. |
| [0002](docs/adr/0002-executor-plugin-interface.md) | Executor Plugin Interface | Defines a six-method async abstract interface with typed Pydantic models for executor plugins. Concrete implementations: AWX (production), local shell (development), with SSH and Kubernetes as future options. |
| [0003](docs/adr/0003-postgres-port-allocation.md) | PostgreSQL Port Allocation | PostgreSQL is the single source of truth for port allocation (range 10000–10999). The Gateway selects an available port atomically via the database, not through the executor or Runner VM. |
| [0004](docs/adr/0004-gateway-no-infra-secrets.md) | Gateway Never Holds Infrastructure Secrets | The Gateway must never store or transmit SSH keys or Runner VM credentials. The executor plugin (AWX) owns all infrastructure secrets. The Gateway authenticates to AWX via an API token only. |

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up the project, running tests, code style, and the pull request workflow.

---

## Related Documentation

- **[Domain Language Glossary](CONTEXT.md)** — Precise definitions of all domain terms, key relationships, and example dialogues. Read this first to ensure consistent terminology.
- **[Product Requirements Document](docs/prd/opencode-gateway.md)** — Full problem statement, solution design, 29 user stories, database schema (7 tables), testing strategy, and out-of-scope items.
- **[Implementation Plan & Effort Estimate](docs/issues/2026-06-04-effort-estimate.md)** — 14-issue breakdown with dependency DAG, per-issue time estimates, risk analysis, and critical path identification.
