"""Tests for cross-process cancellation (issue #190).

Verifies that ``executor_job_id`` is persisted to the database inside
``_launch_and_wait()`` immediately after ``launch_job_template()`` returns,
*before* ``wait_for_job()`` starts, so that a concurrent abort handler in
a different process can read the active AWX job ID from the DB while the
job is still in-flight.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.executors.awx.client import AWXApiClient, AWXJobResult, AWXJobSummary
from app.executors.awx.plugin import AWXExecutorPlugin
from app.executors.models import (
    CleanupWorkspaceRequest,
    StartOpencodeRequest,
    StopOpencodeRequest,
)


# ── Constants ───────────────────────────────────────────────────────────

_CREATE_TEMPLATE = 10
_LIFECYCLE_TEMPLATE = 20
_TEARDOWN_TEMPLATE = 30
_BASE_PATH = "/home/runner/workspaces"


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_plugin(client: AWXApiClient | None = None) -> AWXExecutorPlugin:
    """Create a fully-wired AWXExecutorPlugin with a mock client."""
    if client is None:
        client = AsyncMock(spec=AWXApiClient)
    return AWXExecutorPlugin(
        client=client,
        create_workspace_template_id=_CREATE_TEMPLATE,
        opencode_lifecycle_template_id=_LIFECYCLE_TEMPLATE,
        workspace_teardown_template_id=_TEARDOWN_TEMPLATE,
        workspace_base_path=_BASE_PATH,
    )


# ── Tests ───────────────────────────────────────────────────────────────


class TestCrossProcessCancel:
    """Verify executor_job_id is persisted before wait_for_job completes.

    Acceptance criteria (issue #190):
    1. executor_job_id is persisted inside _launch_and_wait() after
       launch_job_template() returns, before wait_for_job() starts.
    2. The callback correctly writes to the DB while the AWX job is
       still in-flight.
    3. No regression — existing cancel tests still pass.
    """

    WS_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    GW_JOB_ID = UUID("11111111-1111-1111-1111-111111111111")

    # ── start_opencode ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_callback_invoked_before_wait_for_job_start(self):
        """The on_awx_job_launched callback fires before wait_for_job
        is called, proving the DB write happens during the AWX job's
        execution (not after completion)."""
        call_order: list[str] = []
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=201, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            call_order.append("wait_for_job")
            return AWXJobResult(
                job_id=201, status="successful",
                artifacts={
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 9090,
                },
            )

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        async def _on_launch(gw_id: UUID, awx_id: int) -> None:
            call_order.append("callback")
            assert awx_id == 201
            assert gw_id == self.GW_JOB_ID

        req = StartOpencodeRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        await plugin.start_opencode(req, on_awx_job_launched=_on_launch)

        # The callback must have fired before wait_for_job was called.
        assert call_order == ["callback", "wait_for_job"], (
            f"Expected callback before wait_for_job, got: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_callback_writes_to_db_before_wait_returns(self):
        """The on_awx_job_launched callback writes executor_job_id to
        the mock DB connection before wait_for_job returns, simulating
        the cross-process scenario where a second worker reads the DB
        while the first worker is still blocked on wait_for_job."""
        mock_conn = AsyncMock()
        call_order: list[str] = []
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=201, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            call_order.append("wait_for_job")
            return AWXJobResult(
                job_id=201, status="successful",
                artifacts={
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 9090,
                },
            )

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        async def _on_launch(gw_id: UUID, awx_id: int) -> None:
            call_order.append("callback")
            # Simulate the real handler: UPDATE gateway_jobs SET executor_job_id
            await mock_conn.execute(
                "UPDATE gateway_jobs SET executor_job_id = $2 WHERE id = $1",
                gw_id,
                str(awx_id),
            )

        req = StartOpencodeRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        await plugin.start_opencode(req, on_awx_job_launched=_on_launch)

        # The callback (DB write) must happen before wait_for_job.
        assert call_order == ["callback", "wait_for_job"], (
            f"Expected callback before wait_for_job, got: {call_order}"
        )
        # Verify the DB write was actually executed with the correct values.
        mock_conn.execute.assert_awaited_once_with(
            "UPDATE gateway_jobs SET executor_job_id = $2 WHERE id = $1",
            self.GW_JOB_ID,
            str(201),
        )

    @pytest.mark.asyncio
    async def test_callback_receives_correct_awx_job_id(self):
        """The on_awx_job_launched callback receives the correct AWX
        job ID from the just-launched job template."""
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=505, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            return AWXJobResult(
                job_id=505, status="successful",
                artifacts={
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 9090,
                },
            )

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        captured_gw_id: UUID | None = None
        captured_awx_id: int | None = None

        async def _on_launch(gw_id: UUID, awx_id: int) -> None:
            nonlocal captured_gw_id, captured_awx_id
            captured_gw_id = gw_id
            captured_awx_id = awx_id

        req = StartOpencodeRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        await plugin.start_opencode(req, on_awx_job_launched=_on_launch)

        assert captured_gw_id == self.GW_JOB_ID
        assert captured_awx_id == 505

    # ── stop_opencode ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stop_opencode_callback(self):
        """The on_awx_job_launched callback also works for stop_opencode."""
        call_order: list[str] = []
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=202, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            call_order.append("wait_for_job")
            return AWXJobResult(job_id=202, status="successful", artifacts={})

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        async def _on_launch(gw_id: UUID, awx_id: int) -> None:
            call_order.append("callback")
            assert awx_id == 202

        req = StopOpencodeRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        await plugin.stop_opencode(req, on_awx_job_launched=_on_launch)

        assert call_order == ["callback", "wait_for_job"], (
            f"Expected callback before wait_for_job, got: {call_order}"
        )

    # ── cleanup_workspace ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cleanup_workspace_callback(self):
        """The on_awx_job_launched callback also works for cleanup_workspace."""
        call_order: list[str] = []
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=203, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            call_order.append("wait_for_job")
            return AWXJobResult(job_id=203, status="successful", artifacts={})

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        async def _on_launch(gw_id: UUID, awx_id: int) -> None:
            call_order.append("callback")
            assert awx_id == 203

        req = CleanupWorkspaceRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        await plugin.cleanup_workspace(req, on_awx_job_launched=_on_launch)

        assert call_order == ["callback", "wait_for_job"], (
            f"Expected callback before wait_for_job, got: {call_order}"
        )

    # ── Callback omitted ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_callback_no_error(self):
        """When no on_awx_job_launched is passed, the lifecycle method
        works as before (regression guard)."""
        client = AsyncMock(spec=AWXApiClient)

        client.launch_job_template.return_value = AWXJobSummary(
            job_id=301, status="pending",
        )

        async def _wait_for_job(job_id: int) -> AWXJobResult:
            return AWXJobResult(
                job_id=301, status="successful",
                artifacts={
                    "session_id": "00000000-1111-2222-3333-444444444444",
                    "port": 9090,
                },
            )

        client.wait_for_job.side_effect = _wait_for_job
        plugin = _make_plugin(client)

        req = StartOpencodeRequest(
            workspace_id=self.WS_ID,
            gateway_job_id=self.GW_JOB_ID,
        )
        # No callback passed — must not raise.
        resp = await plugin.start_opencode(req)
        assert resp.status == "successful"
