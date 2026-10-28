"""Tests for database schema management — ensure_schema() and pool readiness.

Covers the delegation contract (Alembic migrations run first, then the
required-relation check), failure propagation, and the pool-supervisor
guarantees from issues #825 and #832: a candidate pool whose active
usage/ingestion or AFK reporting schema is incomplete is never published, and
the missing relation is reported while the bounded retry loop keeps running.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.setup import _REQUIRED_TABLES

# Rebuildable projections, dashboard rollups, repair-only audit tables and
# repository seams with no active service caller (issue #832).
_OPTIONAL_OR_DERIVED_TABLES = frozenset(
    {
        "resource_session_associations",
        "closure_links",
        "closure_episodes",
        "closure_unresolved",
        "afk_dashboard_daily",
        "usage_dashboard_daily",
        "reporting_resource_aggregates",
        "execution_outcome_corrections",
    }
)


def _settings(**overrides):
    """Return deterministic reconnect settings (zero jitter, tiny backoff)."""
    from app.core.config import Settings

    defaults = dict(
        env="development",
        database_host="dbhost",
        database_name="gateway",
        database_user="user",
        database_password="test-secret",
        reconnect_timeout_seconds=0.5,
        reconnect_initial_backoff_seconds=0.01,
        reconnect_max_backoff_seconds=0.02,
        reconnect_jitter_ratio=0.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_acquirable_pool(tables: set[str]) -> MagicMock:
    """Return a mock asyncpg.Pool reporting *tables* from information_schema.

    ``acquire()`` is both awaitable and an async context manager, mirroring
    asyncpg's ``PoolAcquireContext``: the supervisor's connection test awaits
    it directly while ``check_required_tables`` uses ``async with``.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(
        return_value=[{"table_name": name} for name in sorted(tables)]
    )

    class _AcquireResult:
        def __await__(self):
            async def _get() -> AsyncMock:
                return conn

            return _get().__await__()

        async def __aenter__(self) -> AsyncMock:
            return conn

        async def __aexit__(self, *exc_info) -> None:
            return None

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_AcquireResult())
    pool.release = AsyncMock()
    pool.close = AsyncMock()
    return pool


class TestEnsureSchema:
    """Tests for ensure_schema() which runs Alembic migrations and checks required tables."""

    @pytest.mark.asyncio
    async def test_ensure_schema_runs_migrations_and_checks_tables(self):
        """ensure_schema() should run Alembic migrations and check required tables."""
        from app.db.schema import ensure_schema

        mock_pool = MagicMock(spec=AsyncMock)

        with patch("app.db.schema.run_migrations") as mock_run_migrations, patch(
            "app.db.schema.check_required_tables"
        ) as mock_check:
            await ensure_schema(mock_pool)

        mock_run_migrations.assert_awaited_once()
        mock_check.assert_awaited_once_with(mock_pool)

    @pytest.mark.asyncio
    async def test_ensure_schema_fails_closed_when_a_required_relation_is_missing(self):
        """A missing required relation propagates after migrations have run.

        ``ensure_schema`` must never swallow the check failure — the
        supervisor relies on the exception to withhold the candidate pool.
        """
        from app.db.schema import ensure_schema

        calls: list[str] = []
        mock_pool = MagicMock(spec=AsyncMock)

        async def _run_migrations() -> None:
            calls.append("migrations")

        async def _check_required_tables(pool) -> None:
            calls.append("required_tables")
            raise RuntimeError(
                "Required database tables are missing: usage_events. "
                "Run 'alembic upgrade head' to create them."
            )

        with patch("app.db.schema.run_migrations", _run_migrations), patch(
            "app.db.schema.check_required_tables", _check_required_tables
        ):
            with pytest.raises(RuntimeError, match="usage_events"):
                await ensure_schema(mock_pool)

        assert calls == ["migrations", "required_tables"], (
            "migrations must run before the required-relation check"
        )


class TestSchemaLifespanIntegration:
    """Tests that the schema is sourced during the app lifespan (startup)."""

    @pytest.mark.asyncio
    async def test_schema_sourced_on_startup(self):
        """The factory lifespan's pool supervisor (issue #773) should call
        ensure_schema() before publishing the pool."""
        from app.core.factory import create_app

        mock_asyncpg_pool = AsyncMock()
        mock_create_pool = AsyncMock(return_value=mock_asyncpg_pool)

        with patch(
            "app.db.session.asyncpg.create_pool", mock_create_pool
        ), patch(
            "app.db.session.ensure_schema", new=AsyncMock()
        ) as mock_ensure:
            app = create_app()
            async with app.router.lifespan_context(app):
                pass

        # ensure_schema should have been called with the underlying asyncpg pool
        mock_ensure.assert_called_once()
        called_pool = mock_ensure.call_args[0][0]
        assert called_pool is mock_asyncpg_pool


class TestIncompleteSchemaNeverPublishesPool:
    """Issue #825 — missing usage relations block candidate publication."""

    @pytest.mark.asyncio
    async def test_identity_only_schema_blocks_publication_and_names_missing_relation(
        self, caplog
    ):
        """Identity tables present but usage relations absent → no pool published."""
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        inner = _make_acquirable_pool({"opencode_clients", "collector_credentials"})
        published: list = []

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(return_value=inner)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ), caplog.at_level(logging.WARNING, logger="app.db.session"):
            supervisor = DatabasePoolSupervisor(_settings(), publish=published.append)
            await supervisor.start()

            assert supervisor.pool is None, (
                "an incomplete-schema candidate must never be published"
            )
            assert published == [None], "only the not-ready sentinel is published"
            assert inner.close.await_count >= 1, "failed candidate must be closed"
            await supervisor.stop()

        assert "usage_events" in caplog.text
        assert "sessions" in caplog.text

    @pytest.mark.asyncio
    async def test_publishes_only_after_complete_required_set_is_present(self):
        """The bounded retry loop publishes once the complete set exists."""
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        incomplete = _make_acquirable_pool(
            {"opencode_clients", "collector_credentials"}
        )
        complete = _make_acquirable_pool(set(_REQUIRED_TABLES))
        candidates = [incomplete, complete]
        published: list = []

        async def _create_pool(**kwargs):
            return candidates.pop(0)

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(side_effect=_create_pool)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ):
            supervisor = DatabasePoolSupervisor(_settings(), publish=published.append)
            await supervisor.start()
            assert supervisor.pool is None
            assert published == [None]

            deadline = asyncio.get_event_loop().time() + 2.0
            while len(published) < 2 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.005)

            assert len(published) == 2, "retry must publish the complete schema"
            assert published[-1].pool is complete
            await supervisor.stop()


class TestAfkOutcomeSchemaNeverPublishesPool:
    """Issue #832 — missing AFK reporting relations block candidate publication."""

    @pytest.mark.asyncio
    async def test_missing_afk_event_relation_blocks_publication_and_names_relation(
        self, caplog
    ):
        """Complete usage schema but missing ``engineering_events`` → no pool."""
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        existing = set(_REQUIRED_TABLES) - {"engineering_events"}
        inner = _make_acquirable_pool(existing)
        published: list = []

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(return_value=inner)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ), caplog.at_level(logging.WARNING, logger="app.db.session"):
            supervisor = DatabasePoolSupervisor(_settings(), publish=published.append)
            await supervisor.start()

            assert supervisor.pool is None, (
                "an AFK-incomplete candidate must never be published"
            )
            assert published == [None], "only the not-ready sentinel is published"
            assert inner.close.await_count >= 1, "failed candidate must be closed"
            await supervisor.stop()

        assert "engineering_events" in caplog.text
        # The complete usage/ingestion layer must not be blamed.
        assert "usage_events" not in caplog.text

    @pytest.mark.asyncio
    async def test_missing_afk_reporting_relation_blocks_publication(
        self, caplog
    ):
        """Complete usage schema but missing ``delivery_state_trails`` → no pool."""
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        existing = set(_REQUIRED_TABLES) - {"delivery_state_trails"}
        inner = _make_acquirable_pool(existing)
        published: list = []

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(return_value=inner)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ), caplog.at_level(logging.WARNING, logger="app.db.session"):
            supervisor = DatabasePoolSupervisor(_settings(), publish=published.append)
            await supervisor.start()

            assert supervisor.pool is None
            assert published == [None]
            assert inner.close.await_count >= 1
            await supervisor.stop()

        assert "delivery_state_trails" in caplog.text

    @pytest.mark.asyncio
    async def test_complete_required_schema_publishes_with_optional_features_off(
        self,
    ):
        """Required relations are enough with optional features disabled.

        Derived/rebuildable relations are deliberately absent and the AFK
        outcome consumer is disabled — startup must still publish the pool.
        """
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        existing = set(_REQUIRED_TABLES) - _OPTIONAL_OR_DERIVED_TABLES
        inner = _make_acquirable_pool(existing)
        published: list = []
        settings = _settings(afk_outcomes_consumer_enabled=False)

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(return_value=inner)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ):
            supervisor = DatabasePoolSupervisor(settings, publish=published.append)
            await supervisor.start()

            assert supervisor.pool is not None, (
                "a complete required set must publish regardless of optional "
                "feature flags"
            )
            assert supervisor.pool.pool is inner
            assert published[-1] is supervisor.pool
            assert inner.close.await_count == 0, "a published pool is not closed"
            await supervisor.stop()

    @pytest.mark.asyncio
    async def test_reconnect_publishes_after_missing_afk_relation_appears(self):
        """The bounded retry loop recovers once the AFK relation exists."""
        from app.db import schema as schema_module
        from app.db.session import DatabasePoolSupervisor

        incomplete = _make_acquirable_pool(set(_REQUIRED_TABLES) - {"delivery_log"})
        complete = _make_acquirable_pool(set(_REQUIRED_TABLES))
        candidates = [incomplete, complete]
        published: list = []

        async def _create_pool(**kwargs):
            return candidates.pop(0)

        with patch(
            "app.db.session.asyncpg.create_pool", AsyncMock(side_effect=_create_pool)
        ), patch(
            "app.db.schema.run_migrations", AsyncMock()
        ), patch(
            "app.db.session.ensure_schema", new=schema_module.ensure_schema
        ):
            supervisor = DatabasePoolSupervisor(_settings(), publish=published.append)
            await supervisor.start()
            assert supervisor.pool is None
            assert published == [None]

            deadline = asyncio.get_event_loop().time() + 2.0
            while len(published) < 2 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.005)

            assert len(published) == 2, (
                "reconnect must publish once the missing AFK relation appears"
            )
            assert published[-1].pool is complete
            await supervisor.stop()
