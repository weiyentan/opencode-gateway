"""Shared test fixtures and helper builders for the OpenCode Gateway test suite.

Centralises duplicated mock rows, sessions, clients, and helper builders
that were previously copy-pasted across individual test files.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session
from app.executors.factory import get_executor

# ══════════════════════════════════════════════════════════════════════════
#  Core helpers
# ══════════════════════════════════════════════════════════════════════════


def mock_row(data: dict) -> MagicMock:
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def make_job_row(
    job_id: uuid.UUID,
    repo_url: str,
    task_summary: str,
    status: str = "pending",
    *,
    completed_at: datetime | None = None,
    opencode_session_id: str | None = None,
    diff: str | None = None,
    workspace_name: str | None = None,
    env_vars: dict[str, str] | None = None,
) -> dict:
    """Return a dict representing a gateway_jobs table row."""
    now = datetime.now(timezone.utc)  # noqa: UP017
    return {
        "id": job_id,
        "repo_url": repo_url,
        "task_summary": task_summary,
        "status": status,
        "executor_type": "local",
        "env_vars": env_vars or {},
        "created_at": now,
        "updated_at": now,
        "completed_at": completed_at,
        "opencode_session_id": opencode_session_id,
        "diff": diff,
        "workspace_name": workspace_name,
    }


def make_workspace_row(
    workspace_id: uuid.UUID,
    *,
    runner_id: uuid.UUID | None = None,
    workspace_name: str = "ws-test",
    path: str = "/data/workspaces/ws-test",
    repo_url: str = "https://github.com/example/repo.git",
    branch: str | None = None,
    port: int | None = None,
    service_name: str | None = None,
    pinned: bool = False,
    cleanup_after: datetime | None = None,
    cleanup_status: str = "active",
    created_at: datetime | None = None,
) -> dict:
    """Return a dict representing a workspaces table row."""
    now = created_at or datetime.now(timezone.utc)  # noqa: UP017
    return {
        "id": workspace_id,
        "runner_id": runner_id,
        "workspace_name": workspace_name,
        "path": path,
        "repo_url": repo_url,
        "branch": branch,
        "port": port,
        "service_name": service_name,
        "pinned": pinned,
        "cleanup_after": cleanup_after,
        "cleanup_status": cleanup_status,
        "created_at": now,
        "updated_at": now,
    }


def create_client(
    mock_conn: AsyncMock,
    *,
    mock_executor: AsyncMock | None = None,
    mock_opencode_client: AsyncMock | None = None,
) -> AsyncClient:
    """Build app with overridden dependencies, return httpx AsyncClient."""
    from app.api.jobs import _get_pool, get_opencode_client

    app = create_app()
    mock_pool = AsyncMock()
    # Set pool.pool to None so background webhook dispatch exits early in tests.
    # The webhook dispatch is tested separately in test_webhooks.py with its own
    # mock setup.
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[_get_pool] = lambda: mock_pool

    # Always inject an executor mock so endpoints that depend on it work
    _mock_exec = mock_executor if mock_executor is not None else AsyncMock()
    app.dependency_overrides[get_executor] = lambda: _mock_exec

    if mock_opencode_client is not None:
        app.dependency_overrides[get_opencode_client] = lambda: mock_opencode_client

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_conn() -> AsyncMock:
    """Return a mock asyncpg connection."""
    return AsyncMock()


@pytest.fixture
def client(mock_conn: AsyncMock) -> AsyncClient:
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    return create_client(mock_conn)


@pytest.fixture
def mock_executor() -> AsyncMock:
    """Return a mock ExecutorPlugin for job lifecycle.

    Pre-configured with successful create_workspace and start_opencode
    responses.  Test files that need different behaviour can override
    this fixture locally.
    """
    from app.executors.models import (
        CreateWorkspaceResponse,
        StartOpencodeResponse,
    )

    executor = AsyncMock()
    executor.create_workspace = AsyncMock(
        return_value=CreateWorkspaceResponse(
            workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            workspace_path="/tmp/opencode/ws",
            status="ready",
        )
    )
    executor.start_opencode = AsyncMock(
        return_value=StartOpencodeResponse(
            session_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            status="running",
            port=8080,
        )
    )
    return executor
