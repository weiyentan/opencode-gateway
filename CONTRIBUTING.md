# Contributing to OpenCode Gateway

Thank you for your interest in contributing! This document covers how to set up the project locally, run tests, follow code style conventions, and submit changes.

---

## Prerequisites

- **Python 3.12** or later
- **PostgreSQL 15** or later (or Docker for a local instance)

> **Quick start with Docker Compose:** If you have Docker (20.10+) and Docker Compose (v2+), you can skip the manual setup below and run the entire Gateway stack in containers:
> ```bash
> cp .env.example .env
> docker compose up -d
> curl http://localhost:8000/health
> ```
> See [Running with Docker](README.md#running-with-docker) in the README for details.

---

## Setting Up the Project

1. **Clone the repository**

   ```bash
   git clone <repo-url>
   cd opencode-gateway
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv .venv

   # Linux / macOS
   source .venv/bin/activate

   # Windows
   .venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure the environment**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` to point to your local PostgreSQL instance. All configuration uses the `GATEWAY_` prefix and is documented in the [README](README.md#configuration).

5. **Set up the database**

   Create a PostgreSQL database named `opencode_gateway` (or whatever you set in `GATEWAY_DATABASE_NAME`) and ensure the credentials in `.env` match.

---

## Running the Development Server

```bash
python -m app
```

This starts the Gateway with auto-reload enabled. The server binds to `http://localhost:8000` by default.

Verify it's running:

```bash
curl http://localhost:8000/health
```

Expected response: `{"status": "ok", "data": {"status": "ok", "version": "0.1.0-dev", "database": "connected"}}` (or `"data": {"status": "ok", "version": "0.1.0-dev", "database": "disconnected"}` if Postgres is unreachable — the Gateway degrades gracefully).

---

## Running Tests

```bash
pytest tests/ -v                 # Run all tests (unit + integration)
pytest tests/ -v -k "db"         # Run only database-related tests (requires Postgres)
pytest tests/integration/ -v -m integration  # Integration tests only (requires Postgres)
```

Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = auto`). Unit tests mock the database layer; integration tests (marked with `@pytest.mark.integration`) require a running PostgreSQL instance.

### Integration test database

A dedicated Docker Compose file starts a Postgres container on port 5433 for integration tests:

```bash
docker compose -f docker-compose.test.yml up -d
pytest tests/integration/ -v -m integration
docker compose -f docker-compose.test.yml down -v
```

### Database seeding

The `scripts/seed.py` tool populates the database with sample data for manual testing:

```bash
python scripts/seed.py --runners 3 --workspaces 5 --jobs 10 --observations 20
```

See `python scripts/seed.py --help` for all options. The script is idempotent and safe to run multiple times.

---

## Code Style

We use automated tooling to maintain consistency:

| Tool | Purpose | Command |
|------|---------|---------|
| **ruff** | Linting (E, F, I, UP rules) and import sorting | `ruff check .` |
| **mypy** | Static type checking (strict mode, Python 3.12 target) | `mypy app/ tests/` |
| **pytest** | Unit and integration tests | `pytest tests/ -v` |

Run these before submitting a pull request. All CI checks must pass.

---

## Project Structure

```
opencode-gateway/
├── app/
│   ├── __init__.py               # Package init
│   ├── __main__.py               # Dev entry point (python -m app)
│   ├── main.py                   # Production entry point (uvicorn)
│   ├── api/
│   │   ├── __init__.py           # Router stubs
│   │   ├── health.py             # GET /health endpoint
│   │   ├── jobs.py               # Job CRUD, dispatch, approval, abort
│   │   ├── runners.py            # Runner list, detail, status management
│   │   └── workspaces.py         # Workspace endpoints (list, get, pin, cleanup)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   └── lifecycle.py          # State machine transition rules
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.py             # Schema migration utility
│   │   ├── session.py            # DatabasePool (asyncpg wrapper)
│   │   └── models/               # SQLAlchemy ORM models
│   │       ├── __init__.py
│   │       ├── base.py           # DeclarativeBase with naming convention
│   │       └── runner.py         # Runner, RunnerObservation, WorkspaceObservation, etc.
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
│   │   ├── __init__.py           # Exports ObservationBasedPolicy, PolicyViolation
│   │   ├── base.py               # PreflightPolicy protocol + PolicyViolation exception
│   │   └── observation.py        # ObservationBasedPolicy — disk/memory/staleness guardrails
│   ├── opencode/
│   │   ├── __init__.py           # Package init, exports OpenCodeServeClient
│   │   ├── protocol.py           # OpenCodeClientProtocol ABC and Pydantic response models
│   │   └── serve_client.py       # httpx-based OpenCode Serve REST API client
│   └── scheduler/
│       ├── __init__.py           # Scheduler package
│       ├── cleaner.py            # CleanupScheduler — background workspace cleanup
│       └── engine.py             # Scheduler engine base class
├── frontend/                     # Aurora Glass telemetry dashboard (HTML/CSS/JS SPA)
├── tests/
│   ├── __init__.py
│   ├── conftest.py               # Shared fixtures (mock_conn, client, async test helpers)
│   ├── integration/              # Integration tests requiring Postgres
│   │   ├── __init__.py
│   │   ├── conftest.py           # DB fixtures, helper factories
│   │   ├── test_job_crud.py      # Job CRUD integration tests
│   │   ├── test_runner.py        # Runner + observation integration tests
│   │   └── test_schema.py        # Schema migration smoke tests
│   ├── test_app_factory.py       # Application factory lifecycle tests
│   ├── test_awx_client.py        # AWXApiClient unit tests
│   ├── test_config.py            # Settings defaults, env overrides, .env loading
│   ├── test_db_pool.py           # DatabasePool connect/acquire/release/close
│   ├── test_entry_points.py      # main.py exports app, title matches
│   ├── test_executor_loader.py   # Executor registry and factory resolution
│   ├── test_executors.py         # Executor plugin interface and models
│   ├── test_executors_awx.py     # AWXExecutorPlugin unit tests
│   ├── test_health.py            # Health endpoint: connected, disconnected, broken
│   ├── test_job_model.py         # Job Pydantic models
│   ├── test_jobs.py              # Job API endpoints
│   ├── test_local_executor.py    # LocalExecutor implementation
│   ├── test_policy.py            # Policy engine unit tests
│   ├── test_runners.py           # Runner API endpoints
│   ├── test_schema.py            # Database schema tests
│   ├── test_serve_client.py      # OpenCode Serve httpx client
│   ├── test_workspace_lifecycle.py # Workspace pin/cleanup lifecycle
│   ├── test_workspace_model.py   # Workspace Pydantic models
│   ├── test_workspaces.py        # Workspace list/get API endpoints
│   └── test_clients/             # Client test suites with mock transport
│       ├── __init__.py
│       ├── test_awx_client_mocktransport.py
│       └── test_serve_client_comprehensive.py
├── scripts/
│   ├── seed.py                   # Database seeding script (runners, jobs, observations)
│   └── worktree-manager.sh       # Git worktree management helper
├── docs/
│   └── adr/                      # Architecture Decision Records (4 ADRs)
├── docker-compose.test.yml       # Standalone Postgres for integration tests
├── pyproject.toml                # Project metadata, pytest, ruff, mypy config
├── requirements.txt              # Runtime and dev dependencies
└── README.md
```

---

## How to Submit Changes

1. **Fork** the repository and create a feature branch from `main`.

   ```bash
   git checkout -b feature/my-change
   ```

2. **Make your changes**, following the code style guidelines above. Write or update tests as appropriate.

3. **Run the full test suite and linting** to ensure nothing is broken.

   ```bash
   pytest tests/ -v
   ruff check .
   mypy app/ tests/
   ```

4. **Commit** with a clear, descriptive message.

   ```bash
   git commit -m "Add description of your change"
   ```

5. **Push** your branch and open a **pull request** against the `main` branch.

   ```bash
   git push origin feature/my-change
   ```

6. In the PR description, explain what the change does and why. Link any related issues. A maintainer will review your PR and provide feedback.

---

## How to Report Issues

- **Bug reports**: Open an issue with steps to reproduce, expected behavior, and actual behavior. Include relevant logs and environment details (Python version, OS, PostgreSQL version).
- **Feature requests**: Describe the use case and why it matters. Reference any related ADRs or PRD sections if applicable.
- **Questions**: Use the issue tracker with a "question" label, or start a discussion if available.

---

## Development Principles

Before contributing, familiarise yourself with the project's design philosophy:

- **[Domain Language Glossary](CONTEXT.md)** — Precise terminology used throughout the project.
- **[Architecture Decision Records](docs/adr/)** — Documented rationale for key architectural choices.
- **Application factory pattern** — `create_app()` builds FastAPI with injectable startup/shutdown hooks.
- **Graceful degradation** — The app starts without PostgreSQL and reports degraded status rather than crashing.
- **Async-first** — All I/O uses `async`/`await`: `asyncpg`, `httpx`, `asyncio`.
- **Pydantic at every boundary** — Settings, API responses, and executor interfaces all use typed Pydantic models.
- **Stub-first development** — Packages exist as stubs with docstrings before full implementation.

---

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
