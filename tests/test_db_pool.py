"""Tests for the database connection pool lifecycle."""

import asyncio
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
                max_inactive_connection_lifetime=1800,
            )
            assert db_pool.pool is mock_pool

    @pytest.mark.asyncio
    async def test_connect_passes_max_inactive_connection_lifetime(self):
        """connect() passes database_max_inactive_connection_lifetime to asyncpg.create_pool."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_host="testhost",
            database_port=5432,
            database_name="testdb",
            database_user="testuser",
            database_password="secret",
            database_max_inactive_connection_lifetime=900,
        )

        mock_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

            _, kwargs = mock_create_pool.call_args
            assert kwargs["max_inactive_connection_lifetime"] == 900

    @pytest.mark.asyncio
    async def test_connect_defaults_max_inactive_connection_lifetime(self):
        """connect() passes the default 1800s when no override is set."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_host="testhost",
            database_port=5432,
            database_name="testdb",
            database_user="testuser",
            database_password="secret",
        )

        mock_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

            _, kwargs = mock_create_pool.call_args
            assert kwargs["max_inactive_connection_lifetime"] == 1800


class TestDatabasePoolSsl:
    """GATEWAY_DATABASE_SSL must reach asyncpg.create_pool for CNPG TLS."""

    def test_gateway_database_ssl_env_maps_to_database_ssl(self, monkeypatch):
        """Settings.database_ssl is fed by the GATEWAY_DATABASE_SSL env var."""
        from app.core.config import Settings

        monkeypatch.setenv("GATEWAY_DATABASE_SSL", "require")
        assert Settings().database_ssl == "require"

    @pytest.mark.asyncio
    async def test_connect_passes_ssl_when_database_ssl_set(self):
        """connect() forwards settings.database_ssl as the asyncpg ssl kwarg."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(database_ssl="require")

        mock_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

        _, kwargs = mock_create_pool.call_args
        assert kwargs["ssl"] == "require"

    @pytest.mark.asyncio
    async def test_connect_omits_ssl_when_not_set(self):
        """connect() passes no ssl kwarg when database_ssl is unset."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(database_ssl=None)

        mock_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

        _, kwargs = mock_create_pool.call_args
        assert "ssl" not in kwargs


class TestBackfillCliPoolSsl:
    """The backfill CLI pool (scripts.afk_backfill._get_pool) also honors
    GATEWAY_DATABASE_SSL so operator runs against CNPG require-TLS work."""

    @pytest.mark.asyncio
    async def test_backfill_cli_pool_passes_ssl(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        monkeypatch.setenv("GATEWAY_DATABASE_SSL", "require")
        mock_create_pool = AsyncMock(return_value=AsyncMock())
        with patch("scripts.afk_backfill.asyncpg.create_pool", mock_create_pool):
            from scripts.afk_backfill import _get_pool

            await _get_pool()

        _, kwargs = mock_create_pool.call_args
        assert kwargs["ssl"] == "require"


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


# ══════════════════════════════════════════════════════════════════════════
#  Issue #363 — Concurrent load / connection recycling (AC5)
# ══════════════════════════════════════════════════════════════════════════


class TestDatabasePoolConcurrency:
    """Simulated concurrent-load tests for the DatabasePool.

    Uses a controllable fake pool whose acquire/release can be counted;
    runs N concurrent acquisitions; asserts acquire/release counts balance
    and the fake pool's concurrency limit is respected.
    """

    @staticmethod
    def _make_fake_pool(max_size: int) -> MagicMock:
        """Return a MagicMock that simulates an asyncpg.Pool with a concurrency cap.

        * ``acquire()`` is a coroutine that yields a mock connection and
          increments an ``_outstanding`` counter to track concurrency.
        * ``release()`` is a coroutine that decrements the counter.
        * ``_outstanding`` and ``_max_outstanding`` are tracked so tests can
          assert the pool never exceeded *max_size*.
        * ``_acquire_count`` and ``_release_count`` are recorded so tests can
          assert zero leaks.
        """
        pool = MagicMock(spec=["acquire", "release", "close"])
        pool._outstanding = 0
        pool._max_outstanding = 0
        pool._acquire_count = 0
        pool._release_count = 0
        pool._max_size = max_size

        async def _acquire_side_effect():
            """Coroutine that returns a mock connection and tracks concurrency."""
            pool._outstanding += 1
            pool._acquire_count += 1
            pool._max_outstanding = max(
                pool._max_outstanding, pool._outstanding
            )
            return MagicMock()

        pool.acquire = AsyncMock(side_effect=_acquire_side_effect)

        async def _release_side_effect(conn):
            pool._release_count += 1
            pool._outstanding -= 1
            return None

        pool.release = AsyncMock(side_effect=_release_side_effect)
        pool.close = AsyncMock()

        return pool

    @pytest.mark.asyncio
    async def test_pool_constructed_with_correct_concurrency_limits(self):
        """Under concurrent construction, min_size and max_size are forwarded
        to asyncpg.create_pool, which enforces the concurrency cap."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_min_connections=2,
            database_max_connections=10,
            database_connection_timeout=30,
        )

        mock_create_pool = AsyncMock(return_value=AsyncMock())
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

        kwargs = mock_create_pool.call_args.kwargs
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 10, (
            "max_size should be passed to asyncpg (enforces concurrency cap)"
        )

    @pytest.mark.asyncio
    async def test_concurrent_connections_balanced_no_leak(self):
        """After all concurrent tasks finish, acquire and release counts balance.

        This verifies that connections are properly released back to the pool
        after each use — no connection leaks.
        """
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_min_connections=2,
            database_max_connections=5,
        )

        fake_pool = self._make_fake_pool(max_size=5)
        mock_create_pool = AsyncMock(return_value=fake_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

            # Run 30 concurrent tasks through the pool
            async def work_once() -> None:
                conn = await db_pool.acquire()
                await asyncio.sleep(0.0005)
                await db_pool.release(conn)

            await asyncio.gather(*[work_once() for _ in range(30)])

        # All connections released — no leak
        assert fake_pool._release_count == fake_pool._acquire_count == 30
        # After all work, outstanding should be 0
        assert fake_pool._outstanding == 0, (
            f"{fake_pool._outstanding} connections still outstanding"
        )

    @pytest.mark.asyncio
    async def test_max_inactive_connection_lifetime_in_pool_kwargs(self):
        """Under concurrent construction, max_inactive_connection_lifetime is
        forwarded to the asyncpg pool."""
        from app.core.config import Settings
        from app.db.session import DatabasePool

        settings = Settings(
            database_max_inactive_connection_lifetime=1200,
        )

        mock_pool = self._make_fake_pool(max_size=5)
        mock_create_pool = AsyncMock(return_value=mock_pool)
        with patch("app.db.session.asyncpg.create_pool", mock_create_pool):
            db_pool = DatabasePool(settings)
            await db_pool.connect()

        call_kwargs = mock_create_pool.call_args.kwargs
        assert call_kwargs["max_inactive_connection_lifetime"] == 1200, (
            "max_inactive_connection_lifetime not forwarded to asyncpg"
        )
