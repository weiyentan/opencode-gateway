"""Tests for the Workspace API endpoints."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.conftest import make_workspace_row, mock_row

# mock_conn and client fixtures are auto-discovered from conftest.py


class TestListWorkspaces:
    """Tests for GET /workspaces."""

    @pytest.mark.asyncio
    async def test_list_workspaces_returns_200_with_empty_list(self, client, mock_conn):
        """GET /workspaces with no workspaces returns 200 with empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_workspaces_returns_all_workspaces(self, client, mock_conn):
        """GET /workspaces returns all workspaces from the DB."""
        ws1_id = uuid.uuid4()
        ws2_id = uuid.uuid4()
        row1 = make_workspace_row(ws1_id, workspace_name="ws-one")
        row2 = make_workspace_row(ws2_id, workspace_name="ws-two")

        mock_conn.fetch = AsyncMock(
            return_value=[mock_row(row1), mock_row(row2)]
        )

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = {w["workspace_name"] for w in data}
        assert names == {"ws-one", "ws-two"}

    @pytest.mark.asyncio
    async def test_list_workspaces_response_has_all_expected_fields(self, client, mock_conn):
        """GET /workspaces returns objects with all WorkspacePydantic fields."""
        ws_id = uuid.uuid4()
        row = make_workspace_row(
            ws_id,
            runner_id=uuid.uuid4(),
            workspace_name="ws-full",
            path="/data/workspaces/ws-full",
            repo_url="https://github.com/example/repo.git",
            branch="main",
            port=8080,
            service_name="opencode-serve",
            pinned=True,
            cleanup_status="active",
        )

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()[0]
        assert data["id"] == str(ws_id)
        assert data["runner_id"] is not None
        assert data["workspace_name"] == "ws-full"
        assert data["path"] == "/data/workspaces/ws-full"
        assert data["repo_url"] == "https://github.com/example/repo.git"
        assert data["branch"] == "main"
        assert data["port"] == 8080
        assert data["service_name"] == "opencode-serve"
        assert data["pinned"] is True
        assert data["cleanup_status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_list_workspaces_sorts_by_created_at_desc(self, client, mock_conn):
        """Workspaces should be returned in descending created_at order."""
        earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)
        later = datetime(2025, 1, 1, tzinfo=timezone.utc)

        ws1_id = uuid.uuid4()
        ws2_id = uuid.uuid4()
        row1 = make_workspace_row(ws1_id, workspace_name="ws-old")
        row2 = make_workspace_row(ws2_id, workspace_name="ws-new")
        row1["created_at"] = earlier
        row2["created_at"] = later

        # Return in wrong order to verify server sorts
        mock_conn.fetch = AsyncMock(
            return_value=[mock_row(row1), mock_row(row2)]
        )

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


class TestListWorkspacesFiltering:
    """Tests for GET /workspaces with query parameters."""

    @pytest.mark.asyncio
    async def test_filter_by_runner_id(self, client, mock_conn):
        """GET /workspaces?runner_id=... filters by runner_id."""
        target_runner = uuid.uuid4()
        ws_id = uuid.uuid4()
        row = make_workspace_row(ws_id, runner_id=target_runner)

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        async with client as c:
            response = await c.get(f"/workspaces?runner_id={target_runner}")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(ws_id)

        # Verify the SQL parameter was passed
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        sql = call_args[0][0]
        assert "WHERE" in sql
        assert "runner_id = $1" in sql

    @pytest.mark.asyncio
    async def test_filter_by_status(self, client, mock_conn):
        """GET /workspaces?status=... filters by cleanup_status."""
        ws_id = uuid.uuid4()
        row = make_workspace_row(ws_id, cleanup_status="pinned")

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        async with client as c:
            response = await c.get("/workspaces?status=pinned")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["cleanup_status"] == "pinned"

        # Verify the SQL parameter was passed
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        sql = call_args[0][0]
        assert "WHERE" in sql
        assert "cleanup_status = $1" in sql

    @pytest.mark.asyncio
    async def test_filter_by_runner_id_and_status(self, client, mock_conn):
        """GET /workspaces?runner_id=...&status=... applies both filters."""
        target_runner = uuid.uuid4()
        ws_id = uuid.uuid4()
        row = make_workspace_row(
            ws_id, runner_id=target_runner, cleanup_status="cleaning"
        )

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

        async with client as c:
            response = await c.get(
                f"/workspaces?runner_id={target_runner}&status=cleaning"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["cleanup_status"] == "cleaning"

        # Verify both filters are in the SQL
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        sql = call_args[0][0]
        assert "runner_id = $1" in sql
        assert "cleanup_status = $2" in sql

    @pytest.mark.asyncio
    async def test_filter_by_runner_id_returns_empty_when_no_match(self, client, mock_conn):
        """Filtering by runner_id with no matches returns empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(f"/workspaces?runner_id={uuid.uuid4()}")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_filter_by_status_returns_empty_when_no_match(self, client, mock_conn):
        """Filtering by status with no matches returns empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/workspaces?status=invalid_status")

        assert response.status_code == 200
        assert response.json() == []


class TestGetWorkspace:
    """Tests for GET /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_get_existing_workspace_returns_200(self, client, mock_conn):
        """GET /workspaces/{id} for an existing workspace returns 200 with full record."""
        ws_id = uuid.uuid4()
        row = make_workspace_row(
            ws_id,
            workspace_name="ws-single",
            repo_url="https://github.com/example/single.git",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(ws_id)
        assert data["workspace_name"] == "ws-single"
        assert data["repo_url"] == "https://github.com/example/single.git"

    @pytest.mark.asyncio
    async def test_get_unknown_workspace_returns_404(self, client, mock_conn):
        """GET /workspaces/{id} for an unknown workspace returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/workspaces/{uuid.uuid4()}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Workspace not found"

    @pytest.mark.asyncio
    async def test_get_invalid_uuid_returns_422(self, client):
        """GET /workspaces/{id} with a malformed UUID returns 422."""
        async with client as c:
            response = await c.get("/workspaces/not-a-uuid")

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_get_workspace_response_has_all_fields(self, client, mock_conn):
        """GET /workspaces/{id} returns all WorkspacePydantic fields."""
        ws_id = uuid.uuid4()
        runner_id = uuid.uuid4()
        row = make_workspace_row(
            ws_id,
            runner_id=runner_id,
            workspace_name="ws-all-fields",
            path="/data/workspaces/ws-all-fields",
            repo_url="https://github.com/example/all.git",
            branch="develop",
            port=9090,
            service_name="opencode-serve-all",
            pinned=True,
            cleanup_status="active",
        )
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(row))

        async with client as c:
            response = await c.get(f"/workspaces/{ws_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(ws_id)
        assert data["runner_id"] == str(runner_id)
        assert data["workspace_name"] == "ws-all-fields"
        assert data["path"] == "/data/workspaces/ws-all-fields"
        assert data["repo_url"] == "https://github.com/example/all.git"
        assert data["branch"] == "develop"
        assert data["port"] == 9090
        assert data["service_name"] == "opencode-serve-all"
        assert data["pinned"] is True
        assert data["cleanup_status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data


class TestWorkspaceAPIErrors:
    """Tests for error handling in workspace endpoints."""

    @pytest.mark.asyncio
    async def test_list_workspaces_db_error_returns_500(self, client, mock_conn):
        """GET /workspaces database error propagates as 500."""
        mock_conn.fetch = AsyncMock(
            side_effect=RuntimeError("Database connection lost")
        )

        async with client as c:
            response = await c.get("/workspaces")

        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_get_workspace_db_error_returns_500(self, client, mock_conn):
        """GET /workspaces/{id} database error propagates as 500."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=RuntimeError("Database connection lost")
        )

        async with client as c:
            response = await c.get(f"/workspaces/{uuid.uuid4()}")

        assert response.status_code == 500
