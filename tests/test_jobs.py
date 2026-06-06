"""Tests for the Job API endpoints."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi import Request

from app.core.factory import create_app
from app.db.session import get_session
from app.executors.factory import get_executor


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    return row


def _make_job_row(job_id, repo_url, task_summary, status="pending", *, completed_at=None):
    """Return a dict representing a jobs table row."""
    now = datetime.now(timezone.utc)
    return {
        "id": job_id,
        "repo_url": repo_url,
        "task_summary": task_summary,
        "status": status,
        "executor_type": "local",
        "created_at": now,
        "updated_at": now,
        "completed_at": completed_at,
    }


@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    return AsyncMock()


@pytest.fixture
def mock_executor():
    """Return a mock ExecutorPlugin."""
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


def _create_client(mock_conn, *, mock_executor=None):
    """Build app with overridden dependencies, return httpx AsyncClient."""
    app = create_app()
    mock_pool = AsyncMock()
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    if mock_executor is not None:
        app.dependency_overrides[get_executor] = lambda: mock_executor

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client(mock_conn):
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    return _create_client(mock_conn)


class TestCreateJob:
    """Tests for POST /jobs."""

    @pytest.mark.asyncio
    async def test_post_valid_job_returns_201(self, client, mock_conn):
        """POST /jobs with valid input returns 201 with job data and status=pending."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug"
        )
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["repo_url"] == "https://github.com/org/repo"
        assert data["task_summary"] == "Fix a bug"
        assert data["status"] == "pending"
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_post_empty_task_summary_returns_422(self, client):
        """POST /jobs with empty task_summary should return 422."""
        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "",
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_post_invalid_url_returns_422(self, client):
        """POST /jobs with an invalid repo_url should return 422."""
        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "not-a-valid-url",
                    "task_summary": "Fix a bug",
                },
            )
        assert response.status_code == 422


class TestGetJob:
    """Tests for GET /jobs/{id}."""

    @pytest.mark.asyncio
    async def test_get_existing_job_returns_200(self, client, mock_conn):
        """GET /jobs/{id} for an existing job returns 200 with full record."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Add feature"
        )
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(job_id)
        assert data["repo_url"] == "https://github.com/org/repo"
        assert data["task_summary"] == "Add feature"
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_unknown_job_returns_404(self, client, mock_conn):
        """GET /jobs/{id} for an unknown job should return 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_invalid_uuid_returns_422(self, client):
        """GET /jobs/{id} with a malformed UUID should return 422."""
        async with client as c:
            response = await c.get("/jobs/not-a-uuid")

        assert response.status_code == 422


class TestJobDispatch:
    """Tests for the executor dispatch wiring in POST /jobs."""

    @pytest.mark.asyncio
    async def test_post_job_dispatches_to_executor_and_completes(self, mock_conn, mock_executor):
        """POST /jobs should dispatch to executor and return completed status with completed_at."""
        job_id = uuid.uuid4()

        # Track status changes across the flow
        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None

        # Verify executor was called
        mock_executor.create_workspace.assert_called_once()
        mock_executor.start_opencode.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_job_transitions_pending_to_running_to_completed(self, mock_conn, mock_executor):
        """Status should transition pending → running → completed in the DB."""
        job_id = uuid.uuid4()

        # Capture update calls to verify status transitions
        execute_calls: list[str] = []

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row_data)
            return None

        async def _execute(sql, *args):
            execute_calls.append(sql)
            # Update row_data.status based on UPDATE statements
            if "UPDATE jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Add feature",
                },
            )

        assert response.status_code == 201
        assert response.json()["status"] == "completed"

        # Verify status transition updates happened
        update_statements = [s for s in execute_calls if "UPDATE jobs" in s]
        assert len(update_statements) >= 2  # pending→running, running→completed

    @pytest.mark.asyncio
    async def test_executor_failure_transitions_job_to_failed(self, mock_conn):
        """When the executor raises, the job should transition to failed status."""
        job_id = uuid.uuid4()

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Create a failing executor
        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Workspace creation failed")
        )

        client = _create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix bug",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "failed"
        assert data["completed_at"] is None

    @pytest.mark.asyncio
    async def test_completed_job_has_completed_at_set(self, mock_conn, mock_executor):
        """A completed job should have completed_at populated."""
        job_id = uuid.uuid4()

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Add feature",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None
