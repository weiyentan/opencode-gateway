"""Tests for database schema management — Alembic migrations and table checks.

With Alembic as the production schema source of truth, these tests verify
that:
- ``ensure_schema()`` delegates to Alembic migrations (not schema.sql)
- ``check_required_tables()`` detects missing tables
- The factory lifespan still integrates correctly
- ``schema.sql`` exists as a static reference but is not the active path
"""

from unittest.mock import MagicMock

import pytest


class TestSchemaSqlFile:
    """Tests that schema.sql exists as a static reference (not active path)."""

    def test_schema_sql_file_exists(self):
        """schema.sql should be present as a static reference."""
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "app" / "db" / "schema.sql"
        assert schema_path.is_file(), f"schema.sql not found at {schema_path}"

    def test_schema_sql_marked_as_reference(self):
        """schema.sql must indicate it is a test-only reference, not production."""
        from pathlib import Path

        schema_path = Path(__file__).parent.parent / "app" / "db" / "schema.sql"
        sql = schema_path.read_text()
        assert "NOT used in production" in sql, (
            "schema.sql should be marked as test-only / not used in production"
        )


class TestEnsureSchema:
    """Tests for ensure_schema() — now delegates to Alembic migrations."""

    @pytest.mark.asyncio
    async def test_ensure_schema_runs_migrations_and_checks_tables(self):
        """ensure_schema() should run Alembic migrations and check required tables."""
        from unittest.mock import patch

        from app.db.schema import ensure_schema

        mock_pool = MagicMock()

        with patch("app.db.schema.run_migrations") as mock_migrate, \
             patch("app.db.schema.check_required_tables") as mock_check:
            await ensure_schema(mock_pool)

        mock_migrate.assert_called_once()
        mock_check.assert_called_once_with(mock_pool)


class TestCheckRequiredTables:
    """Tests for check_required_tables() — validation of table presence."""

    @pytest.mark.asyncio
    async def test_all_tables_present_passes(self):
        """When all required tables exist, no error is raised."""
        from unittest.mock import AsyncMock, MagicMock

        from app.db.setup import check_required_tables

        mock_conn = AsyncMock()
        # Return all required tables as present
        mock_conn.fetch = AsyncMock(return_value=[
            {"table_name": t} for t in [
                "gateway_jobs", "workspaces", "job_events", "approvals",
                "runners", "runner_events", "runner_observations",
                "workspace_observations", "opencode_instance_observations",
            ]
        ])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        # Should not raise
        await check_required_tables(mock_pool)

    @pytest.mark.asyncio
    async def test_missing_tables_raises_runtimeerror(self):
        """When required tables are missing, RuntimeError is raised."""
        from unittest.mock import AsyncMock, MagicMock

        from app.db.setup import check_required_tables

        mock_conn = AsyncMock()
        # Only return some tables — gateway_jobs and workspaces are missing
        mock_conn.fetch = AsyncMock(return_value=[
            {"table_name": "runners"},
            {"table_name": "runner_observations"},
        ])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        with pytest.raises(RuntimeError, match="Required database tables are missing"):
            await check_required_tables(mock_pool)

    @pytest.mark.asyncio
    async def test_missing_tables_message_names_tables(self):
        """Error message must name the missing tables and suggest a fix."""
        from unittest.mock import AsyncMock, MagicMock

        from app.db.setup import check_required_tables

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        with pytest.raises(RuntimeError) as exc_info:
            await check_required_tables(mock_pool)

        msg = str(exc_info.value)
        assert "gateway_jobs" in msg
        assert "workspaces" in msg
        assert "alembic upgrade head" in msg


class TestSchemaLifespanIntegration:
    """Tests that the schema is sourced during the app lifespan (startup)."""

    @pytest.mark.asyncio
    async def test_ensure_schema_called_on_startup(self):
        """The factory lifespan should call ensure_schema() after pool connection."""
        from unittest.mock import AsyncMock, patch

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
