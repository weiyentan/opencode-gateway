"""Tests for the Job API endpoints."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session
from app.executors.factory import get_executor


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get = data.get
    return row


def _make_job_row(job_id, repo_url, task_summary, status="pending", *, completed_at=None,
                  opencode_session_id=None, diff=None, workspace_name=None):
    """Return a dict representing a gateway_jobs table row."""
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
        "opencode_session_id": opencode_session_id,
        "diff": diff,
        "workspace_name": workspace_name,
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


def _create_client(mock_conn, *, mock_executor=None, mock_opencode_client=None):
    """Build app with overridden dependencies, return httpx AsyncClient."""
    from app.api.jobs import get_opencode_client

    app = create_app()
    mock_pool = AsyncMock()
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    # Always inject an executor mock so endpoints that depend on it work
    _mock_exec = mock_executor if mock_executor is not None else AsyncMock()
    app.dependency_overrides[get_executor] = lambda: _mock_exec

    if mock_opencode_client is not None:
        app.dependency_overrides[get_opencode_client] = lambda: mock_opencode_client

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
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
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
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            execute_calls.append(sql)
            # Update row_data.status based on UPDATE statements
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
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
        update_statements = [s for s in execute_calls if "UPDATE gateway_jobs" in s]
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
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
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
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
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

    @pytest.mark.asyncio
    async def test_create_job_returns_503_when_policy_rejects_runner(
        self, mock_conn, mock_executor
    ):
        """POST /jobs returns 503 when ObservationBasedPolicy.check raises PolicyViolation."""
        from unittest.mock import patch

        from app.policy import ObservationBasedPolicy, PolicyViolation

        job_id = uuid.uuid4()
        workspace_uuid = uuid.uuid4()
        runner_uuid = uuid.uuid4()

        # Build mock rows for _resolve_runner_id_for_workspace queries
        ws_row = _mock_row({"runner_id": runner_uuid})
        runner_row = _mock_row({"runner_id": "test-runner-99"})

        async def _execute(sql, *args):
            pass

        async def _fetchrow(sql, *args):
            if "workspaces" in sql and "runner_id" in sql:
                return ws_row
            if "FROM runners WHERE id" in sql:
                return runner_row
            return None

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        with patch.object(
            ObservationBasedPolicy, "check", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = PolicyViolation(
                resource="disk",
                current_value=95.0,
                threshold=80.0,
                runner_id="test-runner-99",
            )

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

        assert response.status_code == 503
        data = response.json()
        assert data["detail"]["resource"] == "disk"
        assert data["detail"]["current_value"] == 95.0
        assert data["detail"]["threshold"] == 80.0
        assert data["detail"]["runner_id"] == "test-runner-99"
        assert "disk" in data["detail"]["message"]
        assert "80%" in data["detail"]["message"]


class TestApproveJob:
    """Tests for POST /jobs/{id}/approve."""

    @pytest.mark.asyncio
    async def test_approve_needs_approval_job_returns_200_and_transitions_to_running(
        self, mock_conn
    ):
        """Approve transitions needs_approval → running."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Approve me",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/approve")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["id"] == str(job_id)

    @pytest.mark.asyncio
    async def test_approve_unknown_job_returns_404(self, client, mock_conn):
        """Approve on non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/jobs/{uuid.uuid4()}/approve")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_wrong_state_returns_409(self, client, mock_conn):
        """Approve on job not in needs_approval state returns 409."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Already running",
            status="running",
        )
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/approve")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_approve_writes_approval_record(self, mock_conn):
        """Approve inserts a record into the approvals table with status='approved'."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Approve me",
            status="needs_approval",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/approve")

        assert response.status_code == 200

        # Verify an INSERT into approvals happened with approved status
        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO approvals" in sql
        ]
        assert len(insert_calls) == 1
        insert_sql, insert_args = insert_calls[0]
        assert "approval_type" in insert_sql
        assert "approved" in insert_args
        assert "manual" in insert_args

    @pytest.mark.asyncio
    async def test_approve_invalid_uuid_returns_422(self, client):
        """Approve with malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/jobs/not-a-uuid/approve")

        assert response.status_code == 422


class TestRejectJob:
    """Tests for POST /jobs/{id}/reject."""

    @pytest.mark.asyncio
    async def test_reject_needs_approval_job_returns_200_and_transitions_to_rejected(
        self, mock_conn
    ):
        """Reject transitions needs_approval → rejected."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Reject me",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'rejected'" in sql:
                row["status"] = "rejected"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/reject")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"
        assert data["id"] == str(job_id)

    @pytest.mark.asyncio
    async def test_reject_unknown_job_returns_404(self, client, mock_conn):
        """Reject on non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/jobs/{uuid.uuid4()}/reject")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_reject_wrong_state_returns_409(self, client, mock_conn):
        """Reject on job not in needs_approval state returns 409."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Pending job",
            status="pending",
        )
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/reject")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_reject_writes_rejection_record(self, mock_conn):
        """Reject inserts a record into the approvals table with status='rejected'."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Reject me",
            status="needs_approval",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'rejected'" in sql:
                row["status"] = "rejected"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/reject")

        assert response.status_code == 200

        # Verify an INSERT into approvals happened with rejected status
        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO approvals" in sql
        ]
        assert len(insert_calls) == 1
        insert_sql, insert_args = insert_calls[0]
        assert "approval_type" in insert_sql
        assert "rejected" in insert_args
        assert "manual" in insert_args

    @pytest.mark.asyncio
    async def test_reject_invalid_uuid_returns_422(self, client):
        """Reject with malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/jobs/not-a-uuid/reject")

        assert response.status_code == 422


class TestAbortJob:
    """Tests for POST /jobs/{id}/abort."""

    @pytest.mark.asyncio
    async def test_abort_without_session_transitions_directly_to_aborted(
        self, mock_conn
    ):
        """Aborting a pending job without a session transitions directly to aborted."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Pending no session",
            status="pending", opencode_session_id=None,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"
        assert data["id"] == str(job_id)

    @pytest.mark.asyncio
    async def test_abort_with_session_succeeds_and_transitions_to_aborted(
        self, mock_conn
    ):
        """Aborting a running job with a session goes aborting→aborted on success."""
        from app.opencode.protocol import SessionAbortResponse

        job_id = uuid.uuid4()
        session_id = "sess-abc-123"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Running with session",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id,
                aborted=True,
                message="Session aborted successfully",
            )
        )

        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"
        assert data["id"] == str(job_id)

        # Verify OpenCode client was called with the correct session ID
        mock_opencode.abort_session.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_abort_with_session_opencode_unreachable_stays_aborting(
        self, mock_conn
    ):
        """When OpenCode Serve is unreachable, job stays aborting and returns 503."""
        job_id = uuid.uuid4()
        session_id = "sess-unreachable"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Running unreachable",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 503
        data = response.json()
        assert "detail" in data
        assert "unreachable" in data["detail"].lower()

        # Job should remain in aborting state
        assert row["status"] == "aborting"

        # Verify OpenCode client was called
        mock_opencode.abort_session.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_abort_unknown_job_returns_404(self, client, mock_conn):
        """Abort on non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/jobs/{uuid.uuid4()}/abort")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", [
        "completed",
        "failed",
        "rejected",
        "aborted",
        "needs_approval",
    ])
    async def test_abort_terminal_state_returns_409(
        self, terminal_status, mock_conn
    ):
        """Abort on a job in a terminal/non-abortable state returns 409."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", f"{terminal_status} job",
            status=terminal_status,
        )
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        # No opencode client needed — the endpoint should reject before calling it
        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 409
        data = response.json()
        assert "detail" in data
        assert terminal_status in data["detail"]

    @pytest.mark.asyncio
    async def test_abort_invalid_uuid_returns_422(self, client):
        """Abort with malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/jobs/not-a-uuid/abort")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_double_abort_first_succeeds_second_returns_409(self, mock_conn):
        """First abort succeeds, second abort on aborted job returns 409."""
        from app.opencode.protocol import SessionAbortResponse

        job_id = uuid.uuid4()
        session_id = "sess-double"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Double abort",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            return _mock_row(row)

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id,
                aborted=True,
                message="Aborted",
            )
        )

        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            # First abort - should succeed, transitioning to aborted
            response1 = await c.post(f"/jobs/{job_id}/abort")
            assert response1.status_code == 200
            data1 = response1.json()
            assert data1["status"] == "aborted"

            # Second abort - job is now aborted, should get 409
            response2 = await c.post(f"/jobs/{job_id}/abort")
            assert response2.status_code == 409
            data2 = response2.json()
            assert "detail" in data2
            assert "aborted" in data2["detail"]

    @pytest.mark.asyncio
    async def test_abort_retry_from_aborting_succeeds(self, mock_conn):
        """Retrying abort from aborting state (after first OpenCode failure) succeeds."""
        from app.opencode.protocol import SessionAbortResponse

        job_id = uuid.uuid4()
        session_id = "sess-retry"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Retry abort",
            status="aborting", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            return _mock_row(row)

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id,
                aborted=True,
                message="Aborted on retry",
            )
        )

        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"

        # Verify OpenCode client was called (retry)
        mock_opencode.abort_session.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_abort_running_job_without_opencode_client_skips_session_call(
        self, mock_conn
    ):
        """When no OpenCode client is available, the job is marked aborted directly."""
        job_id = uuid.uuid4()
        session_id = "sess-no-client"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "No client available",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # No opencode client injected — default get_opencode_client returns None
        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"
        assert data["id"] == str(job_id)

    @pytest.mark.asyncio
    async def test_abort_calls_executor_cleanup(self, mock_conn):
        """Aborting a running job with a workspace calls executor.stop_opencode
        and executor.cleanup_workspace with the correct workspace ID."""
        from app.executors.models import CleanupWorkspaceRequest, StopOpencodeRequest
        from app.opencode.protocol import SessionAbortResponse

        workspace_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        job_id = uuid.uuid4()
        session_id = "sess-exec-cleanup"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "With workspace",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id,
                aborted=True,
                message="Aborted",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock()
        mock_exec.cleanup_workspace = AsyncMock()

        client = _create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"

        mock_exec.stop_opencode.assert_called_once()
        stop_call_arg = mock_exec.stop_opencode.call_args[0][0]
        assert isinstance(stop_call_arg, StopOpencodeRequest)
        assert stop_call_arg.workspace_id == workspace_id

        mock_exec.cleanup_workspace.assert_called_once()
        cleanup_call_arg = mock_exec.cleanup_workspace.call_args[0][0]
        assert isinstance(cleanup_call_arg, CleanupWorkspaceRequest)
        assert cleanup_call_arg.workspace_id == workspace_id

    @pytest.mark.asyncio
    async def test_abort_executor_stop_failure_still_returns_200(self, mock_conn):
        """When executor.stop_opencode raises, abort still returns 200
        and the job is marked aborted."""
        from app.opencode.protocol import SessionAbortResponse

        workspace_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        job_id = uuid.uuid4()
        session_id = "sess-stop-fail"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Stop fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id, aborted=True, message="OK",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock(
            side_effect=RuntimeError("Stop failed")
        )
        mock_exec.cleanup_workspace = AsyncMock()

        client = _create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"

        mock_exec.cleanup_workspace.assert_called_once()

    @pytest.mark.asyncio
    async def test_abort_executor_cleanup_failure_still_returns_200(self, mock_conn):
        """When executor.cleanup_workspace raises, abort still returns 200."""
        from app.opencode.protocol import SessionAbortResponse

        workspace_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        job_id = uuid.uuid4()
        session_id = "sess-cleanup-fail"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Cleanup fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id, aborted=True, message="OK",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock()
        mock_exec.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("Cleanup failed")
        )

        client = _create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"

    @pytest.mark.asyncio
    async def test_abort_no_workspace_skips_executor_cleanup(self, mock_conn):
        """When the job has no workspace_name, executor cleanup is not called."""
        from app.opencode.protocol import SessionAbortResponse

        job_id = uuid.uuid4()
        session_id = "sess-no-workspace"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "No workspace",
            status="running", opencode_session_id=session_id,
            workspace_name=None,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id, aborted=True, message="OK",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock()
        mock_exec.cleanup_workspace = AsyncMock()

        client = _create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "aborted"

        mock_exec.stop_opencode.assert_not_called()
        mock_exec.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_idempotent_after_cleanup_failure(self, mock_conn):
        """First abort succeeds (executor cleanup fails silently), second abort
        returns 409 because the job is already aborted."""
        from app.opencode.protocol import SessionAbortResponse

        workspace_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
        job_id = uuid.uuid4()
        session_id = "sess-idempotent"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Idempotent cleanup fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            return _mock_row(row)

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id=session_id, aborted=True, message="OK",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock(
            side_effect=RuntimeError("Infrastructure unavailable")
        )
        mock_exec.cleanup_workspace = AsyncMock()

        client = _create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response1 = await c.post(f"/jobs/{job_id}/abort")
            assert response1.status_code == 200
            assert response1.json()["status"] == "aborted"

            response2 = await c.post(f"/jobs/{job_id}/abort")
            assert response2.status_code == 409
            data2 = response2.json()
            assert "detail" in data2
            assert "aborted" in data2["detail"]

            assert mock_exec.stop_opencode.call_count == 1


class TestJobEvents:
    """Tests for GET /jobs/{id}/events."""

    @pytest.mark.asyncio
    async def test_events_returns_list_for_approved_job(self, mock_conn):
        """Events returns a list of events for a job that has been approved."""
        job_id = uuid.uuid4()
        job_row = _make_job_row(
            job_id, "https://github.com/org/repo", "Events test",
            status="approved",
        )
        now = datetime.now(timezone.utc)
        approval_records = [
            _mock_row({
                "status": "approved",
                "created_at": now,
                "approved_by": "api",
                "requested_by": "system",
                "requested_action": "run_job",
            })
        ]

        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(job_row))

        async def _fetch_events(sql, *args):
            if "approvals" in sql:
                return approval_records
            elif "job_events" in sql:
                return []
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch_events)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/events")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["event_type"] == "approved"
        assert data[0]["actor"] == "api"
        assert data[0]["details"] == "run_job"
        assert data[0]["previous_status"] is None
        assert "timestamp" in data[0]

    @pytest.mark.asyncio
    async def test_events_returns_empty_list_for_job_with_no_events(self, mock_conn):
        """Events returns empty list for a job with no approval events."""
        job_id = uuid.uuid4()
        job_row = _make_job_row(
            job_id, "https://github.com/org/repo", "No events",
            status="needs_approval",
        )

        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(job_row))
        mock_conn.fetch = AsyncMock(return_value=[])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/events")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_events_returns_404_for_unknown_job(self, client, mock_conn):
        """Events returns 404 for a non-existent job."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}/events")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_events_contains_event_type_timestamp_actor_details(self, mock_conn):
        """Events response includes event_type, timestamp, actor, details for rejected jobs."""
        job_id = uuid.uuid4()
        job_row = _make_job_row(
            job_id, "https://github.com/org/repo", "Check fields",
            status="rejected",
        )
        now = datetime.now(timezone.utc)
        approval_records = [
            _mock_row({
                "status": "rejected",
                "created_at": now,
                "approved_by": "api",
                "requested_by": "system",
                "requested_action": "run_job",
            })
        ]

        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(job_row))

        async def _fetch_events(sql, *args):
            if "approvals" in sql:
                return approval_records
            elif "job_events" in sql:
                return []
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch_events)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/events")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        event = data[0]
        assert "event_type" in event
        assert "timestamp" in event
        assert "actor" in event
        assert "details" in event
        assert event["event_type"] == "rejected"
        assert event["actor"] == "api"
        assert event["details"] == "run_job"
        assert "previous_status" in event
        assert event["previous_status"] is None


class TestJobDiff:
    """Tests for GET /jobs/{id}/diff."""

    @pytest.mark.asyncio
    async def test_get_diff_for_completed_job_returns_200_with_diff(self, mock_conn):
        """GET /jobs/{id}/diff for a completed job with a diff returns 200 and the diff."""
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Add feature",
            "status": "completed",
            "executor_type": "local",
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
            "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new",
        }
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row_data))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["diff"] == row_data["diff"]

    @pytest.mark.asyncio
    async def test_get_diff_for_unknown_job_returns_404(self, client, mock_conn):
        """GET /jobs/{id}/diff for a non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}/diff")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_diff_for_running_job_returns_409(self, mock_conn):
        """GET /jobs/{id}/diff for a running job returns 409 with status info."""
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Running job",
            "status": "running",
            "executor_type": "local",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "diff": None,
        }
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row_data))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 409
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["diff"] is None
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_diff_for_completed_job_without_diff_returns_404(self, mock_conn):
        """GET /jobs/{id}/diff for a completed job with no diff returns 404."""
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "No diff job",
            "status": "completed",
            "executor_type": "local",
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
            "diff": None,
        }
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row_data))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_diff_for_invalid_uuid_returns_422(self, client):
        """GET /jobs/{id}/diff with a malformed UUID should return 422."""
        async with client as c:
            response = await c.get("/jobs/not-a-uuid/diff")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_diff_for_pending_job_returns_404(self, mock_conn):
        """GET /jobs/{id}/diff for a pending job (no diff) returns 404."""
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Pending job",
            "status": "pending",
            "executor_type": "local",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "diff": None,
        }
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row_data))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 404


class TestDoubleApprove:
    """Tests for concurrent double-approve scenario."""

    @pytest.mark.asyncio
    async def test_double_approve_first_succeeds_second_returns_409(self, mock_conn):
        """First approve succeeds, second concurrent approve returns 409."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Double approve",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            return _mock_row(row)

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            # First approve - should succeed, transitioning to running
            response1 = await c.post(f"/jobs/{job_id}/approve")
            assert response1.status_code == 200
            data1 = response1.json()
            assert data1["status"] == "running"

            # Second approve - job is now running, should get 409
            response2 = await c.post(f"/jobs/{job_id}/approve")
            assert response2.status_code == 409
            data2 = response2.json()
            assert "detail" in data2


class TestJobDiffFetch:
    """Tests for diff fetching on job completion (issue #45)."""

    @pytest.mark.asyncio
    async def test_completed_job_fetches_and_persists_diff(self, mock_conn, mock_executor):
        """Diff should be fetched from OpenCode Serve and persisted to the DB."""
        from app.opencode.protocol import SessionDiffResponse

        job_id = uuid.uuid4()
        expected_diff = "diff --git a/file.txt b/file.txt\n+added line\n"

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        execute_calls: list[tuple] = []

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET diff" in sql:
                # Capture the diff value stored
                row_data["diff"] = args[1] if len(args) > 1 else None
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Create a mock OpenCode client that returns a diff
        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id="mock-session",
                diff=expected_diff,
                files_changed=["file.txt"],
            )
        )

        client = _create_client(
            mock_conn,
            mock_executor=mock_executor,
            mock_opencode_client=mock_opencode,
        )

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
        assert data["status"] == "completed"
        assert data["diff"] == expected_diff

        # Verify get_session_diff was called with the session ID
        mock_opencode.get_session_diff.assert_called_once()
        call_args = mock_opencode.get_session_diff.call_args[0]
        assert len(call_args) == 1
        assert call_args[0] is not None  # session_id should be a string

        # Verify a DB UPDATE for the diff column was executed
        diff_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE gateway_jobs SET diff" in sql
        ]
        assert len(diff_updates) == 1
        _, update_args = diff_updates[0]
        assert expected_diff in update_args

    @pytest.mark.asyncio
    async def test_diff_fetch_failure_does_not_fail_job(self, mock_conn, mock_executor):
        """When diff fetch raises, the job should still complete (not fail)."""
        job_id = uuid.uuid4()

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Create a mock OpenCode client that raises on get_session_diff
        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            side_effect=RuntimeError("Serve unreachable")
        )

        client = _create_client(
            mock_conn,
            mock_executor=mock_executor,
            mock_opencode_client=mock_opencode,
        )

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
        # Job MUST complete even though diff fetch failed
        assert data["status"] == "completed"
        assert data["diff"] is None

        # Verify diff fetch was attempted
        mock_opencode.get_session_diff.assert_called_once()

    @pytest.mark.asyncio
    async def test_completed_job_response_includes_diff(self, mock_conn, mock_executor):
        """A completed job response should include the diff field."""
        from app.opencode.protocol import SessionDiffResponse

        job_id = uuid.uuid4()
        expected_diff = "diff content here"

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET diff" in sql:
                row_data["diff"] = args[1] if len(args) > 1 else None
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id="mock-session",
                diff=expected_diff,
                files_changed=["README.md"],
            )
        )

        client = _create_client(
            mock_conn,
            mock_executor=mock_executor,
            mock_opencode_client=mock_opencode,
        )

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
        assert "diff" in data
        assert data["diff"] == expected_diff
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_diff_fetch_not_attempted_when_client_is_none(self, mock_conn, mock_executor):
        """When no OpenCode client is injected (default None), job completes with null diff."""
        job_id = uuid.uuid4()

        row_data = _make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return _mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # NO opencode client injected (default None)
        client = _create_client(
            mock_conn,
            mock_executor=mock_executor,
        )

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
        assert data["status"] == "completed"
        # diff should be null when no client is available
        assert data["diff"] is None

    @pytest.mark.asyncio
    async def test_get_job_returns_diff_for_completed_job(self, mock_conn):
        """GET /jobs/{id} should return the diff for a completed job that has one."""
        job_id = uuid.uuid4()
        expected_diff = "persisted diff content"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Add feature",
            status="completed", completed_at=datetime.now(timezone.utc),
            opencode_session_id="sess-123", diff=expected_diff,
        )
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["diff"] == expected_diff
        assert data["opencode_session_id"] == "sess-123"


class TestAbortEvents:
    """Tests for abort event recording and retrieval via GET /jobs/{id}/events."""

    @pytest.mark.asyncio
    async def test_abort_records_event_in_job_events_table(self, mock_conn):
        """Aborting a job inserts a record into job_events with correct fields."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Abort event test",
            status="pending", opencode_session_id=None,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200

        # Verify an INSERT into job_events happened
        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO job_events" in sql
        ]
        assert len(insert_calls) == 1
        insert_sql, insert_args = insert_calls[0]
        assert "job_events" in insert_sql
        assert "aborted" in insert_args
        assert "api" in insert_args
        assert "Job aborted" in insert_args
        # previous_status should be "pending"
        assert "pending" in insert_args

    @pytest.mark.asyncio
    async def test_abort_event_includes_previous_status(self, mock_conn):
        """Abort event records the status the job had before the abort."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Previous status check",
            status="running", opencode_session_id="sess-123",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        from app.opencode.protocol import SessionAbortResponse
        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id="sess-123", aborted=True, message="OK",
            )
        )
        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200

        # Verify previous_status is "running"
        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO job_events" in sql
        ]
        assert len(insert_calls) == 1
        _, insert_args = insert_calls[0]
        # Find the position of previous_status value (it's the 6th positional arg)
        # Args order: id, job_id, event_type, actor, details, previous_status, created_at
        assert insert_args[5] == "running"

    @pytest.mark.asyncio
    async def test_events_endpoint_returns_abort_events(self, mock_conn):
        """GET /jobs/{id}/events returns abort events from job_events table."""
        job_id = uuid.uuid4()
        job_row = _make_job_row(
            job_id, "https://github.com/org/repo", "Events with abort",
            status="aborted",
        )
        now = datetime.now(timezone.utc)

        # Mock: job exists, no approval records, one abort event
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(job_row))

        abort_event = _mock_row({
            "event_type": "aborted",
            "actor": "api",
            "details": "Job aborted",
            "created_at": now,
            "previous_status": "running",
        })

        async def _fetch(sql, *args):
            if "approvals" in sql:
                return []  # no approval events
            elif "job_events" in sql:
                return [abort_event]
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/events")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        event = data[0]
        assert event["event_type"] == "aborted"
        assert event["actor"] == "api"
        assert event["details"] == "Job aborted"
        assert event["previous_status"] == "running"
        assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_events_returns_abort_and_approval_events_together(self, mock_conn):
        """GET /jobs/{id}/events returns both abort and approval events sorted by time."""
        job_id = uuid.uuid4()
        job_row = _make_job_row(
            job_id, "https://github.com/org/repo", "Mixed events",
            status="aborted",
        )
        t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(job_row))

        approval_rec = _mock_row({
            "status": "approved",
            "approved_by": "api",
            "requested_by": "system",
            "requested_action": "run_job",
            "created_at": t1,
        })

        abort_rec1 = _mock_row({
            "event_type": "aborted",
            "actor": "api",
            "details": "Job aborted",
            "created_at": t2,
            "previous_status": "running",
        })

        abort_rec2 = _mock_row({
            "event_type": "aborted",
            "actor": "system",
            "details": "Retry abort",
            "created_at": t3,
            "previous_status": "aborting",
        })

        async def _fetch(sql, *args):
            if "approvals" in sql:
                return [approval_rec]
            elif "job_events" in sql:
                return [abort_rec1, abort_rec2]
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/events")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 3

        # Should be sorted by timestamp ascending
        assert data[0]["event_type"] == "approved"
        assert data[0]["previous_status"] is None

        assert data[1]["event_type"] == "aborted"
        assert data[1]["previous_status"] == "running"
        assert data[1]["actor"] == "api"

        assert data[2]["event_type"] == "aborted"
        assert data[2]["previous_status"] == "aborting"
        assert data[2]["actor"] == "system"

    @pytest.mark.asyncio
    async def test_events_for_unreachable_abort_does_not_record_event(self, mock_conn):
        """When OpenCode is unreachable during abort, no event is recorded.

        Job stays in aborting state, 503 is returned, no event persisted.
        """
        job_id = uuid.uuid4()
        session_id = "sess-unreachable-events"
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "Unreachable no event",
            status="running", opencode_session_id=session_id,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        client = _create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 503
        assert row["status"] == "aborting"

        # No INSERT into job_events should have happened
        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO job_events" in sql
        ]
        assert len(insert_calls) == 0

    @pytest.mark.asyncio
    async def test_abort_event_recorded_for_no_session_job(self, mock_conn):
        """Abort events are recorded even when there's no session (pending → aborted)."""
        job_id = uuid.uuid4()
        row = _make_job_row(
            job_id, "https://github.com/org/repo", "No session abort event",
            status="pending", opencode_session_id=None,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return _mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        assert row["status"] == "aborted"

        insert_calls = [
            (sql, args) for sql, args in execute_args_list
            if "INSERT INTO job_events" in sql
        ]
        assert len(insert_calls) == 1
        _, insert_args = insert_calls[0]
        assert insert_args[2] == "aborted"      # event_type
        assert insert_args[3] == "api"           # actor
        assert insert_args[4] == "Job aborted"   # details
        assert insert_args[5] == "pending"       # previous_status
