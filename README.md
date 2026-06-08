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
| **Executor Plugin Interface** | `app/executors/` | Abstract async base class defining six methods (`create_workspace`, `start_opencode`, `stop_opencode`, `restart_opencode`, `collect_state`, `cleanup_workspace`), typed Pydantic models, and a registry (`EXECUTOR_REGISTRY`) mapping executor type names to implementation classes. The factory (`factory.py`) resolves the active executor from the `GATEWAY_EXECUTOR_TYPE` config via the registry. MVPs: **local executor** (default, shipping), **AWX** (planned). Design documented in [ADR 0002](docs/adr/0002-executor-plugin-interface.md). |
| **OpenCode Serve Client** | `app/opencode/` | `httpx`-based wrapper for the OpenCode Serve REST API: health checks, session CRUD, task submission, diff retrieval, and abort. |

### Interaction Flow

> **Submit job** → Gateway creates a job record in PostgreSQL → checks runner health via policy module → delegates workspace creation to the executor plugin → executor starts OpenCode Serve on an allocated port → Gateway sends the coding task via the OpenCode client → OpenCode produces a diff → Gateway records the result → caller polls or retrieves the diff.

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
| **Database** | PostgreSQL 15+ via `asyncpg` | Direct connection pool (no ORM yet; SQLAlchemy/Alembic under consideration) |
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

> **Note:** The Gateway supports **graceful degradation** — if PostgreSQL is unreachable at startup, the app still starts and the health endpoint returns `"database": "disconnected"` instead of crashing. This is by design.

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
{"status": "ok", "version": "0.1.0-dev", "database": "connected"}
```

If the database is unreachable, the response still returns HTTP 200 but reports `"database": "disconnected"`.

> **Docker support** is planned (Issue #14) but not yet available. For now, run Postgres locally or use `docker run` to spin up a temporary PostgreSQL container.

---

## API Reference

### Existing Endpoints

These endpoints are implemented and tested.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Application health check. Returns `status`, `version`, and `database` connectivity (`"connected"` or `"disconnected"`). Graceful — always returns 200 even if the database is down. |
| `POST` | `/jobs/{id}/approve` | Approve a job in `needs_approval` state, transitioning it to `pending` for further processing |
| `POST` | `/jobs/{id}/reject` | Reject a job in `needs_approval` state, transitioning it to `rejected` |
| `GET` | `/jobs/{id}/events` | Return approval/rejection event history for a job |

> **Job lifecycle extension:** The approval gate feature introduces two new job statuses — `needs_approval` (job is paused awaiting a decision) and `rejected` (decision was negative). These complement the existing statuses (`pending`, `running`, `completed`, `failed`, `aborted`).

### Planned Endpoints

These endpoints are defined in the [PRD](docs/prd/opencode-gateway.md) but not yet implemented. Status: **planned**.

| Method | Path | Description | Issue |
|--------|------|-------------|-------|
| `POST` | `/jobs` | Submit a coding job | #4 |
| `GET` | `/jobs/{id}` | Get job status, result, and diff | #4, #7 |
| `POST` | `/jobs/{id}/abort` | Abort a running job | #8 |
| `GET` | `/runners` | List registered runners | #3 |
| `GET` | `/runners/{id}` | Get runner details and health | #3 |
| `POST` | `/runners/{id}/cleanup` | Trigger workspace cleanup | #6 |
| `GET` | `/workspaces` | List workspaces | #6 |
| `GET` | `/workspaces/{id}` | Get workspace details | #6 |
| `GET` | `/observations` | Query runner/workspace observations | #3 |


---

## Project Status

**As of June 2026**

| Issue | Title | Status |
|-------|-------|--------|
| #1 | Product Requirements Document | ✅ Complete |
| #2 | Gateway skeleton — FastAPI app factory, Postgres pool, health endpoint | ✅ Complete |
| #3 | Runner registration and observation ingestion | 🔄 Planned |
| #4 | Job submission and tracking with local executor | 🔄 Planned |
| #5 | OpenCode client protocol and HTTP implementation | ✅ Complete |
| #6 | Workspace lifecycle management | 🔄 Planned |
| #7 | Job diff retrieval via OpenCode client | 🔄 Planned |
| #8 | Job abort via OpenCode client | 🔄 Planned |
| #9 | Pre-flight policy: disk pressure guardrails | 🔄 Planned |
| #10 | AWX executor plugin | 🔄 Planned |
| #11 | Approval gates for risky operations | 🔄 In Progress |
| #12 | Background cleanup scheduler | 🔄 Planned |
| #13 | Paperclip integration adapter | 🔄 Planned |
| #14 | Gateway container image and docker-compose setup | 🔄 Planned |

### Dependency DAG

```
#2 (foundation) → #3 (observations) → #9 (policy)
                → #4 (jobs + executors) → #6 (workspaces) → #12 (cleanup scheduler)
                                        → #10 (AWX executor)
                                        → #11 (approvals)
                → #5 (OpenCode client) → #7 (diff), #8 (abort)
                → #14 (Docker)
                                        → #13 (Paperclip adapter)
```

**Critical path**: #2 → #4 → #6 → #12 (or #2 → #4 → #10 if AWX is available early).

Total estimated effort: **7.5–12 hours** wall-clock across 14 issues. See the [full effort estimate](docs/issues/2026-06-04-effort-estimate.md) for per-issue breakdowns and risk analysis.

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
│   │   └── health.py             # GET /health endpoint
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   └── factory.py            # create_app() FastAPI factory
│   ├── db/
│   │   ├── __init__.py
│   │   └── session.py            # DatabasePool (asyncpg wrapper)
│   ├── executors/
│   │   ├── __init__.py           # ExecutorPlugin ABC, EXECUTOR_REGISTRY, model exports
│   │   ├── factory.py            # get_executor() — config-driven registry lookup
│   │   ├── local.py              # LocalExecutor (default, shell-based)
│   │   ├── models.py             # Pydantic request/response models
│   │   └── ...                   # Future: awx.py, ssh.py
│   └── opencode/
│       ├── __init__.py           # Package init, exports OpenCodeServeClient and custom exceptions
│       ├── protocol.py           # OpenCodeClientProtocol ABC and Pydantic response models
│       └── serve_client.py       # httpx-based OpenCode Serve REST API client
├── tests/
│   ├── __init__.py
│   ├── test_app_factory.py       # Application factory lifecycle tests
│   ├── test_config.py            # Settings defaults, env overrides, .env loading
│   ├── test_db_pool.py           # DatabasePool connect/acquire/release/close
│   ├── test_entry_points.py      # main.py exports app, title matches
│   └── test_health.py            # Health endpoint: connected, disconnected, broken
├── docs/
│   ├── adr/                      # Architecture Decision Records (4 ADRs)
│   ├── prd/                      # Product Requirements Document
│   └── issues/                   # Planning and effort estimates
├── .env.example                  # Environment variable template
├── .gitignore
├── .opencode-workflow.yaml
├── CONTEXT.md                    # Domain language glossary
├── pyproject.toml                # Project metadata, pytest, ruff, mypy config
├── requirements.txt              # Runtime and dev dependencies
└── README.md
```

### Running Tests

```bash
pytest tests/ -v                 # All tests (25+ tests across 5 files)
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
| [0003](docs/adr/0003-postgres-port-allocation.md) | PostgreSQL Port Allocation | PostgreSQL is the single source of truth for port allocation (range 4100–4199). The Gateway selects an available port atomically via the database, not through the executor or Runner VM. |
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
