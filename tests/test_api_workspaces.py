"""Tests for the Workspace API endpoints — pin and cleanup lifecycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.core.models.workspace import WorkspaceStatus
from app.db.session import get_session
from app.executors.factory import get_executor


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    return row


def _make_workspace_row(
    workspace_id: uuid.UUID,
    *,
    workspace_name: str = "ws-test001",
    path: str = "/data/workspaces/ws-test001",
    repo_url: str = "https://github.com/example/repo.git",
    pinned: bool = False,
    cleanup_status: str = "active",
    port: Optional[int] = None,
) -> dict:
    """Return a dict representing a workspaces table row."""
    now = datetime.now(timezone.utc)
    return {
        "id": workspace_id,
        "runner_id": None,
        "workspace_name": workspace_name,
        "path": path,
        "repo_url": repo_url,
        "branch": None,
        "port": port,
        "service_name": None,
        "pinned": pinned,
        "cleanup_after": None,
        "cleanup_status": cleanup_status,
        "created_at": now,
        "updated_at": now,
    }


@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    return AsyncMock()


@pytest.fixture
def mock_executor():
    """Return a mock ExecutorPlugin with a working cleanup_workspace."""
    from app.executors.models import CleanupWorkspaceResponse

    executor = AsyncMock()
    executor.cleanup_workspace = AsyncMock(
        return_value=CleanupWorkspaceResponse(status="cleaned")
    )
    return executor


def _create_client(mock_conn, *, mock_executor=None):
    """Build app with overridden dependencies, return httpx AsyncClient."""
    app = create_app()
    mock_pool = AsyncMock()
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    if mock_executor is not None:
        app.dependency_overrides[get_executor] = lambda: mock_executor

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client(mock_conn):
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    return _create_client(mock_conn)


# ────────────────────────────────────────────────────────────────────
# POST /workspaces/{id}/pin
# ────────────────────────────────────────────────────────────────────


class TestPinWorkspace:
    """Tests for POST /workspaces/{id}/pin."""

    @pytest.mark.asyncio
    async def test_pin_unpinned_workspace_sets_pinned_true(self, client, mock_conn):
        """POST /workspaces/{id}/pin on an unpinned workspace sets pinned=true."""
        ws_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, pinned=False, cleanup_status="active")

        fetchrow_calls: list[dict | None] = [row.copy(), row.copy()]
        # After pin, the row should reflect pinned=true and cleanup_status='pinned'
        row_after = row.copy()
        row_after["pinned"] = True
        row_after["cleanup_status"] = WorkspaceStatus.PINNED.value

        async def _fetchrow(sql, *args):
            result = fetchrow_calls.pop(0) if fetchrow_calls else row_after
            return _mock_row(result) if result is not None else None

        async def _execute(sql, *args):
            # Update the row in place
            if "UPDATE workspaces SET pinned" in sql:
                row["pinned"] = not row["pinned"]
                row["cleanup_status"] = (
                    WorkspaceStatus.PINNED.value
                    if row["pinned"]
                    else WorkspaceStatus.ACTIVE.value
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Make sure fetchrow_calls returns the right things
        fetchrow_calls.clear()
        fetchrow_calls.append(_make_workspace_row(ws_id, pinned=False).copy())
        fetchrow_calls.append(None)  # placeholder, we'll use side_effect

        # Redo with simpler approach — track call count
        fetch_count = 0

        async def _fetchrow2(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id, pinned=False))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, pinned=True, cleanup_status="pinned")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow2)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()
        assert data["pinned"] is True
        assert data["cleanup_status"] == "pinned"
        assert data["id"] == str(ws_id)

    @pytest.mark.asyncio
    async def test_unpin_pinned_workspace_sets_pinned_false(self, client, mock_conn):
        """POST /workspaces/{id}/pin on a pinned workspace sets pinned=false."""
        ws_id = uuid.uuid4()

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(
                    _make_workspace_row(ws_id, pinned=True, cleanup_status="pinned")
                )
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, pinned=False, cleanup_status="active")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/pin")

        assert response.status_code == 200
        data = response.json()
        assert data["pinned"] is False
        assert data["cleanup_status"] == "active"
        assert data["id"] == str(ws_id)

    @pytest.mark.asyncio
    async def test_pin_unknown_workspace_returns_404(self, client, mock_conn):
        """POST /workspaces/{id}/pin on a non-existent workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{uuid.uuid4()}/pin")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_pin_invalid_uuid_returns_422(self, client):
        """POST /workspaces/{id}/pin with a malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/workspaces/not-a-uuid/pin")

        assert response.status_code == 422


# ────────────────────────────────────────────────────────────────────
# POST /workspaces/{id}/cleanup
# ────────────────────────────────────────────────────────────────────


class TestCleanupWorkspace:
    """Tests for POST /workspaces/{id}/cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_active_workspace_sets_status_cleaning(
        self, mock_conn, mock_executor
    ):
        """POST /workspaces/{id}/cleanup should set cleanup_status to 'cleaning'."""
        ws_id = uuid.uuid4()

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, cleanup_status="cleaning")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=True)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()
        assert data["cleanup_status"] == "cleaning"
        assert data["id"] == str(ws_id)

    @pytest.mark.asyncio
    async def test_cleanup_calls_executor(self, mock_conn, mock_executor):
        """POST /workspaces/{id}/cleanup should call executor.cleanup_workspace()."""
        ws_id = uuid.uuid4()

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, cleanup_status="cleaning")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=True)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        mock_executor.cleanup_workspace.assert_called_once()
        call_args = mock_executor.cleanup_workspace.call_args[0][0]
        assert call_args.workspace_id == ws_id

    @pytest.mark.asyncio
    async def test_cleanup_unknown_workspace_returns_404(self, client, mock_conn):
        """POST /workspaces/{id}/cleanup on a non-existent workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post(f"/workspaces/{uuid.uuid4()}/cleanup")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_cleanup_invalid_uuid_returns_422(self, client):
        """POST /workspaces/{id}/cleanup with a malformed UUID returns 422."""
        async with client as c:
            response = await c.post("/workspaces/not-a-uuid/cleanup")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_cleanup_uses_advisory_lock_when_port_set(
        self, mock_conn, mock_executor
    ):
        """POST /workspaces/{id}/cleanup should acquire PG advisory lock when port is set."""
        ws_id = uuid.uuid4()
        test_port = 4150

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id, port=test_port))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, port=test_port, cleanup_status="cleaning")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=True)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        # Verify advisory lock was attempted
        mock_conn.fetchval.assert_called_with(
            "SELECT pg_try_advisory_xact_lock($1)", test_port
        )

    @pytest.mark.asyncio
    async def test_cleanup_skips_advisory_lock_when_port_is_none(
        self, mock_conn, mock_executor
    ):
        """POST /workspaces/{id}/cleanup should NOT call advisory lock when port is None."""
        ws_id = uuid.uuid4()

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id, port=None))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, port=None, cleanup_status="cleaning")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock()

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        mock_conn.fetchval.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_returns_409_when_advisory_lock_fails(
        self, mock_conn, mock_executor
    ):
        """POST /workspaces/{id}/cleanup returns 409 when port lock cannot be acquired."""
        ws_id = uuid.uuid4()
        test_port = 4199

        row = _make_workspace_row(ws_id, port=test_port)
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))
        mock_conn.fetchval = AsyncMock(return_value=False)
        mock_conn.execute = AsyncMock(return_value=None)

        client = _create_client(mock_conn, mock_executor=mock_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 409
        data = response.json()
        assert "detail" in data
        assert "currently being allocated" in data["detail"]

    @pytest.mark.asyncio
    async def test_cleanup_still_returns_200_when_executor_fails(
        self, mock_conn
    ):
        """When executor raises, endpoint should still return 200 with cleaning status."""
        ws_id = uuid.uuid4()

        fetch_count = 0

        async def _fetchrow(sql, *args):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                return _mock_row(_make_workspace_row(ws_id))
            else:
                return _mock_row(
                    _make_workspace_row(ws_id, cleanup_status="cleaning")
                )

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=True)

        failing_executor = AsyncMock()
        failing_executor.cleanup_workspace = AsyncMock(
            side_effect=RuntimeError("Cleanup failed")
        )

        client = _create_client(mock_conn, mock_executor=failing_executor)

        async with client as c:
            response = await c.post(f"/workspaces/{ws_id}/cleanup")

        assert response.status_code == 200
        data = response.json()
        assert data["cleanup_status"] == "cleaning"


# ────────────────────────────────────────────────────────────────────
# Endpoint registration
# ────────────────────────────────────────────────────────────────────


class TestWorkspaceRouterRegistration:
    """Verify the workspace router is registered on the app."""

    def test_routes_are_registered(self):
        """The /workspaces/{id}/pin and /workspaces/{id}/cleanup routes should be discoverable."""
        app = create_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/workspaces/{workspace_id}/pin" in routes
        assert "/workspaces/{workspace_id}/cleanup" in routes

    def test_openapi_schema_includes_workspace_endpoints(self):
        """The OpenAPI schema should include the workspace tag and endpoints."""
        app = create_app()
        schema = app.openapi()
        paths = schema.get("paths", {})

        # Check that the pin endpoint exists
        pin_path = "/workspaces/{workspace_id}/pin"
        assert pin_path in paths, f"Expected {pin_path} in OpenAPI paths"
        assert "post" in paths[pin_path]
        assert "workspaces" in paths[pin_path]["post"].get("tags", [])

        # Check that the cleanup endpoint exists
        cleanup_path = "/workspaces/{workspace_id}/cleanup"
        assert cleanup_path in paths, f"Expected {cleanup_path} in OpenAPI paths"
        assert "post" in paths[cleanup_path]
        assert "workspaces" in paths[cleanup_path]["post"].get("tags", [])
