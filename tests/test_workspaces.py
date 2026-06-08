"""Tests for the Workspace API endpoints."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi import Request

from app.core.factory import create_app
from app.db.session import get_session


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    return row


def _make_workspace_row(
    workspace_id,
    workspace_name="ws-test-001",
    *,
    runner_id=None,
    repo_url="https://github.com/example/repo.git",
    path="/data/workspaces/ws-test-001",
    branch=None,
    port=None,
    service_name=None,
    pinned=False,
    cleanup_after=None,
    cleanup_status="active",
):
    """Return a dict representing a workspaces table row."""
    now = datetime.now(timezone.utc)
    return {
        "id": workspace_id,
        "runner_id": runner_id,
        "workspace_name": workspace_name,
        "path": path,
        "repo_url": repo_url,
        "branch": branch,
        "port": port,
        "service_name": service_name,
        "pinned": pinned,
        "cleanup_after": cleanup_after,
        "cleanup_status": cleanup_status,
        "created_at": now,
        "updated_at": now,
    }


@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    return AsyncMock()


def _create_client(mock_conn):
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    app = create_app()
    mock_pool = AsyncMock()
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def client(mock_conn):
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    return _create_client(mock_conn)


class TestListWorkspaces:
    """Tests for GET /workspaces."""

    @pytest.mark.asyncio
    async def test_list_workspaces_returns_all(self, mock_conn):
        """GET /workspaces returns all workspace records."""
        ws1_id = uuid.uuid4()
        ws2_id = uuid.uuid4()
        rows = [
            _make_workspace_row(ws1_id, workspace_name="ws-alpha"),
            _make_workspace_row(ws2_id, workspace_name="ws-beta"),
        ]
        mock_conn.fetch = AsyncMock(return_value=[_mock_row(r) for r in rows])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["workspace_name"] == "ws-alpha"
        assert data[1]["workspace_name"] == "ws-beta"

    @pytest.mark.asyncio
    async def test_list_workspaces_empty_returns_empty_list(self, mock_conn):
        """GET /workspaces when no workspaces exist returns an empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_list_workspaces_filter_by_runner_id(self, mock_conn):
        """GET /workspaces?runner_id=... filters results by runner_id."""
        runner_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, runner_id=runner_id, workspace_name="ws-runner1")
        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get("/workspaces", params={"runner_id": str(runner_id)})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["runner_id"] == str(runner_id)
        assert data[0]["workspace_name"] == "ws-runner1"

    @pytest.mark.asyncio
    async def test_list_workspaces_filter_by_status(self, mock_conn):
        """GET /workspaces?status=pinned filters results by cleanup_status."""
        ws_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, cleanup_status="pinned", workspace_name="ws-pinned")
        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get("/workspaces", params={"status": "pinned"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["cleanup_status"] == "pinned"

    @pytest.mark.asyncio
    async def test_list_workspaces_filter_by_both(self, mock_conn):
        """GET /workspaces?runner_id=...&status=... combines both filters."""
        runner_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        row = _make_workspace_row(
            ws_id, runner_id=runner_id, cleanup_status="active", workspace_name="ws-combo"
        )
        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(
                "/workspaces", params={"runner_id": str(runner_id), "status": "active"}
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["runner_id"] == str(runner_id)
        assert data[0]["cleanup_status"] == "active"

    @pytest.mark.asyncio
    async def test_list_workspaces_includes_all_fields(self, mock_conn):
        """Response includes all workspace fields from the model."""
        ws_id = uuid.uuid4()
        runner_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row = {
            "id": ws_id,
            "runner_id": runner_id,
            "workspace_name": "ws-full",
            "path": "/data/ws-full",
            "repo_url": "https://github.com/example/repo",
            "branch": "main",
            "port": 8080,
            "service_name": "svc-full",
            "pinned": True,
            "cleanup_after": now,
            "cleanup_status": "pinned",
            "created_at": now,
            "updated_at": now,
        }
        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        item = data[0]
        assert item["id"] == str(ws_id)
        assert item["runner_id"] == str(runner_id)
        assert item["workspace_name"] == "ws-full"
        assert item["path"] == "/data/ws-full"
        assert item["repo_url"] == "https://github.com/example/repo"
        assert item["branch"] == "main"
        assert item["port"] == 8080
        assert item["service_name"] == "svc-full"
        assert item["pinned"] is True
        assert item["cleanup_status"] == "pinned"
        assert "cleanup_after" in item
        assert "created_at" in item
        assert "updated_at" in item


class TestGetWorkspace:
    """Tests for GET /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_get_existing_workspace_returns_200(self, mock_conn):
        """GET /workspaces/{id} for an existing workspace returns 200 with full details."""
        ws_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, workspace_name="ws-get-test")
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(ws_id)
        assert data["workspace_name"] == "ws-get-test"
        assert data["repo_url"] == "https://github.com/example/repo.git"
        assert data["path"] == "/data/workspaces/ws-test-001"
        assert data["cleanup_status"] == "active"

    @pytest.mark.asyncio
    async def test_get_unknown_workspace_returns_404(self, mock_conn):
        """GET /workspaces/{id} for an unknown workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/workspaces/{uuid.uuid4()}")

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data
        assert data["detail"] == "Workspace not found"

    @pytest.mark.asyncio
    async def test_get_invalid_uuid_returns_422(self, client):
        """GET /workspaces/{id} with a malformed UUID should return 422."""
        async with client as c:
            response = await c.get("/workspaces/not-a-uuid")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_workspace_includes_all_fields(self, mock_conn):
        """Response includes all workspace fields for a single workspace."""
        ws_id = uuid.uuid4()
        runner_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row = {
            "id": ws_id,
            "runner_id": runner_id,
            "workspace_name": "ws-single",
            "path": "/data/ws-single",
            "repo_url": "https://github.com/example/single",
            "branch": "develop",
            "port": 9000,
            "service_name": "svc-single",
            "pinned": False,
            "cleanup_after": None,
            "cleanup_status": "active",
            "created_at": now,
            "updated_at": now,
        }
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(ws_id)
        assert data["runner_id"] == str(runner_id)
        assert data["workspace_name"] == "ws-single"
        assert data["path"] == "/data/ws-single"
        assert data["repo_url"] == "https://github.com/example/single"
        assert data["branch"] == "develop"
        assert data["port"] == 9000
        assert data["service_name"] == "svc-single"
        assert data["pinned"] is False
        assert data["cleanup_after"] is None
        assert data["cleanup_status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_get_pinned_workspace_shows_pinned_true(self, mock_conn):
        """GET /workspaces/{id} for a pinned workspace shows pinned=true."""
        ws_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, pinned=True, cleanup_status="pinned")
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["pinned"] is True
        assert data["cleanup_status"] == "pinned"

    @pytest.mark.asyncio
    async def test_get_workspace_with_runner_id(self, mock_conn):
        """GET /workspaces/{id} includes runner_id when set."""
        ws_id = uuid.uuid4()
        runner_id = uuid.uuid4()
        row = _make_workspace_row(ws_id, runner_id=runner_id)
        mock_conn.fetchrow = AsyncMock(return_value=_mock_row(row))

        client = _create_client(mock_conn)

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["runner_id"] == str(runner_id)
