"""Comprehensive happy-path lifecycle integration test.

Verifies the complete job lifecycle by observing intermediate states
during execution: submit job → allocate workspace → allocate port →
start OpenCode → job transitions through *starting_opencode* to *running*
→ completed → cleanup succeeds.

Uses the real PostgreSQL test database (``db_conn``) and fake
executor/OpenCode clients with configurable blocking to let the test
inspect database state while the job is mid-flight.

Acceptance Criteria
-------------------
1. Submit job → allocate workspace → allocate port → start OpenCode
2. Job transitions through ``starting_opencode`` to ``running``
3. Terminal result → completed
4. Cleanup succeeds
5. Uses fake clients and real test DB
"""

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import create_runner
from tests.integration.test_lifecycle_integration import (
    TEST_SESSION_ID,
    TEST_WORKSPACE_PATH,
    FakeExecutorPlugin,
    FakeOpenCodeClient,
)

pytestmark = pytest.mark.integration


# ═══════════════════════════════════════════════════════════════════════════
#  Controllable Fake Executor — extends FakeExecutorPlugin with the
#  ability to block *start_opencode* mid-flight so the test can observe
#  the intermediate database state.
# ═══════════════════════════════════════════════════════════════════════════


class ControllableFakeExecutor(FakeExecutorPlugin):
    """Like :class:`FakeExecutorPlugin` but allows blocking
    :meth:`start_opencode` for intermediate-state observation.

    The test calls :meth:`wait_blocked` to wait until the executor is
    waiting, then calls :meth:`unblock` to let it proceed.

    Usage::

        fake = ControllableFakeExecutor()
        # Fire POST /jobs in a background task …
        await fake.wait_blocked(timeout=5)
        # … inspect database state …
        fake.unblock()
        # … await the POST response …
    """

    def __init__(self) -> None:
        super().__init__()
        self._blocked = asyncio.Event()
        self._continue = asyncio.Event()

    async def start_opencode(self, request: object) -> object:
        """Start OpenCode — blocks on *continue* event after recording
        the call so the test can observe intermediate DB state."""
        self.calls["start_opencode"].append(request)
        self._blocked.set()                                 # signal test
        await self._continue.wait()                         # wait for test
        if self.start_opencode_failure:
            raise self.start_opencode_failure
        return self.start_opencode_response

    async def wait_blocked(self, timeout: float = 10) -> None:
        """Wait until the executor is blocked inside ``start_opencode``."""
        await asyncio.wait_for(self._blocked.wait(), timeout=timeout)

    def unblock(self) -> None:
        """Release the executor to continue past ``start_opencode``."""
        self._continue.set()


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def controllable_executor() -> ControllableFakeExecutor:
    """Return a fresh :class:`ControllableFakeExecutor` per test."""
    return ControllableFakeExecutor()


@pytest.fixture
def fake_opencode_client() -> FakeOpenCodeClient:
    """Return a fresh :class:`FakeOpenCodeClient` per test."""
    return FakeOpenCodeClient()


@pytest.fixture
async def runner_id(db_conn) -> uuid.UUID:
    """Create a HEALTHY runner in the test database."""
    return await create_runner(
        db_conn,
        hostname="happy-path-runner",
        status="HEALTHY",
        executor_type="local",
    )


@pytest.fixture
async def app_client(
    db_conn,
    controllable_executor: ControllableFakeExecutor,
    fake_opencode_client: FakeOpenCodeClient,
) -> AsyncClient:
    """Return an httpx AsyncClient wired to the FastAPI app with
    overridden dependencies — real test DB, controllable executor,
    and fake OpenCode client.

    The fixture enters the client context so it stays open across
    the background-task / concurrent-polling pattern.
    """
    from app.api.jobs import get_opencode_client
    from app.core.factory import create_app
    from app.db.session import get_session
    from app.executors.factory import get_executor

    app = create_app()
    # Prevent the app from starting its own DB pool / scheduler.
    app.state.pool = AsyncMock()
    app.state.pool.pool = None

    async def _override_get_session(request):
        yield db_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_executor] = lambda: controllable_executor
    app.dependency_overrides[get_opencode_client] = lambda: fake_opencode_client

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    ) as client:
        yield client


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Full Happy-Path Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestHappyPathLifecycle:
    """Integration test covering the complete job lifecycle from submission
    through OpenCode start, intermediate running state, completion, and
    workspace cleanup — all in one deterministic test."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_payload(runner_id: uuid.UUID) -> dict:
        return {
            "repo_url": "https://github.com/example/happy-path-test.git",
            "task_summary": "Happy path lifecycle integration test",
            "runner_id": str(runner_id),
            "env_vars": {"LOG_LEVEL": "debug", "CUSTOM_VAR": "happy-path"},
        }

    # ------------------------------------------------------------------
    # The integration test
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_happy_path_lifecycle(
        self,
        app_client: AsyncClient,
        controllable_executor: ControllableFakeExecutor,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ) -> None:
        """Full happy-path lifecycle:

        1. Submit job (background task) — the controllable executor blocks
           inside *start_opencode* so we can inspect the DB mid-flight.
        2. While blocked, verify intermediate state:
           - Job status is ``starting_opencode`` (still blocked in start_opencode)
           - Workspace is created and stored on the job
           - Port is allocated and persisted on the workspace
           - No session ID yet (OpenCode hasn't finished starting)
        3. Release the executor, allow OpenCode to "finish starting".
        4. Verify response shows ``completed`` with session ID and diff.
        5. Verify final DB state (completed, opencode_session_id, diff).
        6. Call the workspace cleanup endpoint.
        7. Verify cleanup succeeded — executor was called, workspace
           cleanup_status transitioned to ``cleaned``.
        """
        payload = self._make_payload(runner_id)

        # -- 1. Submit job in background -----------------------------------
        job_task = asyncio.create_task(
            asyncio.wait_for(
                app_client.post("/jobs", json=payload),
                timeout=15,
            )
        )

        # -- 2. Wait for executor to block inside start_opencode ----------
        await controllable_executor.wait_blocked(timeout=10)

        # ---- 2a. Verify intermediate database state ----------------------
        rows = await db_conn.fetch(
            "SELECT id, status, workspace_name, opencode_session_id, "
            "repo_url, task_summary "
            "FROM gateway_jobs "
            "WHERE task_summary = $1",
            "Happy path lifecycle integration test",
        )
        assert len(rows) == 1, "Expected exactly one job row"
        job_row = rows[0]

        job_id: uuid.UUID = job_row["id"]

        # Job must be in "starting_opencode" state — start_opencode has been called
        # but hasn't returned yet, so the job hasn't moved to "running".
        assert job_row["status"] == "starting_opencode", (
            f"Expected status 'starting_opencode' while start_opencode is blocked, "
            f"got '{job_row['status']}'"
        )

        # Workspace must already be created and linked to the job.
        assert job_row["workspace_name"] is not None, (
            "Workspace should be allocated before start_opencode"
        )
        ws_id = uuid.UUID(job_row["workspace_name"])

        # Session ID must NOT yet be set — start_opencode hasn't returned.
        assert job_row["opencode_session_id"] is None, (
            "Session ID should not be set until start_opencode completes"
        )

        # Job metadata is correct.
        assert str(job_row["repo_url"]) == "https://github.com/example/happy-path-test.git"
        assert job_row["task_summary"] == "Happy path lifecycle integration test"

        # ---- 2b. Verify workspace has port allocated ---------------------
        ws_row = await db_conn.fetchrow(
            "SELECT id, port, path, repo_url, cleanup_status "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_row is not None, "Workspace must exist in the database"
        assert ws_row["port"] is not None, (
            "Port must be allocated before start_opencode"
        )
        assert isinstance(ws_row["port"], int), "Port must be an integer"
        assert 10000 <= ws_row["port"] <= 10999, (
            f"Port {ws_row['port']} must be in the allocated range"
        )
        assert ws_row["cleanup_status"] == "active", (
            "Workspace should be active (cleanup has not occurred yet)"
        )
        assert ws_row["path"] == TEST_WORKSPACE_PATH

        # ---- 2c. Verify executor was called for create_workspace ---------
        assert len(controllable_executor.calls["create_workspace"]) == 1, (
            "create_workspace must have been called"
        )
        create_call = controllable_executor.calls["create_workspace"][0]
        assert create_call.repo_url == "https://github.com/example/happy-path-test.git"
        assert create_call.env_vars == {"LOG_LEVEL": "debug", "CUSTOM_VAR": "happy-path"}

        # start_opencode has been called (we're blocked inside it).
        assert len(controllable_executor.calls["start_opencode"]) == 1, (
            "start_opencode must have been called"
        )

        # -- 3. Release the executor to complete start_opencode ------------
        controllable_executor.unblock()

        # -- 4. Wait for the POST /jobs response --------------------------
        response = await job_task
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body["status"] == "ok", f"Response status not ok: {body}"
        data = body["data"]

        # Final status must be "running" (job completes asynchronously).
        assert data["status"] == "running", (
            f"Expected final status 'running', got '{data['status']}'"
        )
        assert data["completed_at"] is None, "completed_at must be None until /complete"
        assert data["opencode_session_id"] is not None, (
            "opencode_session_id must be set"
        )
        assert data["diff"] is not None, "diff must be present"
        assert uuid.UUID(data["id"]) == job_id, "Job ID must match"

        # Complete the job to reach terminal state for DB assertions.
        complete_resp = await app_client.post(f"/jobs/{job_id}/complete", json={"target_status": "completed"})
        assert complete_resp.status_code == 200
        complete_data = complete_resp.json()
        assert complete_data["data"]["status"] == "completed"
        assert complete_data["data"]["completed_at"] is not None

        # -- 5. Verify final database state --------------------------------
        final_row = await db_conn.fetchrow(
            "SELECT status, completed_at, opencode_session_id, diff, "
            "runner_id, workspace_name "
            "FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert final_row is not None
        assert final_row["status"] == "completed"
        assert final_row["completed_at"] is not None
        assert final_row["opencode_session_id"] == str(TEST_SESSION_ID)
        assert final_row["diff"] is not None
        assert uuid.UUID(final_row["runner_id"]) == runner_id
        assert uuid.UUID(final_row["workspace_name"]) == ws_id

        # Workspace must now have cleanup_after set.
        ws_after = await db_conn.fetchrow(
            "SELECT id, cleanup_after, cleanup_status "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_after is not None
        assert ws_after["cleanup_after"] is not None, (
            "cleanup_after must be set after job completion"
        )
        assert ws_after["cleanup_status"] == "active"

        # -- 6. Call the workspace cleanup endpoint -----------------------
        cleanup_response = await app_client.post(
            f"/workspaces/{ws_id}/cleanup"
        )

        assert cleanup_response.status_code == 200, (
            f"Cleanup failed: {cleanup_response.text}"
        )
        cleanup_data = cleanup_response.json()["data"]
        assert cleanup_data["cleanup_status"] in ("cleaning", "cleaned"), (
            f"Unexpected cleanup status: {cleanup_data['cleanup_status']}"
        )

        # -- 7. Verify cleanup executor call -------------------------------
        assert len(controllable_executor.calls["cleanup_workspace"]) == 1
        cleanup_call = controllable_executor.calls["cleanup_workspace"][0]
        assert cleanup_call.workspace_id == ws_id

        # Verify final cleanup DB state.
        ws_final = await db_conn.fetchrow(
            "SELECT cleanup_status, cleanup_completed_at "
            "FROM workspaces WHERE id = $1",
            ws_id,
        )
        assert ws_final is not None
        assert ws_final["cleanup_status"] == "cleaned", (
            f"Expected 'cleaned', got '{ws_final['cleanup_status']}'"
        )
        assert ws_final["cleanup_completed_at"] is not None

    # ------------------------------------------------------------------
    # Edge: Verify that environment variables flow through correctly
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_happy_path_env_vars_flow(
        self,
        app_client: AsyncClient,
        controllable_executor: ControllableFakeExecutor,
        db_conn,
        runner_id: uuid.UUID,
    ) -> None:
        """Environment variables submitted with the job are passed to the
        executor and persisted in the database."""
        env_vars = {
            "API_KEY": "sk-test-123",
            "MODEL": "gpt-4o",
            "LOG_LEVEL": "info",
        }
        payload = {
            "repo_url": "https://github.com/example/env-vars-flow.git",
            "task_summary": "Happy path env vars test",
            "runner_id": str(runner_id),
            "env_vars": env_vars,
        }

        # Submit in background so we can block and inspect.
        job_task = asyncio.create_task(
            asyncio.wait_for(
                app_client.post("/jobs", json=payload),
                timeout=15,
            )
        )

        await controllable_executor.wait_blocked(timeout=10)
        controllable_executor.unblock()

        response = await job_task
        assert response.status_code == 201

        job_id = uuid.UUID(response.json()["data"]["id"])

        # Verify env_vars in DB.
        import json as json_mod

        row = await db_conn.fetchrow(
            "SELECT env_vars FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        raw = row["env_vars"]
        stored = json_mod.loads(raw) if isinstance(raw, str) else dict(raw)
        assert stored == env_vars

        # Verify executor received them.
        assert len(controllable_executor.calls["create_workspace"]) == 1
        assert (
            controllable_executor.calls["create_workspace"][0].env_vars == env_vars
        )
        assert len(controllable_executor.calls["start_opencode"]) == 1
        assert (
            controllable_executor.calls["start_opencode"][0].env_vars == env_vars
        )

    # ------------------------------------------------------------------
    # Edge: Verify diff fetch after completion
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_happy_path_diff_fetch_after_completion(
        self,
        app_client: AsyncClient,
        controllable_executor: ControllableFakeExecutor,
        fake_opencode_client: FakeOpenCodeClient,
        db_conn,
        runner_id: uuid.UUID,
    ) -> None:
        """When an OpenCode client is available, the detailed diff is
        fetched and persisted after the job reaches completed."""
        payload = {
            "repo_url": "https://github.com/example/diff-after-complete.git",
            "task_summary": "Happy path diff fetch test",
            "runner_id": str(runner_id),
        }

        job_task = asyncio.create_task(
            asyncio.wait_for(
                app_client.post("/jobs", json=payload),
                timeout=15,
            )
        )

        await controllable_executor.wait_blocked(timeout=10)
        controllable_executor.unblock()

        response = await job_task
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "running"

        # The diff from the fake client should include "file.py".
        assert "file.py" in data["diff"], (
            "Detailed diff from FakeOpenCodeClient should be persisted"
        )

        job_id = uuid.UUID(data["id"])
        row = await db_conn.fetchrow(
            "SELECT diff FROM gateway_jobs WHERE id = $1",
            job_id,
        )
        assert row["diff"] is not None
        assert "file.py" in row["diff"]
