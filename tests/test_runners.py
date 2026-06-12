"""Tests for the Runner API endpoints (GET /runners)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.conftest import mock_row

# mock_conn and client fixtures are auto-discovered from conftest.py


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
            return_value=[mock_row(row1), mock_row(row2)]
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

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

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

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

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

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

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

        mock_conn.fetch = AsyncMock(return_value=[mock_row(row)])

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
            return_value=[mock_row(row2), mock_row(row1)]
        )

        async with client as c:
            response = await c.get("/runners")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        # Verify descending order by created_at
        created_ats = [r["created_at"] for r in data]
        assert created_ats == sorted(created_ats, reverse=True)


class TestGetRunnerDetail:
    """Tests for GET /runners/{runner_id}."""

    # ------------------------------------------------------------------
    # Helper to set up mock responses for the detail endpoint
    # ------------------------------------------------------------------

    def _setup_detail_mocks(
        self,
        mock_conn,
        runner_id,
        *,
        hostname="runner-detail.example.com",
        status="online",
        executor_type="awx",
        labels=None,
        workspace_obs=None,
        opencode_obs=None,
        latest_obs_present=False,
    ):
        """Configure mock_conn for the detail endpoint's four DB queries.

        Returns the runner row so callers can inspect returned data.
        """
        now = datetime.now(timezone.utc)
        runner_row_data = {
            "id": runner_id,
            "runner_id": str(runner_id),
            "hostname": hostname,
            "status": status,
            "executor_type": executor_type,
            "labels": labels,
            "created_at": now,
            "updated_at": now,
        }
        runner_row = mock_row(runner_row_data)
        runner_row.get.side_effect = runner_row_data.get

        # Call 1: fetchrow for runner (returns the runner row)
        # Call 2: fetchrow for latest observation (returns None or a row)
        if latest_obs_present:
            obs_row_data = {
                "disk_used_percent": 55.0,
                "memory_used_percent": 70.0,
                "load_1m": 1.5,
                "observed_at": now,
            }
            obs_row = mock_row(obs_row_data)
            mock_conn.fetchrow = AsyncMock(side_effect=[runner_row, obs_row])
        else:
            mock_conn.fetchrow = AsyncMock(side_effect=[runner_row, None])

        # fetch for workspace observations and opencode instance observations
        wsmock_rows = []
        if workspace_obs:
            for wo in workspace_obs:
                wsmock_rows.append(mock_row(wo))

        oimock_rows = []
        if opencode_obs:
            for oo in opencode_obs:
                oimock_rows.append(mock_row(oo))

        mock_conn.fetch = AsyncMock(side_effect=[wsmock_rows, oimock_rows])

        return runner_row_data

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_runner_detail_returns_200(self, client, mock_conn):
        """GET /runners/{id} returns 200 for an existing runner."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(mock_conn, r_id)

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_runner_detail_returns_404_for_unknown_id(
        self, client, mock_conn
    ):
        """GET /runners/{id} returns 404 for a non-existent runner."""
        r_id = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 404
        detail = response.json()["detail"]
        assert str(r_id) in detail
        assert "not found" in detail

    @pytest.mark.asyncio
    async def test_get_runner_detail_has_all_base_fields(self, client, mock_conn):
        """The response includes all RunnerResponse fields."""
        r_id = uuid.uuid4()
        labels = {"env": "staging", "region": "eu-west-1"}
        self._setup_detail_mocks(
            mock_conn,
            r_id,
            hostname="runner-staging.example.com",
            status="online",
            executor_type="awx",
            labels=labels,
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(r_id)
        assert data["runner_id"] == str(r_id)
        assert data["hostname"] == "runner-staging.example.com"
        assert data["status"] == "online"
        assert data["executor_type"] == "awx"
        assert data["labels"] == labels
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_get_runner_detail_observation_arrays_are_empty_by_default(
        self, client, mock_conn
    ):
        """workspace_observations and opencode_instance_observations default to []."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(mock_conn, r_id)

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["workspace_observations"] == []
        assert data["opencode_instance_observations"] == []

    @pytest.mark.asyncio
    async def test_get_runner_detail_includes_workspace_observations(
        self, client, mock_conn
    ):
        """The response includes workspace_observations when present."""
        r_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        workspace_obs = [
            {
                "workspace_name": "ws-alpha",
                "status": "active",
                "opencode_status": "running",
                "observed_at": now,
            },
            {
                "workspace_name": "ws-beta",
                "status": "idle",
                "opencode_status": "stopped",
                "observed_at": now,
            },
        ]
        self._setup_detail_mocks(mock_conn, r_id, workspace_obs=workspace_obs)

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert len(data["workspace_observations"]) == 2
        assert data["workspace_observations"][0]["workspace_name"] == "ws-alpha"
        assert data["workspace_observations"][0]["status"] == "active"
        assert data["workspace_observations"][0]["opencode_status"] == "running"
        assert data["workspace_observations"][1]["workspace_name"] == "ws-beta"

    @pytest.mark.asyncio
    async def test_get_runner_detail_includes_opencode_instance_observations(
        self, client, mock_conn
    ):
        """The response includes opencode_instance_observations when present."""
        r_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        opencode_obs = [
            {
                "instance_name": "oc-main",
                "version": "0.1.0",
                "status": "running",
                "observed_at": now,
            },
        ]
        self._setup_detail_mocks(
            mock_conn, r_id, opencode_obs=opencode_obs
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert len(data["opencode_instance_observations"]) == 1
        obs = data["opencode_instance_observations"][0]
        assert obs["instance_name"] == "oc-main"
        assert obs["version"] == "0.1.0"
        assert obs["status"] == "running"
        assert "observed_at" in obs

    @pytest.mark.asyncio
    async def test_get_runner_detail_response_has_both_observation_arrays(
        self, client, mock_conn
    ):
        """Both observation arrays are present in the response."""
        r_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        workspace_obs = [
            {
                "workspace_name": "ws-one",
                "status": "active",
                "opencode_status": "running",
                "observed_at": now,
            },
        ]
        opencode_obs = [
            {
                "instance_name": "oc-one",
                "version": "0.2.0",
                "status": "running",
                "observed_at": now,
            },
        ]
        self._setup_detail_mocks(
            mock_conn,
            r_id,
            workspace_obs=workspace_obs,
            opencode_obs=opencode_obs,
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert len(data["workspace_observations"]) == 1
        assert len(data["opencode_instance_observations"]) == 1
        assert "observed_at" in data["workspace_observations"][0]
        assert "observed_at" in data["opencode_instance_observations"][0]

    @pytest.mark.asyncio
    async def test_get_runner_detail_includes_latest_observation(
        self, client, mock_conn
    ):
        """latest_observation is populated when runner_observations exist."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(mock_conn, r_id, latest_obs_present=True)

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        obs = data["latest_observation"]
        assert obs is not None
        assert obs["disk_used_percent"] == 55.0
        assert obs["memory_used_percent"] == 70.0
        assert obs["load_1m"] == 1.5
        assert "observed_at" in obs
    @pytest.mark.asyncio
    async def test_get_runner_detail_includes_policy_status(self, client, mock_conn):
        """The response includes policy_status and policy_reason fields."""
        r_id = uuid.uuid4()
        runner_data = self._setup_detail_mocks(
            mock_conn, r_id, status="online"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert "policy_status" in data
        assert "policy_reason" in data

    @pytest.mark.asyncio
    async def test_policy_status_healthy(self, client, mock_conn):
        """policy_status is HEALTHY for runners with no pressure status."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="online"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["policy_status"] == "HEALTHY"
        assert data["policy_reason"] == "Runner is healthy"

    @pytest.mark.asyncio
    async def test_policy_status_unknown(self, client, mock_conn):
        """policy_status is UNKNOWN when runner status is UNKNOWN."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="UNKNOWN"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["policy_status"] == "UNKNOWN"
        assert data["policy_reason"] == "Runner observations are stale"

    @pytest.mark.asyncio
    async def test_policy_status_blocked_disk(self, client, mock_conn):
        """policy_status is BLOCKED_DISK_PRESSURE when runner status matches."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="BLOCKED_DISK_PRESSURE"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["policy_status"] == "BLOCKED_DISK_PRESSURE"
        assert data["policy_reason"] == "Runner has disk pressure"

    @pytest.mark.asyncio
    async def test_policy_status_blocked_memory(self, client, mock_conn):
        """policy_status is BLOCKED_MEMORY_PRESSURE when runner status matches."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="BLOCKED_MEMORY_PRESSURE"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["policy_status"] == "BLOCKED_MEMORY_PRESSURE"
        assert data["policy_reason"] == "Runner has memory pressure"


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
