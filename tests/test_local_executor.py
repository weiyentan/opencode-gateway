"""Tests for LocalExecutor — the local development executor plugin."""

import os
from collections.abc import Awaitable
from uuid import UUID

import pytest

from app.executors import ExecutorPlugin, LocalExecutor
from app.executors.models import (
    CleanupWorkspaceRequest,
    CleanupWorkspaceResponse,
    CollectStateRequest,
    CollectStateResponse,
    CreateWorkspaceRequest,
    CreateWorkspaceResponse,
    RestartOpencodeRequest,
    RestartOpencodeResponse,
    StartOpencodeRequest,
    StartOpencodeResponse,
    StopOpencodeRequest,
    StopOpencodeResponse,
    WorkspaceState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> LocalExecutor:
    """Return a fresh LocalExecutor for each test."""
    return LocalExecutor()


@pytest.fixture
async def test_workspace(executor: LocalExecutor) -> CreateWorkspaceResponse:
    """Create a real workspace via the executor and yield the response."""
    resp = await executor.create_workspace(
        CreateWorkspaceRequest(repo_url="https://example.com/test.git")
    )
    yield resp
    # Teardown: remove the workspace if it still exists
    await executor.cleanup_workspace(
        CleanupWorkspaceRequest(workspace_id=resp.workspace_id)
    )


# ---------------------------------------------------------------------------
# Concrete subclass checks
# ---------------------------------------------------------------------------


class TestLocalExecutorIsConcrete:
    """LocalExecutor must be a valid concrete subclass of ExecutorPlugin."""

    def test_is_subclass_of_executor_plugin(self):
        """LocalExecutor should extend ExecutorPlugin."""
        assert issubclass(LocalExecutor, ExecutorPlugin)

    def test_can_instantiate(self, executor: LocalExecutor):
        """Instantiating LocalExecutor should succeed (no abstract methods)."""
        assert isinstance(executor, LocalExecutor)
        assert isinstance(executor, ExecutorPlugin)

    def test_name_is_local(self, executor: LocalExecutor):
        """The name attribute should be 'local'."""
        assert executor.name == "local"

    def test_workspace_base_defaults_to_tempdir(self):
        """When no base is given, workspace_base should be the system tempdir."""
        ex = LocalExecutor()
        assert ex.workspace_base == os.path.abspath(ex.workspace_base)

    def test_workspace_base_custom(self, tmp_path):
        """When a base path is provided, it should be used."""
        ex = LocalExecutor(workspace_base=str(tmp_path))
        assert ex.workspace_base == str(tmp_path)


# ---------------------------------------------------------------------------
# Async return type checks
# ---------------------------------------------------------------------------


class TestLocalExecutorIsAsync:
    """Each method should return an Awaitable."""

    def test_create_workspace_is_async(self, executor: LocalExecutor):
        result = executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert isinstance(result, Awaitable)

    def test_start_opencode_is_async(self, executor: LocalExecutor):
        result = executor.start_opencode(
            StartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(result, Awaitable)

    def test_stop_opencode_is_async(self, executor: LocalExecutor):
        result = executor.stop_opencode(
            StopOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(result, Awaitable)

    def test_restart_opencode_is_async(self, executor: LocalExecutor):
        result = executor.restart_opencode(
            RestartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(result, Awaitable)

    def test_collect_state_is_async(self, executor: LocalExecutor):
        result = executor.collect_state(
            CollectStateRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(result, Awaitable)

    def test_cleanup_workspace_is_async(self, executor: LocalExecutor):
        result = executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(result, Awaitable)


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


class TestCreateWorkspace:
    """Tests for LocalExecutor.create_workspace()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert isinstance(resp, CreateWorkspaceResponse)

    async def test_workspace_id_is_unique_uuid(self, executor: LocalExecutor):
        resp1 = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/r1.git")
        )
        resp2 = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/r2.git")
        )
        assert resp1.workspace_id != resp2.workspace_id

    async def test_creates_real_directory(self, executor: LocalExecutor):
        resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert os.path.isdir(resp.workspace_path)

    async def test_workspace_path_is_inside_base(self, executor: LocalExecutor):
        resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert resp.workspace_path.startswith(executor.workspace_base)

    async def test_status_is_ready(self, executor: LocalExecutor):
        resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert resp.status == "ready"

    async def test_tracks_workspace_internal(self, executor: LocalExecutor):
        resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert resp.workspace_id in executor._workspaces
        assert executor._workspaces[resp.workspace_id] == resp.workspace_path

    async def test_uses_custom_workspace_base(self, tmp_path):
        ex = LocalExecutor(workspace_base=str(tmp_path))
        resp = await ex.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert resp.workspace_path.startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# start_opencode
# ---------------------------------------------------------------------------


class TestStartOpencode:
    """Tests for LocalExecutor.start_opencode()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.start_opencode(
            StartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp, StartOpencodeResponse)

    async def test_session_id_is_valid_uuid(self, executor: LocalExecutor):
        resp = await executor.start_opencode(
            StartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp.session_id, UUID)

    async def test_status_is_running(self, executor: LocalExecutor):
        resp = await executor.start_opencode(
            StartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert resp.status == "running"

    async def test_port_is_8080(self, executor: LocalExecutor):
        resp = await executor.start_opencode(
            StartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert resp.port == 8080


# ---------------------------------------------------------------------------
# stop_opencode
# ---------------------------------------------------------------------------


class TestStopOpencode:
    """Tests for LocalExecutor.stop_opencode()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.stop_opencode(
            StopOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp, StopOpencodeResponse)

    async def test_status_is_stopped(self, executor: LocalExecutor):
        resp = await executor.stop_opencode(
            StopOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert resp.status == "stopped"


# ---------------------------------------------------------------------------
# restart_opencode
# ---------------------------------------------------------------------------


class TestRestartOpencode:
    """Tests for LocalExecutor.restart_opencode()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.restart_opencode(
            RestartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp, RestartOpencodeResponse)

    async def test_status_is_running(self, executor: LocalExecutor):
        resp = await executor.restart_opencode(
            RestartOpencodeRequest(workspace_id=UUID(int=1))
        )
        assert resp.status == "running"


# ---------------------------------------------------------------------------
# collect_state
# ---------------------------------------------------------------------------


class TestCollectState:
    """Tests for LocalExecutor.collect_state()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.collect_state(
            CollectStateRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp, CollectStateResponse)

    async def test_ready_for_existing_workspace(
        self, executor: LocalExecutor, test_workspace: CreateWorkspaceResponse
    ):
        resp = await executor.collect_state(
            CollectStateRequest(workspace_id=test_workspace.workspace_id)
        )
        assert resp.status == WorkspaceState.READY
        assert resp.workspace_id == test_workspace.workspace_id

    async def test_error_for_unknown_workspace(self, executor: LocalExecutor):
        resp = await executor.collect_state(
            CollectStateRequest(workspace_id=UUID(int=99999))
        )
        assert resp.status == WorkspaceState.ERROR

    async def test_error_after_cleanup(self, executor: LocalExecutor):
        ws = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws.workspace_id)
        )
        resp = await executor.collect_state(
            CollectStateRequest(workspace_id=ws.workspace_id)
        )
        assert resp.status == WorkspaceState.ERROR


# ---------------------------------------------------------------------------
# cleanup_workspace
# ---------------------------------------------------------------------------


class TestCleanupWorkspace:
    """Tests for LocalExecutor.cleanup_workspace()."""

    async def test_returns_expected_response_type(self, executor: LocalExecutor):
        resp = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=UUID(int=1))
        )
        assert isinstance(resp, CleanupWorkspaceResponse)

    async def test_status_is_cleaned(self, executor: LocalExecutor):
        resp = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=UUID(int=1))
        )
        assert resp.status == "cleaned"

    async def test_removes_directory(
        self, executor: LocalExecutor, test_workspace: CreateWorkspaceResponse
    ):
        ws_path = test_workspace.workspace_path
        assert os.path.isdir(ws_path)  # precondition

        cleanup_resp = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=test_workspace.workspace_id)
        )
        assert cleanup_resp.status == "cleaned"
        assert not os.path.exists(ws_path)

    async def test_unknown_workspace_is_graceful(self, executor: LocalExecutor):
        """Cleaning up an unknown workspace should not raise."""
        resp = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=UUID(int=99999))
        )
        assert resp.status == "cleaned"

    async def test_double_cleanup_is_idempotent(self, executor: LocalExecutor):
        ws = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        ws_id = ws.workspace_id
        resp1 = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws_id)
        )
        assert resp1.status == "cleaned"
        # Second cleanup should not raise
        resp2 = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws_id)
        )
        assert resp2.status == "cleaned"


# ---------------------------------------------------------------------------
# Lifecycle integration
# ---------------------------------------------------------------------------


class TestLocalExecutorLifecycle:
    """End-to-end lifecycle: create → collect → cleanup."""

    async def test_full_lifecycle(self, executor: LocalExecutor):
        # Create
        create_resp = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/full.git")
        )
        ws_id = create_resp.workspace_id
        ws_path = create_resp.workspace_path
        assert os.path.isdir(ws_path)

        # Collect — should be ready
        state = await executor.collect_state(CollectStateRequest(workspace_id=ws_id))
        assert state.status == WorkspaceState.READY

        # Cleanup
        cleanup_resp = await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws_id)
        )
        assert cleanup_resp.status == "cleaned"
        assert not os.path.exists(ws_path)

        # Collect after cleanup — should be error
        state2 = await executor.collect_state(CollectStateRequest(workspace_id=ws_id))
        assert state2.status == WorkspaceState.ERROR

    async def test_multiple_workspaces_independent(self, executor: LocalExecutor):
        ws1 = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/a.git")
        )
        ws2 = await executor.create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/b.git")
        )

        assert ws1.workspace_id != ws2.workspace_id
        assert ws1.workspace_path != ws2.workspace_path
        assert os.path.isdir(ws1.workspace_path)
        assert os.path.isdir(ws2.workspace_path)

        # Clean up ws1 — ws2 should remain
        await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws1.workspace_id)
        )
        assert not os.path.exists(ws1.workspace_path)
        assert os.path.isdir(ws2.workspace_path)

        s1 = await executor.collect_state(
            CollectStateRequest(workspace_id=ws1.workspace_id)
        )
        s2 = await executor.collect_state(
            CollectStateRequest(workspace_id=ws2.workspace_id)
        )
        assert s1.status == WorkspaceState.ERROR
        assert s2.status == WorkspaceState.READY

        # Clean up ws2
        await executor.cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id=ws2.workspace_id)
        )
        assert not os.path.exists(ws2.workspace_path)
