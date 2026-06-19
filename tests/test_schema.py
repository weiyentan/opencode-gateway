"""Tests for database schema management — schema.sql and ensure_schema()."""

from unittest.mock import AsyncMock, patch

import pytest


class TestSchemaSqlFile:
    """Tests that schema.sql exists and contains the expected DDL."""

    def test_schema_sql_file_exists(self):
        """schema.sql should be present in the app/db/ directory."""
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "app" / "db" / "schema.sql"
        assert schema_path.is_file(), f"schema.sql not found at {schema_path}"

    def test_schema_sql_contains_gateway_jobs_table(self):
        """schema.sql must contain CREATE TABLE IF NOT EXISTS gateway_jobs."""
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "app" / "db" / "schema.sql"
        sql = schema_path.read_text()

        assert "CREATE TABLE IF NOT EXISTS gateway_jobs" in sql
        # Verify all required columns exist in the DDL
        required_columns = [
            "id",
            "status",
            "repo_url",
            "task_summary",
            "runner_id",
            "workspace_name",
            "opencode_url",
            "opencode_session_id",
            "executor_type",
            "executor_job_id",
            "branch_name",
            "commit_sha",
            "mr_url",
            "workflow_run_id",
            "failure_reason",
            "created_at",
            "updated_at",
            "completed_at",
            "diff",
        ]
        for col in required_columns:
            assert col in sql, f"Column '{col}' missing from schema.sql"


class TestEnsureSchema:
    """Tests for ensure_schema() which loads and executes schema.sql."""

    @pytest.mark.asyncio
    async def test_ensure_schema_executes_sql_against_pool(self):
        """ensure_schema() should run Alembic migrations and check required tables."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.db.schema import ensure_schema

        mock_pool = MagicMock(spec=AsyncMock)

        with patch("app.db.schema.run_migrations") as mock_run_migrations, patch(
            "app.db.schema.check_required_tables"
        ) as mock_check:
            await ensure_schema(mock_pool)

        mock_run_migrations.assert_awaited_once()
        mock_check.assert_awaited_once_with(mock_pool)


class TestSchemaLifespanIntegration:
    """Tests that the schema is sourced during the app lifespan (startup)."""

    @pytest.mark.asyncio
    async def test_schema_sourced_on_startup(self):
        """The factory lifespan should call ensure_schema() after pool connection."""
        from unittest.mock import MagicMock

        from app.core.factory import create_app

        mock_asyncpg_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_asyncpg_pool)

        with patch(
            "app.db.session.asyncpg.create_pool", mock_create_pool
        ), patch("app.core.factory.ensure_schema") as mock_ensure:
            app = create_app()
            async with app.router.lifespan_context(app):
                pass

        # ensure_schema should have been called with the underlying asyncpg pool
        mock_ensure.assert_called_once()
        called_pool = mock_ensure.call_args[0][0]
        assert called_pool is mock_asyncpg_pool
