"""Tests for the database connection pool lifecycle."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDatabasePoolConnect:
    """Tests for DatabasePool.connect() initialization."""

    @pytest.mark.asyncio
    async def test_connect_calls_asyncpg_create_pool_with_settings(self):
        """connect() should call asyncpg.create_pool with correct parameters from settings."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_host="testhost",
            database_port=5432,
            database_name="testdb",
            database_user="testuser",
            database_password="secret",
            database_min_connections=5,
            database_max_connections=20,
            database_connection_timeout=45,
        )

        mock_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

            mock_create_pool.assert_called_once_with(
                host="testhost",
                port=5432,
                database="testdb",
                user="testuser",
                password="secret",
                min_size=5,
                max_size=20,
                timeout=45,
            )
            assert db_pool.pool is mock_pool


class TestDatabasePoolEdgeCases:
    """Edge-case tests for DatabasePool methods."""

    @pytest.mark.asyncio
    async def test_acquire_raises_when_pool_not_initialized(self):
        """acquire() should raise RuntimeError if pool was never connected."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        db_pool = DatabasePool(Settings())
        with pytest.raises(RuntimeError, match="not initialized"):
            await db_pool.acquire()

    @pytest.mark.asyncio
    async def test_close_sets_pool_to_none(self):
        """close() should set pool to None after closing."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        mock_asyncpg_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_asyncpg_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(Settings())
            await db_pool.connect()
            assert db_pool.pool is not None

            await db_pool.close()
            assert db_pool.pool is None
            mock_asyncpg_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        """close() should be safe to call multiple times."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        mock_asyncpg_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_asyncpg_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(Settings())
            await db_pool.connect()
            await db_pool.close()
            await db_pool.close()  # should not raise


class TestGetSessionDependency:
    """Tests for the get_session FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_get_session_yields_connection_and_releases(self):
        """get_session() should acquire a connection, yield it, then release it."""
        from unittest.mock import MagicMock

        from app.db.session import get_session

        mock_conn = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)

        request = MagicMock()
        request.app.state.pool = mock_pool

        gen = get_session(request)
        conn = await gen.__anext__()

        assert conn is mock_conn
        mock_pool.acquire.assert_called_once()
        mock_pool.release.assert_not_called()

        await gen.aclose()
        mock_pool.release.assert_called_once_with(mock_conn)

    @pytest.mark.asyncio
    async def test_get_session_raises_when_pool_is_none(self):
        """get_session() should raise when app.state.pool is None."""
        from unittest.mock import MagicMock

        from app.db.session import get_session

        request = MagicMock()
        request.app.state.pool = None

        with pytest.raises(AttributeError):
            await get_session(request).__anext__()


class TestLifespanIntegration:
    """Tests for the pool lifecycle wired into the FastAPI lifespan."""

    @staticmethod
    def _make_acquirable_pool() -> AsyncMock:
        """Return a mock asyncpg.Pool whose acquire() supports async with."""
        mock_conn = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_pool = AsyncMock()
        # acquire() is NOT a coroutine — it returns an async context manager
        mock_pool.acquire = MagicMock(return_value=mock_ctx)
        return mock_pool

    @pytest.mark.asyncio
    async def test_pool_set_on_app_state_during_startup(self):
        """After lifespan startup, app.state.pool should be a connected DatabasePool."""
        from app.core.factory import create_app

        mock_pool = self._make_acquirable_pool()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            app = create_app()
            async with app.router.lifespan_context(app):
                assert app.state.pool is not None
                assert app.state.pool.pool is mock_pool

    @pytest.mark.asyncio
    async def test_pool_closed_during_shutdown(self):
        """After lifespan shutdown, the pool should be closed."""
        from app.core.factory import create_app

        mock_pool = self._make_acquirable_pool()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            app = create_app()
            async with app.router.lifespan_context(app):
                mock_pool.close.assert_not_called()
            # After exiting the context manager, shutdown should have run
            mock_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_graceful_degradation_when_postgres_unavailable(self):
        """If Postgres is unavailable, the app should still start with pool=None."""
        from app.core.factory import create_app

        mock_create_pool = AsyncMock(side_effect=OSError("Connection refused"))
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            app = create_app()
            async with app.router.lifespan_context(app):
                assert app.state.pool is None
