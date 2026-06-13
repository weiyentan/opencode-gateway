"""Tests for cleanup_after retention logic — issue #59.

Covers:
- Successful jobs set cleanup_after = created_at + 72h
- Failed jobs set cleanup_after = created_at + 7d
- Config-driven retention overrides
- Pinned workspaces have cleanup_after = NULL
- Unpinned workspaces reset cleanup_after
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import (
    create_client,
    make_job_row,
    make_workspace_row,
    mock_row,
)

# mock_conn and mock_executor fixtures are auto-discovered from conftest.py



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# (mock_row, make_job_row, make_workspace_row, create_client are imported from tests.conftest)


# ---------------------------------------------------------------------------
# Helper to set config values during tests
# ---------------------------------------------------------------------------

def _patch_settings(**overrides: int):
    """Return a context manager that patches get_settings to include overrides."""
    from app.core.config import Settings

    base = Settings()
    for key, value in overrides.items():
        setattr(base, key, value)

    async def _get():
        return base

    return patch("app.api.jobs.get_settings", _get)


# ---------------------------------------------------------------------------
# Fixtures (mock_conn and mock_executor come from conftest.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: Config defaults and overrides
# ---------------------------------------------------------------------------


class TestCleanupRetentionConfig:
    """Tests that cleanup retention settings have correct defaults and can be overridden."""

    def test_default_success_retention_is_72_hours(self):
        """The default cleanup_success_retention_hours should be 72 (3 days)."""
        from app.core.config import Settings
        settings = Settings()
        assert settings.cleanup_success_retention_hours == 72

    def test_default_failure_retention_is_168_hours(self):
        """The default cleanup_failure_retention_hours should be 168 (7 days)."""
        from app.core.config import Settings
        settings = Settings()
        assert settings.cleanup_failure_retention_hours == 168

    def test_success_retention_override_from_env(self, monkeypatch):
        """cleanup_success_retention_hours should be overridable via env var."""
        monkeypatch.setenv("GATEWAY_CLEANUP_SUCCESS_RETENTION_HOURS", "48")
        from app.core.config import Settings
        settings = Settings()
        assert settings.cleanup_success_retention_hours == 48

    def test_failure_retention_override_from_env(self, monkeypatch):
        """cleanup_failure_retention_hours should be overridable via env var."""
        monkeypatch.setenv("GATEWAY_CLEANUP_FAILURE_RETENTION_HOURS", "336")
        from app.core.config import Settings
        settings = Settings()
        assert settings.cleanup_failure_retention_hours == 336


# ---------------------------------------------------------------------------
# Tests: Successful job sets cleanup_after
# ---------------------------------------------------------------------------


class TestSuccessfulJobCleanupAfter:
    """Tests that a successfully completed job sets cleanup_after on the workspace."""

    @pytest.mark.asyncio
    async def test_completed_job_sets_cleanup_after_to_72h(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """POST /jobs (success) → workspace cleanup_after = created_at + 72h."""
        job_id = uuid.uuid4()
        workspace_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending"
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            if "FROM runners" in sql and "LEFT JOIN" in sql:
                return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner"})
            if "FROM runner_observations" in sql:
                return None
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                job_row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                job_row_data["status"] = "completed"
                job_row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                job_row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                },
            )

        assert response.status_code == 201

        # Verify an UPDATE to workspaces was made with cleanup_after
        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        # First positional arg is workspace_id
        assert ws_args[0] == workspace_id
        # Second arg is the interval (timedelta)
        assert isinstance(ws_args[1], timedelta)
        assert ws_args[1] == timedelta(hours=72)

    @pytest.mark.asyncio
    async def test_completed_job_uses_configured_success_retention(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """When config is overridden, successful job uses the configured value."""
        job_id = uuid.uuid4()

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending"
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            if "FROM runners" in sql and "LEFT JOIN" in sql:
                return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner"})
            if "FROM runner_observations" in sql:
                return None
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                job_row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                job_row_data["status"] = "completed"
                job_row_data["completed_at"] = datetime.now(timezone.utc)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                job_row_data["status"] = "failed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        def _override_settings():
            from app.core.config import Settings
            s = Settings()
            s.cleanup_success_retention_hours = 48
            return s

        with patch("app.api.jobs.get_settings", _override_settings):
            client = create_client(mock_conn, mock_executor=mock_executor)
            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

        assert response.status_code == 201

        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        assert ws_args[1] == timedelta(hours=48)


# ---------------------------------------------------------------------------
# Tests: Failed job sets cleanup_after
# ---------------------------------------------------------------------------


class TestFailedJobCleanupAfter:
    """Tests that a failed job sets cleanup_after on the workspace."""

    @pytest.mark.asyncio
    async def test_failed_job_sets_cleanup_after_to_7d(
        self, mock_conn: AsyncMock
    ) -> None:
        """POST /jobs (failure) → workspace cleanup_after = created_at + 168h."""
        job_id = uuid.uuid4()

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending",
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner"})
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                job_row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                job_row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                job_row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Failing executor
        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Workspace creation failed")
        )

        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                },
            )

        assert response.status_code == 201
        assert response.json()["data"]["status"] == "failed"

        # The job has no workspace_name because the executor failed before creating it,
        # so no workspace update should be emitted
        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 0  # No workspace created, so no update

    @pytest.mark.asyncio
    async def test_failed_job_with_existing_workspace_sets_cleanup_after(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """When a job fails but a workspace record exists, cleanup_after is set."""
        job_id = uuid.uuid4()
        workspace_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending",
        )

        # Simulate: executor succeeds for create_workspace but fails for start_opencode
        mock_start_fail_executor = AsyncMock()
        from app.executors.models import CreateWorkspaceResponse
        mock_start_fail_executor.create_workspace = AsyncMock(
            return_value=CreateWorkspaceResponse(
                workspace_id=workspace_id,
                workspace_path="/tmp/opencode/ws",
                status="ready",
            )
        )
        mock_start_fail_executor.start_opencode = AsyncMock(
            side_effect=RuntimeError("start_opencode failed")
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            if "FROM runners" in sql and "LEFT JOIN" in sql:
                return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner"})
            if "FROM runner_observations" in sql:
                return None
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                job_row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET workspace_name" in sql:
                job_row_data["workspace_name"] = str(workspace_id)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                job_row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                job_row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn, mock_executor=mock_start_fail_executor)

        async with client as c:
            response = await c.post(
                "/jobs",
                json={
                    "repo_url": "https://github.com/org/repo",
                    "task_summary": "Fix a bug",
                },
            )

        assert response.status_code == 201
        assert response.json()["data"]["status"] == "failed"

        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        assert ws_args[0] == workspace_id
        assert isinstance(ws_args[1], timedelta)
        assert ws_args[1] == timedelta(hours=168)  # 7 days

    @pytest.mark.asyncio
    async def test_failed_job_uses_configured_failure_retention(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """When config is overridden, failed job uses the configured failure retention."""
        job_id = uuid.uuid4()
        workspace_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Fix a bug", status="pending",
        )

        mock_start_fail_executor = AsyncMock()
        from app.executors.models import CreateWorkspaceResponse
        mock_start_fail_executor.create_workspace = AsyncMock(
            return_value=CreateWorkspaceResponse(
                workspace_id=workspace_id,
                workspace_path="/tmp/opencode/ws",
                status="ready",
            )
        )
        mock_start_fail_executor.start_opencode = AsyncMock(
            side_effect=RuntimeError("start_opencode failed")
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            if "FROM runners" in sql and "LEFT JOIN" in sql:
                return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
            if "FROM runners WHERE id" in sql:
                return mock_row({"runner_id": "test-runner"})
            if "FROM runner_observations" in sql:
                return None
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                job_row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET workspace_name" in sql:
                job_row_data["workspace_name"] = str(workspace_id)
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                job_row_data["status"] = "failed"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                job_row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        def _override_settings():
            from app.core.config import Settings
            s = Settings()
            s.cleanup_failure_retention_hours = 336  # 14 days
            return s

        with patch("app.api.jobs.get_settings", _override_settings):
            client = create_client(mock_conn, mock_executor=mock_start_fail_executor)
            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

        assert response.status_code == 201
        assert response.json()["data"]["status"] == "failed"

        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        assert ws_args[1] == timedelta(hours=336)


# ---------------------------------------------------------------------------
# Tests: Pinned workspace sets cleanup_after = NULL
# ---------------------------------------------------------------------------


class TestPinnedWorkspaceCleanupAfter:
    """Tests that pinning/unpinning a workspace manages cleanup_after."""

    @pytest.mark.asyncio
    async def test_pin_sets_cleanup_after_to_null(
        self, mock_conn: AsyncMock
    ) -> None:
        """Pinning a workspace sets cleanup_after = NULL."""
        ws_id = uuid.uuid4()
        created = datetime.now(timezone.utc)
        row_data = make_workspace_row(
            ws_id,
            pinned=False,
            cleanup_after=created + timedelta(hours=72),
            created_at=created,
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = True
                if args[1] is None:
                    row_data["cleanup_after"] = None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pinned"] is True
        assert data["cleanup_after"] is None

        # Verify cleanup_after is passed as None in the UPDATE
        update_call = next(
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces SET pinned" in sql
        )
        # cleanup_after positional arg (index 1) should be None
        assert update_call[1][1] is None

    @pytest.mark.asyncio
    async def test_unpin_resets_cleanup_after(
        self, mock_conn: AsyncMock
    ) -> None:
        """Unpinning a workspace resets cleanup_after using success retention."""
        ws_id = uuid.uuid4()
        created = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        row_data = make_workspace_row(
            ws_id,
            pinned=True,
            cleanup_after=None,
            created_at=created,
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = False
                row_data["cleanup_after"] = args[1]

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pinned"] is False
        assert data["cleanup_after"] is not None

        # Verfiy cleanup_after = created_at + 72h
        update_call = next(
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces SET pinned" in sql
        )
        expected = created + timedelta(hours=72)
        assert update_call[1][1] == expected

    @pytest.mark.asyncio
    async def test_pin_on_already_pinned_keeps_cleanup_after_null(
        self, mock_conn: AsyncMock
    ) -> None:
        """Pinning an already-pinned workspace keeps cleanup_after as NULL."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(
            ws_id,
            pinned=True,
            cleanup_after=None,
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = False  # toggle: was True → False
                # The unpinned workspace gets cleanup_after reset
                from datetime import timedelta
                row_data["cleanup_after"] = row_data["created_at"] + timedelta(hours=72)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        # The pin toggled: was True, now False → cleanup_after gets set
        data = response.json()["data"]
        assert data["pinned"] is False
        assert data["cleanup_after"] is not None


# ---------------------------------------------------------------------------
# Tests: Aborted job sets cleanup_after
# ---------------------------------------------------------------------------


class TestAbortedJobCleanupAfter:
    """Tests that an aborted job sets cleanup_after on the workspace."""

    @pytest.mark.asyncio
    async def test_aborted_job_sets_failure_retention_cleanup_after(
        self, mock_conn: AsyncMock
    ) -> None:
        """Aborting a job with a workspace → cleanup_after = created_at + 168h."""
        job_id = uuid.uuid4()
        workspace_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Abort me",
            status="running",
            opencode_session_id="sess-123",
            workspace_name=str(workspace_id),
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                job_row_data["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                job_row_data["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        from app.opencode.protocol import SessionAbortResponse
        mock_opencode = AsyncMock()
        mock_opencode.abort_session = AsyncMock(
            return_value=SessionAbortResponse(
                session_id="sess-123", aborted=True, message="OK",
            )
        )

        mock_exec = AsyncMock()
        mock_exec.stop_opencode = AsyncMock()
        mock_exec.cleanup_workspace = AsyncMock()

        client = create_client(
            mock_conn,
            mock_executor=mock_exec,
            mock_opencode_client=mock_opencode,
        )

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "aborted"

        # Verify cleanup_after UPDATE was called
        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        assert ws_args[0] == workspace_id
        assert ws_args[1] == timedelta(hours=168)

    @pytest.mark.asyncio
    async def test_aborted_job_without_workspace_skips_cleanup_after(
        self, mock_conn: AsyncMock
    ) -> None:
        """Aborting a job with no workspace_name does not emit a workspace UPDATE."""
        job_id = uuid.uuid4()

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Abort no ws",
            status="pending",
            opencode_session_id=None,
            workspace_name=None,
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                job_row_data["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "aborted"

        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 0

    @pytest.mark.asyncio
    async def test_aborted_job_respects_no_opencode_client(
        self, mock_conn: AsyncMock
    ) -> None:
        """Aborting without opencode client still sets cleanup_after."""
        job_id = uuid.uuid4()
        workspace_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

        job_row_data = make_job_row(
            job_id, "https://github.com/org/repo", "Abort no client",
            status="running",
            opencode_session_id="sess-noclient",
            workspace_name=str(workspace_id),
        )

        execute_calls: list[tuple] = []

        async def _fetchrow(sql: str, *args):
            return mock_row(job_row_data)

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            if "UPDATE gateway_jobs SET status = 'aborting'" in sql:
                job_row_data["status"] = "aborting"
            elif "UPDATE gateway_jobs SET status = 'aborted'" in sql:
                job_row_data["status"] = "aborted"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # No opencode client → default get_opencode_client returns None → direct abort
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/jobs/{job_id}/abort")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "aborted"

        ws_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE workspaces" in sql and "cleanup_after" in sql
        ]
        assert len(ws_updates) == 1
        _sql, ws_args = ws_updates[0]
        assert ws_args[0] == workspace_id
        assert ws_args[1] == timedelta(hours=168)
