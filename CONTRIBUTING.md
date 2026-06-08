# Contributing to OpenCode Gateway

Thank you for your interest in contributing! This document covers how to set up the project locally, run tests, follow code style conventions, and submit changes.

---

## Prerequisites

- **Python 3.12** or later
- **PostgreSQL 15** or later (or Docker for a local instance)

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

Expected response: `{"status": "ok", "version": "0.1.0-dev", "database": "connected"}` (or `"disconnected"` if Postgres is unreachable — the Gateway degrades gracefully).

---

## Running Tests

```bash
pytest tests/ -v                 # Run all tests
pytest tests/ -v -k "db"         # Run only database-related tests (requires Postgres)
```

Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = auto`). The database tests require a running PostgreSQL instance with the correct credentials.

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
│   │   ├── approval.py           # POST /jobs/{id}/approve, /reject, /events
│   │   ├── health.py             # GET /health endpoint
│   │   ├── jobs.py               # Job API endpoints (planned: #4)
│   │   └── workspaces.py         # GET /workspaces, /workspaces/{id}, POST pin/cleanup
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   └── models/
│   │       ├── __init__.py       # Exports Approval, Job, Workspace models
│   │       ├── approval.py       # Approval domain model
│   │       ├── job.py            # Job domain model
│   │       └── workspace.py      # Workspace domain model, WorkspaceStatus enum
│   ├── db/
│   │   ├── __init__.py
│   │   └── session.py            # DatabasePool (asyncpg wrapper)
│   ├── executors/
│   │   ├── __init__.py           # Stub — will hold ExecutorPlugin ABC
│   │   └── ...                   # Future: base.py, local.py, awx.py
│   └── opencode/
│       ├── __init__.py           # Package init, exports OpenCodeServeClient and custom exceptions
│       ├── protocol.py           # OpenCodeClientProtocol ABC and Pydantic response models
│       └── serve_client.py       # httpx-based OpenCode Serve REST API client
├── tests/
│   ├── __init__.py
│   ├── test_api_workspaces.py    # Workspace API endpoint tests (list, get, pin, cleanup)
│   ├── test_app_factory.py       # Application factory lifecycle tests
│   ├── test_config.py            # Settings defaults, env overrides, .env loading
│   ├── test_db_pool.py           # DatabasePool connect/acquire/release/close
│   ├── test_entry_points.py      # main.py exports app, title matches
│   ├── test_health.py            # Health endpoint: connected, disconnected, broken
│   ├── test_workspace_model.py   # Workspace domain model and WorkspaceStatus tests
│   └── test_workspaces.py        # Workspace lifecycle integration tests
├── docs/
│   ├── adr/                      # Architecture Decision Records
│   ├── prd/                      # Product Requirements Document
│   └── issues/                   # Planning and effort estimates
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
