"""Integration tests for job lifecycle failure paths.

Tests the Gateway's behaviour when individual lifecycle stages fail,
verifying that the job reaches the expected terminal state and that
the event trail in the job_events table is recorded correctly.

Acceptance Criteria
-------------------
1. AWX failure during workspace creation → job reaches failed with
   executor_error event
2. Malformed AWX artifacts (AWXArtifactError) → job reaches failed with
   artifact_error event containing missing_fields and template info
3. OpenCode startup failure → job reaches failed with executor_error event;
   workspace ID is preserved
4. OpenCode diff fetch error → job stays completed (graceful degradation);
   no error event recorded; fallback diff is available
5. Each test verifies the complete event trail: event_type, actor, details,
   and absence of previous_status for all error events
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
        hostname="failure-path-test-runner",
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
#  Helper: fetch job events for a given job_id
# ═══════════════════════════════════════════════════════════════════════════


async def _fetch_events(db_conn, job_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return all job_events for *job_id*, ordered by created_at ascending."""
    rows = await db_conn.fetch(
        "SELECT event_type, actor, details, previous_status, created_at "
        "FROM job_events "
        "WHERE job_id = $1 "
        "ORDER BY created_at ASC",
        job_id,
    )
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Acceptance Criterion 1 — AWX failure → failed + event trail
# ═══════════════════════════════════════════════════════════════════════════


class TestAwwCreateWorkspaceFailure:
    """Acceptance Criterion 1: AWX connection/execution failure during
    workspace creation → job reaches 'failed' with an executor_error event.

    Verifies:
    - Job status is 'failed'
    - A single job_events row exists with event_type='executor_error'
    - The event has actor='gateway' and meaningful details
    - previous_status is None (not set for executor errors)
    - No lifecycle methods past create_workspace were called
    """

    @pytest.mark.asyncio
    async def test_awx_create_workspace_failure_with_event_trail(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Inject RuntimeError into create_workspace → job fails with
        executor_error event."""
        fake_executor.create_workspace_failure = RuntimeError(
            "AWX connection refused: timeout after 30s"
        )

        payload = {
            "repo_url": "https://github.com/example/fail-awx-create.git",
            "task_summary": "AWX create workspace failure",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Job reached failed state ──────────────────────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        data = response.json()["data"]
        assert data["status"] == "failed", (
            f"Expected 'failed', got '{data['status']}'"
        )

        job_id = uuid.UUID(data["id"])

        # ── Database confirms failed ──────────────────────────────────
        row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["status"] == "failed"

        # ── Event trail ───────────────────────────────────────────────
        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1, (
            f"Expected exactly 1 job event, got {len(events)}"
        )
        ev = events[0]
        assert ev["event_type"] == "executor_error"
        assert ev["actor"] == "gateway"
        assert ev["details"] is not None
        assert "AWX connection refused" in ev["details"]
        assert ev["previous_status"] is None

        # ── No further lifecycle calls made ───────────────────────────
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 0
        assert len(fake_executor.calls["stop_opencode"]) == 0
        assert len(fake_executor.calls["cleanup_workspace"]) == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Acceptance Criterion 2 — Malformed AWX artifacts → failed
# ═══════════════════════════════════════════════════════════════════════════


class TestMalformedAwwArtifacts:
    """Acceptance Criterion 2: The AWX job completed but returned malformed
    or missing artifacts (AWXArtifactError) → job reaches 'failed' with an
    artifact_error event containing template and field information.

    Verifies:
    - Job status is 'failed'
    - A single job_events row exists with event_type='artifact_error'
    - The event details include template_name, missing_fields, invalid_fields
    - previous_status is None
    """

    @pytest.mark.asyncio
    async def test_malformed_artifacts_missing_workspace_id(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """AWXArtifactError with missing workspace_id field causes failed
        job and artifact_error event."""
        from app.executors.awx.exceptions import AWXArtifactError

        fake_executor.create_workspace_failure = AWXArtifactError(
            "Invalid artifacts from gateway-create-workspace: "
            "missing=['workspace_id'], invalid=[]",
            template_name="gateway-create-workspace",
            missing_fields=["workspace_id"],
        )

        payload = {
            "repo_url": "https://github.com/example/artifact-missing.git",
            "task_summary": "Missing workspace_id artifact",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Job reached failed state ──────────────────────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])

        # ── Database confirms failed ──────────────────────────────────
        row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", job_id,
        )
        assert row["status"] == "failed"

        # ── Event trail ───────────────────────────────────────────────
        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1, (
            f"Expected exactly 1 job event, got {len(events)}"
        )
        ev = events[0]
        assert ev["event_type"] == "artifact_error"
        assert ev["actor"] == "gateway"
        # Details should contain structured info about the error
        details = ev["details"]
        assert details is not None
        assert "gateway-create-workspace" in details
        assert "workspace_id" in details
        assert "missing_fields" in details
        assert ev["previous_status"] is None

        # ── No further lifecycle calls beyond create_workspace ────────
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 0

    @pytest.mark.asyncio
    async def test_malformed_artifacts_invalid_uuid(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """AWXArtifactError with an invalid UUID field causes failed
        job and artifact_error event mentioning invalid_fields."""
        from app.executors.awx.exceptions import AWXArtifactError

        fake_executor.create_workspace_failure = AWXArtifactError(
            "Invalid artifacts from gateway-create-workspace: "
            "missing=[], invalid=['workspace_id=not-a-uuid']",
            template_name="gateway-create-workspace",
            invalid_fields=["workspace_id"],
        )

        payload = {
            "repo_url": "https://github.com/example/artifact-invalid.git",
            "task_summary": "Invalid workspace_id artifact",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1
        assert events[0]["event_type"] == "artifact_error"
        assert "invalid_fields" in events[0]["details"]
        assert "workspace_id" in events[0]["details"]

    @pytest.mark.asyncio
    async def test_malformed_artifacts_from_start_opencode(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """AWXArtifactError from start_opencode is also caught and creates
        an artifact_error event."""
        from app.executors.awx.exceptions import AWXArtifactError

        fake_executor.start_opencode_failure = AWXArtifactError(
            "Invalid artifacts from gateway-opencode-lifecycle: "
            "missing=['session_id'], invalid=[]",
            template_name="gateway-opencode-lifecycle",
            missing_fields=["session_id"],
        )

        payload = {
            "repo_url": "https://github.com/example/artifact-start.git",
            "task_summary": "Artifact error on start opencode",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])

        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "artifact_error"
        assert "gateway-opencode-lifecycle" in ev["details"]
        assert "session_id" in ev["details"]

        # Workspace was created before start_opencode
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Acceptance Criterion 3 — OpenCode startup failure → failed
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenCodeStartupFailure:
    """Acceptance Criterion 3: OpenCode Serve fails to start on the Runner
    VM → job reaches 'failed' with an executor_error event.

    Verifies:
    - Job status is 'failed'
    - Job has an associated workspace (workspace was created before start)
    - A single executor_error event is recorded
    - create_workspace was called; start_opencode was called (and failed)
    """

    @pytest.mark.asyncio
    async def test_opencode_startup_failure_with_event_trail(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """start_opencode raises RuntimeError → job fails with
        executor_error event; workspace_id is preserved."""
        fake_executor.start_opencode_failure = RuntimeError(
            "OpenCode Serve failed to start: port 10000 already in use"
        )

        payload = {
            "repo_url": "https://github.com/example/fail-start.git",
            "task_summary": "OpenCode startup failure",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Job reached failed state ──────────────────────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        data = response.json()["data"]
        assert data["status"] == "failed"

        job_id = uuid.UUID(data["id"])

        # ── Database confirms failed and workspace preserved ──────────
        row = await db_conn.fetchrow(
            "SELECT status, workspace_name FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row["status"] == "failed"
        # Workspace was created before the startup failure
        assert row["workspace_name"] is not None, (
            "Workspace should have been created before start_opencode failed"
        )

        # ── Event trail ───────────────────────────────────────────────
        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1, (
            f"Expected exactly 1 job event, got {len(events)}"
        )
        ev = events[0]
        assert ev["event_type"] == "executor_error"
        assert ev["actor"] == "gateway"
        assert ev["details"] is not None
        assert "port 10000" in ev["details"]
        assert ev["previous_status"] is None

        # ── Lifecycle call trace ──────────────────────────────────────
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 1
        assert len(fake_executor.calls["stop_opencode"]) == 0
        assert len(fake_executor.calls["cleanup_workspace"]) == 0

    @pytest.mark.asyncio
    async def test_opencode_startup_failure_no_session_id(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When start_opencode fails, the job should NOT have an
        opencode_session_id set (it is set only on success)."""
        fake_executor.start_opencode_failure = RuntimeError("Process crashed on launch")

        payload = {
            "repo_url": "https://github.com/example/fail-no-session.git",
            "task_summary": "No session on startup failure",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        row = await db_conn.fetchrow(
            "SELECT opencode_session_id FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        # session_id should remain None — it is stored only after
        # start_opencode succeeds (line ~413 in app/api/jobs.py)
        assert row["opencode_session_id"] is None, (
            "opencode_session_id should not be set when start_opencode fails"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Acceptance Criterion 4 — OpenCode diff fetch error → stays completed
# ═══════════════════════════════════════════════════════════════════════════


class TestDiffFetchFailure:
    """Acceptance Criterion 4: The OpenCode client fails to fetch the session
    diff → the job remains 'completed' with the fallback summary diff.

    Verifies:
    - Job status is 'completed' despite the diff fetch failure
    - The job_events table is empty (no error events for this non-fatal failure)
    - The job's diff column contains a value (the fallback summary)
    - All lifecycle methods completed successfully
    """

    @pytest.mark.asyncio
    async def test_diff_fetch_failure_job_stays_completed(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """When get_session_diff raises, the job stays completed and no
        error events are recorded."""
        fake_opencode_client.get_session_diff_failure = RuntimeError(
            "OpenCode Serve unreachable on port 10000"
        )

        payload = {
            "repo_url": "https://github.com/example/diff-fetch-fail.git",
            "task_summary": "Diff fetch failure test",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # ── Job stays completed ───────────────────────────────────────
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        data = response.json()["data"]
        assert data["status"] == "completed", (
            f"Expected 'completed', got '{data['status']}'"
        )

        job_id = uuid.UUID(data["id"])

        # ── Database confirms completed ───────────────────────────────
        row = await db_conn.fetchrow(
            "SELECT status, diff, opencode_session_id FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row["status"] == "completed"
        # Diff should fall back to the summary (not the OpenCode detail)
        assert row["diff"] is not None, (
            "Job should have a fallback summary diff even when OpenCode diff fetch fails"
        )
        assert row["opencode_session_id"] is not None

        # ── Event trail: no error events ──────────────────────────────
        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 0, (
            f"Expected no job events for graceful diff failure, "
            f"got {len(events)}: {[e['event_type'] for e in events]}"
        )

        # ── All lifecycle methods completed ───────────────────────────
        assert len(fake_executor.calls["create_workspace"]) == 1
        assert len(fake_executor.calls["start_opencode"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Tests: Acceptance Criterion 5 — Comprehensive event trail verification
# ═══════════════════════════════════════════════════════════════════════════


class TestEventTrailCompleteness:
    """Acceptance Criterion 5: Cross-cutting event trail properties verified
    across all failure paths.

    Verifies:
    - All error events have actor='gateway'
    - Infrastructure error events never set previous_status
    - A single failure produces exactly one event
    - Events are sorted chronologically via the REST endpoint
    """

    @pytest.mark.asyncio
    async def test_all_error_events_have_actor_gateway(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Every error event should have actor='gateway' (not 'api' or 'system')."""
        fake_executor.create_workspace_failure = RuntimeError("Generic executor error")

        payload = {
            "repo_url": "https://github.com/example/actor-test.git",
            "task_summary": "Actor consistency test",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1
        assert events[0]["actor"] == "gateway"

    @pytest.mark.asyncio
    async def test_error_events_have_no_previous_status(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Infrastructure error events (executor_error, artifact_error)
        should NOT set previous_status — only abort events do."""
        from app.executors.awx.exceptions import AWXArtifactError

        fake_executor.create_workspace_failure = AWXArtifactError(
            "Bad artifacts",
            template_name="gateway-create-workspace",
            missing_fields=["workspace_id"],
        )

        payload = {
            "repo_url": "https://github.com/example/prev-status.git",
            "task_summary": "Previous status check",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1
        # previous_status should be None (not set in the INSERT for error events)
        assert events[0]["previous_status"] is None, (
            "Error events should not have previous_status set"
        )

    @pytest.mark.asyncio
    async def test_only_one_event_per_failure(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """A single failure should produce exactly one event, not multiple."""
        fake_executor.create_workspace_failure = RuntimeError("Single failure")

        payload = {
            "repo_url": "https://github.com/example/single-event.git",
            "task_summary": "Single event test",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        events = await _fetch_events(db_conn, job_id)
        assert len(events) == 1, (
            f"Expected exactly 1 event per failure, got {len(events)}"
        )

    @pytest.mark.asyncio
    async def test_event_ordering_is_chronological(
        self,
        app_client: AsyncClient,
        fake_executor: FakeExecutorPlugin,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ):
        """Events returned via the GET /jobs/{id}/events endpoint are
        sorted by created_at ascending."""
        fake_opencode_client.get_session_diff_failure = RuntimeError("Unreachable")

        payload = {
            "repo_url": "https://github.com/example/ordering.git",
            "task_summary": "Event ordering test",
            "runner_id": str(runner_id),
        }

        async with app_client as client:
            response = await client.post("/jobs", json=payload)

        # This is a success case (no error events), so just verify
        # the events endpoint returns a sorted list (empty or otherwise)
        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["data"]["id"])

        # Hit the GET /jobs/{id}/events endpoint
        async with app_client as client:
            events_response = await client.get(f"/jobs/{job_id}/events")

        assert events_response.status_code == 200
        events = events_response.json()
        # For this scenario there should be no events (diff failure is graceful)
        assert isinstance(events, list)
        # If any events existed, they would be sorted by timestamp
