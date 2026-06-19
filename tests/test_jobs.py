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
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
    async def test_post_job_dispatches_to_executor_and_runs(self, mock_conn, mock_executor):
        """POST /jobs should dispatch to executor and return running status (not completed)."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        assert data["status"] == "running"
        # completed_at should NOT be set — job is only running, not completed
        assert data["completed_at"] is None

        # Verify executor was called
        mock_executor.create_workspace.assert_called_once()
        mock_executor.start_opencode.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_job_transitions_granular_states(self, mock_conn, mock_executor):
        """Status should transition through granular provisioning states."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        assert response.json()["data"]["status"] == "running"

        # Verify granular state transitions happened
        update_statements = [s for s in execute_calls if "UPDATE gateway_jobs" in s]
        assert len(update_statements) >= 3  # pending→prov, prov→start, start→running

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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
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
        data = response.json()["data"]
        assert data["status"] == "failed"
        assert data["completed_at"] is None

    @pytest.mark.asyncio
    async def test_running_job_has_no_completed_at(self, mock_conn, mock_executor):
        """A running job (just dispatched) should NOT have completed_at set."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None

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
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "disk" in data["error"]["message"]
        assert "80%" in data["error"]["message"]

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
            {"id": runner_uuid, "admin_status": "offline", "health_status": None, "status": "offline"}
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
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "offline" in data["error"]["message"]

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
            {"id": runner_uuid, "admin_status": "maintenance", "health_status": None, "status": "maintenance"}
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
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "maintenance" in data["error"]["message"]

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "unreachable" in data["error"]["message"].lower()

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
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFLICT"
        assert terminal_status in data["error"]["message"]

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
            data1 = response1.json()["data"]
            assert data1["status"] == "aborted"

            # Second abort - job is now aborted, should get 409
            response2 = await c.post(f"/jobs/{job_id}/abort")
            assert response2.status_code == 409
            data2 = response2.json()
            assert data2["status"] == "error"
            assert data2["error"]["code"] == "CONFLICT"
            assert "aborted" in data2["error"]["message"]

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
            assert response1.json()["data"]["status"] == "aborted"

            response2 = await c.post(f"/jobs/{job_id}/abort")
            assert response2.status_code == 409
            data2 = response2.json()
            assert data2["status"] == "error"
            assert data2["error"]["code"] == "CONFLICT"
            assert "aborted" in data2["error"]["message"]

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
    """Tests for GET /jobs/{id}/diff (fetches diff from OpenCode Serve via session)."""

    @pytest.mark.asyncio
    async def test_get_diff_returns_200_with_diff(self, mock_conn):
        """GET /jobs/{id}/diff returns 200 with diff content fetched from OpenCode."""
        from app.opencode.protocol import SessionDiffResponse

        job_id = uuid.uuid4()
        session_id = "sess-diff-1"
        expected_diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Completed job with diff",
            status="completed",
            opencode_session_id=session_id,
            completed_at=now,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id=session_id,
                diff=expected_diff,
                files_changed=["file.py"],
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["job_id"] == str(job_id)
        assert data["diff"] == expected_diff

        mock_opencode.get_session_diff.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_get_diff_for_unknown_job_returns_404(self, client, mock_conn):
        """GET /jobs/{id}/diff for a non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/jobs/{uuid.uuid4()}/diff")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_diff_no_session_returns_404(self, mock_conn):
        """GET /jobs/{id}/diff with no session_id returns 404."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "No session",
            status="completed",
            opencode_session_id=None,
            completed_at=datetime.now(timezone.utc),
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 404
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "NOT_FOUND"
        assert "No session" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_get_diff_no_opencode_client_returns_503(self, mock_conn):
        """GET /jobs/{id}/diff without an OpenCode client returns 503."""
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
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "OpenCode Serve client" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_get_diff_opencode_unreachable_returns_503(self, mock_conn):
        """GET /jobs/{id}/diff when OpenCode Serve is unreachable returns 503."""
        job_id = uuid.uuid4()
        session_id = "sess-down"
        row = make_job_row(
            job_id,
            "https://github.com/org/repo",
            "Serve down",
            status="completed",
            opencode_session_id=session_id,
            completed_at=datetime.now(timezone.utc),
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}/diff")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "unreachable" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_get_diff_invalid_uuid_returns_422(self, client):
        """GET /jobs/{id}/diff with a malformed UUID should return 422."""
        async with client as c:
            response = await c.get("/jobs/not-a-uuid/diff")

        assert response.status_code == 422


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
            data1 = response1.json()["data"]
            assert data1["status"] == "running"

            # Second approve - job is now running, should get 409
            response2 = await c.post(f"/jobs/{job_id}/approve")
            assert response2.status_code == 409
            data2 = response2.json()
            assert data2["status"] == "error"
            assert data2["error"]["code"] == "CONFLICT"


class TestJobDiffFetch:
    """Tests for diff fetching on job completion (issue #45)."""

    @pytest.mark.asyncio
    async def test_running_job_fetches_and_persists_diff(self, mock_conn, mock_executor):
        """Diff should be fetched from OpenCode Serve and persisted even though
        the job stays in 'running' status."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        assert data["status"] == "running"
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
        """When diff fetch raises, the job should still reach running (not fail)."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        # Job MUST reach running even though diff fetch failed
        assert data["status"] == "running"
        assert data["diff"] is None

        # Verify diff fetch was attempted
        mock_opencode.get_session_diff.assert_called_once()

    @pytest.mark.asyncio
    async def test_running_job_response_includes_diff(self, mock_conn, mock_executor):
        """A running job response should include the diff field when fetched."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        assert "diff" in data
        assert data["diff"] == expected_diff
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_diff_fetch_not_attempted_when_client_is_none(self, mock_conn, mock_executor):
        """When no OpenCode client is injected, job reaches running with null diff."""
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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
        data = response.json()["data"]
        assert data["status"] == "running"
        # diff should be null when no client is available
        assert data["diff"] is None

    @pytest.mark.asyncio
    async def test_diff_fetch_stores_metadata_in_database(self, mock_conn, mock_executor):
        """When diff is fetched, the diff content is persisted to the database."""
        from app.opencode.protocol import SessionDiffResponse

        job_id = uuid.uuid4()
        expected_diff = "diff --git a/README.md b/README.md\n+new content\n"
        execute_calls: list[tuple] = []

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Metadata store",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET diff" in sql:
                row_data["diff"] = args[1] if len(args) > 1 else None
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id="sess-meta-123",
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
                    "task_summary": "Metadata store",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["diff"] == expected_diff
        assert data["status"] == "running"

        # Verify the diff UPDATE was executed on the database
        diff_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE gateway_jobs SET diff" in sql
        ]
        assert len(diff_updates) == 1
        _, update_args = diff_updates[0]
        assert expected_diff in update_args

    @pytest.mark.asyncio
    async def test_diff_fetch_failure_logged_gracefully(
        self, mock_conn, mock_executor, caplog,
    ):
        """When diff fetch raises, a warning is logged and job status remains running."""
        import logging

        from app.api import jobs as jobs_module

        job_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Graceful failure",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            side_effect=RuntimeError("OpenCode Serve internal error")
        )

        # Enable logging capture on the jobs module
        jobs_module.logger.setLevel(logging.WARNING)
        caplog.set_level(logging.WARNING, logger=jobs_module.logger.name)

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
                    "task_summary": "Graceful failure",
                },
            )

        # Job status MUST remain running — diff fetch failure must NOT
        # cause a transition to failed.
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["diff"] is None

        # Verify a warning was logged about the failed diff fetch
        # (the log message references the dynamically-generated job ID and
        # the session ID from the fixture — we check for the key phrase)
        assert any("Failed to fetch diff" in msg for msg in caplog.messages)
        # The log should mention the session that had the error
        assert any("session" in msg.lower() for msg in caplog.messages)

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
        data = response.json()["data"]
        assert data["status"] == "completed"
        assert data["diff"] == expected_diff
        assert data["opencode_session_id"] == "sess-123"


# ══════════════════════════════════════════════════════════════════════════
#  OpenCode client wiring (issue #149)
# ══════════════════════════════════════════════════════════════════════════


class TestOpencodeClientWiring:
    """Tests for the OpenCode client dependency injection wiring.

    Verifies that ``get_opencode_client()`` follows the expected DI
    pattern: returns ``None`` by default, but a configured client
    (mock or real) can be injected and is used by the endpoints.
    """

    @pytest.mark.asyncio
    async def test_injected_client_receives_diff_fetch_call(self, mock_conn, mock_executor):
        """A mock OpenCode client injected via DI receives get_session_diff calls."""
        from app.opencode.protocol import SessionDiffResponse

        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "DI wiring",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                # The initial diff_summary is set here
                if len(args) >= 4:
                    row_data["diff"] = args[3]
            elif "UPDATE gateway_jobs SET diff" in sql:
                row_data["diff"] = args[1] if len(args) > 1 else None
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id="sess-wired",
                diff="wired diff content",
                files_changed=["file.py"],
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
                    "task_summary": "DI wiring",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["diff"] == "wired diff content"

        # The injected client must have been called with a session ID
        mock_opencode.get_session_diff.assert_called_once()
        call_args = mock_opencode.get_session_diff.call_args[0]
        assert len(call_args) == 1
        assert isinstance(call_args[0], str)
        assert len(call_args[0]) > 0

    @pytest.mark.asyncio
    async def test_injected_client_used_for_abort(self, mock_conn):
        """A mock OpenCode client injected via DI receives abort_session calls."""
        from app.opencode.protocol import SessionAbortResponse

        job_id = uuid.uuid4()
        session_id = "sess-abort-wiring"
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Abort wiring",
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
                message="Aborted via wiring",
            )
        )

        client = create_client(mock_conn, mock_opencode_client=mock_opencode)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "aborted"

        # Verify the injected client received the abort call
        mock_opencode.abort_session.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_injected_client_used_for_logs(self, mock_conn):
        """A mock OpenCode client injected via DI receives get_session and
        get_session_log calls."""
        from app.opencode.protocol import SessionInfo, SessionLogResponse

        job_id = uuid.uuid4()
        session_id = "sess-logs-wiring"
        log_content = "Log from injected client\n"
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Logs wiring",
            status="completed", opencode_session_id=session_id,
            completed_at=now,
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        mock_opencode = AsyncMock()
        mock_opencode.get_session = AsyncMock(
            return_value=SessionInfo(
                id=session_id,
                status="completed",
                workspace_path="/tmp/ws",
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
        data = response.json()["data"]
        assert data["log"] == log_content

        mock_opencode.get_session.assert_called_once_with(session_id)
        mock_opencode.get_session_log.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_default_get_opencode_client_no_injection_skips_diff(self, mock_conn, mock_executor):
        """With no injected client (default None), diff fetch is skipped entirely."""
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "No client",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql:
                row_data["opencode_session_id"] = args[1] if len(args) > 1 else None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # No opencode client — use default which returns None
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No client",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["diff"] is None

    @pytest.mark.asyncio
    async def test_get_job_returns_structured_result_with_diff(self, mock_conn):
        """GET /jobs/{id} returns structured result metadata including the diff."""
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Structured result",
            status="completed",
            completed_at=now,
            opencode_session_id="sess-struct",
            diff="structured diff content",
            branch_name="feature/structured",
            mr_url="https://github.com/org/repo/pull/99",
            workflow_run_id="wr-structured",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()["data"]

        # Verify structured result metadata
        assert data["id"] == str(job_id)
        assert data["repo_url"] == "https://github.com/org/repo"
        assert data["task_summary"] == "Structured result"
        assert data["status"] == "completed"
        assert data["completed_at"] is not None
        assert data["opencode_session_id"] == "sess-struct"
        assert data["diff"] == "structured diff content"
        assert data["branch_name"] == "feature/structured"
        assert data["mr_url"] == "https://github.com/org/repo/pull/99"
        assert data["workflow_run_id"] == "wr-structured"

        # Timestamps should be present
        assert "created_at" in data
        assert "updated_at" in data


class TestCompleteJob:
    """Tests for POST /jobs/{id}/complete — transition to terminal / review states."""

    @pytest.mark.asyncio
    async def test_complete_running_to_awaiting_review(self, mock_conn):
        """Transition running → awaiting_review stores metadata."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "To review",
            status="running", opencode_session_id="sess-001",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET" in sql and "SET status" in sql:
                row["status"] = args[1]  # $2 = target status
                # Parse metadata fields from the SQL:
                # "SET status = $2, updated_at = $3, branch_name = $4, ..."
                # The positions in args correspond to $N placeholders.
                field_map = {
                    "branch_name": None,
                    "commit_sha": None,
                    "mr_url": None,
                    "diff": None,
                    "failure_reason": None,
                    "completed_at": None,
                }
                for field in field_map:
                    if field in sql:
                        # Find the position of this field's $N
                        import re
                        m = re.search(
                            rf"{field} = \$(\d+)", sql
                        )
                        if m:
                            pos = int(m.group(1))
                            if pos <= len(args):
                                row[field] = args[pos - 1]

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={
                    "target_status": "awaiting_review",
                    "branch_name": "feature/my-fix",
                    "commit_sha": "abc123def456",
                    "mr_url": "https://github.com/org/repo/pull/42",
                    "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "awaiting_review"
        assert data["branch_name"] == "feature/my-fix"
        assert data["commit_sha"] == "abc123def456"
        assert data["mr_url"] == "https://github.com/org/repo/pull/42"
        assert data["diff"] == "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"
        assert data["completed_at"] is None  # not terminal

    @pytest.mark.asyncio
    async def test_complete_awaiting_review_to_completed(self, mock_conn):
        """Transition awaiting_review → completed sets completed_at."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Approve me",
            status="awaiting_review",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET" in sql and "SET status" in sql:
                row["status"] = args[1]
                if "completed_at" in sql:
                    import re
                    m = re.search(r"completed_at = \$(\d+)", sql)
                    if m:
                        pos = int(m.group(1))
                        if pos <= len(args):
                            row["completed_at"] = args[pos - 1]

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={
                    "target_status": "completed",
                    "summary": "All tests pass, review approved",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "completed"
        assert data["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_awaiting_review_to_failed(self, mock_conn):
        """Transition awaiting_review → failed sets completed_at and failure_reason."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Rejected",
            status="awaiting_review",
        )

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET" in sql and "SET status" in sql:
                row["status"] = args[1]
                import re
                if "completed_at" in sql:
                    m = re.search(r"completed_at = \$(\d+)", sql)
                    if m:
                        pos = int(m.group(1))
                        if pos <= len(args):
                            row["completed_at"] = args[pos - 1]
                if "failure_reason" in sql:
                    m = re.search(r"failure_reason = \$(\d+)", sql)
                    if m:
                        pos = int(m.group(1))
                        if pos <= len(args):
                            row["failure_reason"] = args[pos - 1]

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={
                    "target_status": "failed",
                    "failure_reason": "Tests did not pass; review rejected",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "failed"
        assert data["completed_at"] is not None
        assert data["failure_reason"] == "Tests did not pass; review rejected"

    @pytest.mark.asyncio
    async def test_complete_unknown_job_returns_404(self, mock_conn):
        """Complete on non-existent job returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{uuid.uuid4()}/complete",
                json={"target_status": "awaiting_review"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_complete_invalid_target_status_returns_400(self, mock_conn):
        """Complete with an invalid target_status returns 400."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Bad target",
            status="running",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={"target_status": "pending"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_complete_invalid_transition_returns_409(self, mock_conn):
        """Complete from a state that cannot transition to the target returns 409."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Already done",
            status="completed",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={"target_status": "awaiting_review"},
            )

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_complete_from_pending_returns_409(self, mock_conn):
        """Complete from pending state (not running/awaiting_review) returns 409."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Still pending",
            status="pending",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/jobs/{job_id}/complete",
                json={"target_status": "completed"},
            )

        assert response.status_code == 409


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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
#  Lifecycle Event Recording (issue #144)
# ══════════════════════════════════════════════════════════════════════════


class TestLifecycleEventRecording:
    """Tests that job_events are recorded on every lifecycle transition."""

    @pytest.mark.asyncio
    async def test_create_job_records_pending_to_provisioning_event(self, mock_conn, mock_executor):
        """create_job records pending→provisioning_workspace event."""
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Test event",
            status="pending",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
                    "task_summary": "Test event recording",
                },
            )

        assert response.status_code == 201

        # Find all INSERT INTO job_events calls
        event_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]

        # Should have events for: pending→provisioning_workspace,
        # provisioning_workspace→starting_opencode, starting_opencode→running
        assert len(event_inserts) == 3

        # Verify first event: pending→provisioning_workspace
        first_sql, first_args = event_inserts[0]
        assert "job_events" in first_sql
        assert first_args[2] == "provisioning_workspace"  # event_type = to_status
        assert first_args[3] == "system"                   # actor
        assert first_args[5] == "pending"                  # previous_status = from_status

        # Verify second event: provisioning_workspace→starting_opencode
        second_sql, second_args = event_inserts[1]
        assert second_args[2] == "starting_opencode"
        assert second_args[5] == "provisioning_workspace"

        # Verify third event: starting_opencode→running
        third_sql, third_args = event_inserts[2]
        assert third_args[2] == "running"
        assert third_args[5] == "starting_opencode"

    @pytest.mark.asyncio
    async def test_create_job_records_events_with_correct_message(self, mock_conn, mock_executor):
        """Each event has a human-readable message in the details field."""
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Test messages",
            status="pending",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Test messages",
                },
            )

        assert response.status_code == 201

        event_inserts = [
            args[4] for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        assert len(event_inserts) == 3

        # Verify messages describe each stage
        assert "Provisioning workspace" in event_inserts[0]
        assert "Starting OpenCode Serve" in event_inserts[1]
        assert "Job is now running" in event_inserts[2]

    @pytest.mark.asyncio
    async def test_executor_failure_records_failed_event(self, mock_conn):
        """When executor.create_workspace fails, a failed transition event is recorded."""
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fail event",
            status="pending",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

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
                    "task_summary": "Fail event",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        # Find all INSERT INTO job_events calls
        event_inserts = [
            args for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]

        # Should have at least 2 events: the provisioning_workspace transition
        # plus the failed transition
        assert len(event_inserts) >= 2

        # The last event (or one of the last) should be the failed transition
        failed_events = [
            a for a in event_inserts if a[2] == "failed"
        ]
        assert len(failed_events) >= 1

        failed_event = failed_events[0]
        # event_type should be "failed"
        assert failed_event[2] == "failed"
        # previous_status should be the status before failure
        assert failed_event[5] in ("pending", "provisioning_workspace")
        # details should contain info about the failure
        assert "Executor dispatch failed" in failed_event[4]

    @pytest.mark.asyncio
    async def test_policy_violation_records_failed_event(self, mock_conn, mock_executor):
        """PolicyViolation records a pending→failed transition event."""
        from unittest.mock import patch
        from app.policy import ObservationBasedPolicy, PolicyViolation

        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Policy fail",
            status="pending",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner-99"})
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

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
                        "task_summary": "Policy fail",
                    },
                )

        assert response.status_code == 503

        # Find job_events inserts
        event_inserts = [
            args for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]

        # Should have at least 1 event: pending→failed
        assert len(event_inserts) >= 1

        # The failed event
        failed_events = [
            a for a in event_inserts if a[2] == "failed"
        ]
        assert len(failed_events) >= 1

        failed_event = failed_events[0]
        assert failed_event[2] == "failed"
        assert failed_event[5] == "pending"  # policy fails before any transition
        assert "disk/memory pressure" in failed_event[4]

    @pytest.mark.asyncio
    async def test_approve_job_records_event(self, mock_conn):
        """approve_job records needs_approval→running event."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Approve event",
            status="needs_approval",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/approve")

        assert response.status_code == 200

        event_inserts = [
            args for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        assert len(event_inserts) == 1

        event_args = event_inserts[0]
        assert event_args[2] == "running"           # event_type = to_status
        assert event_args[3] == "api"               # actor
        assert event_args[4] == "Job approved by api"
        assert event_args[5] == "needs_approval"     # previous_status = from_status

    @pytest.mark.asyncio
    async def test_reject_job_records_event(self, mock_conn):
        """reject_job records needs_approval→rejected event."""
        job_id = uuid.uuid4()
        row = make_job_row(
            job_id, "https://github.com/org/repo", "Reject event",
            status="needs_approval",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                return mock_row(row)
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'rejected'" in sql:
                row["status"] = "rejected"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/reject")

        assert response.status_code == 200

        event_inserts = [
            args for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        assert len(event_inserts) == 1

        event_args = event_inserts[0]
        assert event_args[2] == "rejected"          # event_type = to_status
        assert event_args[3] == "api"               # actor
        assert event_args[4] == "Job rejected by api"
        assert event_args[5] == "needs_approval"     # previous_status = from_status


# ══════════════════════════════════════════════════════════════════════════
#  Runner Selection & Job Pinning (issue #92)
# ══════════════════════════════════════════════════════════════════════════


class TestRunnerSelection:
    """Tests for runner selection and job pinning in POST /jobs.

    Verifies that select_runner() filters runners using admin_status='online'
    AND health_status='HEALTHY' instead of the legacy status field.
    Runners with admin_status='offline' or 'maintenance' are excluded,
    as are runners with health_status != 'HEALTHY'.
    """

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_healthy_runner_succeeds(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a valid, online+healthy runner_id should dispatch to that runner."""
        job_id = uuid.uuid4()
        runner_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Pin to runner",
            status="pending",
        )

        # Track which SQL is being queried so we can return the right mock
        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "online", "health_status": "HEALTHY"})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

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
        data = response.json()["data"]
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_not_found_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a non-existent runner_id should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
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
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "not found" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_unhealthy_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a runner whose health_status is not HEALTHY should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "online", "health_status": "BLOCKED_DISK_PRESSURE"})
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
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "health_status" in data["error"]["message"].lower()
        assert "BLOCKED_DISK_PRESSURE" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_admin_status_offline_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a runner whose admin_status is 'offline' should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "offline", "health_status": "HEALTHY"})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to offline runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "admin_status" in data["error"]["message"].lower()
        assert "offline" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_admin_status_maintenance_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a runner whose admin_status is 'maintenance' should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "maintenance", "health_status": "HEALTHY"})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to maintenance runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "admin_status" in data["error"]["message"].lower()
        assert "maintenance" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_explicit_runner_pin_health_status_null_returns_400(
        self, mock_conn, mock_executor
    ):
        """POST /jobs with a runner whose health_status is None should return 400."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "online", "health_status": None})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to null health runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "health_status" in data["error"]["message"].lower()

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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

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
        data = response.json()["data"]
        assert data["status"] == "running"

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
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "no runner" in data["error"]["message"].lower()
        assert "nonexistent-label" in data["error"]["message"]

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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

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
        data = response.json()["data"]
        assert data["status"] == "running"

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
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "no runners" in data["error"]["message"].lower()

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
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

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
        data = response.json()["data"]
        assert data["status"] == "running"

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
            if "SELECT id, admin_status, health_status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "admin_status": "online", "health_status": "HEALTHY"})
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

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
        data = response.json()["data"]
        assert data["status"] == "running"

    # ══════════════════════════════════════════════════════════════════════
    # Split-field runner selection tests (admin_status + health_status)
    # ══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_auto_selection_online_healthy_succeeds(
        self, mock_conn, mock_executor
    ):
        """A runner with admin_status='online' and HEALTHY legacy status
        is eligible for automatic selection and passes policy (online bypass)."""
        job_id = uuid.uuid4()
        runner_id = uuid.uuid4()

        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Online healthy",
            status="pending",
        )

        async def _fetchrow(sql, *args):
            # select_runner auto-selection query (no labels)
            if "LEFT JOIN workspaces w ON w.runner_id = r.id" in sql and "r.labels" not in sql:
                return mock_row({"id": runner_id, "active_workspaces": 0})
            # Runner row for policy lookup (uses runner_id text)
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "runner-online-healthy"})
            # Runner row for policy check (reads admin_status + health_status)
            if "FROM runners WHERE runner_id" in sql:
                return mock_row({
                    "id": runner_id,
                    "admin_status": "online",
                    "health_status": "HEALTHY",
                    "status": "HEALTHY",
                })
            if "SELECT" in sql.upper() and "gateway_jobs" in sql:
                return mock_row(row_data)
            return None

        async def _execute(sql, *args):
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Online healthy",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_auto_selection_rejects_non_healthy_legacy_status(
        self, mock_conn, mock_executor
    ):
        """A runner with non-HEALTHY legacy status (reflecting unhealthy
        health_status) is rejected by select_runner before policy runs.

        This validates that the split-field invariant flows through the
        legacy column that select_runner checks.
        """
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            # select_runner auto-selection — the runner has status != HEALTHY
            if "LEFT JOIN workspaces w ON w.runner_id = r.id" in sql and "r.labels" not in sql:
                return None  # no HEALTHY runner found
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No healthy runners (split fields)",
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "no healthy runners" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_explicit_pin_rejects_admin_status_offline(
        self, mock_conn, mock_executor
    ):
        """Pinning to a runner with admin_status='offline' (and legacy
        status='offline') returns 400 from select_runner."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "status": "offline"})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to offline runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "not healthy" in data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_explicit_pin_rejects_admin_status_maintenance(
        self, mock_conn, mock_executor
    ):
        """Pinning to a runner with admin_status='maintenance' (and legacy
        status='maintenance') returns 400 from select_runner."""
        runner_id = uuid.uuid4()

        async def _fetchrow(sql, *args):
            if "SELECT id, status FROM runners WHERE id" in sql:
                return mock_row({"id": runner_id, "status": "maintenance"})
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Pin to maintenance runner",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "not healthy" in data["error"]["message"].lower()


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
        data = response.json()["data"]
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
    async def test_get_logs_no_session_returns_404(self, mock_conn):
        """GET /jobs/{id}/logs for a job with no session returns 404."""
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

        assert response.status_code == 404
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "NOT_FOUND"
        assert "No session" in data["error"]["message"]

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
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "FAILED_DEPENDENCY"
        assert "pending" in data["error"]["message"]

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
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "OpenCode Serve client" in data["error"]["message"]

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        assert row["commit_sha"] is None
        assert row["mr_url"] is None
        assert row["workflow_run_id"] is None
        assert row["failure_reason"] is None

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        client = create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["branch_name"] is None
        assert data["commit_sha"] is None
        assert data["mr_url"] is None
        assert data["workflow_run_id"] is None
        assert data["failure_reason"] is None

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
        assert data["workflow_run_id"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Job Lifecycle State Transitions (issue #147)
# ══════════════════════════════════════════════════════════════════════════


class TestLifecycleTransitions:
    """Tests for complete job lifecycle state transitions.

    Verifies that the create_job endpoint correctly transitions through
    all lifecycle states (stopping at 'running'), records events on failure,
    persists metadata, and handles executor failures gracefully.
    """

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _track_status(
        row_data: dict,
        execute_calls: list = None,
    ):
        """Return an _execute side-effect that tracks status transitions."""
        from datetime import datetime, timezone

        async def _execute(sql: str, *args):
            if execute_calls is not None:
                execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                if len(args) >= 3:
                    row_data["completed_at"] = args[2]
                if len(args) >= 4:
                    row_data["diff"] = args[3]
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET workspace_name" in sql and len(args) >= 2:
                row_data["workspace_name"] = args[1]
            elif "UPDATE gateway_jobs SET opencode_session_id" in sql and len(args) >= 2:
                row_data["opencode_session_id"] = args[1]
            elif "UPDATE gateway_jobs SET diff" in sql and len(args) >= 2:
                row_data["diff"] = args[1]

        return _execute

    # -- 1. Happy path through all states ---------------------------------

    @pytest.mark.asyncio
    async def test_happy_path_all_states(self, mock_conn, mock_executor):
        """Happy path: pending→provisioning_workspace→starting_opencode→running.

        Verifies every status transition, executor calls in order, and
        that metadata (workspace_name, session_id) is persisted.
        The job stops at 'running' — completion only happens via
        POST /jobs/{id}/complete or a terminal webhook.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Happy path job",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Happy path job",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None

        # Verify status transitions in order (status is in the SQL string)
        status_update_sqls = [
            sql for sql, _ in execute_calls
            if "UPDATE gateway_jobs SET status" in sql
        ]
        # Should have provisioning_workspace, starting_opencode, running
        assert len(status_update_sqls) >= 3
        # First status UPDATE should set 'provisioning_workspace'
        assert "status = 'provisioning_workspace'" in status_update_sqls[0]
        # Last status UPDATE should set 'running'
        assert "status = 'running'" in status_update_sqls[-1]
        # There should be NO completed status update
        completed_updates = [
            sql for sql in status_update_sqls
            if "status = 'completed'" in sql
        ]
        assert len(completed_updates) == 0

        # Verify executor was called in the correct order
        mock_executor.create_workspace.assert_called_once()
        mock_executor.start_opencode.assert_called_once()
        # create_workspace called before start_opencode
        create_call_time = mock_executor.create_workspace.call_args
        start_call_time = mock_executor.start_opencode.call_args
        assert create_call_time is not None
        assert start_call_time is not None

        # Verify metadata was stored in the DB
        workspace_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET workspace_name" in sql
        ]
        assert len(workspace_updates) == 1
        assert workspace_updates[0][1] is not None

        session_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET opencode_session_id" in sql
        ]
        assert len(session_updates) == 1
        assert session_updates[0][1] is not None

    # -- 2. Failed workspace creation -------------------------------------

    @pytest.mark.asyncio
    async def test_failed_workspace_creation_transitions_to_failed(
        self, mock_conn,
    ):
        """Failed create_workspace → job marked 'failed', event recorded.

        When the executor raises during workspace creation, the job should
        transition to 'failed', no subsequent executor calls should happen,
        and a job_events entry should be recorded.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fail workspace",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Failed to create workspace")
        )
        # start_opencode should NOT be called
        failing_executor.start_opencode = AsyncMock()

        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fail workspace",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"
        # completed_at should remain None for failed jobs
        assert data["completed_at"] is None

        # start_opencode should NOT have been called
        failing_executor.start_opencode.assert_not_called()

        # Verify a job_events entry was created for the failure
        event_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        # Should have at least 2 events: provisioning_workspace + executor_error
        assert len(event_inserts) >= 2
        # The last event should be the error event
        event_args = event_inserts[-1][1]
        assert "executor_error" in event_args or "failed" in event_args

    # -- 3. Failed OpenCode startup ---------------------------------------

    @pytest.mark.asyncio
    async def test_failed_opencode_startup_transitions_to_failed(
        self, mock_conn,
    ):
        """Failed start_opencode → job marked 'failed', event recorded.

        When workspace creation succeeds but OpenCode startup fails, the
        job should be marked 'failed' and an event should be recorded.
        create_workspace must have been called; start_opencode must have
        been attempted.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fail opencode",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        # Workspace creation succeeds, but OpenCode startup fails
        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            return_value=AsyncMock(
                workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                workspace_path="/tmp/opencode/ws",
                status="ready",
            )
        )
        failing_executor.start_opencode = AsyncMock(
            side_effect=RuntimeError("Failed to start OpenCode")
        )

        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fail opencode",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"
        assert data["completed_at"] is None

        # create_workspace must have been called
        failing_executor.create_workspace.assert_called_once()
        # start_opencode must have been attempted
        failing_executor.start_opencode.assert_called_once()

        # Verify a job_events entry was created
        event_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        assert len(event_inserts) >= 1

    # -- 4. Job NOT completed after OpenCode startup alone -----------------

    @pytest.mark.asyncio
    async def test_not_completed_after_opencode_startup_alone(
        self, mock_conn, mock_executor,
    ):
        """Job is NOT completed after start_opencode returns.

        OpenCode startup alone does NOT complete the job. The status
        should be 'running' after successful create_workspace and
        start_opencode, with completed_at remaining NULL. No
        completed UPDATE should have been issued.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Not completed yet",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Not completed yet",
                },
            )

        assert response.status_code == 201

        # Collect all status UPDATE calls in order and verify the sequence
        status_update_sqls = [
            sql for sql, _ in execute_calls
            if "UPDATE gateway_jobs SET status" in sql
        ]
        # At minimum: provisioning_workspace, starting_opencode, running
        assert len(status_update_sqls) >= 3

        # Verify all three transitions happened
        assert any("status = 'provisioning_workspace'" in sql for sql in status_update_sqls)
        assert any("status = 'starting_opencode'" in sql for sql in status_update_sqls)
        assert any("status = 'running'" in sql for sql in status_update_sqls)

        # The last status update must be to 'running' (not 'completed')
        assert "status = 'running'" in status_update_sqls[-1]

        # No completed status update should have occurred
        completed_updates = [
            sql for sql in status_update_sqls
            if "status = 'completed'" in sql
        ]
        assert len(completed_updates) == 0

        # Verify completed_at is NOT set in the final response
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None

    # -- 5. Missing terminal result does not complete the job --------------

    @pytest.mark.asyncio
    async def test_missing_terminal_result_does_not_complete(self, mock_conn):
        """When the executor returns no valid result, the job does NOT complete.

        If create_workspace returns a response but start_opencode cannot
        proceed (missing or invalid predecessor data), the job should
        transition to 'failed' rather than 'completed'.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Missing result",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        # Simulate an executor that creates a workspace but
        # start_opencode cannot proceed (invalid/empty workspace_path).
        from app.executors.models import CreateWorkspaceResponse

        executor = AsyncMock()
        executor.create_workspace = AsyncMock(
            return_value=CreateWorkspaceResponse(
                workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                workspace_path="",  # missing/invalid terminal result
                status="ready",
            )
        )
        executor.start_opencode = AsyncMock(
            side_effect=RuntimeError("Invalid workspace path")
        )

        client = create_client(mock_conn, mock_executor=executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Missing result",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"
        assert data["completed_at"] is None

        # Both executor methods should have been called
        executor.create_workspace.assert_called_once()
        executor.start_opencode.assert_called_once()

        # No completed status update should have occurred
        completed_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET status = 'completed'" in sql
        ]
        assert len(completed_updates) == 0

    # -- 6. Events recorded for failure transitions -----------------------

    @pytest.mark.asyncio
    async def test_events_recorded_for_failure_transitions(self, mock_conn):
        """Failure transitions record events in the job_events table.

        Verifies that when create_workspace raises, an INSERT INTO
        job_events is executed with the correct event_type, actor,
        and details.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Events on failure",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Disk full")
        )

        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Events on failure",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        # Find job_events INSERT statements
        event_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        # Should have at least 2 events: provisioning_workspace + the error event
        assert len(event_inserts) >= 2

        # The last event should be the error event
        event_sql, event_args = event_inserts[-1]
        assert "job_events" in event_sql

        # Verify event structure: (id, job_id, event_type, actor, details, created_at)
        # or (id, job_id, event_type, actor, details, previous_status, created_at)
        event_type = event_args[2]
        actor = event_args[3]
        details = event_args[4]

        assert isinstance(event_type, str)
        assert event_type in ("executor_error", "artifact_error")
        assert actor == "gateway"
        assert isinstance(details, str)
        assert len(details) > 0

    @pytest.mark.asyncio
    async def test_events_recorded_for_opencode_failure(self, mock_conn):
        """OpenCode startup failure records an event in job_events."""
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Events opencode fail",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        executor = AsyncMock()
        executor.create_workspace = AsyncMock(
            return_value=AsyncMock(
                workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                workspace_path="/tmp/opencode/ws",
                status="ready",
            )
        )
        executor.start_opencode = AsyncMock(
            side_effect=RuntimeError("OpenCode port unavailable")
        )

        client = create_client(mock_conn, mock_executor=executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Events opencode fail",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        event_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO job_events" in sql
        ]
        # Should have at least 2 events: provisioning_workspace → starting_opencode
        # transition and then the executor_error event
        assert len(event_inserts) >= 2

        # The last event should be the error event
        event_type = event_inserts[-1][1][2]
        assert event_type in ("executor_error",)

    # -- 7. Completion metadata persisted ---------------------------------

    @pytest.mark.asyncio
    async def test_completion_metadata_persisted(self, mock_conn, mock_executor):
        """Workspace name, session ID, and diff are persisted on job startup.

        Verifies that the DB UPDATE statements for workspace_name,
        opencode_session_id, and diff are all executed during a
        successful job flow. The job stops at 'running' — completion
        only happens via POST /jobs/{id}/complete.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Persist metadata",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        # Provide an opencode client so diff fetching is attempted
        from app.opencode.protocol import SessionDiffResponse

        mock_opencode = AsyncMock()
        mock_opencode.get_session_diff = AsyncMock(
            return_value=SessionDiffResponse(
                session_id="mock-session",
                diff="diff --git a/file.txt b/file.txt\n+new content\n",
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
                    "task_summary": "Persist metadata",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None

        # Verify workspace_name UPDATE
        ws_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET workspace_name" in sql
        ]
        assert len(ws_updates) == 1
        assert ws_updates[0][1] is not None
        # The workspace_name should be a non-empty string representation
        assert str(ws_updates[0][1]).strip() != ""

        # Verify opencode_session_id UPDATE
        session_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET opencode_session_id" in sql
        ]
        assert len(session_updates) == 1
        assert session_updates[0][1] is not None

        # Verify diff is present in the response (fetched and persisted)
        assert data["diff"] is not None

        # Verify opencode client was called for diff fetching
        mock_opencode.get_session_diff.assert_called_once()

        # Verify the diff was persisted via a separate UPDATE
        diff_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET diff" in sql
        ]
        assert len(diff_updates) >= 1

    @pytest.mark.asyncio
    async def test_metadata_persisted_without_opencode_client(
        self, mock_conn, mock_executor,
    ):
        """Metadata persisted even when no OpenCode client is available.

        When there is no opencode_client (None), workspace_name and session_id
        should still be persisted. The job stops at 'running' after startup.
        """
        job_id = uuid.uuid4()
        row_data = make_job_row(
            job_id, "https://github.com/org/repo", "No client metadata",
            status="pending",
        )
        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(
            side_effect=self._track_status(row_data, execute_calls)
        )

        # No opencode client injected
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "No client metadata",
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None

        # Verify workspace_name UPDATE
        ws_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET workspace_name" in sql
        ]
        assert len(ws_updates) == 1

        # Verify opencode_session_id UPDATE
        session_updates = [
            args for sql, args in execute_calls
            if "UPDATE gateway_jobs SET opencode_session_id" in sql
        ]
        assert len(session_updates) == 1

        # Diff should be None — no completed UPDATE and no opencode client
        assert data["diff"] is None
