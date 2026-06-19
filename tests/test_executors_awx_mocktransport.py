"""MockTransport tests for AWXExecutorPlugin.

Uses ``httpx.MockTransport`` to simulate the AWX REST API, exercising
the full request/response path through both ``AWXApiClient`` and
``AWXExecutorPlugin`` without real network calls.

Follows the same pattern as ``test_serve_client_comprehensive.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest

from app.executors.awx.client import AWXApiClient
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

# ── Constants ────────────────────────────────────────────────────────────

CREATE_TEMPLATE = 10
LIFECYCLE_TEMPLATE = 20
TEARDOWN_TEMPLATE = 30
BASE_PATH = "/home/runner/workspaces"
BASE_URL = "https://awx.example.com"
TEST_TOKEN = "test-token-abc123"

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_client(handler) -> AWXApiClient:
    """Build an ``AWXApiClient`` wired to an ``httpx.MockTransport``."""
    client = AWXApiClient(
        base_url=BASE_URL,
        token=TEST_TOKEN,
        timeout_seconds=300,
        poll_interval_seconds=5,
    )
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(300),
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    return client


def _make_plugin(handler, **overrides: int) -> AWXExecutorPlugin:
    """Create a fully-wired AWXExecutorPlugin with a mock transport client."""
    client = _make_client(handler)
    return AWXExecutorPlugin(
        client=client,
        create_workspace_template_id=overrides.get("create_id", CREATE_TEMPLATE),
        opencode_lifecycle_template_id=overrides.get("lifecycle_id", LIFECYCLE_TEMPLATE),
        workspace_teardown_template_id=overrides.get("teardown_id", TEARDOWN_TEMPLATE),
        workspace_base_path=overrides.get("base_path", BASE_PATH),
    )


def _make_awx_handler(
    launch_response: dict | None = None,
    job_responses: list[dict] | None = None,
    artifact_response: dict | None = None,
    cancel_response: dict | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a stateful handler that simulates an AWX job lifecycle.

    The handler distinguishes between:
    - ``POST .../launch/`` → returns ``launch_response``
    - ``POST .../cancel/`` → returns ``cancel_response``
    - ``GET .../jobs/<id>/`` → cycles through ``job_responses``,
      then returns ``artifact_response`` for the final artifacts fetch.

    Args:
        launch_response: Response for the launch endpoint.
        job_responses: Ordered list of responses for the job status endpoint.
        artifact_response: Final response after job reaches terminal status
            (used for the artifacts fetch in the successful case).
        cancel_response: Response for the cancel endpoint.

    Returns:
        A callable ``(httpx.Request) -> httpx.Response``.
    """
    if launch_response is None:
        launch_response = {"id": 42, "status": "pending"}
    if job_responses is None:
        job_responses = [
            {
                "id": 42,
                "status": "successful",
                "started": "2024-01-01T00:00:00Z",
                "finished": "2024-01-01T00:05:00Z",
            },
        ]
    if artifact_response is None:
        artifact_response = job_responses[-1].copy()
        if "artifacts" not in artifact_response:
            artifact_response["artifacts"] = {}

    call_index = 0
    job_completed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_index, job_completed
        url = str(request.url)

        # Launch endpoint
        if "/launch/" in url:
            return httpx.Response(200, json=launch_response)

        # Cancel endpoint
        if "/cancel/" in url:
            return httpx.Response(200, json=cancel_response or artifact_response)

        # Job status endpoint
        if "/jobs/" in url:
            if job_completed:
                return httpx.Response(200, json=artifact_response)

            if call_index < len(job_responses):
                resp_data = job_responses[call_index]
                call_index += 1
                status = resp_data.get("status", "")
                if status in ("successful", "failed", "error", "canceled"):
                    job_completed = True
                return httpx.Response(200, json=resp_data)

            return httpx.Response(200, json=job_responses[-1])

        return httpx.Response(404)

    return handler


# ══════════════════════════════════════════════════════════════════════════
# 1.  CREATE WORKSPACE
# ══════════════════════════════════════════════════════════════════════════


class TestCreateWorkspace:
    """AWXExecutorPlugin.create_workspace()."""

    @pytest.mark.asyncio
    async def test_creates_workspace_with_minimal_request(self) -> None:
        """Should create a workspace with just a repo_url."""
        handler = _make_awx_handler(
            launch_response={"id": 101, "status": "pending"},
            job_responses=[
                {
                    "id": 101,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ],
            artifact_response={
                "id": 101,
                "status": "successful",
                "artifacts": {
                    "workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "workspace_path": "/home/runner/workspaces/ws1",
                },
            },
        )
        plugin = _make_plugin(handler)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.create_workspace(req)

        assert isinstance(resp, CreateWorkspaceResponse)
        assert resp.status == "successful"
        assert resp.workspace_path == "/home/runner/workspaces/ws1"
        assert str(resp.workspace_id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @pytest.mark.asyncio
    async def test_creates_workspace_with_branch_and_job_id(self) -> None:
        """Should create a workspace with branch and job_id."""
        job_id = UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["branch"] == "feature/x"
            assert body["extra_vars"]["job_id"] == str(job_id)
            return httpx.Response(200, json={"id": 102, "status": "pending"})

        job_responses = [
            {
                "id": 102,
                "status": "successful",
                "started": "2024-01-01T00:00:00Z",
                "finished": "2024-01-01T00:05:00Z",
            },
        ]

        call_index = [0]
        artifacts_sent = [False]

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return launch_handler(request)
            if "/jobs/" in url:
                if artifacts_sent[0]:
                    return httpx.Response(
                        200,
                        json={
                            "id": 102,
                            "status": "successful",
                            "artifacts": {
                                "workspace_id": "11111111-2222-3333-4444-555555555555",
                                "workspace_path": "/tmp/ws",
                            },
                        },
                    )
                idx = call_index[0]
                if idx < len(job_responses):
                    call_index[0] += 1
                    resp = job_responses[idx]
                    if resp.get("status") == "successful":
                        artifacts_sent[0] = True
                    return httpx.Response(200, json=resp)
                return httpx.Response(200, json=job_responses[-1])
            return httpx.Response(404)

        plugin = _make_plugin(handler)

        req = CreateWorkspaceRequest(
            repo_url="https://example.com/repo.git",
            branch="feature/x",
            job_id=job_id,
        )
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.create_workspace(req)

        assert resp.workspace_path == "/tmp/ws"
        assert str(resp.workspace_id) == "11111111-2222-3333-4444-555555555555"

    @pytest.mark.asyncio
    async def test_creates_workspace_with_missing_artifacts_raises(
        self,
    ) -> None:
        """When AWX returns no artifacts, an AWXArtifactError is raised."""
        handler = _make_awx_handler(
            launch_response={"id": 103, "status": "pending"},
            job_responses=[
                {
                    "id": 103,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:05:00Z",
                },
            ],
            artifact_response={
                "id": 103,
                "status": "successful",
                "artifacts": {},
            },
        )
        plugin = _make_plugin(handler)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXArtifactError) as exc_info:
                await plugin.create_workspace(req)

        err = exc_info.value
        assert err.template_name == "gateway-create-workspace"
        assert "workspace_id" in err.missing_fields
        assert "workspace_path" in err.missing_fields


# ══════════════════════════════════════════════════════════════════════════
# 2.  START / STOP / RESTART OPENCODE
# ══════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    """start_opencode, stop_opencode, restart_opencode."""

    WS_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    @pytest.mark.asyncio
    async def test_start_opencode_with_explicit_path(self) -> None:
        """Should start OpenCode with an explicit workspace path."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["action"] == "start"
            assert body["extra_vars"]["workspace_path"] == "/explicit/path"
            return httpx.Response(200, json={"id": 201, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 201, "status": "pending"},
            job_responses=[
                {
                    "id": 201,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:03:00Z",
                },
            ],
            artifact_response={
                "id": 201,
                "status": "successful",
                "artifacts": {
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 9090,
                },
            },
        )

        # Override the handler to also verify launch body
        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = StartOpencodeRequest(
            workspace_id=self.WS_ID,
            workspace_path="/explicit/path",
        )
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.start_opencode(req)

        assert isinstance(resp, StartOpencodeResponse)
        assert resp.status == "successful"
        assert resp.port == 9090
        assert str(resp.session_id) == "00000000-1111-2222-3333-444444444444"

    @pytest.mark.asyncio
    async def test_start_opencode_derives_path_from_workspace_id(self) -> None:
        """Should derive workspace path when no explicit path is given."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            expected_path = f"{BASE_PATH}/{self.WS_ID}"
            assert body["extra_vars"]["workspace_path"] == expected_path
            return httpx.Response(200, json={"id": 202, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 202, "status": "pending"},
            job_responses=[
                {
                    "id": 202,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:03:00Z",
                },
            ],
            artifact_response={
                "id": 202,
                "status": "successful",
                "artifacts": {
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 8080,
                },
            },
        )

        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = StartOpencodeRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.start_opencode(req)

        assert resp.port == 8080

    @pytest.mark.asyncio
    async def test_start_opencode_missing_artifacts_raises(self) -> None:
        """When AWX returns no artifacts, an AWXArtifactError is raised."""
        handler = _make_awx_handler(
            launch_response={"id": 203, "status": "pending"},
            job_responses=[
                {
                    "id": 203,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:03:00Z",
                },
            ],
            artifact_response={
                "id": 203,
                "status": "successful",
                "artifacts": {},
            },
        )
        plugin = _make_plugin(handler)

        req = StartOpencodeRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXArtifactError) as exc_info:
                await plugin.start_opencode(req)

        err = exc_info.value
        assert "gateway-opencode-lifecycle" in err.template_name
        assert "session_id" in err.missing_fields
        assert "port" in err.missing_fields

    @pytest.mark.asyncio
    async def test_stop_opencode(self) -> None:
        """Should stop OpenCode via the lifecycle template."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["action"] == "stop"
            return httpx.Response(200, json={"id": 301, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 301, "status": "pending"},
            job_responses=[
                {
                    "id": 301,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ],
            artifact_response={
                "id": 301,
                "status": "successful",
                "artifacts": {},
            },
        )

        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = StopOpencodeRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.stop_opencode(req)

        assert isinstance(resp, StopOpencodeResponse)
        assert resp.status == "successful"

    @pytest.mark.asyncio
    async def test_restart_opencode(self) -> None:
        """Should restart OpenCode via the lifecycle template."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["action"] == "restart"
            return httpx.Response(200, json={"id": 302, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 302, "status": "pending"},
            job_responses=[
                {
                    "id": 302,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ],
            artifact_response={
                "id": 302,
                "status": "successful",
                "artifacts": {},
            },
        )

        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = RestartOpencodeRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.restart_opencode(req)

        assert isinstance(resp, RestartOpencodeResponse)
        assert resp.status == "successful"


# ══════════════════════════════════════════════════════════════════════════
# 3.  COLLECT / CLEANUP
# ══════════════════════════════════════════════════════════════════════════


class TestTeardown:
    """collect_state and cleanup_workspace."""

    WS_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    @pytest.mark.asyncio
    async def test_collect_state(self) -> None:
        """Should collect workspace state via the teardown template."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["action"] == "collect"
            return httpx.Response(200, json={"id": 401, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 401, "status": "pending"},
            job_responses=[
                {
                    "id": 401,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ],
            artifact_response={
                "id": 401,
                "status": "successful",
                "artifacts": {
                    "status": "running",
                    "process_status": "active",
                    "port": 8080,
                },
            },
        )

        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = CollectStateRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.collect_state(req)

        assert isinstance(resp, CollectStateResponse)
        assert resp.workspace_id == self.WS_ID
        assert resp.status == WorkspaceState.RUNNING
        assert resp.process_status == "active"
        assert resp.port == 8080

    @pytest.mark.asyncio
    async def test_collect_state_unrecognised_status_defaults_to_error(self) -> None:
        """Unrecognised workspace status should default to ERROR."""
        handler = _make_awx_handler(
            launch_response={"id": 402, "status": "pending"},
            job_responses=[
                {
                    "id": 402,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ],
            artifact_response={
                "id": 402,
                "status": "successful",
                "artifacts": {"status": "bogus"},
            },
        )
        plugin = _make_plugin(handler)

        req = CollectStateRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.collect_state(req)

        assert resp.status == WorkspaceState.ERROR

    @pytest.mark.asyncio
    async def test_collect_state_missing_status_raises(self) -> None:
        """When artifacts lack a 'status' key, an AWXArtifactError is raised."""
        handler = _make_awx_handler(
            launch_response={"id": 403, "status": "pending"},
            job_responses=[
                {
                    "id": 403,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:02:00Z",
                },
            ],
            artifact_response={
                "id": 403,
                "status": "successful",
                "artifacts": {},
            },
        )
        plugin = _make_plugin(handler)

        req = CollectStateRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXArtifactError) as exc_info:
                await plugin.collect_state(req)

        err = exc_info.value
        assert "gateway-workspace-teardown" in err.template_name
        assert "status" in err.missing_fields

    @pytest.mark.asyncio
    async def test_cleanup_workspace(self) -> None:
        """Should cleanup workspace via the teardown template."""

        def launch_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["extra_vars"]["action"] == "cleanup"
            return httpx.Response(200, json={"id": 501, "status": "pending"})

        handler = _make_awx_handler(
            launch_response={"id": 501, "status": "pending"},
            job_responses=[
                {
                    "id": 501,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                },
            ],
            artifact_response={
                "id": 501,
                "status": "successful",
                "artifacts": {},
            },
        )

        original_handler = handler

        def verifying_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return launch_handler(request)
            return original_handler(request)

        plugin = _make_plugin(verifying_handler)

        req = CleanupWorkspaceRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            resp = await plugin.cleanup_workspace(req)

        assert isinstance(resp, CleanupWorkspaceResponse)
        assert resp.status == "successful"


# ══════════════════════════════════════════════════════════════════════════
# 4.  ERROR PROPAGATION
# ══════════════════════════════════════════════════════════════════════════


class TestErrorPropagation:
    """Verify that AWX errors propagate correctly through the plugin."""

    WS_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    @pytest.mark.asyncio
    async def test_create_workspace_connection_error(self) -> None:
        """AWXConnectionError from launch should propagate."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        plugin = _make_plugin(handler)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with pytest.raises(AWXConnectionError, match="refused"):
            await plugin.create_workspace(req)

    @pytest.mark.asyncio
    async def test_create_workspace_timeout_error(self) -> None:
        """AWXTimeoutError from wait_for_job should propagate."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return httpx.Response(200, json={"id": 99, "status": "pending"})
            # Always return "running" to simulate a stuck job
            return httpx.Response(200, json={"id": 99, "status": "running"})

        plugin = _make_plugin(handler)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with patch(
            "app.executors.awx.client.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with patch(
                "app.executors.awx.client.time.monotonic",
                side_effect=[0, 0, 99999],
            ):
                with pytest.raises(AWXTimeoutError):
                    await plugin.create_workspace(req)

    @pytest.mark.asyncio
    async def test_start_opencode_job_failure(self) -> None:
        """AWXJobError from a failed job should propagate."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return httpx.Response(200, json={"id": 77, "status": "pending"})
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "status": "failed",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                },
            )

        plugin = _make_plugin(handler)

        req = StartOpencodeRequest(workspace_id=UUID(int=1))
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXJobError):
                await plugin.start_opencode(req)

    @pytest.mark.asyncio
    async def test_stop_opencode_client_error(self) -> None:
        """AWXClientError should propagate from launch."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.HTTPError("Generic transport error")

        plugin = _make_plugin(handler)

        req = StopOpencodeRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXClientError):
            await plugin.stop_opencode(req)

    @pytest.mark.asyncio
    async def test_collect_state_timeout(self) -> None:
        """AWXTimeoutError should propagate from collect_state."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return httpx.Response(200, json={"id": 55, "status": "pending"})
            return httpx.Response(200, json={"id": 55, "status": "running"})

        plugin = _make_plugin(handler)

        req = CollectStateRequest(workspace_id=UUID(int=1))
        with patch(
            "app.executors.awx.client.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with patch(
                "app.executors.awx.client.time.monotonic",
                side_effect=[0, 0, 99999],
            ):
                with pytest.raises(AWXTimeoutError):
                    await plugin.collect_state(req)

    @pytest.mark.asyncio
    async def test_restart_opencode_http_error(self) -> None:
        """AWXHTTPError should propagate from restart."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "Service unavailable"})

        plugin = _make_plugin(handler)

        req = RestartOpencodeRequest(workspace_id=UUID(int=1))
        with pytest.raises(AWXHTTPError):
            await plugin.restart_opencode(req)

    @pytest.mark.asyncio
    async def test_cleanup_workspace_job_error(self) -> None:
        """AWXJobError should propagate from cleanup."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return httpx.Response(200, json={"id": 33, "status": "pending"})
            return httpx.Response(
                200,
                json={
                    "id": 33,
                    "status": "failed",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                },
            )

        plugin = _make_plugin(handler)

        req = CleanupWorkspaceRequest(workspace_id=UUID(int=1))
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AWXJobError):
                await plugin.cleanup_workspace(req)


# ══════════════════════════════════════════════════════════════════════════
# 5.  TEMPLATE ID VERIFICATION
# ══════════════════════════════════════════════════════════════════════════


class TestTemplateIDs:
    """Verify that each lifecycle method uses the correct template ID."""

    WS_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    @pytest.mark.asyncio
    async def test_create_workspace_uses_correct_template(self) -> None:
        """create_workspace should use CREATE_TEMPLATE."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert f"/api/v2/job_templates/{CREATE_TEMPLATE}/launch/" in url
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        # Second call will be for job status
        call_count = [0]

        def stateful_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/launch/" in url:
                return handler(request)
            call_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                    "artifacts": {
                        "workspace_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "workspace_path": "/some/path",
                    },
                },
            )

        plugin = _make_plugin(stateful_handler)

        req = CreateWorkspaceRequest(repo_url="https://example.com/repo.git")
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            await plugin.create_workspace(req)

    @pytest.mark.asyncio
    async def test_start_opencode_uses_correct_template(self) -> None:
        """start_opencode should use LIFECYCLE_TEMPLATE."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert f"/api/v2/job_templates/{LIFECYCLE_TEMPLATE}/launch/" in url
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        call_count = [0]

        def stateful_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return handler(request)
            call_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                    "artifacts": {
                        "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "port": 8080,
                    },
                },
            )

        plugin = _make_plugin(stateful_handler)

        req = StartOpencodeRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            await plugin.start_opencode(req)

    @pytest.mark.asyncio
    async def test_collect_state_uses_correct_template(self) -> None:
        """collect_state should use TEARDOWN_TEMPLATE."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert f"/api/v2/job_templates/{TEARDOWN_TEMPLATE}/launch/" in url
            return httpx.Response(200, json={"id": 1, "status": "pending"})

        call_count = [0]

        def stateful_handler(request: httpx.Request) -> httpx.Response:
            if "/launch/" in str(request.url):
                return handler(request)
            call_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "status": "successful",
                    "started": "2024-01-01T00:00:00Z",
                    "finished": "2024-01-01T00:01:00Z",
                    "artifacts": {"status": "running"},
                },
            )

        plugin = _make_plugin(stateful_handler)

        req = CollectStateRequest(workspace_id=self.WS_ID)
        with patch("app.executors.awx.client.asyncio.sleep", new_callable=AsyncMock):
            await plugin.collect_state(req)
