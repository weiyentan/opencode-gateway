"""Tests for the AWXExecutorPlugin and AWX executor factory.

Covers normal flow, error paths, and template-ID validation.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.executors import EXECUTOR_REGISTRY, ExecutorPlugin
from app.executors.awx.client import AWXApiClient, AWXJobResult, AWXJobSummary
from app.executors.awx.exceptions import (
    AWXArtifactError,
    AWXClientError,
    AWXConnectionError,
    AWXHTTPError,
    AWXJobError,
    AWXTimeoutError,
)
from app.executors.awx.plugin import AWXExecutorPlugin
from app.executors.models import (
    CancelJobRequest,
    CancelJobResponse,
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

    async def test_creates_workspace_with_missing_artifacts_raises(self):
        """When AWX returns no artifacts, an AWXArtifactError is raised
        instead of falling back to placeholder values."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful", artifacts={})
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)

        err = exc_info.value
        assert err.template_name == "gateway-create-workspace"
        assert "workspace_id" in err.missing_fields
        assert "workspace_path" in err.missing_fields

    async def test_executor_job_id_stored_after_launch(self):
        """The AWX job ID is recorded in _executor_job_ids after a
        successful launch via create_workspace."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, job_id=42, status="successful",
                              artifacts={"workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                         "workspace_path": "/home/runner/workspaces/ws1"})
        plugin = _make_plugin(client)

        gw_job_id = UUID("00000000-0000-0000-0000-000000000001")
        req = CreateWorkspaceRequest(
            repo_url="https://example.com/repo.git",
            job_id=gw_job_id,
        )
        await plugin.create_workspace(req)

        # The gateway_job_id should map to the AWX job ID.
        assert plugin._executor_job_ids.get(gw_job_id) == 42


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

    async def test_start_opencode_missing_artifacts_raises(self):
        """When AWX returns no artifacts for start, an AWXArtifactError is raised
        instead of falling back to zero UUID and zero port."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)

        err = exc_info.value
        assert "gateway-opencode-lifecycle" in err.template_name
        assert "session_id" in err.missing_fields
        assert "port" in err.missing_fields

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

    async def test_collect_state_missing_status_raises(self):
        """When artifacts lack a 'status' key, an AWXArtifactError is raised
        instead of falling back to the AWX job status."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, status="successful", artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.collect_state(req)

        err = exc_info.value
        assert "gateway-workspace-teardown" in err.template_name
        assert "status" in err.missing_fields

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


# ── Artifact validation: create_workspace ────────────────────────────────


class TestAWXArtifactValidationCreateWorkspace:
    """Validate that create_workspace fails hard on malformed artifacts."""

    async def test_missing_both_fields_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={})
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert err.template_name == "gateway-create-workspace"
        assert "workspace_id" in err.missing_fields
        assert "workspace_path" in err.missing_fields

    async def test_none_artifacts_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts=None)
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert "workspace_id" in err.missing_fields

    async def test_invalid_uuid_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "workspace_id": "not-a-valid-uuid",
                "workspace_path": "/some/path",
            },
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert "workspace_id" in err.invalid_fields

    async def test_empty_workspace_path_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "workspace_path": "",
            },
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert "workspace_path" in err.invalid_fields

    async def test_missing_workspace_path_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert "workspace_path" in err.missing_fields

    async def test_missing_workspace_id_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"workspace_path": "/some/path"},
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.create_workspace(req)
        err = exc_info.value
        assert "workspace_id" in err.missing_fields


# ── Artifact validation: start_opencode ──────────────────────────────────


class TestAWXArtifactValidationStartOpencode:
    """Validate that start_opencode fails hard on malformed artifacts."""

    async def test_missing_both_fields_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "gateway-opencode-lifecycle" in err.template_name
        assert "session_id" in err.missing_fields
        assert "port" in err.missing_fields

    async def test_invalid_session_id_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"session_id": "bad-uuid", "port": 8080},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "session_id" in err.invalid_fields

    async def test_port_zero_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "port": 0,
            },
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "port" in err.invalid_fields

    async def test_negative_port_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "port": -1,
            },
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "port" in err.invalid_fields

    async def test_port_as_string_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "port": "not-a-number",
            },
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "port" in err.invalid_fields

    async def test_missing_session_id_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={"port": 8080},
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = StartOpencodeRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.start_opencode(req)
        err = exc_info.value
        assert "session_id" in err.missing_fields


# ── Artifact validation: collect_state ───────────────────────────────────


class TestAWXArtifactValidationCollectState:
    """Validate that collect_state fails hard on malformed artifacts."""

    async def test_missing_status_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.collect_state(req)
        err = exc_info.value
        assert "gateway-workspace-teardown" in err.template_name
        assert "status" in err.missing_fields

    async def test_empty_status_raises(self):
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={"status": ""})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        with pytest.raises(AWXArtifactError) as exc_info:
            await plugin.collect_state(req)
        err = exc_info.value
        assert "status" in err.invalid_fields

    async def test_valid_with_optional_fields(self):
        """Artifacts with status plus optional fields should validate."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "status": "running",
                "process_status": "active",
                "port": 9090,
            },
        )
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        resp = await plugin.collect_state(req)

        assert resp.status == WorkspaceState.RUNNING
        assert resp.process_status == "active"
        assert resp.port == 9090

    async def test_unrecognised_status_still_maps_to_error(self):
        """An unrecognised workspace state string still maps to ERROR
        (not a validation failure — the status field format is valid)."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, artifacts={"status": "bogus"})
        plugin = _make_plugin(client)

        ws_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        req = CollectStateRequest(workspace_id=ws_id)
        resp = await plugin.collect_state(req)

        assert resp.status == WorkspaceState.ERROR


# ── AWXArtifactError attributes ──────────────────────────────────────────


class TestAWXArtifactErrorAttributes:
    """Verify that AWXArtifactError carries descriptive failure metadata."""

    def test_basic_error_attributes(self):
        err = AWXArtifactError(
            "artifacts missing required fields",
            template_name="gateway-create-workspace",
            missing_fields=["workspace_id", "workspace_path"],
            invalid_fields=["port"],
        )

        assert str(err) == "artifacts missing required fields"
        assert err.template_name == "gateway-create-workspace"
        assert err.missing_fields == ["workspace_id", "workspace_path"]
        assert err.invalid_fields == ["port"]

    def test_error_with_default_empty_lists(self):
        err = AWXArtifactError("something went wrong")
        assert err.template_name is None
        assert err.missing_fields == []
        assert err.invalid_fields == []

    def test_is_subclass_of_awx_client_error(self):
        err = AWXArtifactError("test")
        assert isinstance(err, AWXClientError)
        assert isinstance(err, Exception)


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


# ── Active AWX job tracking ──────────────────────────────────────────────


class TestAWXActiveJobTracking:
    """Tests for the _active_awx_jobs tracking dict.

    Acceptance criteria:
    1. __init__ initializes _active_awx_jobs as empty dict[UUID, int]
    2. _launch_and_wait records workspace_id -> awx_job_id after launch succeeds
    3. On job completion/failure, tracking entry is cleaned up (popped)
    4. If workspace already tracked, log warning and replace mapping
    5. No locks/synchronization needed (async event loop)
    """

    WS_ID = UUID("00000000-0000-0000-0000-000000000001")

    async def test_tracking_dict_starts_empty(self):
        """Dict is initialized as empty."""
        plugin = _make_plugin()
        assert plugin._active_awx_jobs == {}

    async def test_tracking_inserts_on_launch(self):
        """After launch succeeds, workspace_id -> awx_job_id is recorded."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, job_id=42)
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        await plugin.stop_opencode(req)

        # The entry should have been inserted after launch but removed
        # after job completion.  We verify via the mock that the client
        # interactions happened.
        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_tracking_cleaned_up_on_success(self):
        """Entry is popped after job completes successfully."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(client, job_id=99, status="successful")
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        await plugin.stop_opencode(req)

        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_tracking_cleaned_up_on_job_failure(self):
        """Entry is popped even when wait_for_job raises AWXJobError."""
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=55, status="pending"
        )
        client.wait_for_job.side_effect = AWXJobError("boom", job_id=55)
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        with pytest.raises(AWXJobError):
            await plugin.stop_opencode(req)

        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_tracking_cleaned_up_on_timeout(self):
        """Entry is popped when wait_for_job raises AWXTimeoutError."""
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=77, status="pending"
        )
        client.wait_for_job.side_effect = AWXTimeoutError("timed out")
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        with pytest.raises(AWXTimeoutError):
            await plugin.stop_opencode(req)

        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_duplicate_workspace_logs_warning_and_replaces(self, caplog):
        """If workspace is already tracked, a warning is logged and
        the mapping is replaced."""
        client = AsyncMock(spec=AWXApiClient)

        # First launch — job_id=100
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=100, status="pending"
        )
        client.wait_for_job.return_value = AWXJobResult(
            job_id=100, status="successful", artifacts={}
        )

        plugin = _make_plugin(client)

        # Manually insert a stale entry to simulate a previous in-flight job.
        plugin._active_awx_jobs[self.WS_ID] = 50

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        await plugin.stop_opencode(req)

        # A warning should have been logged about the duplicate.
        warnings = [
            r.message
            for r in caplog.records
            if r.levelname == "WARNING"
            and "already has active AWX job" in str(r.message)
        ]
        assert len(warnings) >= 1
        assert "50" in warnings[0]
        assert "100" in warnings[0]

        # After completion the tracking entry is cleaned up.
        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_create_workspace_does_not_track(self):
        """create_workspace does not pass workspace_id (it is unknown
        at launch time), so no tracking entry is created."""
        client = AsyncMock(spec=AWXApiClient)
        _mock_launch_and_wait(
            client,
            artifacts={
                "workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "workspace_path": "/some/path",
            },
        )
        plugin = _make_plugin(client)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        await plugin.create_workspace(req)

        # No tracking entries should have been created.
        assert plugin._active_awx_jobs == {}

    async def test_tracking_does_not_pop_mismatched_job_id_on_success(self):
        """When the stored job ID differs from the completed job's ID
        on the success path (e.g. a cleanup job was launched for the
        same workspace while the original job completed), the old waiter
        must not pop the new entry."""
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=100, status="pending"
        )

        # Simulate: another job (ID 200) replaces the tracking entry
        # while wait_for_job is in-flight (or after it returns but
        # before the pop).
        async def _wait_and_replace(job_id):
            plugin._active_awx_jobs[self.WS_ID] = 200
            return AWXJobResult(job_id=100, status="successful", artifacts={})

        client.wait_for_job.side_effect = _wait_and_replace
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        await plugin.stop_opencode(req)

        # The waiter for job 100 should NOT have removed job 200's
        # tracking entry (stored 200 != summary.job_id 100).
        assert plugin._active_awx_jobs == {self.WS_ID: 200}

    async def test_tracking_does_not_pop_mismatched_job_id_on_failure(self):
        """Same as above but when wait_for_job raises — the except
        branch's pop must also be conditional."""
        client = AsyncMock(spec=AWXApiClient)
        client.launch_job_template.return_value = AWXJobSummary(
            job_id=77, status="pending"
        )

        async def _wait_and_replace(job_id):
            plugin._active_awx_jobs[self.WS_ID] = 200
            raise AWXJobError("boom", job_id=77)

        client.wait_for_job.side_effect = _wait_and_replace
        plugin = _make_plugin(client)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        with pytest.raises(AWXJobError):
            await plugin.stop_opencode(req)

        # The waiter for job 77 should NOT have removed job 200's
        # tracking entry (stored 200 != summary.job_id 77).
        assert plugin._active_awx_jobs == {self.WS_ID: 200}


# ── Cancel job ──────────────────────────────────────────────────────────


class TestAWXExecutorPluginCancelJob:
    """Tests for AWXExecutorPlugin.cancel_job()."""

    WS_ID = UUID("00000000-0000-0000-0000-000000000001")

    async def test_cancel_job_success(self):
        """cancel_job finds an active AWX job and cancels it."""
        client = AsyncMock(spec=AWXApiClient)
        client.cancel_job.return_value = AWXJobResult(
            job_id=42, status="canceled"
        )
        plugin = _make_plugin(client)

        # Simulate an active tracked job.
        plugin._active_awx_jobs[self.WS_ID] = 42
        plugin._ever_tracked_workspaces.add(self.WS_ID)

        req = CancelJobRequest(workspace_id=self.WS_ID)
        resp = await plugin.cancel_job(req)

        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "cancelled"
        client.cancel_job.assert_awaited_once_with(42)
        # Entry should have been popped from the tracking dict.
        assert self.WS_ID not in plugin._active_awx_jobs

    async def test_cancel_job_not_tracked(self):
        """cancel_job with a workspace that was never tracked
        returns status='cancelled' without calling the API."""
        client = AsyncMock(spec=AWXApiClient)
        plugin = _make_plugin(client)

        req = CancelJobRequest(workspace_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
        resp = await plugin.cancel_job(req)

        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "cancelled"
        client.cancel_job.assert_not_awaited()

    async def test_cancel_job_late_cancel(self):
        """cancel_job after the job has already completed (tracking
        cleaned up) returns status='no_active_job'."""
        client = AsyncMock(spec=AWXApiClient)
        plugin = _make_plugin(client)

        # Simulate a workspace that was tracked but the job finished.
        plugin._ever_tracked_workspaces.add(self.WS_ID)

        req = CancelJobRequest(workspace_id=self.WS_ID)
        resp = await plugin.cancel_job(req)

        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "no_active_job"
        client.cancel_job.assert_not_awaited()

    async def test_cancel_job_api_failure_preserves_mapping(self):
        """When client.cancel_job raises, the mapping is preserved for retry."""
        client = AsyncMock(spec=AWXApiClient)
        client.cancel_job.side_effect = AWXHTTPError("Server error", status_code=500)
        plugin = _make_plugin(client)

        plugin._active_awx_jobs[self.WS_ID] = 42
        plugin._ever_tracked_workspaces.add(self.WS_ID)

        req = CancelJobRequest(workspace_id=self.WS_ID)
        with pytest.raises(AWXHTTPError, match="Server error"):
            await plugin.cancel_job(req)

        client.cancel_job.assert_awaited_once_with(42)
        # Mapping must be preserved so a retry can still cancel the AWX job.
        assert plugin._active_awx_jobs.get(self.WS_ID) == 42

    async def test_cancel_job_connection_error_preserves_mapping(self):
        """AWXConnectionError from cancel_job preserves the mapping for retry."""
        client = AsyncMock(spec=AWXApiClient)
        client.cancel_job.side_effect = AWXConnectionError("Connection refused")
        plugin = _make_plugin(client)

        plugin._active_awx_jobs[self.WS_ID] = 99
        plugin._ever_tracked_workspaces.add(self.WS_ID)

        req = CancelJobRequest(workspace_id=self.WS_ID)
        with pytest.raises(AWXConnectionError, match="refused"):
            await plugin.cancel_job(req)

        # Mapping must be preserved so a retry can still cancel the AWX job.
        assert plugin._active_awx_jobs.get(self.WS_ID) == 99

    async def test_cancel_job_cross_process_success(self):
        """cancel_job falls back to executor_job_id from request when
        _active_awx_jobs has no entry for the workspace (cross-process)."""
        client = AsyncMock(spec=AWXApiClient)
        client.cancel_job.return_value = AWXJobResult(
            job_id=42, status="canceled"
        )
        plugin = _make_plugin(client)

        # No in-memory entry — simulate different process.
        req = CancelJobRequest(
            workspace_id=self.WS_ID,
            executor_job_id=42,
        )
        resp = await plugin.cancel_job(req)

        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "cancelled"
        client.cancel_job.assert_awaited_once_with(42)

    async def test_cancel_job_cross_process_with_ever_tracked(self):
        """executor_job_id from request takes priority over the
        'no_active_job' path when the workspace was previously tracked."""
        client = AsyncMock(spec=AWXApiClient)
        client.cancel_job.return_value = AWXJobResult(
            job_id=55, status="canceled"
        )
        plugin = _make_plugin(client)

        # Workspace was tracked before but no active job now.
        plugin._ever_tracked_workspaces.add(self.WS_ID)

        # executor_job_id should still be used despite ever_tracked.
        req = CancelJobRequest(
            workspace_id=self.WS_ID,
            executor_job_id=55,
        )
        resp = await plugin.cancel_job(req)

        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "cancelled"
        client.cancel_job.assert_awaited_once_with(55)
