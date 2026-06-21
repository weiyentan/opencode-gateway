"""Tests for the ExecutorPlugin ABC and associated Pydantic models."""

import os
from collections.abc import Awaitable
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from app.executors import ExecutorPlugin
from app.executors.local import LocalExecutor
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

# ---------------------------------------------------------------------------
# Helper: concrete executor subclass
# ---------------------------------------------------------------------------


class _FakeExecutor(ExecutorPlugin):
    """Minimal concrete executor that implements all 7 abstract methods."""

    name = "fake"

    async def create_workspace(self, request: CreateWorkspaceRequest) -> CreateWorkspaceResponse:
        return CreateWorkspaceResponse(
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_path="/tmp/ws",
            status="ready",
        )

    async def start_opencode(self, request: StartOpencodeRequest) -> StartOpencodeResponse:
        return StartOpencodeResponse(
            session_id="00000000-0000-0000-0000-000000000002",
            status="running",
            port=8080,
        )

    async def stop_opencode(self, request: StopOpencodeRequest) -> StopOpencodeResponse:
        return StopOpencodeResponse(status="stopped")

    async def restart_opencode(self, request: RestartOpencodeRequest) -> RestartOpencodeResponse:
        return RestartOpencodeResponse(status="running")

    async def collect_state(self, request: CollectStateRequest) -> CollectStateResponse:
        return CollectStateResponse(
            workspace_id=request.workspace_id,
            status=WorkspaceState.READY,
            process_status="running",
            port=8080,
        )

    async def cleanup_workspace(self, request: CleanupWorkspaceRequest) -> CleanupWorkspaceResponse:
        return CleanupWorkspaceResponse(status="cleaned")

    async def cancel_job(self, request: CancelJobRequest) -> CancelJobResponse:
        return CancelJobResponse(status="cancelled")


# ---------------------------------------------------------------------------
# ABC tests
# ---------------------------------------------------------------------------


class TestExecutorPluginABC:
    """Tests for the ExecutorPlugin abstract base class."""

    def test_cannot_instantiate_directly(self):
        """Instantiating ExecutorPlugin directly should raise TypeError."""

        class Incomplete(ExecutorPlugin):
            name = "incomplete"
            # missing all abstract methods

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_can_instantiate_full_implementation(self):
        """A subclass that implements all 7 methods should instantiate fine."""
        executor = _FakeExecutor()
        assert executor.name == "fake"

    def test_init_signature_is_enforced_when_missing_method(self):
        """Missing even one abstract method should prevent instantiation."""

        class MissingOne(ExecutorPlugin):
            name = "missing-one"

            async def create_workspace(self, request): ...  # type: ignore[override]
            async def start_opencode(self, request): ...  # type: ignore[override]
            async def stop_opencode(self, request): ...  # type: ignore[override]
            # restart_opencode is intentionally missing
            async def collect_state(self, request): ...  # type: ignore[override]
            async def cleanup_workspace(self, request): ...  # type: ignore[override]

        with pytest.raises(TypeError):
            MissingOne()  # type: ignore[abstract]

    def test_missing_cancel_job_prevents_instantiation(self):
        """A subclass missing only cancel_job should raise TypeError."""
        class MissingCancelJob(ExecutorPlugin):
            name = "missing-cancel-job"

            async def create_workspace(self, request): ...
            async def start_opencode(self, request): ...
            async def stop_opencode(self, request): ...
            async def restart_opencode(self, request): ...
            async def collect_state(self, request): ...
            async def cleanup_workspace(self, request): ...

        with pytest.raises(TypeError):
            MissingCancelJob()

    def test_abc_declares_name_annotation(self):
        """ExecutorPlugin ABC should declare name in its annotations."""
        assert "name" in ExecutorPlugin.__annotations__
        assert ExecutorPlugin.__annotations__["name"] == "str"


class TestExecutorPluginIsAsync:
    """Each method returns an awaitable so the Gateway can call it with await."""

    def test_create_workspace_is_async(self):
        result = _FakeExecutor().create_workspace(
            CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        )
        assert isinstance(result, Awaitable)

    def test_start_opencode_is_async(self):
        result = _FakeExecutor().start_opencode(
            StartOpencodeRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)

    def test_stop_opencode_is_async(self):
        result = _FakeExecutor().stop_opencode(
            StopOpencodeRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)

    def test_restart_opencode_is_async(self):
        result = _FakeExecutor().restart_opencode(
            RestartOpencodeRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)

    def test_collect_state_is_async(self):
        result = _FakeExecutor().collect_state(
            CollectStateRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)

    def test_cleanup_workspace_is_async(self):
        result = _FakeExecutor().cleanup_workspace(
            CleanupWorkspaceRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)

    def test_cancel_job_is_async(self):
        result = _FakeExecutor().cancel_job(
            CancelJobRequest(workspace_id="00000000-0000-0000-0000-000000000001")
        )
        assert isinstance(result, Awaitable)


# ---------------------------------------------------------------------------
# Pydantic model tests — CreateWorkspace
# ---------------------------------------------------------------------------


class TestCreateWorkspaceModels:
    """Tests for CreateWorkspaceRequest / CreateWorkspaceResponse."""

    def test_request_minimal_fields(self):
        """repo_url is the only required field."""
        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        assert req.repo_url == "https://example.com/repo.git"
        assert req.branch is None
        assert req.job_id is None
        assert req.env_vars == {}

    def test_request_all_fields(self):
        """All fields should be accepted."""
        req = CreateWorkspaceRequest(
            repo_url="https://example.com/repo.git",
            branch="feature/x",
            job_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            env_vars={"MY_VAR": "my_value"},
        )
        assert req.branch == "feature/x"
        assert str(req.job_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert req.env_vars == {"MY_VAR": "my_value"}

    def test_request_missing_repo_url_raises(self):
        """repo_url is required — missing it should raise ValidationError."""
        with pytest.raises(ValidationError):
            CreateWorkspaceRequest()

    def test_response_fields(self):
        resp = CreateWorkspaceResponse(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            workspace_path="/home/runner/ws/1",
            status="ready",
        )
        assert str(resp.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert resp.workspace_path == "/home/runner/ws/1"
        assert resp.status == "ready"

    def test_response_serializes_to_json(self):
        resp = CreateWorkspaceResponse(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            workspace_path="/tmp/ws",
            status="ready",
        )
        data = resp.model_dump(mode="json")
        assert data["workspace_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert data["workspace_path"] == "/tmp/ws"
        assert data["status"] == "ready"


# ---------------------------------------------------------------------------
# Pydantic model tests — StartOpencode
# ---------------------------------------------------------------------------


class TestStartOpencodeModels:
    """Tests for StartOpencodeRequest / StartOpencodeResponse."""

    def test_request_required_fields(self):
        req = StartOpencodeRequest(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            workspace_path="/tmp/ws",
        )
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert req.workspace_path == "/tmp/ws"
        assert req.env_vars == {}

    def test_request_with_env_vars(self):
        """env_vars should be accepted and stored."""
        req = StartOpencodeRequest(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            env_vars={"FOO": "bar", "BAZ": "qux"},
        )
        assert req.env_vars == {"FOO": "bar", "BAZ": "qux"}

    def test_request_minimal(self):
        """workspace_path defaults to None."""
        req = StartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert req.workspace_path is None

    def test_request_port_field_exists_and_defaults_to_none(self):
        """StartOpencodeRequest has a port field (Optional[int]) defaulting to None."""
        req = StartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert req.port is None

    def test_request_port_accepts_custom_value(self):
        """port field should accept an explicit integer value."""
        req = StartOpencodeRequest(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            port=9090,
        )
        assert req.port == 9090

    def test_response_fields(self):
        resp = StartOpencodeResponse(
            session_id="00000000-1111-2222-3333-444444444444",
            status="running",
            port=8080,
        )
        assert str(resp.session_id) == "00000000-1111-2222-3333-444444444444"
        assert resp.status == "running"
        assert resp.port == 8080

    def test_response_serializes_to_json(self):
        resp = StartOpencodeResponse(
            session_id="00000000-1111-2222-3333-444444444444",
            status="running",
            port=8080,
        )
        data = resp.model_dump(mode="json")
        assert data == {
            "session_id": "00000000-1111-2222-3333-444444444444",
            "status": "running",
            "port": 8080,
        }


# ---------------------------------------------------------------------------
# Pydantic model tests — StopOpencode
# ---------------------------------------------------------------------------


class TestStopOpencodeModels:
    """Tests for StopOpencodeRequest / StopOpencodeResponse."""

    def test_request_required_fields(self):
        req = StopOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_request_missing_workspace_id_raises(self):
        with pytest.raises(ValidationError):
            StopOpencodeRequest()

    def test_response_fields(self):
        resp = StopOpencodeResponse(status="stopped")
        assert resp.status == "stopped"

    def test_response_serializes_to_json(self):
        resp = StopOpencodeResponse(status="stopped")
        data = resp.model_dump(mode="json")
        assert data == {"status": "stopped"}


# ---------------------------------------------------------------------------
# Pydantic model tests — RestartOpencode
# ---------------------------------------------------------------------------


class TestRestartOpencodeModels:
    """Tests for RestartOpencodeRequest / RestartOpencodeResponse."""

    def test_request_required_fields(self):
        req = RestartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_response_fields(self):
        resp = RestartOpencodeResponse(status="running")
        assert resp.status == "running"


# ---------------------------------------------------------------------------
# Pydantic model tests — CollectState
# ---------------------------------------------------------------------------


class TestCollectStateModels:
    """Tests for CollectStateRequest / CollectStateResponse."""

    def test_request_required_fields(self):
        req = CollectStateRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_response_fields(self):
        resp = CollectStateResponse(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            status=WorkspaceState.RUNNING,
            process_status="running",
            port=8080,
        )
        assert resp.status == WorkspaceState.RUNNING
        assert resp.process_status == "running"
        assert resp.port == 8080

    def test_workspace_state_enum_values(self):
        """WorkspaceState enum should have the expected members."""
        assert WorkspaceState.READY.value == "ready"
        assert WorkspaceState.RUNNING.value == "running"
        assert WorkspaceState.STOPPED.value == "stopped"
        assert WorkspaceState.ERROR.value == "error"

    def test_response_serializes_to_json(self):
        resp = CollectStateResponse(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            status=WorkspaceState.RUNNING,
            process_status="running",
            port=8080,
        )
        data = resp.model_dump(mode="json")
        assert data["status"] == "running"
        assert data["process_status"] == "running"
        assert data["port"] == 8080


# ---------------------------------------------------------------------------
# Pydantic model tests — CleanupWorkspace
# ---------------------------------------------------------------------------


class TestCleanupWorkspaceModels:
    """Tests for CleanupWorkspaceRequest / CleanupWorkspaceResponse."""

    def test_request_required_fields(self):
        req = CleanupWorkspaceRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_response_fields(self):
        resp = CleanupWorkspaceResponse(status="cleaned")
        assert resp.status == "cleaned"


# ---------------------------------------------------------------------------
# Pydantic model tests — CancelJob
# ---------------------------------------------------------------------------


class TestCancelJobModels:
    """Tests for CancelJobRequest / CancelJobResponse."""

    def test_request_required_fields(self):
        req = CancelJobRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert str(req.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    async def test_request_missing_workspace_id_defaults_to_none(self):
        """CancelJobRequest.workspace_id defaults to None when not provided."""
        req = CancelJobRequest()
        assert req.workspace_id is None
        assert req.executor_job_id is None

    def test_request_rejects_non_uuid(self):
        """CancelJobRequest should reject non-UUID workspace_id values."""
        with pytest.raises(ValidationError):
            CancelJobRequest(workspace_id="not-a-uuid")

    def test_response_fields(self):
        resp = CancelJobResponse(status="cancelled")
        assert resp.status == "cancelled"

    def test_response_accepts_any_string_status(self):
        """CancelJobResponse.status should accept any string."""
        resp = CancelJobResponse(status="already_done")
        assert resp.status == "already_done"

    def test_response_serializes_to_json(self):
        resp = CancelJobResponse(status="cancelled")
        data = resp.model_dump(mode="json")
        assert data == {"status": "cancelled"}


# ---------------------------------------------------------------------------
# Model type enforcement
# ---------------------------------------------------------------------------


class TestModelsArePydantic:
    """All request and response models must be Pydantic BaseModel subclasses."""

    def test_request_models_are_pydantic(self):
        assert issubclass(CreateWorkspaceRequest, BaseModel)
        assert issubclass(StartOpencodeRequest, BaseModel)
        assert issubclass(StopOpencodeRequest, BaseModel)
        assert issubclass(RestartOpencodeRequest, BaseModel)
        assert issubclass(CollectStateRequest, BaseModel)
        assert issubclass(CleanupWorkspaceRequest, BaseModel)
        assert issubclass(CancelJobRequest, BaseModel)

    def test_response_models_are_pydantic(self):
        assert issubclass(CreateWorkspaceResponse, BaseModel)
        assert issubclass(StartOpencodeResponse, BaseModel)
        assert issubclass(StopOpencodeResponse, BaseModel)
        assert issubclass(RestartOpencodeResponse, BaseModel)
        assert issubclass(CollectStateResponse, BaseModel)
        assert issubclass(CleanupWorkspaceResponse, BaseModel)
        assert issubclass(CancelJobResponse, BaseModel)


# ---------------------------------------------------------------------------
# LocalExecutor tests
# ---------------------------------------------------------------------------


class TestLocalExecutor:
    """Tests for the LocalExecutor concrete implementation."""

    def test_can_instantiate_directly(self):
        executor = LocalExecutor()
        assert executor.name == "local"
        assert isinstance(executor, ExecutorPlugin)

    async def test_create_workspace_creates_directory(self):
        executor = LocalExecutor()
        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        resp = await executor.create_workspace(req)
        assert os.path.isdir(resp.workspace_path)
        assert UUID(str(resp.workspace_id))
        assert resp.status == "ready"

    async def test_create_and_cleanup_workspace(self):
        executor = LocalExecutor()
        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        resp = await executor.create_workspace(req)
        path = resp.workspace_path
        assert os.path.isdir(path)
        # Now clean it up
        cleanup_req = CleanupWorkspaceRequest(workspace_id=resp.workspace_id)
        cleanup_resp = await executor.cleanup_workspace(cleanup_req)
        assert cleanup_resp.status == "cleaned"
        assert not os.path.exists(path)

    async def test_start_opencode_returns_session(self):
        executor = LocalExecutor()
        req = StartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.start_opencode(req)
        assert resp.status == "running"
        assert resp.port > 0
        assert UUID(str(resp.session_id))

    async def test_start_opencode_with_env_vars(self):
        """env_vars should be stored on the executor for test verification."""
        executor = LocalExecutor()
        req = StartOpencodeRequest(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            env_vars={"MY_SECRET": "s3cret", "LOG_LEVEL": "debug"},
        )
        resp = await executor.start_opencode(req)
        assert resp.status == "running"
        assert executor._last_env_vars == {"MY_SECRET": "s3cret", "LOG_LEVEL": "debug"}

    async def test_start_opencode_default_port_is_8080(self):
        """When port is not provided, LocalExecutor defaults to 8080."""
        executor = LocalExecutor()
        req = StartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.start_opencode(req)
        assert resp.port == 8080

    async def test_start_opencode_uses_custom_port(self):
        """When port is provided, LocalExecutor uses it."""
        executor = LocalExecutor()
        req = StartOpencodeRequest(
            workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            port=9090,
        )
        resp = await executor.start_opencode(req)
        assert resp.port == 9090
        assert executor._last_port == 9090

    async def test_stop_opencode(self):
        executor = LocalExecutor()
        req = StopOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.stop_opencode(req)
        assert resp.status == "stopped"

    async def test_restart_opencode(self):
        executor = LocalExecutor()
        req = RestartOpencodeRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.restart_opencode(req)
        assert resp.status == "running"

    async def test_collect_state(self):
        executor = LocalExecutor()
        req = CollectStateRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.collect_state(req)
        assert resp.workspace_id == req.workspace_id

    async def test_cancel_job_returns_cancelled(self):
        """cancel_job is a no-op that returns status='cancelled'."""
        executor = LocalExecutor()
        req = CancelJobRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        resp = await executor.cancel_job(req)
        assert isinstance(resp, CancelJobResponse)
        assert resp.status == "cancelled"

    async def test_cancel_job_no_side_effects(self):
        """cancel_job does not mutate any executor state."""
        executor = LocalExecutor()
        req = CancelJobRequest(workspace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        # Snapshot initial state
        before = dict(executor.__dict__)
        await executor.cancel_job(req)
        # State must be identical after the call
        assert dict(executor.__dict__) == before


# ---------------------------------------------------------------------------
# Plugin loader / factory tests
# ---------------------------------------------------------------------------


def test_get_executor_default_local():
    """When GATEWAY_EXECUTOR_TYPE is not set, get_executor returns a LocalExecutor."""
    from app.executors.factory import get_executor

    executor = get_executor()
    from app.executors.local import LocalExecutor

    assert isinstance(executor, LocalExecutor)


def test_executor_registry_shape():
    """EXECUTOR_REGISTRY maps 'local' to LocalExecutor."""
    from app.executors import EXECUTOR_REGISTRY
    from app.executors.local import LocalExecutor

    assert "local" in EXECUTOR_REGISTRY
    assert EXECUTOR_REGISTRY["local"] is LocalExecutor


def test_get_executor_unknown_type(monkeypatch):
    """An unknown GATEWAY_EXECUTOR_TYPE should raise a clear ValueError."""
    monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "nonexistent")

    # Settings is cached at module level — reload the factory so it picks
    # up the new env value.
    import importlib

    import app.executors.factory
    importlib.reload(app.executors.factory)

    from app.executors.factory import get_executor

    with pytest.raises(ValueError, match="Unknown executor type"):
        get_executor()
