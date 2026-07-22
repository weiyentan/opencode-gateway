# Contributing to OpenCode Gateway

Thank you for your interest in contributing! This document covers how to set up the project locally, run tests, follow code style conventions, and submit changes.

---

## Prerequisites

- **Python 3.12** or later
- **PostgreSQL 15** or later (or Docker for a local instance)

> **Quick start with Docker Compose:** If you have Docker (20.10+) and Docker Compose (v2+), you can skip the manual setup below and run the entire same-origin stack in containers:
> ```bash
> cp .env.example .env
> docker compose up -d
> curl http://localhost:8080/health    # proxied to gateway by frontend nginx
> ```
> See [Running with Docker](README.md#running-with-docker-same-origin-local-stack) in the README for details.

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
pytest tests/ -v                 # Run all tests
pytest tests/ -v -k "db"         # Run only database-related tests (requires Postgres)
```

Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = auto`). Most tests mock the database layer; tests tagged with `test_db` or `test_schema` require a running PostgreSQL instance.

### Integration test database

A dedicated Docker Compose file starts a Postgres container on port 5433 for tests that need a real database:

```bash
docker compose -f docker-compose.test.yml up -d
pytest tests/ -v -k "db"
docker compose -f docker-compose.test.yml down -v
```

### E2E smoke test

An end-to-end smoke test validates the same-origin local stack (frontend nginx + Gateway + Postgres):

```bash
docker compose -f docker-compose.yaml -f docker-compose.smoke.yml up -d
pytest tests/test_smoke_local_stack.py -v
docker compose -f docker-compose.yaml -f docker-compose.smoke.yml down -v
```

### Database seeding

The `scripts/seed.py` tool populates the database with sample data for manual testing:

```bash
python scripts/seed.py --help
```

See the script's help for all available options. It is idempotent and safe to run multiple times.

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
│   ├── __init__.py
│   ├── __main__.py               # Dev entry point (python -m app)
│   ├── main.py                   # Production entry point (uvicorn)
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
│       ├── __init__.py
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
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── Dockerfile                # nginx:alpine container for frontend + API proxy
│   ├── nginx.conf                # Reverse proxy config (envsubst template)
│   ├── docker-entrypoint.sh      # Substitutes GATEWAY_UPSTREAM at runtime
│   └── tests/
├── tests/
│   ├── __init__.py
│   ├── conftest.py               # Shared fixtures
│   ├── test_app_factory.py       # Application factory lifecycle tests
│   ├── test_auth.py              # API key authentication tests
│   ├── test_auth_envelope.py     # Response envelope with auth
│   ├── test_config.py            # Settings defaults, env overrides, .env loading
│   ├── test_db_pool.py           # DatabasePool connect/acquire/release/close
│   ├── test_entry_points.py      # main.py exports app, title matches
│   ├── test_factory.py           # Factory tests
│   ├── test_frontend.py          # Frontend container and proxy tests
│   ├── test_health.py            # Health endpoint: connected, disconnected, broken
│   ├── test_identity.py          # Token generation and hashing
│   ├── test_ingest.py            # Telemetry ingest endpoint
│   ├── test_schema.py            # Database schema tests
│   ├── test_secrets.py           # Secret detection utilities
│   ├── test_setup.py             # Migration runner tests
│   ├── test_smoke_local_stack.py # E2E smoke test for same-origin Docker stack
│   └── test_usage.py             # Usage reporting endpoint tests
├── scripts/
│   ├── seed.py                   # Database seeding script
│   ├── worktree-manager.sh       # Git worktree management helper
│   └── worktree-manager.ps1      # Git worktree management helper (PowerShell)
├── docs/
│   └── adr/                      # Architecture Decision Records
├── alembic/                      # Alembic migrations
├── .env.example
├── .github/
│   └── workflows/
│       └── publish.yml           # Builds and publishes Gateway + Aurora Glass images
├── docker-compose.yaml           # Same-origin local development stack
├── docker-compose.smoke.yml      # E2E smoke test override (GATEWAY_ENV=development)
├── docker-compose.test.yml       # Standalone Postgres for integration tests
├── Dockerfile                    # Gateway image (Python/FastAPI)
├── pyproject.toml                # Project metadata, pytest, ruff, mypy config
├── requirements.txt              # Runtime and dev dependencies
├── README.md
└── CONTRIBUTING.md
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
