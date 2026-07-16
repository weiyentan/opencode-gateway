# OpenCode Gateway

*An observability service for headless OpenCode.*

OpenCode Gateway provides monitoring, telemetry collection, and health tracking for OpenCode serve instances. It ingests observations from Runner VMs, stores time-series metrics in PostgreSQL, and exposes them through a clean REST API. Platform engineers and agent orchestrators (like Paperclip) use the Gateway to monitor OpenCode deployments at scale.

> **Note:** This project is currently undergoing a refactor from an execution control plane into an observability service. Execution-era subsystems (executor plugins, job scheduling, workspace lifecycle) were removed in issue #207. Observability features will be built out in subsequent slices.

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
| **API Layer** | `app/api/` | REST endpoints (currently: `/health`). API key authentication from day one. Consistent JSON response envelope for all endpoints. |
| **Core Engine** | `app/core/` | Pydantic-based settings and config (`GATEWAY_` env prefix), application factory, logging with secret redaction, and auth middleware. |
| **Database Layer** | `app/db/` | asyncpg connection pool, SQLAlchemy ORM base (for future models), Alembic migrations, and advisory lock utilities. |

Additional layers will be added as observability features are built out in future slices.

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

Expected response:

```json
{"status": "ok", "data": {"status": "ok", "version": "0.1.0-dev", "database": "connected"}}
```

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

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Application health check. Returns `status`, `version`, and `database` connectivity. Graceful — always returns 200 even if the database is down. |

Additional endpoints will be added in future slices as observability features are built out.

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
│   │   └── health.py             # GET /health endpoint
│   ├── core/
│   │   ├── config.py             # Pydantic Settings (GATEWAY_ prefix)
│   │   ├── auth.py               # API key middleware
│   │   ├── envelope.py           # Response envelope middleware
│   │   ├── factory.py            # create_app() FastAPI factory
│   │   ├── logging.py            # RedactingFormatter
│   │   └── secrets.py            # Secret detection utilities
│   └── db/
│       ├── session.py            # DatabasePool (asyncpg wrapper)
│       ├── schema.py             # Schema management (delegates to Alembic)
│       ├── setup.py              # Migration runner + table validation
│       ├── lock.py               # Advisory locks
│       └── models/
│           ├── __init__.py
│           └── base.py           # SQLAlchemy declarative base
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
