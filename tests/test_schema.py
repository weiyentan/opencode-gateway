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
            "created_at",
            "updated_at",
            "completed_at",
        ]
        for col in required_columns:
            assert col in sql, f"Column '{col}' missing from schema.sql"


class TestEnsureSchema:
    """Tests for ensure_schema() which loads and executes schema.sql."""

    @pytest.mark.asyncio
    async def test_ensure_schema_executes_sql_against_pool(self):
        """ensure_schema() should read schema.sql and execute it against the given pool."""
        from unittest.mock import MagicMock, patch

        from app.db.schema import ensure_schema

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        # pool.acquire() is NOT a coroutine — it returns an async context manager.
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        fake_sql = (
            "CREATE TABLE IF NOT EXISTS gateway_jobs (\n"
            "    id UUID PRIMARY KEY\n"
            ");\n"
        )

        with patch("app.db.schema._SCHEMA_SQL", fake_sql):
            await ensure_schema(mock_pool)

        mock_pool.acquire.assert_called_once()
        mock_conn.execute.assert_called_once()
        executed_sql = mock_conn.execute.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS gateway_jobs" in executed_sql


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
