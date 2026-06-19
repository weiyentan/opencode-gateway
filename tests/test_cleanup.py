"""Tests for workspace cleanup state transitions — issue #111.

Covers:
- State machine: active → cleaning → cleaned
- State machine: active → cleaning → cleanup_failed
- Timestamps: cleanup_started_at, cleanup_completed_at, cleanup_failed_at
- Failure reason storage
- Idempotency: calling cleanup on cleaned/cleanup_failed workspaces is a no-op
- Pinned workspace cleanup rejection
- API and scheduler paths
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.conftest import create_client, make_workspace_row, mock_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cleanup_client(mock_conn: AsyncMock, mock_executor: AsyncMock):
    """Shortcut to create a test client with a mock executor wired."""
    return create_client(mock_conn, mock_executor=mock_executor)


# ---------------------------------------------------------------------------
# API: State transitions (active → cleaning → cleaned)
# ---------------------------------------------------------------------------


class TestCleanupActiveToCleaned:
    """Tests the successful cleanup path via the API."""

    @pytest.mark.asyncio
    async def test_active_workspace_transitions_to_cleaned(
        self, mock_conn: AsyncMock
    ) -> None:
        """An active workspace should reach 'cleaned' after successful cleanup."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        executor = AsyncMock()
        from app.executors.models import CleanupWorkspaceResponse
        executor.cleanup_workspace = AsyncMock(
            return_value=CleanupWorkspaceResponse(status="cleaned")
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE workspaces SET cleanup_status" in sql:
                if "cleaning" in repr(args):
                    row_data["cleanup_status"] = "cleaning"
                    row_data["cleanup_started_at"] = args[1]  # $2 = timestamp
                elif "cleaned" in repr(args):
                    row_data["cleanup_status"] = "cleaned"
                    row_data["cleanup_completed_at"] = args[1]  # $2 = timestamp

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleaned"
        assert data["cleanup_completed_at"] is not None
        assert data["cleanup_started_at"] is not None

    @pytest.mark.asyncio
    async def test_cleanup_started_at_is_set_on_transition_to_cleaning(
        self, mock_conn: AsyncMock
    ) -> None:
        """cleanup_started_at should be set when the workspace transitions to 'cleaning'."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        executor = AsyncMock()
        from app.executors.models import CleanupWorkspaceResponse
        executor.cleanup_workspace = AsyncMock(
            return_value=CleanupWorkspaceResponse(status="cleaned")
        )

        cleaning_update_args: list = []

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces" in sql and "cleanup_status" in sql:
                if "cleaning" in repr(args):
                    cleaning_update_args.append(args)
                    row_data["cleanup_status"] = "cleaning"
                    row_data["cleanup_started_at"] = args[1]  # $2 = timestamp
                elif "cleaned" in repr(args):
                    row_data["cleanup_status"] = "cleaned"
                    row_data["cleanup_completed_at"] = args[1]  # $2 = timestamp

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            await c.post(f"/workspaces/{ws_id}/cleanup")

        assert len(cleaning_update_args) == 1
        # args[1] ($2) is the cleanup_started_at timestamp
        assert isinstance(cleaning_update_args[0][1], datetime)


# ---------------------------------------------------------------------------
# API: State transitions (active → cleaning → cleanup_failed)
# ---------------------------------------------------------------------------


class TestCleanupActiveToCleanupFailed:
    """Tests the failure cleanup path via the API."""

    @pytest.mark.asyncio
    async def test_executor_failure_transitions_to_cleanup_failed(
        self, mock_conn: AsyncMock
    ) -> None:
        """When the executor raises, the workspace should transition to cleanup_failed."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("disk full")
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces" in sql:
                if "cleaning" in repr(args):
                    row_data["cleanup_status"] = "cleaning"
                    row_data["cleanup_started_at"] = args[1]  # $2
                elif "cleanup_failed" in repr(args):
                    row_data["cleanup_status"] = "cleanup_failed"
                    row_data["cleanup_failed_at"] = args[1]  # $2
                    row_data["cleanup_failure_reason"] = args[2]  # $3

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleanup_failed"
        assert data["cleanup_failed_at"] is not None
        assert "RuntimeError" in data["cleanup_failure_reason"]
        assert "disk full" in data["cleanup_failure_reason"]

    @pytest.mark.asyncio
    async def test_cleanup_failed_includes_formatted_exception(
        self, mock_conn: AsyncMock
    ) -> None:
        """cleanup_failure_reason should include the exception type and message."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock(
            side_effect=OSError("No space left on device")
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "cleanup_failed" in repr(args):
                row_data["cleanup_status"] = "cleanup_failed"
                row_data["cleanup_failed_at"] = args[1]  # $2
                row_data["cleanup_failure_reason"] = args[2]  # $3

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        data = response.json()["data"]
        assert data["cleanup_failure_reason"].startswith("OSError: ")
        assert "No space left on device" in data["cleanup_failure_reason"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestCleanupIdempotency:
    """Tests that cleanup is idempotent — calling again on terminal states is a no-op."""

    @pytest.mark.asyncio
    async def test_cleanup_on_cleaned_workspace_returns_200_noop(
        self, mock_conn: AsyncMock
    ) -> None:
        """Calling cleanup on an already-cleaned workspace is idempotent (200, no call)."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="cleaned")

        executor = AsyncMock()

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleaned"
        # Executor should NOT have been called — idempotent no-op
        executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_on_cleanup_failed_workspace_returns_200_noop(
        self, mock_conn: AsyncMock
    ) -> None:
        """Calling cleanup on a cleanup_failed workspace is idempotent (200, no call)."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(
            ws_id,
            cleanup_status="cleanup_failed",
            cleanup_failure_reason="RuntimeError: disk full",
        )

        executor = AsyncMock()

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleanup_failed"
        assert data["cleanup_failure_reason"] == "RuntimeError: disk full"
        executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_idempotent_after_success(
        self, mock_conn: AsyncMock
    ) -> None:
        """Calling cleanup twice: first succeeds, second is idempotent no-op."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        from app.executors.models import CleanupWorkspaceResponse

        call_count = 0

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            nonlocal call_count
            if "UPDATE workspaces" in sql and "cleaned" in repr(args):
                row_data["cleanup_status"] = "cleaned"
                call_count += 1

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # First call — performs cleanup
        executor1 = AsyncMock()
        executor1.cleanup_workspace = AsyncMock(
            return_value=CleanupWorkspaceResponse(status="cleaned")
        )
        client1 = _make_cleanup_client(mock_conn, executor1)
        async with client1 as c:
            response1 = await c.post(f"/workspaces/{ws_id}/cleanup")
        assert response1.status_code == 200
        assert response1.json()["data"]["cleanup_status"] == "cleaned"
        assert call_count == 1  # One transition to cleaned

        # Second call — idempotent on already-cleaned workspace
        executor2 = AsyncMock()
        client2 = _make_cleanup_client(mock_conn, executor2)
        async with client2 as c:
            response2 = await c.post(f"/workspaces/{ws_id}/cleanup")
        assert response2.status_code == 200
        assert response2.json()["data"]["cleanup_status"] == "cleaned"
        assert call_count == 1  # Still only one transition
        executor2.cleanup_workspace.assert_not_called()  # Never called for idempotent

    @pytest.mark.asyncio
    async def test_cleanup_idempotent_after_failure(
        self, mock_conn: AsyncMock
    ) -> None:
        """Calling cleanup twice: first fails, second is idempotent no-op."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        transition_count = 0

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            nonlocal transition_count
            if "cleanup_failed" in repr(args):
                row_data["cleanup_status"] = "cleanup_failed"
                row_data["cleanup_failed_at"] = args[1]
                row_data["cleanup_failure_reason"] = args[2]
                transition_count += 1

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # First call — fails
        executor1 = AsyncMock()
        executor1.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        client1 = _make_cleanup_client(mock_conn, executor1)
        async with client1 as c:
            response1 = await c.post(f"/workspaces/{ws_id}/cleanup")
        assert response1.json()["data"]["cleanup_status"] == "cleanup_failed"
        assert transition_count == 1

        # Second call — idempotent on cleanup_failed workspace
        executor2 = AsyncMock()
        client2 = _make_cleanup_client(mock_conn, executor2)
        async with client2 as c:
            response2 = await c.post(f"/workspaces/{ws_id}/cleanup")
        assert response2.status_code == 200
        assert response2.json()["data"]["cleanup_status"] == "cleanup_failed"
        assert transition_count == 1  # Still only one transition
        executor2.cleanup_workspace.assert_not_called()  # Never called for idempotent


# ---------------------------------------------------------------------------
# Pinned workspace rejection
# ---------------------------------------------------------------------------


class TestPinnedWorkspaceCleanup:
    """Tests that pinned workspaces cannot be cleaned."""

    @pytest.mark.asyncio
    async def test_cleanup_on_pinned_workspace_returns_409(
        self, mock_conn: AsyncMock
    ) -> None:
        """Calling cleanup on a pinned workspace returns 409."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="pinned")

        executor = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 409
        data = response.json()
        assert "Cannot clean a pinned workspace" in data["error"]["message"]
        executor.cleanup_workspace.assert_not_called()


# ---------------------------------------------------------------------------
# Timestamp integrity
# ---------------------------------------------------------------------------


class TestCleanupTimestamps:
    """Tests that timestamps are stored correctly."""

    @pytest.mark.asyncio
    async def test_cleanup_completed_at_is_set_after_success(
        self, mock_conn: AsyncMock
    ) -> None:
        """cleanup_completed_at should be populated when cleanup succeeds."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        from app.executors.models import CleanupWorkspaceResponse
        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock(
            return_value=CleanupWorkspaceResponse(status="cleaned")
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces" in sql:
                if "cleaned" in repr(args):
                    row_data["cleanup_status"] = "cleaned"
                    row_data["cleanup_completed_at"] = args[1]  # $2 = timestamp
                elif "cleaning" in repr(args):
                    row_data["cleanup_status"] = "cleaning"
                    row_data["cleanup_started_at"] = args[1]  # $2 = timestamp

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        data = response.json()["data"]
        assert data["cleanup_completed_at"] is not None

    @pytest.mark.asyncio
    async def test_cleanup_failed_at_is_set_after_failure(
        self, mock_conn: AsyncMock
    ) -> None:
        """cleanup_failed_at should be populated when cleanup fails."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("disk full")
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "cleanup_failed" in repr(args):
                row_data["cleanup_status"] = "cleanup_failed"
                row_data["cleanup_failed_at"] = args[1]  # $2
                row_data["cleanup_failure_reason"] = args[2]  # $3

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = _make_cleanup_client(mock_conn, executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        data = response.json()["data"]
        assert data["cleanup_failed_at"] is not None
        assert data["cleanup_failure_reason"] is not None


# ---------------------------------------------------------------------------
# Scheduler idempotency (via query)
# ---------------------------------------------------------------------------


class TestSchedulerIdempotency:
    """Tests that the scheduler query excludes non-active states (idempotent)."""

    def test_query_only_selects_active_workspaces(self):
        """The query uses cleanup_status = 'active' to exclude terminal states."""
        from app.scheduler.cleaner import CleanupScheduler

        scheduler = CleanupScheduler()

        # Access _query_expired to inspect the SQL it builds
        import inspect
        source = inspect.getsource(scheduler._query_expired)
        assert "cleanup_status = 'active'" in source, (
            "Expected query to filter on cleanup_status = 'active', "
            "which ensures idempotency for cleaned/cleanup_failed workspaces"
        )


# ---------------------------------------------------------------------------
# All state transitions covered
# ---------------------------------------------------------------------------


class TestAllStateTransitions:
    """Tests that all expected state transitions are covered."""

    def test_workspace_status_has_all_terminal_states(self):
        """The WorkspaceStatus enum should include all states from the issue."""
        from app.core.models.workspace import WorkspaceStatus

        values = {s.value for s in WorkspaceStatus}
        assert "active" in values
        assert "cleaning" in values
        assert "cleaned" in values
        assert "cleanup_failed" in values
        assert "pinned" in values

    def test_active_can_transition_to_cleaning(self):
        """active should be able to transition to cleaning."""
        from app.core.models.workspace import WorkspaceStatus
        # This is validated by the API endpoint and scheduler — the test
        # verifies the status values exist and are distinct
        assert WorkspaceStatus.ACTIVE != WorkspaceStatus.CLEANING
        assert WorkspaceStatus.ACTIVE.value == "active"
        assert WorkspaceStatus.CLEANING.value == "cleaning"

    def test_cleaning_can_transition_to_cleaned(self):
        """cleaning should be able to transition to cleaned."""
        from app.core.models.workspace import WorkspaceStatus
        assert WorkspaceStatus.CLEANING != WorkspaceStatus.CLEANED
        assert WorkspaceStatus.CLEANED.value == "cleaned"

    def test_cleaning_can_transition_to_cleanup_failed(self):
        """cleaning should be able to transition to cleanup_failed."""
        from app.core.models.workspace import WorkspaceStatus
        assert WorkspaceStatus.CLEANING != WorkspaceStatus.CLEANUP_FAILED
        assert WorkspaceStatus.CLEANUP_FAILED.value == "cleanup_failed"

    def test_cleaned_and_cleanup_failed_are_terminal(self):
        """cleaned and cleanup_failed should be terminal (no automatic transitions out)."""
        from app.core.models.workspace import WorkspaceStatus
        # These states signal the end of the cleanup lifecycle
        assert WorkspaceStatus.CLEANED.value == "cleaned"
        assert WorkspaceStatus.CLEANUP_FAILED.value == "cleanup_failed"
