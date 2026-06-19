"""Integration tests: no premature completion and cleanup scenarios.

Tests that:
1. Job stays "running" until terminal result arrives (NOT immediately "completed")
2. Cleanup succeeds after job completion
3. Cleanup failure is logged but job status remains "completed"

Each test uses a real PostgreSQL database (via ``db_conn``) paired with
deterministic fake executor / OpenCode clients.

The key mechanism for proving "no premature completion" is
:class:`TrackedFakeExecutorPlugin`, which queries the database during
each lifecycle call and records the job's status at that moment.
This captures the intermediate ``running`` state before the final
``completed`` state is set.
"""

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import create_runner

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants — deterministic IDs (use distinct UUIDs from other test files)
# ---------------------------------------------------------------------------

TEST_WORKSPACE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
TEST_SESSION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
TEST_WORKSPACE_PATH = "/data/workspaces/test-workspace"


# ═══════════════════════════════════════════════════════════════════════════
#  Tracked Fake Executor Plugin — records job status from the DB during
#  each lifecycle call so tests can verify intermediate state transitions.
# ═══════════════════════════════════════════════════════════════════════════


class TrackedFakeExecutorPlugin:
    """Fake :class:`ExecutorPlugin` that records job status snapshots from the DB.

    On each lifecycle call the plugin queries the database for the associated
    job's current status and stores it in ``status_snapshots``.  This allows
    tests to prove that the job was in the expected intermediate state (e.g.
    ``running``) before the final state (``completed``) was set.

    The same failure-injection and call-tracking mechanisms from
    :class:`FakeExecutorPlugin` are replicated here so this class is fully
    self-contained.
    """

    name = "tracked_fake"

    def __init__(self, db_conn: Any = None) -> None:
        from app.executors.models import (
            CleanupWorkspaceResponse,
            CollectStateResponse,
            CreateWorkspaceResponse,
            RestartOpencodeResponse,
            StartOpencodeResponse,
            StopOpencodeResponse,
            WorkspaceState,
        )

        self.db_conn = db_conn

        # ── Default deterministic responses ──────────────────────────
        self.create_workspace_response = CreateWorkspaceResponse(
            workspace_id=TEST_WORKSPACE_ID,
            workspace_path=TEST_WORKSPACE_PATH,
            status="ready",
        )
        self.start_opencode_response = StartOpencodeResponse(
            session_id=TEST_SESSION_ID,
            status="running",
            port=10000,
        )
        self.stop_opencode_response = StopOpencodeResponse(status="stopped")
        self.restart_opencode_response = RestartOpencodeResponse(status="running")
        self.collect_state_response = CollectStateResponse(
            workspace_id=TEST_WORKSPACE_ID,
            status=WorkspaceState.READY,
            process_status="running",
            port=10000,
        )
        self.cleanup_workspace_response = CleanupWorkspaceResponse(status="cleaned")

        # ── Failure injection ────────────────────────────────────────
        self.create_workspace_failure: Exception | None = None
        self.start_opencode_failure: Exception | None = None
        self.cleanup_workspace_failure: Exception | None = None

        # ── Call tracking ────────────────────────────────────────────
        self.calls: dict[str, list[Any]] = {
            "create_workspace": [],
            "start_opencode": [],
            "stop_opencode": [],
            "restart_opencode": [],
            "collect_state": [],
            "cleanup_workspace": [],
        }

        # ── Status snapshots captured during each lifecycle call ──────
        # Maps method name -> list of status strings recorded at call time.
        self.status_snapshots: dict[str, list[str]] = {
            "create_workspace": [],
            "start_opencode": [],
            "cleanup_workspace": [],
        }

    # ── Lifecycle methods ──────────────────────────────────────────────

    async def create_workspace(self, request: Any) -> Any:
        self.calls["create_workspace"].append(request)

        # Snapshot job status from the DB while inside the request handler.
        # At this point the job should already be "running".
        if self.db_conn is not None and request.job_id is not None:
            row = await self.db_conn.fetchrow(
                "SELECT status FROM gateway_jobs WHERE id = $1",
                request.job_id,
            )
            if row is not None:
                self.status_snapshots["create_workspace"].append(row["status"])

        if self.create_workspace_failure:
            raise self.create_workspace_failure
        return self.create_workspace_response

    async def start_opencode(self, request: Any) -> Any:
        self.calls["start_opencode"].append(request)

        # Snapshot job status from the DB.  The job is looked up via the
        # workspace_name column which stores the workspace UUID as text.
        if self.db_conn is not None:
            row = await self.db_conn.fetchrow(
                "SELECT status FROM gateway_jobs WHERE workspace_name = $1",
                str(request.workspace_id),
            )
            if row is not None:
                self.status_snapshots["start_opencode"].append(row["status"])

        if self.start_opencode_failure:
            raise self.start_opencode_failure
        return self.start_opencode_response

    async def stop_opencode(self, request: Any) -> Any:
        self.calls["stop_opencode"].append(request)
        return self.stop_opencode_response

    async def cleanup_workspace(self, request: Any) -> Any:
        self.calls["cleanup_workspace"].append(request)
        if self.cleanup_workspace_failure:
            raise self.cleanup_workspace_failure
        return self.cleanup_workspace_response

    async def restart_opencode(self, request: Any) -> Any:
        self.calls["restart_opencode"].append(request)
        return self.restart_opencode_response

    async def collect_state(self, request: Any) -> Any:
        self.calls["collect_state"].append(request)
        return self.collect_state_response


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_opencode_client() -> Any:
    """Return a simple fake OpenCode client for diff/log responses.

    Returns ``None`` by default so the job endpoint skips diff fetching
    (the diff is still set via the summary fallback).  Tests that need a
    working client can override this fixture in their class.
    """
    return None


@pytest.fixture
async def runner_id(db_conn) -> uuid.UUID:
    """Create a HEALTHY runner in the test database and return its UUID."""
    return await create_runner(
        db_conn,
        hostname="completion-cleanup-test-runner",
        status="HEALTHY",
        executor_type="local",
    )


@pytest.fixture
def tracked_executor(db_conn) -> TrackedFakeExecutorPlugin:
    """Return a :class:`TrackedFakeExecutorPlugin` wired to the test DB."""
    return TrackedFakeExecutorPlugin(db_conn=db_conn)


@pytest.fixture
async def app_client(
    db_conn,
    tracked_executor: TrackedFakeExecutorPlugin,
    fake_opencode_client: Any,
) -> AsyncClient:
    """Return an httpx AsyncClient wired to the FastAPI app with overridden
    dependencies: the tracked executor and database connection.
    """
    from app.api.jobs import get_opencode_client
    from app.core.factory import create_app
    from app.db.session import get_session
    from app.executors.factory import get_executor

    app = create_app()
    app.state.pool = AsyncMock()
    app.state.pool.pool = None

    async def _override_get_session(request):
        yield db_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_executor] = lambda: tracked_executor
    app.dependency_overrides[get_opencode_client] = lambda: fake_opencode_client

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )
    return client


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: No Premature Completion
# ═══════════════════════════════════════════════════════════════════════════


class TestNoPrematureCompletion:
    """Verify that jobs transition through ``running`` state on the way to
    ``completed``, rather than jumping directly from ``pending``.

    The tracked executor queries the database at each lifecycle boundary,
    capturing the job's status at that point.  If the job went through
    ``running``, the snapshots taken inside ``create_workspace`` and
    ``start_opencode`` will show ``running``.
    """

    @pytest.mark.asyncio
    async def test_job_recorded_as_running_during_executor_calls(
        self,
        app_client: AsyncClient,
        tracked_executor: TrackedFakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """The job is in ``running`` state when the executor is called.

        The tracked executor records a DB snapshot during ``create_workspace``
        and ``start_opencode``.  Both snapshots must show ``running``,
        proving the job was NOT prematurely completed before the executor
        finished its work.
        """
        payload = {
            "repo_url": "https://github.com/example/no-premature.git",
            "task_summary": "No premature completion test",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Verify the final response shows running ─────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        data = response.json()["data"]
        assert data["status"] == "running"

        # ── Verify status snapshots captured during executor calls ────
        # Both create_workspace and start_opencode should have been called
        # while the job was in 'running' state.
        assert (
            len(tracked_executor.status_snapshots["create_workspace"]) == 1
        ), "Expected one snapshot during create_workspace"
        assert (
            tracked_executor.status_snapshots["create_workspace"][0] == "running"
        ), (
            f"Expected 'running' during create_workspace, "
            f"got '{tracked_executor.status_snapshots['create_workspace'][0]}'"
        )

        assert (
            len(tracked_executor.status_snapshots["start_opencode"]) == 1
        ), "Expected one snapshot during start_opencode"
        assert (
            tracked_executor.status_snapshots["start_opencode"][0] == "running"
        ), (
            f"Expected 'running' during start_opencode, "
            f"got '{tracked_executor.status_snapshots['start_opencode'][0]}'"
        )

        # ── Verify the executor was called in the expected order ──────
        assert len(tracked_executor.calls["create_workspace"]) == 1
        assert len(tracked_executor.calls["start_opencode"]) == 1

    @pytest.mark.asyncio
    async def test_job_db_record_shows_completed_with_timestamps(
        self,
        app_client: AsyncClient,
        tracked_executor: TrackedFakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """The final DB record has ``completed`` status and a valid completed_at.

        Additionally verify that ``completed_at`` differs from ``created_at``,
        confirming the job existed in a non-terminal state for some duration.
        """
        payload = {
            "repo_url": "https://github.com/example/timestamps.git",
            "task_summary": "Timestamp verification test",
            "runner_id": str(runner_id),
        }

        response = await app_client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["completed_at"] is None
        job_id = uuid.UUID(data["id"])

        # After POST /jobs, the job should be in "running" state.
        row = await db_conn.fetchrow(
            "SELECT status, created_at, completed_at, updated_at "
            "FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row is not None
        assert row["status"] == "running"
        assert row["completed_at"] is None

        # Complete the job to reach terminal state for timestamp verification.
        complete_resp = await app_client.post(
            f"/jobs/{job_id}/complete",
            json={"target_status": "completed"},
        )
        assert complete_resp.status_code == 200

        row = await db_conn.fetchrow(
            "SELECT status, created_at, completed_at, updated_at "
            "FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        # completed_at must be after created_at (proving the job existed
        # before it was marked completed)
        assert row["completed_at"] > row["created_at"], (
            "completed_at must be after created_at — "
            "job should not be completed at creation time"
        )
        # updated_at must be >= completed_at (the final update sets both)
        assert row["updated_at"] >= row["completed_at"]

    @pytest.mark.asyncio
    async def test_executor_calls_prove_running_transition(
        self,
        app_client: AsyncClient,
        tracked_executor: TrackedFakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Executor call count and snapshots together prove the running transition.

        The flow is::
            pending → running → [create_workspace] → [start_opencode] → completed (via /complete)

        By verifying that create_workspace and start_opencode were called
        AND that the DB showed 'running' during those calls, we prove
        the job passed through the running state.
        """
        payload = {
            "repo_url": "https://github.com/example/prove-running.git",
            "task_summary": "Prove running transition",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        assert response.json()["data"]["status"] == "running"

        # Sequence proof: create_workspace must have been called before
        # start_opencode (enforced by the sync handler).
        assert len(tracked_executor.calls["create_workspace"]) == 1
        assert len(tracked_executor.calls["start_opencode"]) == 1

        # Both snapshots must show 'running' — the job was NOT yet completed
        # when these calls were made.
        ws_snapshots = tracked_executor.status_snapshots["create_workspace"]
        oc_snapshots = tracked_executor.status_snapshots["start_opencode"]
        assert ws_snapshots == ["running"]
        assert oc_snapshots == ["running"]


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Cleanup After Completion
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanupAfterCompletion:
    """Verify that workspace cleanup works correctly after a job completes,
    and that cleanup failures do not affect the job's completed status.
    """

    @pytest.mark.asyncio
    async def test_cleanup_succeeds_after_job_completion(
        self,
        db_conn,
        tracked_executor: TrackedFakeExecutorPlugin,
        runner_id: uuid.UUID,
    ):
        """Workspace can be cleaned up through the API after the job completes.

        This test submits a job, calls the workspace cleanup endpoint,
        and verifies that:
        - The cleanup endpoint returns 200
        - The workspace cleanup_status becomes ``cleaned``
        - The job status remains ``completed``
        """
        # 1. Create the app client with the tracked executor
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: tracked_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            # 2. Submit a job
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/cleanup-success.git",
                    "task_summary": "Test cleanup after completion",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 201
        job_data = response.json()["data"]
        assert job_data["status"] == "running"
        job_id = uuid.UUID(job_data["id"])

        # 3. Complete the job to reach terminal state for cleanup.
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            complete_resp = await client.post(
                f"/jobs/{job_id}/complete",
                json={"target_status": "completed"},
            )
        assert complete_resp.status_code == 200

        # 4. Get the workspace ID from the job record
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row is not None
        ws_id = uuid.UUID(row["workspace_name"])

        # 5. Call the workspace cleanup endpoint
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        assert cleanup_response.status_code == 200, (
            f"Cleanup failed: {cleanup_response.text}"
        )
        cleanup_data = cleanup_response.json()["data"]
        assert cleanup_data["cleanup_status"] in ("cleaning", "cleaned"), (
            f"Unexpected cleanup_status: {cleanup_data['cleanup_status']}"
        )

        # 6. Verify executor.cleanup_workspace was called
        assert len(tracked_executor.calls["cleanup_workspace"]) == 1
        cleanup_call = tracked_executor.calls["cleanup_workspace"][0]
        assert cleanup_call.workspace_id == ws_id

        # 7. Verify the job status is STILL completed (not affected by cleanup)
        job_row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert job_row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_affect_job_status(
        self,
        db_conn,
        tracked_executor: TrackedFakeExecutorPlugin,
        runner_id: uuid.UUID,
    ):
        """When workspace cleanup fails, the job remains ``completed``.

        The cleanup endpoint reports the failure on the workspace record
        (``cleanup_failed`` status), but the job's status must stay
        ``completed``.
        """
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: tracked_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)

        # 1. Submit a job
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/cleanup-fail.git",
                    "task_summary": "Test cleanup failure preserves job status",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 201
        job_data = response.json()["data"]
        assert job_data["status"] == "running"
        job_id = uuid.UUID(job_data["id"])

        # 2. Complete the job to reach terminal state for cleanup.
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            complete_resp = await client.post(
                f"/jobs/{job_id}/complete",
                json={"target_status": "completed"},
            )
        assert complete_resp.status_code == 200

        # 3. Get the workspace ID
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row is not None
        ws_id = uuid.UUID(row["workspace_name"])

        # 4. Verify the workspace exists and is active
        ws_row = await db_conn.fetchrow(
            "SELECT cleanup_status FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row is not None
        assert ws_row["cleanup_status"] == "active"

        # 5. Inject a cleanup failure into the executor
        tracked_executor.cleanup_workspace_failure = RuntimeError(
            "rm -rf workspace failed: Permission denied"
        )

        # 6. Call the workspace cleanup endpoint
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        # The endpoint returns 200 even on failure (it handles the error
        # internally and records cleanup_failed status on the workspace)
        assert cleanup_response.status_code == 200, (
            f"Expected 200 on graceful cleanup failure, got "
            f"{cleanup_response.status_code}: {cleanup_response.text}"
        )
        cleanup_data = cleanup_response.json()["data"]

        # 7. Verify the workspace shows cleanup_failed status
        assert cleanup_data["cleanup_status"] == "cleanup_failed", (
            f"Expected cleanup_failed, got {cleanup_data['cleanup_status']}"
        )
        assert cleanup_data["cleanup_failure_reason"] is not None
        assert "rm -rf workspace failed" in cleanup_data["cleanup_failure_reason"]

        # 8. THE KEY ASSERTION: verify the job status is STILL completed
        job_row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert job_row["status"] == "completed", (
            f"Job status changed to '{job_row['status']}' after cleanup "
            f"failure — must remain 'completed'"
        )

        # 9. Verify the workspace record confirms the failure in the DB
        ws_row_after = await db_conn.fetchrow(
            "SELECT cleanup_status, cleanup_failed_at, cleanup_failure_reason "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row_after["cleanup_status"] == "cleanup_failed"
        assert ws_row_after["cleanup_failed_at"] is not None
        assert ws_row_after["cleanup_failure_reason"] is not None

    @pytest.mark.asyncio
    async def test_cleanup_failure_logs_reason_in_workspace(
        self,
        db_conn,
        tracked_executor: TrackedFakeExecutorPlugin,
        runner_id: uuid.UUID,
    ):
        """Workspace cleanup failure details are logged in the workspace record.

        The reason column should contain the exception type and message,
        proving the failure was captured for observability.
        """
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: tracked_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)

        # 1. Submit a job
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/cleanup-reason.git",
                    "task_summary": "Test cleanup failure reason",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # 2. Get workspace ID
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])

        # 3. Inject cleanup failure
        tracked_executor.cleanup_workspace_failure = RuntimeError(
            "Disk quota exceeded"
        )

        # 4. Call cleanup
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        assert cleanup_response.status_code == 200

        # 5. Verify the failure reason is stored
        cleanup_data = cleanup_response.json()["data"]
        assert cleanup_data["cleanup_failure_reason"] is not None
        assert "RuntimeError" in cleanup_data["cleanup_failure_reason"]
        assert "Disk quota exceeded" in cleanup_data["cleanup_failure_reason"]

        # 6. Verify the failed_at timestamp is set
        assert cleanup_data["cleanup_failed_at"] is not None

        # 7. Double-check via direct DB query
        ws_row = await db_conn.fetchrow(
            "SELECT cleanup_failure_reason, cleanup_failed_at "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row["cleanup_failure_reason"] is not None
        assert "Disk quota exceeded" in ws_row["cleanup_failure_reason"]
        assert ws_row["cleanup_failed_at"] is not None

    @pytest.mark.asyncio
    async def test_cleanup_success_releases_port(
        self,
        db_conn,
        tracked_executor: TrackedFakeExecutorPlugin,
        runner_id: uuid.UUID,
    ):
        """After successful cleanup, the workspace port is released (set to NULL).

        This verifies that cleanup includes port deallocation so the
        port becomes available for reuse.
        """
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: tracked_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)

        # 1. Submit a job
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/port-release.git",
                    "task_summary": "Test port release on cleanup",
                    "runner_id": str(runner_id),
                },
            )

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # 2. Get workspace and capture the allocated port
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])

        ws_before = await db_conn.fetchrow(
            "SELECT port FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_before["port"] is not None, "Port should be allocated after job"

        # 3. Clean up (should succeed)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        assert cleanup_response.status_code == 200
        cleanup_data = cleanup_response.json()["data"]

        # 4. Verify port is released (NULL)
        assert cleanup_data["port"] is None, (
            f"Port should be NULL after cleanup, got {cleanup_data['port']}"
        )

        # 5. Confirm via direct DB query
        ws_after = await db_conn.fetchrow(
            "SELECT port FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_after["port"] is None, "Port must be NULL in DB after cleanup"
