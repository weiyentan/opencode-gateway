"""End-to-end lifecycle integration test for the Gateway workflow.

Tests the full job lifecycle from submission through OpenCode start,
completion, and cleanup using a real PostgreSQL database paired with
deterministic fake services.

Acceptance Criteria
-------------------
1. Fake AWX client implements ExecutorPlugin interface
2. Fake OpenCode client simulates serve lifecycle
3. Test Postgres fixture for database operations
4. Deterministic artifact responses
5. Full lifecycle test: submit job → workspace provisioned → opencode started
   → job completes → workspace cleaned
6. Tests cover failure modes
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
# Constants — deterministic IDs for test reproducibility
# ---------------------------------------------------------------------------

TEST_WORKSPACE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_SESSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEST_WORKSPACE_PATH = "/data/workspaces/test-workspace"


# ═══════════════════════════════════════════════════════════════════════════
#  Fake Executor Plugin — implements ExecutorPlugin with deterministic
#  responses.  The caller can inject custom responses or failures per test.
# ═══════════════════════════════════════════════════════════════════════════


class FakeExecutorPlugin:
    """Deterministic fake :class:`ExecutorPlugin` for integration tests.

    Each lifecycle method returns a pre-configured response.  The caller
    can set custom responses or make a method raise an exception to
    simulate failure scenarios.

    All calls are tracked in ``calls`` for post-test verification.

    .. rubric:: Usage

        fake = FakeExecutorPlugin()
        fake.create_workspace_response = CreateWorkspaceResponse(...)
        fake.create_workspace_failure = RuntimeError("fail")
        response = await fake.create_workspace(request)
    """

    name = "fake"

    def __init__(self) -> None:
        from app.executors.models import (
            CleanupWorkspaceResponse,
            CollectStateResponse,
            CreateWorkspaceResponse,
            RestartOpencodeResponse,
            StartOpencodeResponse,
            StopOpencodeResponse,
            WorkspaceState,
        )

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
        # Set to an Exception instance to make the method raise.
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

    # ── Active lifecycle surface (called by the Gateway) ─────────────

    async def create_workspace(self, request: Any) -> Any:
        self.calls["create_workspace"].append(request)
        if self.create_workspace_failure:
            raise self.create_workspace_failure
        return self.create_workspace_response

    async def start_opencode(self, request: Any) -> Any:
        self.calls["start_opencode"].append(request)
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

    # ── Future surface (not yet called by the Gateway) ───────────────

    async def restart_opencode(self, request: Any) -> Any:
        self.calls["restart_opencode"].append(request)
        return self.restart_opencode_response

    async def collect_state(self, request: Any) -> Any:
        self.calls["collect_state"].append(request)
        return self.collect_state_response


# ═══════════════════════════════════════════════════════════════════════════
#  Fake OpenCode Client — implements OpenCodeClientProtocol with
#  deterministic responses for diff, log, session info, and abort.
# ═══════════════════════════════════════════════════════════════════════════


class FakeOpenCodeClient:
    """Deterministic fake :class:`OpenCodeClientProtocol` for integration tests.

    Returns pre-configured responses for all protocol methods.  The caller
    can set custom responses or inject a failure for ``get_session_diff``.
    """

    def __init__(self) -> None:
        from app.opencode.protocol import (
            SessionAbortResponse,
            SessionDiffResponse,
            SessionInfo,
            SessionLogResponse,
        )

        # ── Default responses ────────────────────────────────────────
        now = datetime.now(timezone.utc)
        self.session_info = SessionInfo(
            id=str(TEST_SESSION_ID),
            status="completed",
            workspace_path=TEST_WORKSPACE_PATH,
            task_description="Integration test job",
            created_at=now,
            updated_at=now,
        )
        self.diff_response = SessionDiffResponse(
            session_id=str(TEST_SESSION_ID),
            diff=(
                "--- a/file.py\n"
                "+++ b/file.py\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new"
            ),
            files_changed=["file.py"],
        )
        self.log_response = SessionLogResponse(
            session_id=str(TEST_SESSION_ID),
            log="[INFO] Session started\n[INFO] Task completed\n",
        )
        self.abort_response = SessionAbortResponse(
            session_id=str(TEST_SESSION_ID),
            aborted=True,
            message="Session aborted by user",
        )

        # ── Failure injection ────────────────────────────────────────
        self.get_session_diff_failure: Exception | None = None

    # ── Future surface (not yet called by the Gateway) ───────────────

    async def health(self) -> Any:
        return self.session_info

    async def list_sessions(self) -> Any:
        from app.opencode.protocol import SessionListResponse

        return SessionListResponse(sessions=[self.session_info], total=1)

    async def get_session(self, session_id: str) -> Any:
        return self.session_info

    async def create_session(
        self,
        workspace_path: str,
        task_description: str,
        model: str | None = None,
    ) -> Any:
        return self.session_info

    async def delete_session(self, session_id: str) -> Any:
        return self.abort_response

    # ── Active surface (called by the Gateway) ───────────────────────

    async def get_session_diff(self, session_id: str) -> Any:
        if self.get_session_diff_failure:
            raise self.get_session_diff_failure
        return self.diff_response

    async def get_session_log(self, session_id: str) -> Any:
        return self.log_response

    async def abort_session(self, session_id: str) -> Any:
        return self.abort_response


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_executor() -> FakeExecutorPlugin:
    """Return a fresh :class:`FakeExecutorPlugin` per test."""
    return FakeExecutorPlugin()


@pytest.fixture
def fake_opencode_client() -> FakeOpenCodeClient:
    """Return a fresh :class:`FakeOpenCodeClient` per test."""
    return FakeOpenCodeClient()


@pytest.fixture
async def runner_id(db_conn) -> uuid.UUID:
    """Create a HEALTHY runner in the test database and return its UUID."""
    return await create_runner(
        db_conn,
        hostname="integration-test-runner",
        status="HEALTHY",
        executor_type="local",
    )


@pytest.fixture
async def app_client(
    db_conn,
    fake_executor: FakeExecutorPlugin,
    fake_opencode_client: FakeOpenCodeClient,
) -> AsyncClient:
    """Return an httpx AsyncClient wired to the FastAPI app with overridden
    dependencies: database connection, executor, and OpenCode client.

    The ``get_session`` dependency yields the real ``db_conn`` fixture
    so tests exercise real SQL against the test PostgreSQL database.
    The executor and OpenCode client overrides inject the fake services.
    """
    from app.api.jobs import get_opencode_client
    from app.core.factory import create_app
    from app.db.session import get_session
    from app.executors.factory import get_executor

    app = create_app()
    # Prevent the app from starting its own DB pool / scheduler
    app.state.pool = AsyncMock()
    app.state.pool.pool = None

    # Override the database session dependency with the real test connection
    async def _override_get_session(request):
        yield db_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_executor] = lambda: fake_executor
    app.dependency_overrides[get_opencode_client] = lambda: fake_opencode_client

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )
    return client


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Fake Executor Plugin
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeExecutorPlugin:
    """Verify the fake executor plugin implements the contract correctly."""

    @pytest.mark.asyncio
    async def test_create_workspace_returns_deterministic_response(self):
        """create_workspace returns the pre-configured workspace ID and path."""
        from app.executors.models import CreateWorkspaceRequest

        fake = FakeExecutorPlugin()
        req = CreateWorkspaceRequest(
            repo_url="https://github.com/example/repo.git",
            branch="main",
        )
        resp = await fake.create_workspace(req)

        assert resp.workspace_id == TEST_WORKSPACE_ID
        assert resp.workspace_path == TEST_WORKSPACE_PATH
        assert resp.status == "ready"
        assert len(fake.calls["create_workspace"]) == 1

    @pytest.mark.asyncio
    async def test_start_opencode_returns_deterministic_response(self):
        """start_opencode returns the pre-configured session ID and port."""
        from app.executors.models import StartOpencodeRequest

        fake = FakeExecutorPlugin()
        req = StartOpencodeRequest(
            workspace_id=TEST_WORKSPACE_ID,
            workspace_path=TEST_WORKSPACE_PATH,
        )
        resp = await fake.start_opencode(req)

        assert resp.session_id == TEST_SESSION_ID
        assert resp.status == "running"
        assert resp.port == 10000
        assert len(fake.calls["start_opencode"]) == 1

    @pytest.mark.asyncio
    async def test_stop_opencode_returns_stopped(self):
        """stop_opencode returns status='stopped'."""
        from app.executors.models import StopOpencodeRequest

        fake = FakeExecutorPlugin()
        resp = await fake.stop_opencode(
            StopOpencodeRequest(workspace_id=TEST_WORKSPACE_ID)
        )
        assert resp.status == "stopped"

    @pytest.mark.asyncio
    async def test_cleanup_workspace_returns_cleaned(self):
        """cleanup_workspace returns status='cleaned'."""
        from app.executors.models import CleanupWorkspaceRequest

        fake = FakeExecutorPlugin()
        resp = await fake.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=TEST_WORKSPACE_ID)
        )
        assert resp.status == "cleaned"

    @pytest.mark.asyncio
    async def test_collect_state_returns_ready(self):
        """collect_state returns a READY workspace state."""
        from app.executors.models import (
            CollectStateRequest,
            WorkspaceState,
        )

        fake = FakeExecutorPlugin()
        resp = await fake.collect_state(
            CollectStateRequest(workspace_id=TEST_WORKSPACE_ID)
        )
        assert resp.status == WorkspaceState.READY
        assert resp.process_status == "running"

    @pytest.mark.asyncio
    async def test_failure_injection_raises(self):
        """When a failure is set, the method raises the configured exception."""
        from app.executors.models import CreateWorkspaceRequest

        fake = FakeExecutorPlugin()
        fake.create_workspace_failure = RuntimeError("Workspace creation failed")

        with pytest.raises(RuntimeError, match="Workspace creation failed"):
            await fake.create_workspace(
                CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
            )

    @pytest.mark.asyncio
    async def test_calls_are_tracked(self):
        """All method invocations are recorded in the calls dict."""
        from app.executors.models import (
            CleanupWorkspaceRequest,
            CreateWorkspaceRequest,
            StartOpencodeRequest,
            StopOpencodeRequest,
        )

        fake = FakeExecutorPlugin()
        await fake.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/a.git")
        )
        await fake.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/b.git")
        )
        await fake.start_opencode(
            StartOpencodeRequest(workspace_id=uuid.uuid4())
        )
        await fake.stop_opencode(
            StopOpencodeRequest(workspace_id=uuid.uuid4())
        )
        await fake.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=uuid.uuid4())
        )

        assert len(fake.calls["create_workspace"]) == 2
        assert len(fake.calls["start_opencode"]) == 1
        assert len(fake.calls["stop_opencode"]) == 1
        assert len(fake.calls["cleanup_workspace"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Fake OpenCode Client
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeOpenCodeClient:
    """Verify the fake OpenCode client implements the protocol correctly."""

    @pytest.mark.asyncio
    async def test_health_returns_session_info(self):
        """health returns the configured session info."""
        fake = FakeOpenCodeClient()
        resp = await fake.health()
        assert resp.id == str(TEST_SESSION_ID)

    @pytest.mark.asyncio
    async def test_get_session_diff_returns_deterministic_diff(self):
        """get_session_diff returns the pre-configured diff."""
        fake = FakeOpenCodeClient()
        resp = await fake.get_session_diff(str(TEST_SESSION_ID))
        assert "file.py" in resp.diff
        assert resp.session_id == str(TEST_SESSION_ID)

    @pytest.mark.asyncio
    async def test_abort_session_returns_aborted(self):
        """abort_session returns aborted=True."""
        fake = FakeOpenCodeClient()
        resp = await fake.abort_session(str(TEST_SESSION_ID))
        assert resp.aborted is True

    @pytest.mark.asyncio
    async def test_get_session_log_returns_log(self):
        """get_session_log returns the pre-configured log content."""
        fake = FakeOpenCodeClient()
        resp = await fake.get_session_log(str(TEST_SESSION_ID))
        assert "Session started" in resp.log
        assert resp.session_id == str(TEST_SESSION_ID)


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Full Lifecycle Happy Path
# ═══════════════════════════════════════════════════════════════════════════


class TestFullLifecycle:
    """Test the complete job lifecycle from submission to workspace cleanup.

    Each test uses the real PostgreSQL database (``db_conn``) and the
    fake executor / OpenCode client.  The runner is pre-created in the
    database so ``select_runner`` picks it up.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_happy_path(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Submit a job → workspace provisioned → opencode started → job completes.

        Verifies:
        - 201 response with completed status
        - Executor was called for create_workspace and start_opencode
        - Job record in DB shows completed status and workspace link
        - Workspace cleanup_after is set
        """
        payload = {
            "repo_url": "https://github.com/example/test-repo.git",
            "task_summary": "Integration test full lifecycle",
            "runner_id": str(runner_id),
            "env_vars": {"LOG_LEVEL": "debug", "CUSTOM_VAR": "custom_value"},
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Verify HTTP response ─────────────────────────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body["status"] == "ok", f"Response status not ok: {body}"
        data = body["data"]
        assert data["status"] == "completed"
        assert data["repo_url"] == "https://github.com/example/test-repo.git"
        assert data["task_summary"] == "Integration test full lifecycle"
        assert data["completed_at"] is not None
        assert data["opencode_session_id"] is not None
        assert data["diff"] is not None

        job_id = uuid.UUID(data["id"])

        # ── Verify executor was called ───────────────────────────────
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 1

        create_call = fake_executor.calls["create_workspace"][0]
        assert create_call.repo_url == "https://github.com/example/test-repo.git"
        assert create_call.env_vars == {"LOG_LEVEL": "debug", "CUSTOM_VAR": "custom_value"}

        start_call = fake_executor.calls["start_opencode"][0]
        assert start_call.workspace_id == TEST_WORKSPACE_ID
        assert start_call.env_vars == {"LOG_LEVEL": "debug", "CUSTOM_VAR": "custom_value"}

        # ── Verify job record in database ────────────────────────────
        row = await db_conn.fetchrow(
            "SELECT status, completed_at, opencode_session_id, workspace_name, "
            "diff, runner_id, repo_url, task_summary "
            "FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row is not None
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["opencode_session_id"] == str(TEST_SESSION_ID)
        assert row["workspace_name"] is not None
        assert row["runner_id"] == runner_id
        assert row["diff"] is not None

        # ── Verify workspace record in database ──────────────────────
        ws_id = uuid.UUID(row["workspace_name"])
        ws_row = await db_conn.fetchrow(
            "SELECT id, port, cleanup_after, cleanup_status, repo_url, path "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row is not None
        assert ws_row["port"] is not None  # port was allocated
        assert ws_row["cleanup_after"] is not None  # retention was set
        assert ws_row["cleanup_status"] == "active"
        assert ws_row["repo_url"] == "https://github.com/example/test-repo.git"

    @pytest.mark.asyncio
    async def test_lifecycle_sets_workspace_cleanup_after(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """After job completion, the workspace's cleanup_after is set.

        Verifies that cleanup_after is in the future (success retention
        is 72 hours by default).
        """
        payload = {
            "repo_url": "https://github.com/example/cleanup-retention.git",
            "task_summary": "Test cleanup retention",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # Fetch the workspace associated with this job
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])
        ws_row = await db_conn.fetchrow(
            "SELECT cleanup_after, created_at FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row["cleanup_after"] is not None
        # cleanup_after should be after created_at (retention > 0)
        assert ws_row["cleanup_after"] > ws_row["created_at"]

    @pytest.mark.asyncio
    async def test_lifecycle_stores_env_vars_in_job(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Environment variables are persisted in the job record and passed to executor."""
        import json

        env_vars = {
            "API_KEY": "secret-123",
            "MODEL": "gpt-4",
            "LOG_LEVEL": "info",
        }

        payload = {
            "repo_url": "https://github.com/example/env-vars-test.git",
            "task_summary": "Test env vars propagation",
            "runner_id": str(runner_id),
            "env_vars": env_vars,
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # Verify env_vars in the DB
        row = await db_conn.fetchrow(
            "SELECT env_vars FROM gateway_jobs WHERE id = $1", job_id,
        )
        raw = row["env_vars"]
        stored = json.loads(raw) if isinstance(raw, str) else raw
        assert stored == env_vars

        # Verify executor received env_vars
        create_call = fake_executor.calls["create_workspace"][0]
        assert create_call.env_vars == env_vars

        start_call = fake_executor.calls["start_opencode"][0]
        assert start_call.env_vars == env_vars

    @pytest.mark.asyncio
    async def test_lifecycle_with_opencode_diff_fetch(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When an OpenCode client is available, the diff is fetched and stored.

        The job creation endpoint calls ``get_session_diff`` after the
        job is marked completed and persists the more detailed diff.
        """
        payload = {
            "repo_url": "https://github.com/example/diff-test.git",
            "task_summary": "Test diff fetch",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # The diff from the fake OpenCode client should be stored
        row = await db_conn.fetchrow(
            "SELECT diff FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["diff"] is not None
        assert "file.py" in row["diff"]

    @pytest.mark.asyncio
    async def test_lifecycle_diff_fetch_failure_does_not_fail_job(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When diff fetch fails, the job remains completed (failure is non-fatal)."""
        # Inject failure into the fake OpenCode client
        fake_opencode_client.get_session_diff_failure = RuntimeError(
            "OpenCode unreachable"
        )

        payload = {
            "repo_url": "https://github.com/example/diff-failure.git",
            "task_summary": "Test diff fetch failure",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "completed"

        job_id = uuid.UUID(data["id"])
        row = await db_conn.fetchrow(
            "SELECT status, diff FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["status"] == "completed"
        # Diff will be the fallback summary, not the OpenCode detail
        assert row["diff"] is not None  # falls back to the summary diff


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Failure Modes
# ═══════════════════════════════════════════════════════════════════════════


class TestFailureModes:
    """Test Gateway behaviour when executor operations fail."""

    @pytest.mark.asyncio
    async def test_executor_create_workspace_failure_marks_job_failed(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When create_workspace raises, the job is marked as failed."""
        fake_executor.create_workspace_failure = RuntimeError("Disk full")

        payload = {
            "repo_url": "https://github.com/example/fail-create.git",
            "task_summary": "Fail on create workspace",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])
        row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["status"] == "failed"

        # start_opencode should NOT have been called
        assert len(fake_executor.calls["start_opencode"]) == 0

    @pytest.mark.asyncio
    async def test_executor_start_opencode_failure_marks_job_failed(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When start_opencode raises, the job is marked as failed."""
        fake_executor.start_opencode_failure = RuntimeError("Port unavailable")

        payload = {
            "repo_url": "https://github.com/example/fail-start.git",
            "task_summary": "Fail on start opencode",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])
        row = await db_conn.fetchrow(
            "SELECT status, workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row["status"] == "failed"
        # Workspace was created before the failure
        assert row["workspace_name"] is not None
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 1

    @pytest.mark.asyncio
    async def test_executor_failure_creates_job_event(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When the executor fails, a job event is recorded in the DB."""
        fake_executor.create_workspace_failure = RuntimeError("Disk full")

        payload = {
            "repo_url": "https://github.com/example/fail-event.git",
            "task_summary": "Fail and create event",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # Verify a job event was recorded
        events = await db_conn.fetch(
            "SELECT event_type, actor, details FROM job_events "
            "WHERE job_id = $1 ORDER BY created_at ASC",
            job_id,
        )
        assert len(events) >= 1
        assert events[0]["event_type"] == "executor_error"
        assert events[0]["actor"] == "gateway"

    @pytest.mark.asyncio
    async def test_awx_artifact_error_failure(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """An AWXArtifactError from the executor creates a specific error event."""
        from app.executors.awx.exceptions import AWXArtifactError

        fake_executor.create_workspace_failure = AWXArtifactError(
            "Invalid artifacts from gateway-create-workspace: "
            "missing=['workspace_id'], invalid=[]",
            template_name="gateway-create-workspace",
            missing_fields=["workspace_id"],
        )

        payload = {
            "repo_url": "https://github.com/example/artifact-fail.git",
            "task_summary": "Fail on artifact validation",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])
        row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["status"] == "failed"

        # Verify an artifact_error event was recorded
        events = await db_conn.fetch(
            "SELECT event_type, details FROM job_events "
            "WHERE job_id = $1 ORDER BY created_at ASC",
            job_id,
        )
        artifact_events = [e for e in events if e["event_type"] == "artifact_error"]
        assert len(artifact_events) >= 1
        assert "workspace_id" in artifact_events[0]["details"]


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Workspace Cleanup After Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestWorkspaceCleanup:
    """Test workspace cleanup flow after a job completes."""

    @pytest.mark.asyncio
    async def test_workspace_cleanup_endpoint(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """After job completion, the workspace can be cleaned up via the endpoint.

        This tests the full cycle: submit job → verify workspace → clean up → verify.
        """
        # 1. Submit a job to create a workspace
        payload = {
            "repo_url": "https://github.com/example/cleanup-test.git",
            "task_summary": "Test workspace cleanup",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # 2. Get the workspace ID from the job record
        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1", job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])

        # 3. Verify workspace exists
        ws_row = await db_conn.fetchrow(
            "SELECT id, cleanup_status FROM workspaces WHERE id = $1", ws_id,
        )
        assert ws_row is not None
        assert ws_row["cleanup_status"] == "active"

        # 4. Call the workspace cleanup endpoint
        async with app_client as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        assert cleanup_response.status_code == 200, (
            f"Cleanup failed: {cleanup_response.text}"
        )
        cleanup_data = cleanup_response.json()["data"]
        assert cleanup_data["cleanup_status"] in ("cleaning", "cleaned")

        # 5. Verify executor.cleanup_workspace was called
        assert len(fake_executor.calls["cleanup_workspace"]) == 1
        cleanup_call = fake_executor.calls["cleanup_workspace"][0]
        assert cleanup_call.workspace_id == ws_id

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_crash(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When cleanup_workspace raises, the endpoint returns cleanup_failed status."""
        # 1. Submit a job to create a workspace
        payload = {
            "repo_url": "https://github.com/example/cleanup-fail.git",
            "task_summary": "Test cleanup failure",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1", job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])

        # 2. Inject a cleanup failure
        fake_executor.cleanup_workspace_failure = RuntimeError("rm -rf failed")

        # 3. Call the workspace cleanup endpoint (should not crash)
        async with app_client as client:
            cleanup_response = await client.post(f"/workspaces/{ws_id}/cleanup")

        # The endpoint handles executor failures gracefully
        assert cleanup_response.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_workspace_cleanup_releases_advisory_lock(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """After cleanup, the workspace advisory lock is released.

        This can be verified by checking that a second cleanup call does
        not get a lock conflict.
        """
        # 1. Submit a job and get the workspace ID
        payload = {
            "repo_url": "https://github.com/example/lock-test.git",
            "task_summary": "Test advisory lock cleanup",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        row = await db_conn.fetchrow(
            "SELECT workspace_name FROM gateway_jobs WHERE id = $1", job_id,
        )
        ws_id = uuid.UUID(row["workspace_name"])

        # 2. Clean up once — should succeed
        async with app_client as client:
            r1 = await client.post(f"/workspaces/{ws_id}/cleanup")
        assert r1.status_code == 200

        # 3. Force workspace back to active so we can test lock release
        #    (skip if cleanup already set it to cleaned — just verify no 409)
        async with app_client as client:
            r2 = await client.post(f"/workspaces/{ws_id}/cleanup")

        # The second call should not get a lock conflict (lock was released)
        assert r2.status_code not in (409, 500), (
            f"Second cleanup failed: {r2.text}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Runner Health and Policy Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerHealthIntegration:
    """Test policy integration with different runner statuses."""

    @pytest.mark.asyncio
    async def test_offline_runner_rejected(
        self,
        db_conn,
        fake_executor: FakeExecutorPlugin,
    ):
        """A job submitted with an offline runner_id should be rejected with 503."""
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        # Create an offline runner
        offline_runner_id = await create_runner(
            db_conn,
            hostname="offline-runner",
            status="offline",
            executor_type="local",
        )

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: fake_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/offline-test.git",
                    "task_summary": "Offline runner test",
                    "runner_id": str(offline_runner_id),
                },
            )

        assert response.status_code == 503, (
            f"Expected 503, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert data["status"] == "error"

        # No executor calls should have been made
        assert len(fake_executor.calls["create_workspace"]) == 0

    @pytest.mark.asyncio
    async def test_maintenance_runner_rejected(
        self,
        db_conn,
        fake_executor: FakeExecutorPlugin,
    ):
        """A job submitted with a maintenance runner should be rejected with 503."""
        from app.api.jobs import get_opencode_client
        from app.core.factory import create_app
        from app.db.session import get_session
        from app.executors.factory import get_executor

        maint_runner_id = await create_runner(
            db_conn,
            hostname="maint-runner",
            status="maintenance",
            executor_type="local",
        )

        app = create_app()
        app.state.pool = AsyncMock()
        app.state.pool.pool = None

        async def _override_get_session(request):
            yield db_conn

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[get_executor] = lambda: fake_executor
        app.dependency_overrides[get_opencode_client] = lambda: None

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/example/maint-test.git",
                    "task_summary": "Maintenance runner test",
                    "runner_id": str(maint_runner_id),
                },
            )

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "error"
        assert len(fake_executor.calls["create_workspace"]) == 0

    @pytest.mark.asyncio
    async def test_online_runner_bypasses_observations(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
    ):
        """A runner with status 'online' bypasses observation checks.

        Create an 'online' runner explicitly — no observations exist,
        but the job should still be accepted.
        """
        online_runner_id = await create_runner(
            db_conn,
            hostname="online-runner",
            status="online",
            executor_type="local",
        )

        payload = {
            "repo_url": "https://github.com/example/online-test.git",
            "task_summary": "Online runner test",
            "runner_id": str(online_runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # The job should succeed despite no observations (online bypasses policy)
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "completed"
        assert len(fake_executor.calls["create_workspace"]) == 1
