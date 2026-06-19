"""Tests for Workspace lifecycle endpoints — pin, cleanup, and port allocation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from tests.conftest import create_client, make_workspace_row, mock_row

from app.core.factory import create_app  # used by TestWorkspaceRoutesRegistered

# mock_conn and client fixtures are auto-discovered from conftest.py


# ---------------------------------------------------------------------------
# Fixtures (local overrides)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_executor() -> AsyncMock:
    """Return a mock ExecutorPlugin for cleanup operations.

    Deliberately different from conftest:mock_executor — this one only
    provides cleanup_workspace, not the full job lifecycle mock.
    """
    from app.executors.models import CleanupWorkspaceResponse

    executor = AsyncMock()
    executor.cleanup_workspace = AsyncMock(
        return_value=CleanupWorkspaceResponse(status="cleaned")
    )
    return executor


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /workspaces/{id}/pin
# ---------------------------------------------------------------------------


class TestPinWorkspace:
    """Tests for POST /workspaces/{id}/pin."""

    @pytest.mark.asyncio
    async def test_pin_flips_false_to_true(self, mock_conn: AsyncMock) -> None:
        """Pinning an unpinned workspace should set pinned=True."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, pinned=False)

        calls = []

        async def _fetchrow(sql: str, *args):
            calls.append(("fetchrow", sql, args))
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            calls.append(("execute", sql, args))
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = True

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pinned"] is True
        assert data["id"] == str(ws_id)

    @pytest.mark.asyncio
    async def test_pin_flips_true_to_false(self, mock_conn: AsyncMock) -> None:
        """Pinning an already-pinned workspace should set pinned=False."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, pinned=True)

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = False

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pinned"] is False

    @pytest.mark.asyncio
    async def test_pin_unknown_workspace_returns_404(self, client: AsyncClient, mock_conn: AsyncMock) -> None:
        """Pin on a non-existent workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{uuid.uuid4()}/pin")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_pin_updates_updated_at(self, mock_conn: AsyncMock) -> None:
        """The updated_at field should be refreshed after pin toggle."""
        ws_id = uuid.uuid4()
        old_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        row_data = make_workspace_row(ws_id, pinned=False)
        row_data["updated_at"] = old_time

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = True
                row_data["updated_at"] = datetime.now(timezone.utc)

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        # Verify updated_at in response is newer than old_time
        updated_str = response.json()["data"]["updated_at"]
        # Python 3.9 fromisoformat doesn't accept 'Z', replace with +00:00
        if updated_str.endswith("Z"):
            updated_str = updated_str[:-1] + "+00:00"
        updated = datetime.fromisoformat(updated_str)
        assert updated > old_time

    @pytest.mark.asyncio
    async def test_pin_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        """Pin with a malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/workspaces/not-a-uuid/pin")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_pin_returns_all_workspace_fields(self, mock_conn: AsyncMock) -> None:
        """The response should include all workspace fields."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(
            ws_id,
            workspace_name="ws-complete",
            path="/data/workspaces/ws-complete",
            repo_url="https://github.com/example/repo.git",
            pinned=False,
            cleanup_status="active",
            port=8080,
            branch="feature/test",
            service_name="opencode-serve-test",
        )

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET pinned" in sql:
                row_data["pinned"] = True

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["id"] == str(ws_id)
        assert data["workspace_name"] == "ws-complete"
        assert data["path"] == "/data/workspaces/ws-complete"
        assert data["repo_url"] == "https://github.com/example/repo.git"
        assert data["port"] == 8080
        assert data["branch"] == "feature/test"
        assert data["service_name"] == "opencode-serve-test"
        assert data["cleanup_status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data


# ---------------------------------------------------------------------------
# POST /workspaces/{id}/cleanup
# ---------------------------------------------------------------------------


class TestCleanupWorkspace:
    """Tests for POST /workspaces/{id}/cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_active_workspace_returns_200(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """Cleanup on an active workspace transitions to cleaning and calls executor."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        # Track advisory lock calls
        lock_acquired = False

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            nonlocal lock_acquired
            if "pg_try_advisory_lock" in sql:
                lock_acquired = True
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET cleanup_status" in sql:
                if "active" in sql or "WorkspaceStatus.ACTIVE" not in repr(args):
                    row_data["cleanup_status"] = "cleaning"
            if "pg_advisory_unlock" in sql:
                pass

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleaning"
        mock_executor.cleanup_workspace.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_unknown_workspace_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ) -> None:
        """Cleanup on a non-existent workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{uuid.uuid4()}/cleanup")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_cleanup_already_cleaning_returns_409(
        self, mock_conn: AsyncMock
    ) -> None:
        """Cleanup on a workspace already in 'cleaning' status returns 409."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="cleaning")

        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row_data))
        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 409
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFLICT"
        assert "already being cleaned" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_cleanup_lock_contention_returns_409(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """When advisory lock cannot be acquired, cleanup returns 409."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return False  # lock already held by another process
            return None

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 409
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFLICT"
        assert "lock held by another process" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_cleanup_executor_failure_transitions_to_cleanup_failed(
        self, mock_conn: AsyncMock
    ) -> None:
        """When the executor raises, cleanup status transitions to cleanup_failed with 200."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        failing_executor = AsyncMock()
        failing_executor.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("rmtree failed")
        )

        execute_calls = []

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
                elif "cleanup_failed" in repr(args):
                    row_data["cleanup_status"] = "cleanup_failed"
                    row_data["cleanup_failed_at"] = args[1]  # $2 = timestamp
                    row_data["cleanup_failure_reason"] = args[2]  # $3 = reason
            if "pg_advisory_unlock" in sql:
                pass

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        # On failure, returns 200 with cleanup_failed status (not 500)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleanup_status"] == "cleanup_failed"
        assert data["cleanup_failure_reason"] is not None
        assert "RuntimeError" in data["cleanup_failure_reason"]
        assert data["cleanup_failed_at"] is not None

    @pytest.mark.asyncio
    async def test_cleanup_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        """Cleanup with a malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/workspaces/not-a-uuid/cleanup")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_cleanup_passes_workspace_id_to_executor(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """The executor.cleanup_workspace call should receive the correct workspace_id."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET cleanup_status" in sql:
                row_data["cleanup_status"] = "cleaning"
            if "pg_advisory_unlock" in sql:
                pass

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            await c.post(f"/workspaces/{ws_id}/cleanup")

        mock_executor.cleanup_workspace.assert_called_once()
        call_args = mock_executor.cleanup_workspace.call_args[0][0]
        assert call_args.workspace_id == ws_id

    @pytest.mark.asyncio
    async def test_cleanup_releases_lock_on_success(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """The advisory lock must be released after successful cleanup."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

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
                row_data["cleanup_status"] = "cleaning"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            await c.post(f"/workspaces/{ws_id}/cleanup")

        unlock_calls = [
            (sql, args) for sql, args in execute_calls
            if "pg_advisory_unlock" in sql
        ]
        # Two unlocks: port release (47001) + cleanup lock (47002, workspace-id)
        assert len(unlock_calls) == 2
        # Verify the port lock key is released
        port_unlocks = [
            (sql, args) for sql, args in unlock_calls
            if args == (47001,)
        ]
        assert len(port_unlocks) == 1

    @pytest.mark.asyncio
    async def test_cleanup_releases_lock_on_failure(
        self, mock_conn: AsyncMock
    ) -> None:
        """The advisory lock must still be released after a cleanup failure."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        failing_executor = AsyncMock()
        failing_executor.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("rmtree failed")
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
                row_data["cleanup_status"] = "cleaning"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            await c.post(f"/workspaces/{ws_id}/cleanup")

        unlock_calls = [
            (sql, args) for sql, args in execute_calls
            if "pg_advisory_unlock" in sql
        ]
        assert len(unlock_calls) == 1

    @pytest.mark.asyncio
    async def test_cleanup_workspace_status_transitions_to_cleaning(
        self, mock_conn: AsyncMock, mock_executor: AsyncMock
    ) -> None:
        """The workspace should be in 'cleaning' status while cleanup is in progress."""
        ws_id = uuid.uuid4()
        row_data = make_workspace_row(ws_id, cleanup_status="active")

        async def _fetchrow(sql: str, *args):
            return mock_row(row_data)

        async def _fetchval(sql: str, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            return None

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET cleanup_status" in sql:
                row_data["cleanup_status"] = "cleaning"
            if "pg_advisory_unlock" in sql:
                pass

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.fetchval = AsyncMock(side_effect=_fetchval)
        mock_conn.execute = AsyncMock(side_effect=_execute)
        client = create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        assert response.json()["data"]["cleanup_status"] == "cleaning"


# ---------------------------------------------------------------------------
# Port allocation with advisory lock
# ---------------------------------------------------------------------------


class TestPortAllocation:
    """Tests for the port allocation advisory lock pattern."""

    @pytest.mark.asyncio
    async def test_allocate_port_returns_first_port_when_no_ports_allocated(
        self, mock_conn: AsyncMock
    ) -> None:
        """When no ports are allocated, allocate_port returns 10000."""
        from app.core.ports import allocate_port

        async def _fetch(sql: str, *args):
            return []

        async def _execute(sql: str, *args):
            pass

        mock_conn.fetch = AsyncMock(side_effect=_fetch)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(mock_conn)
        assert port == 10000

    @pytest.mark.asyncio
    async def test_allocate_port_finds_first_free_port(self, mock_conn: AsyncMock) -> None:
        """When ports are already allocated, allocate_port finds the first gap."""
        from app.core.ports import allocate_port

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in (10000, 10001, 10002)]

        async def _execute(sql: str, *args):
            pass

        mock_conn.fetch = AsyncMock(side_effect=_fetch)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(mock_conn)
        assert port == 10003

    @pytest.mark.asyncio
    async def test_allocate_port_acquires_and_releases_lock(
        self, mock_conn: AsyncMock
    ) -> None:
        """allocate_port must acquire then release the PG advisory lock."""
        from app.core.ports import allocate_port

        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))

        mock_conn.fetch = AsyncMock(side_effect=_fetch)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        await allocate_port(mock_conn)

        lock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_lock" in s]
        unlock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_unlock" in s]
        assert len(lock_calls) == 1
        assert len(unlock_calls) == 1

    @pytest.mark.asyncio
    async def test_allocate_port_uses_port_lock_key(
        self, mock_conn: AsyncMock
    ) -> None:
        """The lock key must match PORT_LOCK_KEY (47001)."""
        from app.core.ports import allocate_port
        from app.db.lock import PORT_LOCK_KEY

        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))

        mock_conn.fetch = AsyncMock(side_effect=_fetch)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        await allocate_port(mock_conn)

        lock_call = next((s, a) for s, a in execute_calls if "pg_advisory_lock" in s)
        assert lock_call[1][0] == PORT_LOCK_KEY


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestWorkspaceRoutesRegistered:
    """Verify workspace routes are registered on the FastAPI app."""

    def test_pin_route_is_registered(self) -> None:
        """POST /workspaces/{id}/pin should be a registered route."""
        app = create_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/workspaces/{workspace_id}/pin" in routes

    def test_cleanup_route_is_registered(self) -> None:
        """POST /workspaces/{id}/cleanup should be a registered route."""
        app = create_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/workspaces/{workspace_id}/cleanup" in routes

    def test_openapi_schema_includes_workspace_endpoints(self) -> None:
        """The OpenAPI schema should include workspace operations."""
        app = create_app()
        schema = app.openapi()
        paths = list(schema["paths"].keys())
        workspace_paths = [p for p in paths if "workspaces" in p]
        assert len(workspace_paths) >= 2
        assert "/workspaces/{workspace_id}/pin" in paths
        assert "/workspaces/{workspace_id}/cleanup" in paths
