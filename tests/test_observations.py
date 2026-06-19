"""Tests for the POST /observations endpoint."""

import uuid
from unittest.mock import AsyncMock

import pytest

from tests.conftest import mock_row

# mock_conn and client fixtures are auto-discovered from conftest.py


class TestIngestObservations:
    """Tests for POST /observations."""

    MINIMAL_PAYLOAD = {
        "runner_id": "runner-alpha-1",
        "hostname": "alpha-1.example.com",
        "executor_type": "awx",
    }

    FULL_PAYLOAD = {
        "runner_id": "runner-beta-2",
        "hostname": "beta-2.example.com",
        "executor_type": "local",
        "labels": {"env": "staging", "region": "us-east-1"},
        "disk_used_percent": 65.2,
        "memory_used_percent": 72.1,
        "load_1m": 1.5,
        "load_5m": 1.2,
        "load_15m": 0.9,
        "workspaces": [
            {"workspace_name": "ws-frontend", "status": "running", "opencode_status": "active"},
            {"workspace_name": "ws-backend", "status": "stopped", "opencode_status": None},
        ],
        "opencode_instances": [
            {"instance_name": "oc-serve-main", "version": "0.5.0", "status": "running"},
            {"instance_name": "oc-serve-canary", "version": "0.6.0-rc1", "status": "stopped"},
        ],
    }

    # ------------------------------------------------------------------
    # 201 success cases
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_minimal_payload_returns_201(self, client, mock_conn):
        """POST /observations with only required fields returns 201."""
        runner_uuid = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "ok"
        assert data["runner_id"] == "runner-alpha-1"

    @pytest.mark.asyncio
    async def test_full_payload_returns_201(self, client, mock_conn):
        """POST /observations with all optional fields returns 201."""
        runner_uuid = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json=self.FULL_PAYLOAD)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "ok"
        assert data["runner_id"] == "runner-beta-2"

    @pytest.mark.asyncio
    async def test_response_structure(self, client, mock_conn):
        """Response contains only status and runner_id fields."""
        runner_uuid = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201
        data = response.json()["data"]
        assert set(data.keys()) == {"status", "runner_id"}

    # ------------------------------------------------------------------
    # Runner upsert behaviour
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_inserts_new_runner(self, client, mock_conn):
        """A new runner_id triggers an INSERT with status HEALTHY."""
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []
        insert_args_captured: list[tuple] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            insert_args_captured.append(args)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201

        # The upsert SQL should be an INSERT INTO runners ... ON CONFLICT
        assert len(insert_sql_captured) == 1
        upsert_sql = insert_sql_captured[0]
        assert "INSERT INTO runners" in upsert_sql
        assert "ON CONFLICT (runner_id)" in upsert_sql
        assert "HEALTHY" in upsert_sql

        # Verify runner_id, hostname, executor_type were passed as args
        args = insert_args_captured[0]
        assert args[0] == "runner-alpha-1"  # runner_id param ($1)
        assert args[1] == "alpha-1.example.com"  # hostname ($2)
        assert args[2] == "awx"  # executor_type ($3)

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_runner(self, client, mock_conn):
        """An existing runner_id triggers an UPDATE via ON CONFLICT DO UPDATE."""
        runner_uuid = uuid.uuid4()

        async def _fetchrow(sql, *args):
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            # First call — creates runner
            response1 = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-update-test",
                    "hostname": "old-host.example.com",
                    "executor_type": "awx",
                },
            )
            assert response1.status_code == 201

            # Reset the call for a clean slate
            mock_conn.fetchrow.reset_mock()
            mock_conn.fetchrow = AsyncMock(
                side_effect=lambda sql, *args: mock_row({"id": runner_uuid})
            )

            # Second call — upserts (updates) existing runner with new hostname
            response2 = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-update-test",
                    "hostname": "new-host.example.com",
                    "executor_type": "awx",
                },
            )
            assert response2.status_code == 201

        # Verify the ON CONFLICT clause was present
        call_sql = mock_conn.fetchrow.call_args[0][0]
        assert "DO UPDATE" in call_sql
        assert "EXCLUDED.hostname" in call_sql

    @pytest.mark.asyncio
    async def test_upsert_sets_status_healthy(self, client, mock_conn):
        """The upsert always sets runner status to HEALTHY."""
        runner_uuid = uuid.uuid4()
        insert_args_captured: list[tuple] = []

        async def _fetchrow(sql, *args):
            insert_args_captured.append(args)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201
        # The status is set to 'HEALTHY' in the SQL, not as a parameter,
        # so we check the SQL string contains it
        call_sql = mock_conn.fetchrow.call_args[0][0]
        assert "HEALTHY" in call_sql

    # ------------------------------------------------------------------
    # Observation row creation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_creates_runner_observation(self, client, mock_conn):
        """A RunnerObservation row is created with disk/memory/load metrics."""
        runner_uuid = uuid.uuid4()
        execute_calls: list[tuple[str, tuple]] = []

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.execute = AsyncMock(side_effect=_execute)

        async with client as c:
            response = await c.post("/observations", json=self.FULL_PAYLOAD)

        assert response.status_code == 201

        # Find the runner_observations INSERT
        obs_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO runner_observations" in sql
        ]
        assert len(obs_inserts) == 1

        _sql, args = obs_inserts[0]
        assert args[1] == runner_uuid  # runner_id FK
        assert args[2] == 65.2  # disk_used_percent
        assert args[3] == 72.1  # memory_used_percent
        assert args[4] == 1.5  # load_1m
        assert args[5] == 1.2  # load_5m
        assert args[6] == 0.9  # load_15m

    @pytest.mark.asyncio
    async def test_creates_workspace_observations(self, client, mock_conn):
        """WorkspaceObservation rows are created for each workspace entry."""
        runner_uuid = uuid.uuid4()
        execute_calls: list[tuple[str, tuple]] = []

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.execute = AsyncMock(side_effect=_execute)

        async with client as c:
            response = await c.post("/observations", json=self.FULL_PAYLOAD)

        assert response.status_code == 201

        ws_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO workspace_observations" in sql
        ]
        assert len(ws_inserts) == 2

        # First workspace: ws-frontend
        sql1, args1 = ws_inserts[0]
        assert args1[1] == runner_uuid
        assert args1[2] == "ws-frontend"
        assert args1[3] == "running"
        assert args1[4] == "active"

        # Second workspace: ws-backend
        sql2, args2 = ws_inserts[1]
        assert args2[1] == runner_uuid
        assert args2[2] == "ws-backend"
        assert args2[3] == "stopped"
        assert args2[4] is None

    @pytest.mark.asyncio
    async def test_workspaces_optional(self, client, mock_conn):
        """When workspaces is not provided, no workspace_observations are inserted."""
        runner_uuid = uuid.uuid4()
        execute_calls: list[tuple[str, tuple]] = []

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.execute = AsyncMock(side_effect=_execute)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201

        ws_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO workspace_observations" in sql
        ]
        assert len(ws_inserts) == 0

    @pytest.mark.asyncio
    async def test_creates_opencode_instance_observations(self, client, mock_conn):
        """OpenCodeInstanceObservation rows are created for each instance entry."""
        runner_uuid = uuid.uuid4()
        execute_calls: list[tuple[str, tuple]] = []

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.execute = AsyncMock(side_effect=_execute)

        async with client as c:
            response = await c.post("/observations", json=self.FULL_PAYLOAD)

        assert response.status_code == 201

        inst_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO opencode_instance_observations" in sql
        ]
        assert len(inst_inserts) == 2

        # First instance: oc-serve-main
        sql1, args1 = inst_inserts[0]
        assert args1[1] == runner_uuid
        assert args1[2] == "oc-serve-main"
        assert args1[3] == "0.5.0"
        assert args1[4] == "running"

        # Second instance: oc-serve-canary
        sql2, args2 = inst_inserts[1]
        assert args2[1] == runner_uuid
        assert args2[2] == "oc-serve-canary"
        assert args2[3] == "0.6.0-rc1"
        assert args2[4] == "stopped"

    @pytest.mark.asyncio
    async def test_opencode_instances_optional(self, client, mock_conn):
        """When opencode_instances is not provided, no instance rows are inserted."""
        runner_uuid = uuid.uuid4()
        execute_calls: list[tuple[str, tuple]] = []

        mock_conn.fetchrow = AsyncMock(return_value=mock_row({"id": runner_uuid}))

        async def _execute(sql, *args):
            execute_calls.append((sql, args))

        mock_conn.execute = AsyncMock(side_effect=_execute)

        async with client as c:
            response = await c.post("/observations", json=self.MINIMAL_PAYLOAD)

        assert response.status_code == 201

        inst_inserts = [
            (sql, args) for sql, args in execute_calls
            if "INSERT INTO opencode_instance_observations" in sql
        ]
        assert len(inst_inserts) == 0

    # ------------------------------------------------------------------
    # 422 validation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_missing_runner_id_returns_422(self, client):
        """Missing required field runner_id returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "hostname": "test.example.com",
                    "executor_type": "awx",
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_hostname_returns_422(self, client):
        """Missing required field hostname returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "executor_type": "awx",
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_executor_type_returns_422(self, client):
        """Missing required field executor_type returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "hostname": "test.example.com",
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_body_returns_422(self, client):
        """Empty request body returns 422."""
        async with client as c:
            response = await c.post("/observations", json={})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_disk_percent_returns_422(self, client):
        """disk_used_percent > 100 returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "hostname": "test.example.com",
                    "executor_type": "awx",
                    "disk_used_percent": 150,
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_disk_percent_returns_422(self, client):
        """disk_used_percent < 0 returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "hostname": "test.example.com",
                    "executor_type": "awx",
                    "disk_used_percent": -10,
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_memory_percent_returns_422(self, client):
        """memory_used_percent > 100 returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "hostname": "test.example.com",
                    "executor_type": "awx",
                    "memory_used_percent": 200,
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_load_returns_422(self, client, mock_conn):
        """load_1m < 0 returns 422."""
        async with client as c:
            response = await c.post(
                "/observations",
                json={
                    "runner_id": "runner-1",
                    "hostname": "test.example.com",
                    "executor_type": "awx",
                    "load_1m": -0.5,
                },
            )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Admin status preservation — observation ingestion must never overwrite
# the operator-set admin_status column.
# --------------------------------------------------------------------------


class TestAdminStatusPreservation:
    """Tests verifying that admin_status is never changed by observation ingestion.

    The ``runners.admin_status`` column is operator-controlled (offline,
    maintenance, online).  Observation ingestion via the upsert SQL must
    never include ``admin_status`` in its ``SET`` clause.  These tests
    verify the guard is in place.
    """

    @pytest.mark.asyncio
    async def test_upsert_sql_does_not_set_admin_status(self, client, mock_conn):
        """The upsert SQL's DO UPDATE SET clause must not assign admin_status."""  # noqa: E501
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json={
                "runner_id": "runner-admin-test",
                "hostname": "admin-test.example.com",
                "executor_type": "awx",
            })

        assert response.status_code == 201
        assert len(insert_sql_captured) == 1

        upsert_sql = insert_sql_captured[0]

        # Check for assigned columns in the SET clause (e.g.
        # "hostname       = EXCLUDED.hostname,") using a regex that
        # matches column names that are being assigned.
        set_clause_start = upsert_sql.index("DO UPDATE SET")
        set_clause = upsert_sql[set_clause_start:]

        # Find all column assignments: word characters followed by = (with optional whitespace)
        import re
        assigned_columns = re.findall(r"(\w+)\s*=", set_clause)

        assert "admin_status" not in assigned_columns, (
            "The DO UPDATE SET clause must not assign admin_status. "
            "Assigned columns found: " + ", ".join(assigned_columns)
        )

        # Verify that health_status IS still updated
        assert "health_status" in assigned_columns, (
            "The DO UPDATE SET clause must still update health_status. "
            "Assigned columns: " + ", ".join(assigned_columns)
        )

        # Verify that the legacy status column IS still updated (not just as part of health_status)
        assert "status" in assigned_columns, (
            "The DO UPDATE SET clause must still update the legacy status column. "
            "Assigned columns: " + ", ".join(assigned_columns)
        )

    @pytest.mark.asyncio
    async def test_upsert_insert_does_not_include_admin_status(self, client, mock_conn):
        """The INSERT INTO runners column list must not include admin_status."""
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json={
                "runner_id": "runner-insert-test",
                "hostname": "insert-test.example.com",
                "executor_type": "awx",
            })

        assert response.status_code == 201
        assert len(insert_sql_captured) == 1

        upsert_sql = insert_sql_captured[0]

        # The INSERT column list is between "INSERT INTO runners (" and ")"
        # followed by "VALUES"
        insert_part = upsert_sql[:upsert_sql.index("VALUES")]

        # admin_status must not appear in the INSERT column list
        assert "admin_status" not in insert_part, (
            "The INSERT column list must not include admin_status. "
            "Found 'admin_status' in: " + insert_part
        )

    @pytest.mark.asyncio
    async def test_upsert_preserves_existing_admin_status_offline(self, client, mock_conn):
        """A runner with admin_status='offline' retains it after observation upsert.

        This test verifies the mechanism at the SQL-level: the upsert's
        SET clause does not assign to admin_status.  In production the
        actual database row is queried, but with mocks we verify the
        SQL does not set admin_status.
        """
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json={
                "runner_id": "runner-offline-test",
                "hostname": "offline-test.example.com",
                "executor_type": "awx",
            })

        assert response.status_code == 201
        assert len(insert_sql_captured) == 1

        upsert_sql = insert_sql_captured[0]

        import re
        set_clause_start = upsert_sql.index("DO UPDATE SET")
        set_clause = upsert_sql[set_clause_start:]
        assigned_columns = re.findall(r"(\w+)\s*=", set_clause)

        assert "admin_status" not in assigned_columns, (
            "admin_status must not be in the SET clause, otherwise "
            "offline runners would lose their operator-set status. "
            "Assigned columns: " + ", ".join(assigned_columns)
        )

    @pytest.mark.asyncio
    async def test_upsert_preserves_existing_admin_status_maintenance(self, client, mock_conn):
        """A runner with admin_status='maintenance' retains it after observation upsert."""
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json={
                "runner_id": "runner-maintenance-test",
                "hostname": "maintenance-test.example.com",
                "executor_type": "awx",
            })

        assert response.status_code == 201
        assert len(insert_sql_captured) == 1

        upsert_sql = insert_sql_captured[0]

        import re
        set_clause_start = upsert_sql.index("DO UPDATE SET")
        set_clause = upsert_sql[set_clause_start:]
        assigned_columns = re.findall(r"(\w+)\s*=", set_clause)

        assert "admin_status" not in assigned_columns, (
            "admin_status must not be in the SET clause, otherwise "
            "maintenance runners would lose their operator-set status. "
            "Assigned columns: " + ", ".join(assigned_columns)
        )

    @pytest.mark.asyncio
    async def test_upsert_still_sets_health_status_and_legacy_status(self, client, mock_conn):
        """Observation upsert still updates health_status and status (legacy column).

        The guard only prevents admin_status from being overwritten.
        The health_status and status columns must still be set to
        'HEALTHY' on each observation.
        """
        runner_uuid = uuid.uuid4()
        insert_sql_captured: list[str] = []

        async def _fetchrow(sql, *args):
            insert_sql_captured.append(sql)
            return mock_row({"id": runner_uuid})

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(return_value=None)

        async with client as c:
            response = await c.post("/observations", json={
                "runner_id": "runner-health-check",
                "hostname": "health-check.example.com",
                "executor_type": "awx",
            })

        assert response.status_code == 201
        assert len(insert_sql_captured) == 1

        upsert_sql = insert_sql_captured[0]
        assert "status" in upsert_sql
        assert "health_status" in upsert_sql
        assert "HEALTHY" in upsert_sql
