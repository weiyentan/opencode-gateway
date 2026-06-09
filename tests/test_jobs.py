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
    row.get = data.get
    return row


def _make_job_row(job_id, repo_url, task_summary, status="pending", *, completed_at=None,
                  opencode_session_id=None, diff=None):
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

    if mock_executor is not None:
        app.dependency_overrides[get_executor] = lambda: mock_executor

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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
        mock_conn.fetch = AsyncMock(return_value=approval_records)

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
        mock_conn.fetch = AsyncMock(return_value=approval_records)

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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
                return _mock_row(row_data)
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
