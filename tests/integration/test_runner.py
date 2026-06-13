"""Integration tests for Runner queries against a real Postgres database.

Tests runner listing, detail queries, and observation relationships.
"""

from __future__ import annotations

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9
import uuid
from datetime import datetime, timezone

import pytest

from tests.integration.conftest import create_runner, create_workspace

pytestmark = pytest.mark.integration


class TestRunnerQueries:
    """Listing and querying runners."""

    async def test_list_runners_returns_empty_when_none_exist(self, db_conn):
        """When no runners are seeded, the list query returns no rows."""
        rows = await db_conn.fetch("SELECT * FROM runners")
        assert len(rows) == 0

    async def test_list_runners_returns_all_created(self, db_conn):
        """After creating runners, list returns all of them."""
        _ = await create_runner(db_conn, hostname="alpha.example.com")
        _ = await create_runner(db_conn, hostname="beta.example.com")
        _ = await create_runner(db_conn, hostname="gamma.example.com")

        rows = await db_conn.fetch(
            "SELECT id, runner_id, hostname, status FROM runners "
            "ORDER BY hostname"
        )
        assert len(rows) == 3
        hostnames = [row["hostname"] for row in rows]
        assert hostnames == [
            "alpha.example.com",
            "beta.example.com",
            "gamma.example.com",
        ]

    async def test_list_runners_includes_latest_observation(self, db_conn):
        """The lateral-join query returns the latest observation per runner."""
        rid = await create_runner(db_conn)

        now = datetime.now(timezone.utc)

        # Insert two observations — the query should pick the latest
        await db_conn.execute(
            "INSERT INTO runner_observations "
            "(id, runner_id, disk_used_percent, memory_used_percent, load_1m, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, 30.0, 50.0, 1.0, now,
        )
        await db_conn.execute(
            "INSERT INTO runner_observations "
            "(id, runner_id, disk_used_percent, memory_used_percent, load_1m, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, 55.0, 72.0, 2.5, now,
        )

        # The lateral-join query from runners API
        rows = await db_conn.fetch(
            "SELECT r.id, r.hostname, "
            "lo.disk_used_percent, lo.memory_used_percent, lo.load_1m, lo.observed_at "
            "FROM runners r "
            "LEFT JOIN LATERAL ( "
            "  SELECT disk_used_percent, memory_used_percent, load_1m, observed_at "
            "  FROM runner_observations "
            "  WHERE runner_id = r.id "
            "  ORDER BY observed_at DESC "
            "  LIMIT 1 "
            ") lo ON true "
            "ORDER BY r.created_at DESC"
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["disk_used_percent"] == 55.0
        assert row["memory_used_percent"] == 72.0
        assert row["load_1m"] == 2.5


class TestRunnerDetail:
    """Single runner detail queries."""

    async def test_fetch_runner_by_id(self, db_conn):
        """Fetching a runner by UUID returns the full record."""
        rid = await create_runner(
            db_conn,
            hostname="detail.example.com",
            status="HEALTHY",
            executor_type="awx",
        )

        row = await db_conn.fetchrow(
            "SELECT id, runner_id, hostname, status, executor_type, labels, "
            "created_at, updated_at FROM runners WHERE id = $1",
            rid,
        )
        assert row is not None
        assert row["runner_id"] == str(rid)
        assert row["hostname"] == "detail.example.com"
        assert row["status"] == "HEALTHY"
        assert row["executor_type"] == "awx"

    async def test_fetch_nonexistent_runner_returns_none(self, db_conn):
        """Querying a non-existent runner returns None."""
        row = await db_conn.fetchrow(
            "SELECT id FROM runners WHERE id = $1", uuid.uuid4()
        )
        assert row is None

    async def test_runner_with_observations_includes_workspace_observations(
        self, db_conn
    ):
        """A runner detail query returns its workspace observations."""
        rid = await create_runner(db_conn)

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "INSERT INTO workspace_observations "
            "(id, runner_id, workspace_name, status, opencode_status, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, "ws-alpha", "active", "running", now,
        )
        await db_conn.execute(
            "INSERT INTO workspace_observations "
            "(id, runner_id, workspace_name, status, opencode_status, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, "ws-beta", "idle", "stopped", now,
        )

        rows = await db_conn.fetch(
            "SELECT workspace_name, status, opencode_status "
            "FROM workspace_observations "
            "WHERE runner_id = $1 ORDER BY workspace_name",
            rid,
        )
        assert len(rows) == 2
        assert rows[0]["workspace_name"] == "ws-alpha"
        assert rows[0]["status"] == "active"
        assert rows[1]["workspace_name"] == "ws-beta"
        assert rows[1]["opencode_status"] == "stopped"

    async def test_runner_with_opencode_instance_observations(self, db_conn):
        """A runner detail query returns its OpenCode instance observations."""
        rid = await create_runner(db_conn)

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "INSERT INTO opencode_instance_observations "
            "(id, runner_id, instance_name, version, status, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, "oc-main", "0.2.0", "running", now,
        )

        rows = await db_conn.fetch(
            "SELECT instance_name, version, status "
            "FROM opencode_instance_observations WHERE runner_id = $1",
            rid,
        )
        assert len(rows) == 1
        assert rows[0]["instance_name"] == "oc-main"
        assert rows[0]["version"] == "0.2.0"
        assert rows[0]["status"] == "running"


class TestRunnerWorkspaceRelationship:
    """Runner-workspace FK relationship tests."""

    async def test_workspace_can_reference_runner(self, db_conn):
        """A workspace can reference a runner via runner_id FK."""
        rid = await create_runner(db_conn)
        ws_id = await create_workspace(db_conn, runner_id=rid)

        # Verify the workspace's runner_id matches
        ws_row = await db_conn.fetchrow(
            "SELECT runner_id FROM workspaces WHERE id = $1", ws_id
        )
        assert ws_row["runner_id"] == rid

    async def test_runner_observations_cascade_on_runner_delete(self, db_conn):
        """Deleting a runner cascades to its observations."""
        rid = await create_runner(db_conn)

        # Add an observation
        await db_conn.execute(
            "INSERT INTO runner_observations "
            "(id, runner_id, disk_used_percent, memory_used_percent, load_1m, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid, 42.0, 60.0, 1.2,
            datetime.now(timezone.utc),
        )

        # Delete the runner
        await db_conn.execute("DELETE FROM runners WHERE id = $1", rid)

        # Observations should be gone (CASCADE)
        obs_count = await db_conn.fetchval(
            "SELECT count(*) FROM runner_observations WHERE runner_id = $1", rid
        )
        assert obs_count == 0

    async def test_multiple_runners_with_observations(self, db_conn):
        """Each runner can have its own set of observations."""
        rid_a = await create_runner(db_conn, hostname="a.example.com")
        rid_b = await create_runner(db_conn, hostname="b.example.com")

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "INSERT INTO runner_observations "
            "(id, runner_id, disk_used_percent, memory_used_percent, load_1m, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid_a, 10.0, 20.0, 0.5, now,
        )
        await db_conn.execute(
            "INSERT INTO runner_observations "
            "(id, runner_id, disk_used_percent, memory_used_percent, load_1m, observed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uuid.uuid4(), rid_b, 90.0, 95.0, 5.0, now,
        )

        # Each runner should have exactly 1 observation
        count_a = await db_conn.fetchval(
            "SELECT count(*) FROM runner_observations WHERE runner_id = $1", rid_a
        )
        count_b = await db_conn.fetchval(
            "SELECT count(*) FROM runner_observations WHERE runner_id = $1", rid_b
        )
        assert count_a == 1
        assert count_b == 1


class TestRunnerStatusEnum:
    """Status field behaviour."""

    async def test_runner_default_status_is_unknown(self, db_conn):
        """When no status is provided, the default is determined by the schema."""
        rid = uuid.uuid4()
        # Use the same DEFAULT as the migration (UNKNOWN)
        await db_conn.execute(
            "INSERT INTO runners (id, runner_id, hostname, executor_type, "
            "created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $5)",
            rid,
            str(rid),
            "default-status.example.com",
            "local",
            datetime.now(timezone.utc),
        )

        row = await db_conn.fetchrow(
            "SELECT status FROM runners WHERE id = $1", rid
        )
        assert row is not None
        # The column has a server_default of 'UNKNOWN'
        assert row["status"] == "UNKNOWN"

    async def test_runner_can_be_healthy(self, db_conn):
        """A runner explicitly set to HEALTHY is stored correctly."""
        rid = await create_runner(db_conn, status="HEALTHY")

        row = await db_conn.fetchrow(
            "SELECT status FROM runners WHERE id = $1", rid
        )
        assert row["status"] == "HEALTHY"

    async def test_runner_can_be_blocked_disk_pressure(self, db_conn):
        """A runner can be marked BLOCKED_DISK_PRESSURE."""
        rid = await create_runner(db_conn, status="BLOCKED_DISK_PRESSURE")

        row = await db_conn.fetchrow(
            "SELECT status FROM runners WHERE id = $1", rid
        )
        assert row["status"] == "BLOCKED_DISK_PRESSURE"

    async def test_runner_can_be_blocked_memory_pressure(self, db_conn):
        """A runner can be marked BLOCKED_MEMORY_PRESSURE."""
        rid = await create_runner(db_conn, status="BLOCKED_MEMORY_PRESSURE")

        row = await db_conn.fetchrow(
            "SELECT status FROM runners WHERE id = $1", rid
        )
        assert row["status"] == "BLOCKED_MEMORY_PRESSURE"
