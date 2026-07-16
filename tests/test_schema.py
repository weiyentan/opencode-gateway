"""Tests for database schema management — ensure_schema()."""

from unittest.mock import AsyncMock, patch

import pytest


class TestEnsureSchema:
    """Tests for ensure_schema() which runs Alembic migrations and checks required tables."""

    @pytest.mark.asyncio
    async def test_ensure_schema_runs_migrations_and_checks_tables(self):
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
