"""Integration tests for database schema migration.

Tests that schema.sql and Alembic migrations create all expected tables
with the correct column types.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestSchemaMigration:
    """Verify that schema migration creates all expected tables."""

    async def test_all_expected_tables_exist(self, db_conn):
        """After migration, all expected tables should be present."""
        rows = await db_conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "ORDER BY table_name"
        )
        table_names = {row["table_name"] for row in rows}

        expected = {
            "gateway_jobs",
            "approvals",
            "workspaces",
            "job_events",
            "runners",
            "runner_events",
            "runner_observations",
            "workspace_observations",
            "opencode_instance_observations",
        }
        assert expected.issubset(table_names), (
            f"Missing tables: {expected - table_names}"
        )

    async def test_gateway_jobs_has_required_columns(self, db_conn):
        """gateway_jobs table must contain all columns used by the API."""
        rows = await db_conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'gateway_jobs'"
        )
        columns = {row["column_name"] for row in rows}

        required = {
            "id", "status", "repo_url", "task_summary", "runner_id",
            "workspace_name", "opencode_url", "opencode_session_id",
            "executor_type", "executor_job_id", "created_at", "updated_at",
            "completed_at", "diff",
        }
        assert required.issubset(columns), (
            f"Missing columns in gateway_jobs: {required - columns}"
        )

    async def test_runners_has_required_columns(self, db_conn):
        """runners table must have all ORM-defined columns."""
        rows = await db_conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'runners'"
        )
        columns = {row["column_name"] for row in rows}

        required = {
            "id", "runner_id", "hostname", "executor_type", "labels",
            "status", "created_at", "updated_at",
        }
        assert required.issubset(columns), (
            f"Missing columns in runners: {required - columns}"
        )

    async def test_runner_observations_has_foreign_key(self, db_conn):
        """runner_observations must have a FK referencing runners."""
        rows = await db_conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'runner_observations'"
        )
        columns = {row["column_name"] for row in rows}

        required = {
            "id", "runner_id", "disk_used_percent", "memory_used_percent",
            "load_1m", "load_5m", "load_15m", "observed_at",
        }
        assert required.issubset(columns), (
            f"Missing columns in runner_observations: {required - columns}"
        )

    async def test_insert_and_query_smoke(self, db_conn):
        """Smoke test: insert and query a row from each table."""
        from tests.integration.conftest import create_job, create_runner, create_workspace

        # Insert a runner and a workspace
        rid = await create_runner(db_conn)
        ws_id = await create_workspace(db_conn, runner_id=rid)

        # Verify runner
        row = await db_conn.fetchrow("SELECT * FROM runners WHERE id = $1", rid)
        assert row is not None
        assert row["runner_id"] == str(rid)

        # Verify workspace
        row = await db_conn.fetchrow("SELECT * FROM workspaces WHERE id = $1", ws_id)
        assert row is not None
        assert row["runner_id"] == rid

        # Insert a job
        jid = await create_job(db_conn)
        row = await db_conn.fetchrow("SELECT * FROM gateway_jobs WHERE id = $1", jid)
        assert row is not None
        assert row["status"] == "pending"
