"""Tests for the Runner API endpoints (GET /runners)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.conftest import create_client, mock_row

# mock_conn and client fixtures are auto-discovered from conftest.py


def _make_runner_row(
    runner_id_val,
    *,
    hostname="runner-01.example.com",
    status="online",
    admin_status=None,
    health_status=None,
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
        "admin_status": admin_status,
        "health_status": health_status,
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
        assert response.json()["data"] == []

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
        data = response.json()["data"]
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
        data = response.json()["data"][0]
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
        data = response.json()["data"][0]
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
        data = response.json()["data"][0]
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
        obs = response.json()["data"][0]["latest_observation"]
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
        data = response.json()["data"]
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
        admin_status=None,
        health_status=None,
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
            "admin_status": admin_status,
            "health_status": health_status,
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
        error_data = response.json()
        assert error_data["status"] == "error"
        assert error_data["error"]["code"] == "NOT_FOUND"
        assert str(r_id) in error_data["error"]["message"]
        assert "not found" in error_data["error"]["message"]

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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
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
        data = response.json()["data"]
        assert "policy_status" in data
        assert "policy_reason" in data

    @pytest.mark.asyncio
    async def test_policy_status_healthy(self, client, mock_conn):
        """policy_status is HEALTHY for runners with no pressure status."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="HEALTHY", health_status="HEALTHY"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["policy_status"] == "HEALTHY"
        assert data["policy_reason"] == "Runner is healthy"

    @pytest.mark.asyncio
    async def test_policy_status_unknown(self, client, mock_conn):
        """policy_status is UNKNOWN when health_status is UNKNOWN."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="UNKNOWN", health_status="UNKNOWN"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["policy_status"] == "UNKNOWN"
        assert data["policy_reason"] == "Runner observations are stale"

    @pytest.mark.asyncio
    async def test_policy_status_blocked_disk(self, client, mock_conn):
        """policy_status is BLOCKED_DISK_PRESSURE when health_status matches."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="BLOCKED_DISK_PRESSURE", health_status="BLOCKED_DISK_PRESSURE"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["policy_status"] == "BLOCKED_DISK_PRESSURE"
        assert data["policy_reason"] == "Runner has disk pressure"

    @pytest.mark.asyncio
    async def test_policy_status_blocked_memory(self, client, mock_conn):
        """policy_status is BLOCKED_MEMORY_PRESSURE when health_status matches."""
        r_id = uuid.uuid4()
        self._setup_detail_mocks(
            mock_conn, r_id, status="BLOCKED_MEMORY_PRESSURE", health_status="BLOCKED_MEMORY_PRESSURE"
        )

        async with client as c:
            response = await c.get(f"/runners/{r_id}")

        assert response.status_code == 200
        data = response.json()["data"]
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


class TestPostRunnerStatus:
    """Tests for POST /runners/{runner_id}/status."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _setup_mocks(
        mock_conn,
        *,
        runner_id_val=None,
        runner_id_str=None,
        hostname="runner-offline.example.com",
        current_status="HEALTHY",
        current_admin_status=None,
    ):
        """Configure mock_conn.fetchrow to return a runner row, and track execute calls.

        Parameters
        ----------
        current_status:
            The value for the legacy ``status`` column.
        current_admin_status:
            The value for the ``admin_status`` column. When None, the mock
            returns None (simulating a runner that has never had its admin
            status set by an operator).
        """
        if runner_id_val is None:
            runner_id_val = uuid.uuid4()
        if runner_id_str is None:
            runner_id_str = str(runner_id_val)

        db_row = {
            "id": runner_id_val,
            "runner_id": runner_id_str,
            "hostname": hostname,
            "admin_status": current_admin_status,
            "status": current_status,
        }

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "FROM runners" in sql:
                return mock_row(db_row)
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        return runner_id_val, execute_calls

    # ------------------------------------------------------------------
    # valid transitions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_status, target_status", [
        ("HEALTHY", "offline"),
        ("HEALTHY", "maintenance"),
        ("BLOCKED_DISK_PRESSURE", "offline"),
        ("BLOCKED_DISK_PRESSURE", "maintenance"),
        ("BLOCKED_MEMORY_PRESSURE", "offline"),
        ("BLOCKED_MEMORY_PRESSURE", "maintenance"),
        ("UNKNOWN", "offline"),
        ("UNKNOWN", "maintenance"),
        ("online", "offline"),
        ("online", "maintenance"),
        ("offline", "online"),
        ("offline", "maintenance"),
        ("maintenance", "online"),
        ("maintenance", "offline"),
    ])
    async def test_valid_transition_returns_200(
        self, current_status, target_status, mock_conn
    ):
        """POST /runners/{id}/status with a valid transition returns 200."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_status=current_status
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": target_status, "reason": "Testing transition"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["previous_status"] == current_status
        assert data["current_status"] == target_status
        assert data["reason"] == "Testing transition"
        assert "updated_at" in data

        # Verify runner admin_status UPDATE was issued
        update_calls = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET admin_status" in sql
        ]
        assert len(update_calls) == 1
        _sql, args = update_calls[0]
        assert args[0] == target_status

    # ------------------------------------------------------------------
    # runner_events logging
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_status_change_logs_to_runner_events(self, mock_conn):
        """POST /runners/{id}/status inserts a record into runner_events."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_status="HEALTHY"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": "offline", "reason": "Maintenance window"},
            )

        assert response.status_code == 200

        # Verify runner_events INSERT
        insert_calls = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO runner_events" in sql
        ]
        assert len(insert_calls) == 1
        _sql, args = insert_calls[0]
        # args: (event_id, runner_id, event_type, old_status, new_status, reason, created_at)
        assert args[1] == r_id  # runner_id
        assert args[2] == "runner_status_offline"
        assert args[3] == "HEALTHY"
        assert args[4] == "offline"
        assert args[5] == "Maintenance window"

    @pytest.mark.asyncio
    async def test_status_change_logs_default_reason_when_empty(self, mock_conn):
        """When reason is empty, the runner_events reason uses a default message."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_status="UNKNOWN"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": "maintenance"},
            )

        assert response.status_code == 200

        insert_calls = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO runner_events" in sql
        ]
        assert len(insert_calls) == 1
        _sql, args = insert_calls[0]
        assert args[2] == "runner_status_maintenance"
        assert args[3] == "UNKNOWN"
        assert args[4] == "maintenance"
        assert args[5] == "Runner status changed to maintenance"

    # ------------------------------------------------------------------
    # invalid transitions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_status, target_status", [
        ("HEALTHY", "online"),       # cannot go directly from system status to online
        ("BLOCKED_DISK_PRESSURE", "online"),
        ("BLOCKED_MEMORY_PRESSURE", "online"),
        ("UNKNOWN", "online"),
        ("online", "HEALTHY"),       # invalid target
        ("online", "BLOCKED_DISK_PRESSURE"),
        ("offline", "HEALTHY"),       # invalid target
        ("offline", "BLOCKED_MEMORY_PRESSURE"),
    ])
    async def test_invalid_transition_returns_422(
        self, current_status, target_status, mock_conn
    ):
        """POST /runners/{id}/status with an invalid transition returns 422."""
        r_id, _ = self._setup_mocks(
            mock_conn, current_status=current_status
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": target_status, "reason": "Invalid attempt"},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bogus_status", [
        "HEALTHY",
        "UNKNOWN",
        "running",
        "paused",
        "",
    ])
    async def test_unknown_target_status_returns_422(
        self, bogus_status, mock_conn
    ):
        """POST /runners/{id}/status with a non-manual target returns 422."""
        r_id, _ = self._setup_mocks(mock_conn, current_status="HEALTHY")

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": bogus_status, "reason": "Bad status"},
            )

        assert response.status_code == 422

    # ------------------------------------------------------------------
    # not found
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_status_on_unknown_runner_returns_404(self, mock_conn):
        """POST /runners/{id}/status on a non-existent runner returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock()

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{uuid.uuid4()}/status",
                json={"status": "offline", "reason": "Test"},
            )

        assert response.status_code == 404

    # ------------------------------------------------------------------
    # response structure
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_response_includes_previous_and_current_status(self, mock_conn):
        """The response body includes previous_status, current_status, and reason."""
        r_id, _ = self._setup_mocks(
            mock_conn, current_status="BLOCKED_DISK_PRESSURE"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/status",
                json={"status": "maintenance", "reason": "Disk replacement"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["previous_status"] == "BLOCKED_DISK_PRESSURE"
        assert data["current_status"] == "maintenance"
        assert data["reason"] == "Disk replacement"
        assert data["hostname"] == "runner-offline.example.com"
        assert "id" in data
        assert "runner_id" in data
        assert "updated_at" in data


class TestPostRunnerAdminStatus:
    """Tests for POST /runners/{runner_id}/admin-status."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _setup_mocks(
        mock_conn,
        *,
        runner_id_val=None,
        runner_id_str=None,
        hostname="runner-admin.example.com",
        current_admin_status=None,
    ):
        """Configure mock_conn.fetchrow to return a runner row, and track execute calls.

        Parameters
        ----------
        current_admin_status:
            The value for the ``admin_status`` column. When None, the mock
            returns None (simulating a runner that has never had its admin
            status set by an operator).
        """
        if runner_id_val is None:
            runner_id_val = uuid.uuid4()
        if runner_id_str is None:
            runner_id_str = str(runner_id_val)

        db_row = {
            "id": runner_id_val,
            "runner_id": runner_id_str,
            "hostname": hostname,
            "admin_status": current_admin_status,
        }

        execute_calls: list[tuple] = []

        async def _fetchrow(sql, *args):
            if "FROM runners" in sql:
                return mock_row(db_row)
            return None

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        return runner_id_val, execute_calls

    # ------------------------------------------------------------------
    # valid transitions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target_admin_status", [
        "online",
        "offline",
        "maintenance",
    ])
    async def test_valid_admin_status_returns_200(
        self, target_admin_status, mock_conn
    ):
        """POST /runners/{id}/admin-status with a valid value returns 200."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_admin_status=None
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": target_admin_status},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["previous_admin_status"] is None
        assert data["current_admin_status"] == target_admin_status
        assert "updated_at" in data

        # Verify only admin_status UPDATE was issued (no status or health_status)
        update_calls = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET" in sql
        ]
        assert len(update_calls) == 1
        _sql, args = update_calls[0]
        # UPDATE runners SET admin_status = $1, updated_at = $2 WHERE id = $3
        assert args[0] == target_admin_status
        # Confirm status/health_status are NOT in the SET clause
        assert "health_status" not in _sql
        assert "status = $" not in _sql.replace("admin_status", "")

    @pytest.mark.asyncio
    async def test_admin_status_does_not_affect_health_status(self, mock_conn):
        """POST /runners/{id}/admin-status does not touch health_status."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_admin_status="maintenance"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": "online"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["previous_admin_status"] == "maintenance"
        assert data["current_admin_status"] == "online"

        # Verify the UPDATE statement only touches admin_status
        update_calls = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET" in sql
        ]
        assert len(update_calls) == 1
        _sql = update_calls[0][0]
        assert "admin_status" in _sql
        assert "health_status" not in _sql
        assert "status" not in _sql.replace("admin_status", "")

    # ------------------------------------------------------------------
    # runner_events logging
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_admin_status_change_logs_to_runner_events(self, mock_conn):
        """POST /runners/{id}/admin-status inserts a record into runner_events."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_admin_status=None
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": "offline"},
            )

        assert response.status_code == 200

        # Verify runner_events INSERT
        insert_calls = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO runner_events" in sql
        ]
        assert len(insert_calls) == 1
        _sql, args = insert_calls[0]
        # args: (event_id, runner_id, event_type, old_status, new_status, reason, created_at)
        assert args[1] == r_id  # runner_id
        assert args[2] == "admin_status_offline"
        assert args[3] is None  # old_status was None (no prior admin status)
        assert args[4] == "offline"
        assert args[5] == "Admin status changed to offline"

    @pytest.mark.asyncio
    async def test_admin_status_change_logs_with_previous_admin_status(self, mock_conn):
        """runner_events records the previous admin_status when one existed."""
        r_id, execute_calls = self._setup_mocks(
            mock_conn, current_admin_status="online"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": "maintenance"},
            )

        assert response.status_code == 200

        insert_calls = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO runner_events" in sql
        ]
        assert len(insert_calls) == 1
        _sql, args = insert_calls[0]
        assert args[2] == "admin_status_maintenance"
        assert args[3] == "online"  # old_status
        assert args[4] == "maintenance"
        assert args[5] == "Admin status changed to maintenance"

    # ------------------------------------------------------------------
    # invalid values
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bogus_status", [
        "HEALTHY",
        "UNKNOWN",
        "running",
        "paused",
        "",
        "BLOCKED_DISK_PRESSURE",
    ])
    async def test_invalid_admin_status_returns_422(
        self, bogus_status, mock_conn
    ):
        """POST /runners/{id}/admin-status with an invalid value returns 422."""
        r_id, _ = self._setup_mocks(mock_conn, current_admin_status=None)

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": bogus_status},
            )

        assert response.status_code == 422

    # ------------------------------------------------------------------
    # not found
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_admin_status_on_unknown_runner_returns_404(self, mock_conn):
        """POST /runners/{id}/admin-status on non-existent runner returns 404."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock()

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{uuid.uuid4()}/admin-status",
                json={"admin_status": "offline"},
            )

        assert response.status_code == 404

    # ------------------------------------------------------------------
    # response structure
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_admin_status_response_includes_previous_and_current(self, mock_conn):
        """The response body includes previous_admin_status, current_admin_status."""
        r_id, _ = self._setup_mocks(
            mock_conn, current_admin_status="online"
        )

        client = create_client(mock_conn)

        async with client as c:
            response = await c.post(
                f"/runners/{r_id}/admin-status",
                json={"admin_status": "offline"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["previous_admin_status"] == "online"
        assert data["current_admin_status"] == "offline"
        assert data["hostname"] == "runner-admin.example.com"
        assert "id" in data
        assert "runner_id" in data
        assert "updated_at" in data
