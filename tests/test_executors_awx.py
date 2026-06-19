"""Tests for the AWXExecutorPlugin and AWX executor factory.

Covers normal flow, error paths, and template-ID validation.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from app.executors import EXECUTOR_REGISTRY, ExecutorPlugin
from app.executors.awx.client import AWXApiClient, AWXJobResult, AWXJobSummary
from app.executors.awx.exceptions import (
    AWXClientError,
    AWXConnectionError,
    AWXJobError,
    AWXTimeoutError,
)
from app.executors.awx.plugin import AWXExecutorPlugin
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

# ── Constants ───────────────────────────────────────────────────────────

_CREATE_TEMPLATE = 10
_LIFECYCLE_TEMPLATE = 20
_TEARDOWN_TEMPLATE = 30
_BASE_PATH = "/home/runner/workspaces"

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_plugin(
    client: AWXApiClient | None = None,
    *,
    create_id: int = _CREATE_TEMPLATE,
    lifecycle_id: int = _LIFECYCLE_TEMPLATE,
    teardown_id: int = _TEARDOWN_TEMPLATE,
    base_path: str = _BASE_PATH,
) -> AWXExecutorPlugin:
    """Create a fully-wired AWXExecutorPlugin with a mock client."""
    if client is None:
        client = AsyncMock(spec=AWXApiClient)
    return AWXExecutorPlugin(
        client=client,
        create_workspace_template_id=create_id,
        opencode_lifecycle_template_id=lifecycle_id,
        workspace_teardown_template_id=teardown_id,
        workspace_base_path=base_path,
    )


def _mock_launch_and_wait(
    mock_client: AsyncMock,
    *,
    job_id: int = 42,
    status: str = "successful",
    artifacts: dict | None = None,
) -> None:
    """Configure a mock AWXApiClient for a successful launch-and-wait cycle."""
    mock_client.launch_job_template.return_value = AWXJobSummary(
        job_id=job_id, status="pending"
    )
    mock_client.wait_for_job.return_value = AWXJobResult(
        job_id=job_id, status=status, artifacts=artifacts or {}
    )


# ── Constructor validation ──────────────────────────────────────────────


class TestAWXExecutorPluginInit:
    """Constructor validation — template IDs must not be None."""

    def test_valid_template_ids(self):
        plugin = _make_plugin()
        assert plugin.name == "awx"
        assert isinstance(plugin, ExecutorPlugin)

    def test_create_template_none_raises(self):
        with pytest.raises(ValueError, match="create_workspace_template_id"):
            AWXExecutorPlugin(
                client=AsyncMock(spec=AWXApiClient),
                create_workspace_template_id=None,  # type: ignore[arg-type]
                opencode_lifecycle_template_id=_LIFECYCLE_TEMPLATE,
                workspace_teardown_template_id=_TEARDOWN_TEMPLATE,
            )

    def test_lifecycle_template_none_raises(self):
        with pytest.raises(ValueError, match="opencode_lifecycle_template_id"):
            AWXExecutorPlugin(
                client=AsyncMock(spec=AWXApiClient),
                create_workspace_template_id=_CREATE_TEMPLATE,
                opencode_lifecycle_template_id=None,  # type: ignore[arg-type]
                workspace_teardown_template_id=_TEARDOWN_TEMPLATE,
            )

    def test_teardown_template_none_raises(self):
        with pytest.raises(ValueError, match="workspace_teardown_template_id"):
            AWXExecutorPlugin(
                client=AsyncMock(spec=AWXApiClient),
                create_workspace_template_id=_CREATE_TEMPLATE,
                opencode_lifecycle_template_id=_LIFECYCLE_TEMPLATE,
                workspace_teardown_template_id=None,  # type: ignore[arg-type]
            )

    def test_default_base_path(self):
        plugin = AWXExecutorPlugin(
            client=AsyncMock(spec=AWXApiClient),
            create_workspace_template_id=_CREATE_TEMPLATE,
            opencode_lifecycle_template_id=_LIFECYCLE_TEMPLATE,
            workspace_teardown_template_id=_TEARDOWN_TEMPLATE,
        )
        assert plugin._workspace_base_path == _BASE_PATH

    def test_custom_base_path(self):
        plugin = AWXExecutorPlugin(
            client=AsyncMock(spec=AWXApiClient),
            create_workspace_template_id=_CREATE_TEMPLATE,
            opencode_lifecycle_template_id=_LIFECYCLE_TEMPLATE,
            workspace_teardown_template_id=_TEARDOWN_TEMPLATE,
            workspace_base_path="/custom/path/",
        )
        assert plugin._workspace_base_path == "/custom/path"


# ── create_workspace ────────────────────────────────────────────────────


class TestAWXExecutorPluginCreateWorkspace:
    """Tests for AWXExecutorPlugin.create_workspace()."""

    async def test_creates_workspace_with_minimal_request(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                       "workspace_path": "/home/runner/workspaces/ws1"},
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        resp = await plugin.create_workspace(req)

        assert isinstance(resp, CreateWorkspaceResponse)
        assert resp.status == "successful"
        assert resp.workspace_path == "/home/runner/workspaces/ws1"
        assert str(resp.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        # Verify the right template and extra_vars were used.
        client.launch_job_template.assert_awaited_once_with(
            _CREATE_TEMPLATE,
            extra_vars={"repo_url": "https://example.com/repo.git"},
        )
        client.wait_for_job.assert_awaited_once_with(42)

    async def test_creates_workspace_with_branch_and_job_id(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"workspace_id": "11111111-2222-3333-4444-555555555555",
                       "workspace_path": "/tmp/ws"},
        )
        plugin = _make_plugin(client)

        job_id = UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")
        req = CreateWorkspaceRequest(
            repo_url="https://example.com/repo.git",
            branch="feature/x",
            job_id=job_id,
        )
        resp = await plugin.create_workspace(req)

        assert resp.workspace_path == "/tmp/ws"
        client.launch_job_template.assert_awaited_once_with(
            _CREATE_TEMPLATE,
            extra_vars={
                "repo_url": "https://example.com/repo.git",
                "branch": "feature/x",
                "job_id": str(job_id),
            },
        )

    async def test_creates_workspace_with_missing_artifacts_returns_defaults(self):
        """When AWX returns no artifacts, sensible defaults are used."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful", artifacts={})
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        resp = await plugin.create_workspace(req)

        assert resp.status == "successful"
        assert resp.workspace_path == ""
        assert resp.workspace_id == UUID(int=0)


# ── start / stop / restart ──────────────────────────────────────────────


class TestAWXExecutorPluginLifecycle:
    """Tests for start_opencode, stop_opencode, restart_opencode."""

    async def test_start_opencode_with_explicit_path(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"session_id": "00000000-1111-2222-3333-444444444444",
                       "port": 9090},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(
            workspace_id=ws_id,
            workspace_path="/explicit/path",
        )
        resp = await plugin.start_opencode(req)

        assert isinstance(resp, StartOpencodeResponse)
        assert resp.status == "successful"
        assert resp.port == 9090
        assert str(resp.session_id) == "00000000-1111-2222-3333-444444444444"

        client.launch_job_template.assert_awaited_once_with(
            _LIFECYCLE_TEMPLATE,
            extra_vars={"action": "start", "workspace_path": "/explicit/path"},
        )

    async def test_start_opencode_derives_path_from_workspace_id(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"session_id": "00000000-1111-2222-3333-444444444444",
                       "port": 8080},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        resp = await plugin.start_opencode(req)

        assert resp.port == 8080
        client.launch_job_template.assert_awaited_once_with(
            _LIFECYCLE_TEMPLATE,
            extra_vars={
                "action": "start",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
            },
        )

    async def test_start_opencode_missing_artifacts_returns_defaults(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        resp = await plugin.start_opencode(req)

        assert resp.port == 0
        assert resp.session_id == UUID(int=0)

    async def test_start_opencode_passes_port_in_extra_vars(self):
        """When port is provided in the request, it is passed as an extra_var."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"session_id": "00000000-1111-2222-3333-444444444444",
                       "port": 10042},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(
            workspace_id=ws_id,
            port=10042,
        )
        resp = await plugin.start_opencode(req)

        assert resp.port == 10042
        client.launch_job_template.assert_awaited_once_with(
            _LIFECYCLE_TEMPLATE,
            extra_vars={
                "action": "start",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
                "port": 10042,
            },
        )

    async def test_stop_opencode(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful")
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StopOpencodeRequest(workspace_id=ws_id)
        resp = await plugin.stop_opencode(req)

        assert isinstance(resp, StopOpencodeResponse)
        assert resp.status == "successful"
        client.launch_job_template.assert_awaited_once_with(
            _LIFECYCLE_TEMPLATE,
            extra_vars={
                "action": "stop",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
            },
        )

    async def test_restart_opencode(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful")
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = RestartOpencodeRequest(workspace_id=ws_id)
        resp = await plugin.restart_opencode(req)

        assert isinstance(resp, RestartOpencodeResponse)
        assert resp.status == "successful"
        client.launch_job_template.assert_awaited_once_with(
            _LIFECYCLE_TEMPLATE,
            extra_vars={
                "action": "restart",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
            },
        )


# ── collect / cleanup ───────────────────────────────────────────────────


class TestAWXExecutorPluginTeardown:
    """Tests for collect_state and cleanup_workspace."""

    async def test_collect_state(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"status": "running", "process_status": "active", "port": 8080},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        resp = await plugin.collect_state(req)

        assert isinstance(resp, CollectStateResponse)
        assert resp.workspace_id == ws_id
        assert resp.status == WorkspaceState.RUNNING
        assert resp.process_status == "active"
        assert resp.port == 8080

        client.launch_job_template.assert_awaited_once_with(
            _TEARDOWN_TEMPLATE,
            extra_vars={
                "action": "collect",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
            },
        )

    async def test_collect_state_unrecognised_status_defaults_to_error(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={"status": "bogus"})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        resp = await plugin.collect_state(req)

        assert resp.status == WorkspaceState.ERROR

    async def test_collect_state_falls_back_to_job_status(self):
        """When artifacts lack a 'status' key, use the AWX job status."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful", artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        resp = await plugin.collect_state(req)

        # "successful" is not a WorkspaceState, so it defaults to ERROR.
        assert resp.status == WorkspaceState.ERROR

    async def test_cleanup_workspace(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful")
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CleanupWorkspaceRequest(workspace_id=ws_id)
        resp = await plugin.cleanup_workspace(req)

        assert isinstance(resp, CleanupWorkspaceResponse)
        assert resp.status == "successful"
        client.launch_job_template.assert_awaited_once_with(
            _TEARDOWN_TEMPLATE,
            extra_vars={
                "action": "cleanup",
                "workspace_path": f"{_BASE_PATH}/{ws_id}",
            },
        )


# ── Error handling ──────────────────────────────────────────────────────


class TestAWXExecutorPluginErrorHandling:
    """Verify that AWX errors are propagated correctly."""

    async def test_create_workspace_connection_error(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.side_effect = AWXConnectionError("refused")
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXConnectionError, match="refused"):
            await plugin.create_workspace(req)

    async def test_create_workspace_timeout_error(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=99, status="pending"
        )
        client.wait_for_job.side_effect = AWXTimeoutError("timed out")
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXTimeoutError, match="timed out"):
            await plugin.create_workspace(req)

    async def test_start_opencode_job_failure(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=77, status="pending"
        )
        client.wait_for_job.side_effect = AWXJobError("job failed", job_id=77)
        plugin = _make_plugin(client)

        req = StartOpencodeRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXJobError, match="job failed"):
            await plugin.start_opencode(req)

    async def test_stop_opencode_client_error(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.side_effect = AWXClientError("generic")
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXClientError, match="generic"):
            await plugin.stop_opencode(req)

    async def test_collect_state_timeout(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=55, status="pending"
        )
        client.wait_for_job.side_effect = AWXTimeoutError("too slow")
        plugin = _make_plugin(client)

        req = CollectStateRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXTimeoutError, match="too slow"):
            await plugin.collect_state(req)

    async def test_cleanup_workspace_job_error(self):
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=33, status="pending"
        )
        client.wait_for_job.side_effect = AWXJobError("cleanup failed", job_id=33)
        plugin = _make_plugin(client)

        req = CleanupWorkspaceRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXJobError, match="cleanup failed"):
            await plugin.cleanup_workspace(req)


# ── Registry ────────────────────────────────────────────────────────────


class TestAWXExecutorRegistry:
    """Verify the AWX executor is registered properly."""

    def test_registry_contains_awx(self):
        assert "awx" in EXECUTOR_REGISTRY
        assert EXECUTOR_REGISTRY["awx"] is AWXExecutorPlugin

    def test_registry_contains_local(self):
        assert "local" in EXECUTOR_REGISTRY

    def test_awx_plugin_is_executor_subclass(self):
        assert issubclass(AWXExecutorPlugin, ExecutorPlugin)


# ── Factory ─────────────────────────────────────────────────────────────


class TestAWXExecutorFactory:
    """Tests for the factory's AWX-specific behaviour."""

    def test_awx_type_returns_awx_executor(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "awx")
        monkeypatch.setenv("GATEWAY_AWX_BASE_URL", "https://awx.example.com")
        monkeypatch.setenv("GATEWAY_AWX_TOKEN", "test-token")
        monkeypatch.setenv("GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID", "10")
        monkeypatch.setenv("GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID", "20")
        monkeypatch.setenv("GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID", "30")

        import app.executors.factory
        importlib.reload(app.executors.factory)
        from app.executors.factory import get_executor

        executor = get_executor()
        assert isinstance(executor, AWXExecutorPlugin)
        assert executor.name == "awx"

    def test_awx_missing_template_ids_raises(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "awx")
        monkeypatch.setenv("GATEWAY_AWX_BASE_URL", "https://awx.example.com")
        monkeypatch.setenv("GATEWAY_AWX_TOKEN", "test-token")
        # Do NOT set template IDs — they default to 0 which triggers error.

        import app.executors.factory
        importlib.reload(app.executors.factory)
        from app.executors.factory import get_executor

        with pytest.raises(ValueError, match="missing or zero"):
            get_executor()

    def test_awx_missing_single_template_id_raises(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "awx")
        monkeypatch.setenv("GATEWAY_AWX_BASE_URL", "https://awx.example.com")
        monkeypatch.setenv("GATEWAY_AWX_TOKEN", "test-token")
        monkeypatch.setenv("GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID", "10")
        monkeypatch.setenv("GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID", "20")
        # workspace_teardown_template_id is missing → defaults to 0

        import app.executors.factory
        importlib.reload(app.executors.factory)
        from app.executors.factory import get_executor

        with pytest.raises(ValueError, match="missing or zero"):
            get_executor()

    def test_awx_partial_template_ids_raises(self, monkeypatch):
        """Only setting two of three template IDs should still raise."""
        monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "awx")
        monkeypatch.setenv("GATEWAY_AWX_BASE_URL", "https://awx.example.com")
        monkeypatch.setenv("GATEWAY_AWX_TOKEN", "test-token")
        monkeypatch.setenv("GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID", "10")
        monkeypatch.setenv("GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID", "20")
        # workspace_teardown_template_id is missing → defaults to 0

        import app.executors.factory
        importlib.reload(app.executors.factory)
        from app.executors.factory import get_executor

        with pytest.raises(ValueError, match="missing or zero"):
            get_executor()
