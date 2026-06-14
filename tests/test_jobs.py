"""Tests for the Job API endpoints."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import create_client, make_job_row, mock_row

# mock_conn, mock_executor, and client fixtures are auto-discovered from conftest.py


@pytest.fixture(autouse=True)
def _patch_select_runner_for_existing_tests(request):
    """Auto-patch select_runner so existing tests don't break.

    The runner selection logic (issue #92) is tested explicitly in
    ``TestRunnerSelection`` — those tests bypass this patch.  Every
    other test class gets a dummy runner UUID so the job creation
    flow proceeds without needing to mock the ``runners`` table.
    """
    # Skip patching when the test class is TestRunnerSelection itself
    if request.cls and request.cls.__name__ == "TestRunnerSelection":
        yield
        return

    _dummy = uuid.UUID("00000000-0000-0000-0000-000000000099")

    async def _fake_select(conn, runner_id=None, labels=None):
        return _dummy

    with patch("app.api.jobs.select_runner", new=_fake_select):
        yield


class TestCreateJob:
    """Tests for POST /jobs."""

    @pytest.mark.asyncio
    async def test_post_valid_job_returns_201(self, client, mock_conn):
        """POST /jobs with valid input returns 201 with job data and status=pending."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug"
        )
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                    "env_vars": {},
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

    @pytest.mark.asyncio
    async def test_create_job_with_env_vars(self, client, mock_conn):
        """POST /jobs with env_vars should store and pass them through."""
        import json

        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Task with env vars",
            env_vars={"MY_VAR": "my_value", "LOG_LEVEL": "debug"},
        )
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Task with env vars",
                    "env_vars": {"MY_VAR": "my_value", "LOG_LEVEL": "debug"},
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["repo_url"] == "https://github.com/org/repo"
        assert data["task_summary"] == "Task with env vars"

        # Verify env_vars was passed in the INSERT statement (7th positional arg)
        insert_call = mock_conn.execute.call_args_list[0]
        assert "INSERT INTO gateway_jobs" in str(insert_call)
        assert insert_call.args[6] == json.dumps({"MY_VAR": "my_value", "LOG_LEVEL": "debug"})


class TestGetJob:
    """Tests for GET /jobs/{id}."""

    @pytest.mark.asyncio
    async def test_get_existing_job_returns_200(self, client, mock_conn):
        """GET /jobs/{id} for an existing job returns 200 with full record."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Add feature"
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

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
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(mock_conn, mock_executor=mock_executor)

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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(mock_conn, mock_executor=mock_executor)

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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(mock_conn, mock_executor=failing_executor)

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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(mock_conn, mock_executor=mock_executor)

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
        """POST /jobs returns 503 when ObservationBasedPolicy.check raises PolicyViolation.

        The policy check runs before workspace creation, so no
        executor.create_workspace() or cleanup_workspace() call is made.
        """
        from unittest.mock import patch

        from app.policy import ObservationBasedPolicy, PolicyViolation

        # Mock the runner resolution query: return the text runner_id
        # for the dummy UUID returned by the patched select_runner.
        async def _fetchrow(sql, *args):
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner-99"})
            return None

        async def _execute(sql, *args):
            pass

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn, mock_executor=mock_executor)

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

        # Policy check runs before workspace creation — no executor calls
        mock_executor.create_workspace.assert_not_called()
        mock_executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_job_returns_503_when_runner_is_offline(
        self, mock_conn, mock_executor
    ):
        """POST /jobs returns 503 when the runner status is 'offline'."""
        runner_uuid = uuid.uuid4()
        runner_text_id = "test-runner-offline"

        runner_by_id_row = mock_row({"runner_id": runner_text_id})
        runner_by_text_row = mock_row(
            {"id": runner_uuid, "status": "offline"}
        )

        async def _execute(sql, *args):
            pass

        async def _fetchrow(sql, *args):
            if "FROM runners WHERE id" in sql:
                return runner_by_id_row
            if "FROM runners WHERE runner_id" in sql:
                return runner_by_text_row
            return None

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn, mock_executor=mock_executor)

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
        assert data["detail"]["resource"] == "manual_status"
        assert data["detail"]["runner_id"] == runner_text_id
        assert "offline" in data["detail"]["message"]

        # Policy check happens before workspace creation — no workspace was
        # created, so no cleanup should occur.
        mock_executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_job_returns_503_when_runner_is_maintenance(
        self, mock_conn, mock_executor
    ):
        """POST /jobs returns 503 when the runner status is 'maintenance'."""
        runner_uuid = uuid.uuid4()
        runner_text_id = "test-runner-maint"

        runner_by_id_row = mock_row({"runner_id": runner_text_id})
        runner_by_text_row = mock_row(
            {"id": runner_uuid, "status": "maintenance"}
        )

        async def _execute(sql, *args):
            pass

        async def _fetchrow(sql, *args):
            if "FROM runners WHERE id" in sql:
                return runner_by_id_row
            if "FROM runners WHERE runner_id" in sql:
                return runner_by_text_row
            return None

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn, mock_executor=mock_executor)

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
        assert data["detail"]["resource"] == "manual_status"
        assert data["detail"]["runner_id"] == runner_text_id
        assert "maintenance" in data["detail"]["message"]

        # Policy check happens before workspace creation — no workspace was
        # created, so no cleanup should occur.
        mock_executor.cleanup_workspace.assert_not_called()


class TestApproveJob:
    """Tests for POST /jobs/{id}/approve."""

    @pytest.mark.asyncio
    async def test_approve_needs_approval_job_returns_200_and_transitions_to_running(
        self, mock_conn
    ):
        """Approve transitions needs_approval → running."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Approve me",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Already running",
            status="running",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/approve")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_approve_writes_approval_record(self, mock_conn):
        """Approve inserts a record into the approvals table with status='approved'."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Approve me",
            status="needs_approval",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Reject me",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'rejected'" in sql:
                row["status"] = "rejected"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Pending job",
            status="pending",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/reject")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_reject_writes_rejection_record(self, mock_conn):
        """Reject inserts a record into the approvals table with status='rejected'."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Reject me",
            status="needs_approval",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'rejected'" in sql:
                row["status"] = "rejected"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Pending no session",
            status="pending", opencode_session_id=None,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Running with session",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Running unreachable",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", f"{terminal_status} job",
            status=terminal_status,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        # No opencode client needed — the endpoint should reject before calling it
        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Double abort",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            return mock_row(row)

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

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Retry abort",
            status="aborting", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            return mock_row(row)

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

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "No client available",
            status="running", opencode_session_id=session_id,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                row["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # No opencode client injected — default get_opencode_client returns None
        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "With workspace",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(
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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Stop fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(
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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Cleanup fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(
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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "No workspace",
            status="running", opencode_session_id=session_id,
            workspace_name=None,
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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

        client = create_client(
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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Idempotent cleanup fail",
            status="running", opencode_session_id=session_id,
            workspace_name=str(workspace_id),
        )

        async def _fetchrow(sql, *args):
            return mock_row(row)

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

        client = create_client(
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
        job_row = make_job_row(
            job_id, "https://github.com/org/repo", "Events test",
            status="approved",
        )
        now = datetime.now(timezone.utc)
        approval_records = [
            mock_row({
                "status": "approved",
                "created_at": now,
                "approved_by": "api",
                "requested_by": "system",
                "requested_action": "run_job",
            })
        ]

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(job_row))

        async def _fetch_events(sql, *args):
            if "approvals" in sql:
                return approval_records
            elif "job_events" in sql:
                return []
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch_events)

        client = create_client(mock_conn)

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
        job_row = make_job_row(
            job_id, "https://github.com/org/repo", "No events",
            status="needs_approval",
        )

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(job_row))
        mock_conn.fetch = AsyncMock(return_value=[])

        client = create_client(mock_conn)

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
        job_row = make_job_row(
            job_id, "https://github.com/org/repo", "Check fields",
            status="rejected",
        )
        now = datetime.now(timezone.utc)
        approval_records = [
            mock_row({
                "status": "rejected",
                "created_at": now,
                "approved_by": "api",
                "requested_by": "system",
                "requested_action": "run_job",
            })
        ]

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(job_row))

        async def _fetch_events(sql, *args):
            if "approvals" in sql:
                return approval_records
            elif "job_events" in sql:
                return []
            return []

        mock_conn.fetch = AsyncMock(side_effect=_fetch_events)

        client = create_client(mock_conn)

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
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))

        client = create_client(mock_conn)

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
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))

        client = create_client(mock_conn)

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
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))

        client = create_client(mock_conn)

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
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 404


class TestDoubleApprove:
    """Tests for concurrent double-approve scenario."""

    @pytest.mark.asyncio
    async def test_double_approve_first_succeeds_second_returns_409(self, mock_conn):
        """First approve succeeds, second concurrent approve returns 409."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Double approve",
            status="needs_approval",
        )

        async def _fetchrow(sql, *args):
            return mock_row(row)

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(
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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(
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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Add feature", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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

        client = create_client(
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

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix bug", status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
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
        client = create_client(
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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Add feature",
            status="completed", completed_at=datetime.now(timezone.utc),
            opencode_session_id="sess-123", diff=expected_diff,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Abort event test",
            status="pending", opencode_session_id=None,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Previous status check",
            status="running", opencode_session_id="sess-123",
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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
        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        job_row = make_job_row(
            job_id, "https://github.com/org/repo", "Events with abort",
            status="aborted",
        )
        now = datetime.now(timezone.utc)

        # Mock: job exists, no approval records, one abort event
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(job_row))

        abort_event = mock_row({
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

        client = create_client(mock_conn)

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
        job_row = make_job_row(
            job_id, "https://github.com/org/repo", "Mixed events",
            status="aborted",
        )
        t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(job_row))

        approval_rec = mock_row({
            "status": "approved",
            "approved_by": "api",
            "requested_by": "system",
            "requested_action": "run_job",
            "created_at": t1,
        })

        abort_rec1 = mock_row({
            "event_type": "aborted",
            "actor": "api",
            "details": "Job aborted",
            "created_at": t2,
            "previous_status": "running",
        })

        abort_rec2 = mock_row({
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

        client = create_client(mock_conn)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Unreachable no event",
            status="running", opencode_session_id=session_id,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
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
        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

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
        row = make_job_row(
            job_id, "https://github.com/org/repo", "No session abort event",
            status="pending", opencode_session_id=None,
        )

        execute_args_list: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_args_list.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                row["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

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


# ══════════════════════════════════════════════════════════════════════════
#  Runner Selection & Job Pinning (issue #92)
# ══════════════════════════════════════════════════════════════════════════


class TestRunnerSelection:
    """Tests for runner selection and job pinning in POST /jobs."""

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_healthy_runner_succeeds(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a valid, healthy runner_id should dispatch to that runner."""
        job_id = uuid.uuid4()
        runner_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Pin to runner",
            status="pending",
        )

        # Track which SQL is being queried so we can return the right mock
        async def _fetchrow(sql, *args):
            if "SELECT id, status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "status": "HEALTHY"})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_not_found_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a non-existent runner_id should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, status FROM runners WHERE id" in sql:
                return None  # runner not found
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to non-existent runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert "not found" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_unhealthy_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a runner that is not HEALTHY should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "status": "BLOCKED_DISK_PRESSURE"})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to unhealthy runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert "not healthy" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_label_based_selection_matching_runner_found(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with labels should auto-select a matching healthy runner."""
        job_id = uuid.uuid4()
        runner_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Label match",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "r.labels IS NOT NULL AND r.labels ?&" in sql:
                # Label-based query returns a runner
                return mock_row({"id": runner_id, "active_workspaces": 2})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Label match",
                    "labels": ["gpu", "high-memory"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_label_based_selection_no_matching_runner_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with labels that match no healthy runner should return 400."""
        async def _fetchrow(sql, *args):
            if "r.labels IS NOT NULL AND r.labels ?&" in sql:
                return None  # no matching runner
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No matching label",
                    "labels": ["nonexistent-label"],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert "no healthy runner" in data["detail"].lower()
        assert "nonexistent-label" in data["detail"]

    @pytest.mark.asyncio
    async def test_automatic_selection_healthy_runner_available(
        self, mock_conn, mock_executor
    ):
        """POST /jobs without runner_id or labels should auto-select a healthy runner."""
        job_id = uuid.uuid4()
        runner_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Auto select",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            # The automatic selection query doesn't have label conditions
            if (
                "LEFT JOIN workspaces w ON w.runner_id = r.id" in sql
                and "r.labels" not in sql
            ):
                return mock_row({"id": runner_id, "active_workspaces": 1})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Auto select",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_automatic_selection_no_healthy_runners_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs without constraints when no healthy runners exist → 400."""
        async def _fetchrow(sql, *args):
            if (
                "LEFT JOIN workspaces w ON w.runner_id = r.id" in sql
                and "r.labels" not in sql
            ):
                return None  # no healthy runner
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No healthy runners",
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert "no healthy runners" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_load_balancing_picks_runner_with_fewest_workspaces(
        self, mock_conn, mock_executor
    ):
        """Runner selection should prefer the runner with the fewest active workspaces."""
        job_id = uuid.uuid4()
        runner_with_fewer = uuid.UUID("11111111-1111-1111-1111-111111111111")

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Load balance",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if (
                "LEFT JOIN workspaces w ON w.runner_id = r.id" in sql
                and "r.labels" not in sql
            ):
                # Return the runner with fewest workspaces
                return mock_row({"id": runner_with_fewer, "active_workspaces": 0})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Load balance",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_runner_id_takes_precedence_over_labels(
        self, mock_conn, mock_executor
    ):
        """When both runner_id and labels are provided, runner_id takes precedence."""
        job_id = uuid.uuid4()
        runner_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Pin over labels",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            # Only the explicit runner lookup should be called (not the label query)
            if "SELECT id, status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "status": "HEALTHY"})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin over labels",
                    "runner_id": str(runner_id),
                    "labels": ["gpu"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "completed"
class TestJobLogs:
    """Tests for GET /jobs/{id}/logs (log retrieval via OpenCode Serve proxy)."""

    @pytest.mark.asyncio
    async def test_get_logs_returns_full_log_output(self, mock_conn):
        """GET /jobs/{id}/logs returns 200 with full log content from the session."""
        from app.opencode.protocol import SessionInfo, SessionLogResponse

        job_id = uuid.uuid4()
        session_id = "sess-logs-1"
        log_content = (
            "INFO: Starting session...\n"
            "INFO: Cloning repository...\n"
            "INFO: Running analysis...\n"
            "INFO: Session complete.\n"
        )
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Run logging task",
            status="completed",
            opencode_session_id=session_id,
            completed_at=now,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="completed",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )
        mock_opencode.get_session_log = AsyncMock(
            return_value=SessionLogResponse(
                session_id=session_id,
                log=log_content,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["session_id"] == session_id
        assert data["log"] == log_content

        # Verify the OpenCode client was called correctly
        mock_opencode.get_session.assert_called_once_with(session_id)
        mock_opencode.get_session_log.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_get_logs_for_unknown_job_returns_404(self, client, mock_conn):
        """GET /jobs/{id}/logs for a non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}/logs")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_logs_no_session_returns_409(self, mock_conn):
        """GET /jobs/{id}/logs for a job with no session returns 409."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "No session yet",
            status="pending",
            opencode_session_id=None,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 409
        assert "detail" in response.json()

    @pytest.mark.asyncio
    async def test_get_logs_session_not_started_returns_424(self, mock_conn):
        """GET /jobs/{id}/logs when session status is 'pending' returns 424."""
        from app.opencode.protocol import SessionInfo

        job_id = uuid.uuid4()
        session_id = "sess-pending"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Pending session",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="pending",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 424
        assert "detail" in response.json()
        assert "pending" in response.json()["detail"]

        # get_session was called but get_session_log was not
        mock_opencode.get_session.assert_called_once_with(session_id)
        assert not mock_opencode.get_session_log.called

    @pytest.mark.asyncio
    async def test_get_logs_session_created_returns_424(self, mock_conn):
        """GET /jobs/{id}/logs when session status is 'created' returns 424."""
        from app.opencode.protocol import SessionInfo

        job_id = uuid.uuid4()
        session_id = "sess-created"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Created session",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="created",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 424

    @pytest.mark.asyncio
    async def test_get_logs_session_queued_returns_424(self, mock_conn):
        """GET /jobs/{id}/logs when session status is 'queued' returns 424."""
        from app.opencode.protocol import SessionInfo

        job_id = uuid.uuid4()
        session_id = "sess-queued"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Queued session",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="queued",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 424

    @pytest.mark.asyncio
    async def test_get_logs_no_opencode_client_returns_503(self, mock_conn):
        """GET /jobs/{id}/logs without an OpenCode client returns 503."""
        job_id = uuid.uuid4()
        session_id = "sess-no-client"
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "No client",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        # No opencode client injected
        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 503
        assert "detail" in response.json()

    @pytest.mark.asyncio
    async def test_get_logs_opencode_unreachable_returns_503(self, mock_conn):
        """GET /jobs/{id}/logs when OpenCode Serve is unreachable returns 503."""
        job_id = uuid.uuid4()
        session_id = "sess-down"
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Serve down",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_get_logs_invalid_uuid_returns_422(self, client):
        """GET /jobs/{id}/logs with a malformed UUID returns 422."""
        async with client as c:
            response = await c.get("/jobs/not-a-uuid/logs")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_logs_for_running_session_returns_logs(self, mock_conn):
        """GET /jobs/{id}/logs for a running session returns 200 with logs."""
        from app.opencode.protocol import SessionInfo, SessionLogResponse

        job_id = uuid.uuid4()
        session_id = "sess-running"
        log_content = "Running analysis step 1/10...\n"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Running task",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="running",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )
        mock_opencode.get_session_log = AsyncMock(
            return_value=SessionLogResponse(
                session_id=session_id,
                log=log_content,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 200
        data = response.json()
        assert data["log"] == log_content

    @pytest.mark.asyncio
    async def test_get_logs_for_failed_session_returns_logs(self, mock_conn):
        """GET /jobs/{id}/logs for a failed session returns 200 with logs."""
        from app.opencode.protocol import SessionInfo, SessionLogResponse

        job_id = uuid.uuid4()
        session_id = "sess-failed"
        log_content = "ERROR: Task failed at step 3\n"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Failed task",
            status="failed",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="failed",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )
        mock_opencode.get_session_log = AsyncMock(
            return_value=SessionLogResponse(
                session_id=session_id,
                log=log_content,
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 200
        data = response.json()
        assert data["log"] == log_content

    @pytest.mark.asyncio
    async def test_get_logs_log_fetch_failure_returns_503(self, mock_conn):
        """GET /jobs/{id}/logs when get_session_log raises returns 503."""
        from app.opencode.protocol import SessionInfo

        job_id = uuid.uuid4()
        session_id = "sess-log-fail"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Log fetch failure",
            status="running",
            opencode_session_id=session_id,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="running",
                workspace_path="/tmp/opencode/ws",
                created_at=now,
            )
        )
        mock_opencode.get_session_log = AsyncMock(
            side_effect=RuntimeError("Log service unavailable")
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/logs")

        assert response.status_code == 503


# ══════════════════════════════════════════════════════════════════════════
#  Job Listing (GET /jobs) & Enriched Responses (issue #91)
# ══════════════════════════════════════════════════════════════════════════


class TestListJobs:
    """Tests for GET /jobs listing endpoint."""

    @pytest.mark.asyncio
    async def test_list_jobs_returns_paginated_results(self, mock_conn):
        """GET /jobs returns a paginated list of job summaries ordered by created_at DESC."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Job 1",
            status="completed",
            branch_name="feature-branch",
            mr_url="https://github.com/org/repo/pull/1",
            workflow_run_id="wr-123",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs")

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data
        assert data["total"] == 1
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == str(job_id)
        assert data["items"][0]["status"] == "completed"

        # Verify SQL order is correct
        call_sql = mock_conn.fetch.call_args[0][0]
        assert "ORDER BY created_at DESC" in call_sql

    @pytest.mark.asyncio
    async def test_list_jobs_with_limit_and_offset(self, mock_conn):
        """GET /jobs respects limit and offset query parameters."""
        job_id_1 = uuid.uuid4()
        job_id_2 = uuid.uuid4()
        row1 = make_job_row(job_id_1, "https://github.com/org/repo", "Job 1")
        row2 = make_job_row(job_id_2, "https://github.com/org/repo", "Job 2")

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 10}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row1), mock_row(row2)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs?limit=2&offset=5")

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 2
        assert data["offset"] == 5
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_jobs_filters_by_status(self, mock_conn):
        """GET /jobs with status filter returns only matching jobs."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Failed job",
            status="failed",
        )

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs?status=failed")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["status"] == "failed"

        # Verify the SQL query included the status filter
        call_sql = mock_conn.fetch.call_args[0][0]
        assert "WHERE" in call_sql
        assert "status" in call_sql

    @pytest.mark.asyncio
    async def test_list_jobs_filters_by_workflow_run_id(self, mock_conn):
        """GET /jobs with workflow_run_id filter returns only matching jobs."""
        workflow_id = "wr-789"
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Workflow job",
            status="completed", workflow_run_id=workflow_id,
        )

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs?workflow_run_id={workflow_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["workflow_run_id"] == workflow_id

        call_sql = mock_conn.fetch.call_args[0][0]
        assert "WHERE" in call_sql
        assert "workflow_run_id" in call_sql
        assert mock_conn.fetch.call_args[0][1:] == (workflow_id, 50, 0)

    @pytest.mark.asyncio
    async def test_list_jobs_filters_by_runner_id(self, mock_conn):
        """GET /jobs with runner_id filter returns only matching jobs."""
        runner_id = uuid.uuid4()
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Runner job",
            status="completed",
        )

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs?runner_id={runner_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

        # Verify the SQL query included the runner_id filter
        call_sql = mock_conn.fetch.call_args[0][0]
        assert "WHERE" in call_sql
        assert "runner_id" in call_sql
        assert mock_conn.fetch.call_args[0][1:] == (runner_id, 50, 0)

    @pytest.mark.asyncio
    async def test_list_jobs_filters_by_combined_fields(self, mock_conn):
        """GET /jobs supports combining status, runner_id, and workflow_run_id filters."""
        runner_id = uuid.uuid4()
        workflow_id = "wr-789"
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Combined filter job",
            status="completed", workflow_run_id=workflow_id,
        )
        row["runner_id"] = runner_id

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(
                f"/jobs?status=completed&runner_id={runner_id}&workflow_run_id={workflow_id}"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["workflow_run_id"] == workflow_id

        count_sql = mock_conn.fetchrow.call_args[0][0]
        assert "status = $1" in count_sql
        assert "runner_id = $2" in count_sql
        assert "workflow_run_id = $3" in count_sql
        assert mock_conn.fetchrow.call_args[0][1:] == ("completed", runner_id, workflow_id)

        call_sql = mock_conn.fetch.call_args[0][0]
        assert "status = $1" in call_sql
        assert "runner_id = $2" in call_sql
        assert "workflow_run_id = $3" in call_sql
        assert "LIMIT $4 OFFSET $5" in call_sql
        assert mock_conn.fetch.call_args[0][1:] == (
            "completed",
            runner_id,
            workflow_id,
            50,
            0,
        )

    @pytest.mark.asyncio
    async def test_list_jobs_empty_result(self, mock_conn):
        """GET /jobs returns empty list when no jobs match."""
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 0}))
        mock_conn.fetch = AsyncMock(return_value=[])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs")

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["limit"] == 50
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_jobs_invalid_limit_returns_422(self, client):
        """GET /jobs with a non-integer limit returns 422."""
        async with client as c:
            response = await c.get("/jobs?limit=invalid")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_list_jobs_invalid_status_returns_422(self, client):
        """GET /jobs with an invalid status returns 422."""
        async with client as c:
            response = await c.get("/jobs?status=not-a-real-status")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_list_jobs_empty_workflow_run_id_is_treated_as_absent(self, mock_conn):
        """GET /jobs with an empty workflow_run_id does not apply the filter."""
        job_id = uuid.uuid4()
        row = make_job_row(job_id, "https://github.com/org/repo", "Job 1")

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs?workflow_run_id=")

        assert response.status_code == 200
        assert mock_conn.fetchrow.call_args[0][1:] == ()
        assert mock_conn.fetch.call_args[0][1:] == (50, 0)

    @pytest.mark.asyncio
    async def test_list_jobs_non_positive_limit_returns_422(self, client):
        """GET /jobs with limit < 1 returns 422."""
        async with client as c:
            response = await c.get("/jobs?limit=0")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_list_jobs_negative_offset_returns_422(self, client):
        """GET /jobs with offset < 0 returns 422."""
        async with client as c:
            response = await c.get("/jobs?offset=-1")

        assert response.status_code == 422


class TestEnrichedJobResponse:
    """Tests for enriched JobResponse fields (branch_name, mr_url, workflow_run_id)."""

    @pytest.mark.asyncio
    async def test_job_response_includes_branch_name(self, mock_conn):
        """GET /jobs/{id} returns branch_name in the response."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Branch test",
            status="completed",
            branch_name="feature/my-branch",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert "branch_name" in data
        assert data["branch_name"] == "feature/my-branch"

    @pytest.mark.asyncio
    async def test_job_response_includes_mr_url(self, mock_conn):
        """GET /jobs/{id} returns mr_url in the response."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "MR test",
            status="completed",
            mr_url="https://github.com/org/repo/pull/42",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert "mr_url" in data
        assert data["mr_url"] == "https://github.com/org/repo/pull/42"

    @pytest.mark.asyncio
    async def test_job_response_includes_workflow_run_id(self, mock_conn):
        """GET /jobs/{id} returns workflow_run_id in the response."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Workflow test",
            status="completed",
            workflow_run_id="wr-456",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert "workflow_run_id" in data
        assert data["workflow_run_id"] == "wr-456"

    @pytest.mark.asyncio
    async def test_job_response_new_fields_are_null_by_default(self, mock_conn):
        """Enriched fields are None when not set in the database."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Default null fields",
            status="pending",
        )
        # Ensure the new fields are not set in the row
        assert row["branch_name"] is None
        assert row["mr_url"] is None
        assert row["workflow_run_id"] is None

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["branch_name"] is None
        assert data["mr_url"] is None
        assert data["workflow_run_id"] is None

    @pytest.mark.asyncio
    async def test_enriched_fields_in_list_response(self, mock_conn):
        """GET /jobs includes enriched fields in each item."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Enriched list",
            status="completed",
            branch_name="main",
            mr_url="https://github.com/org/repo/pull/99",
            workflow_run_id="wr-999",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"count": 1}))
        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get("/jobs")

        assert response.status_code == 200
        data = response.json()
        item = data["items"][0]
        assert item["branch_name"] == "main"
        assert item["mr_url"] == "https://github.com/org/repo/pull/99"
        assert item["workflow_run_id"] == "wr-999"


class TestJobCreateWithWorkflowRunId:
    """Tests for JobCreateRequest with optional workflow_run_id."""

    @pytest.mark.asyncio
    async def test_create_job_with_workflow_run_id(self, mock_conn):
        """POST /jobs accepts workflow_run_id and returns it in the response."""
        job_id = uuid.uuid4()
        workflow_run_id = "wr-001"
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Workflow job",
            status="pending", workflow_run_id=workflow_run_id,
        )

        async def _fetchrow(sql, *args):
            if "gateway_jobs" in sql:
                return mock_row(row)
            # For the policy runner lookup, return None so policy is skipped
            return None

        async def _execute(sql, *args):
            pass

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Workflow job",
                    "workflow_run_id": workflow_run_id,
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["workflow_run_id"] == workflow_run_id

        insert_call = mock_conn.execute.call_args_list[0]
        assert "workflow_run_id" in insert_call.args[0]
        assert insert_call.args[7] == workflow_run_id

    @pytest.mark.asyncio
    async def test_create_job_with_empty_workflow_run_id_stores_none(self, mock_conn):
        """POST /jobs treats an empty workflow_run_id as absent."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Workflow job", status="pending"
        )

        async def _fetchrow(sql, *args):
            if "gateway_jobs" in sql:
                return mock_row(row)
            return None

        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Workflow job",
                    "workflow_run_id": "",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["workflow_run_id"] is None

        insert_call = mock_conn.execute.call_args_list[0]
        assert insert_call.args[7] is None

    @pytest.mark.asyncio
    async def test_create_job_without_workflow_run_id(self, mock_conn):
        """POST /jobs without workflow_run_id defaults to None."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "No workflow",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "gateway_jobs" in sql:
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            pass

        mock_conn.execute = AsyncMock(side_effect=_execute)
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No workflow",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["workflow_run_id"] is None
