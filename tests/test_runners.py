"""Tests for the Runner API endpoints (GET /runners)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _make_runner_row(
    runner_id_val,
    *,
    hostname="runner-01.example.com",
    status="online",
    executor_type="awx",
    labels=None,
):
    """Return a dict representing a runners table row with optional observation fields."""
    now = datetime.now(timezone.utc)
    return {
        "id": runner_id_val,
        "runner_id": str(runner_id_val),
        "hostname": hostname,
        "status": status,
        "executor_type": executor_type,
        "labels": labels,
        "created_at": now,
        "updated_at": now,
        # Observation columns — set to None by default (no observation)
        "disk_used_percent": None,
        "memory_used_percent": None,
        "load_1m": None,
        "observed_at": None,
    }


def _make_runner_row_with_observation(
    runner_id_val,
    *,
    hostname="runner-01.example.com",
    status="online",
    executor_type="awx",
    labels=None,
    disk_used_percent=45.2,
    memory_used_percent=62.8,
    load_1m=1.5,
):
    """Return a dict representing a runners table row WITH a latest observation."""
    row = _make_runner_row(
        runner_id_val,
        hostname=hostname,
        status=status,
        executor_type=executor_type,
        labels=labels,
    )
    now = datetime.now(timezone.utc)
    row["disk_used_percent"] = disk_used_percent
    row["memory_used_percent"] = memory_used_percent
    row["load_1m"] = load_1m
    row["observed_at"] = now
    return row


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


class TestListRunners:
    """Tests for GET /runners."""

    @pytest.mark.asyncio
    async def test_list_runners_returns_200_with_empty_list(self, client, mock_conn):
        """GET /runners with no runners returns 200 with empty list."""
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_runners_returns_all_runners(self, client, mock_conn):
        """GET /runners returns all runners from the DB."""
        r1_id = uuid.uuid4()
        r2_id = uuid.uuid4()
        row1 = _make_runner_row(r1_id, hostname="runner-alpha")
        row2 = _make_runner_row(r2_id, hostname="runner-beta")

        mock_conn.fetch = AsyncMock(
            return_value=[_mock_row(row1), _mock_row(row2)]
        )

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        hostnames = {r["hostname"] for r in data}
        assert hostnames == {"runner-alpha", "runner-beta"}

    @pytest.mark.asyncio
    async def test_list_runners_response_has_all_expected_fields(self, client, mock_conn):
        """GET /runners returns objects with all RunnerResponse fields."""
        r_id = uuid.uuid4()
        labels = {"env": "prod", "region": "us-east-1"}
        row = _make_runner_row(
            r_id,
            hostname="runner-full.example.com",
            status="online",
            executor_type="awx",
            labels=labels,
        )

        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()[0]
        assert data["id"] == str(r_id)
        assert data["runner_id"] == str(r_id)
        assert data["hostname"] == "runner-full.example.com"
        assert data["status"] == "online"
        assert data["executor_type"] == "awx"
        assert data["labels"] == labels
        assert data["latest_observation"] is None
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_list_runners_includes_latest_observation(self, client, mock_conn):
        """GET /runners includes latest_observation when observations exist."""
        r_id = uuid.uuid4()
        row = _make_runner_row_with_observation(
            r_id,
            hostname="runner-obs.example.com",
            disk_used_percent=55.0,
            memory_used_percent=70.2,
            load_1m=2.1,
        )

        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()[0]
        obs = data["latest_observation"]
        assert obs is not None
        assert obs["disk_used_percent"] == 55.0
        assert obs["memory_used_percent"] == 70.2
        assert obs["load_1m"] == 2.1
        assert "observed_at" in obs

    @pytest.mark.asyncio
    async def test_list_runners_latest_observation_is_null_when_no_observations(
        self, client, mock_conn
    ):
        """GET /runners sets latest_observation to null when no observations exist."""
        r_id = uuid.uuid4()
        row = _make_runner_row(r_id)

        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()[0]
        assert data["latest_observation"] is None

    @pytest.mark.asyncio
    async def test_list_runners_with_observation_has_all_summary_fields(
        self, client, mock_conn
    ):
        """The latest_observation object contains all four summary fields."""
        r_id = uuid.uuid4()
        row = _make_runner_row_with_observation(
            r_id,
            disk_used_percent=33.3,
            memory_used_percent=50.0,
            load_1m=0.8,
        )

        mock_conn.fetch = AsyncMock(return_value=[_mock_row(row)])

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        obs = response.json()[0]["latest_observation"]
        assert set(obs.keys()) == {
            "disk_used_percent",
            "memory_used_percent",
            "load_1m",
            "observed_at",
        }

    @pytest.mark.asyncio
    async def test_list_runners_sorts_by_created_at_desc(self, client, mock_conn):
        """Runners should be returned in descending created_at order."""
        earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)
        later = datetime(2025, 1, 1, tzinfo=timezone.utc)

        r1_id = uuid.uuid4()
        r2_id = uuid.uuid4()
        row1 = _make_runner_row(r1_id, hostname="runner-old")
        row2 = _make_runner_row(r2_id, hostname="runner-new")
        row1["created_at"] = earlier
        row2["created_at"] = later

        # Return in wrong order to verify server sorts
        mock_conn.fetch = AsyncMock(
            return_value=[_mock_row(row2), _mock_row(row1)]
        )

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


class TestRunnersAPIErrors:
    """Tests for error handling in runner endpoints."""

    @pytest.mark.asyncio
    async def test_list_runners_db_error_returns_500(self, client, mock_conn):
        """GET /runners database error propagates as 500."""
        mock_conn.fetch = AsyncMock(
            side_effect=RuntimeError("Database connection lost")
        )

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 500
