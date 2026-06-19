"""Tests for database setup — migrations and required table checks.

Tests the ``check_required_tables()`` function which verifies that all
required tables exist in the database after Alembic migrations have been
applied.  Each test simulates different table sets by controlling what
``information_schema.tables`` returns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.setup import _REQUIRED_TABLES, check_required_tables

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_mock_pool(existing_tables: set[str]) -> MagicMock:
    """Build a mock asyncpg pool that reports *existing_tables*.

    Queries against ``information_schema.tables`` on this pool will
    return only the table names in *existing_tables*.
    """
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        return_value=[{"table_name": t} for t in sorted(existing_tables)]
    )

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_ctx
    return mock_pool


# ── Tests ────────────────────────────────────────────────────────────────


class TestCheckRequiredTables:
    """Tests for ``check_required_tables()`` startup validation.

    Each test verifies that ``check_required_tables()`` raises a
    ``RuntimeError`` with a clear message naming the missing table(s)
    when required tables are absent, and passes silently when all
    required tables are present.
    """

    # -- Success case ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_success_when_all_tables_exist(self) -> None:
        """Should pass silently when the live ``_REQUIRED_TABLES`` are present."""
        existing = set(_REQUIRED_TABLES)
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    # -- Single-table missing cases -------------------------------------

    @pytest.mark.asyncio
    async def test_raises_when_gateway_jobs_missing(self) -> None:
        """Should raise ``RuntimeError`` naming *gateway_jobs* when absent."""
        required = set(_REQUIRED_TABLES)
        existing = required - {"gateway_jobs"}
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "gateway_jobs" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_raises_when_workspaces_missing(self) -> None:
        """Should raise ``RuntimeError`` naming *workspaces* when absent."""
        required = set(_REQUIRED_TABLES)
        existing = required - {"workspaces"}
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "workspaces" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_raises_when_runner_events_missing(self) -> None:
        """Should raise ``RuntimeError`` naming *runner_events* when absent.

        *runner_events* is not yet in the production ``_REQUIRED_TABLES``
        list (it will be added by a separate issue), so we patch the list
        to include it for this test.
        """
        required = set(_REQUIRED_TABLES) | {"runner_events", "webhooks"}
        existing = required - {"runner_events"}
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "runner_events" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_raises_when_webhooks_missing(self) -> None:
        """Should raise ``RuntimeError`` naming *webhooks* when absent.

        *webhooks* is not yet in the production ``_REQUIRED_TABLES`` list
        (it will be added by a separate issue), so we patch the list to
        include it for this test.
        """
        required = set(_REQUIRED_TABLES) | {"runner_events", "webhooks"}
        existing = required - {"webhooks"}
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "webhooks" in msg
        assert "alembic upgrade head" in msg

    # -- Multi-table missing case ---------------------------------------

    @pytest.mark.asyncio
    async def test_error_message_lists_all_missing_tables(self) -> None:
        """Error message should name **all** missing tables, not just the first."""
        required = set(_REQUIRED_TABLES) | {"runner_events", "webhooks"}
        missing = {"runner_events", "webhooks", "approvals"}
        existing = required - missing
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        for table in missing:
            assert table in msg, f"Missing table '{table}' not found in error message"
        assert "alembic upgrade head" in msg

    # -- Edge cases -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_raises_when_database_is_empty(self) -> None:
        """Should raise when the *required* list is not empty, even if DB is empty.

        If the database has no tables at all, all required tables should
        be reported as missing.
        """
        required = set(_REQUIRED_TABLES)
        existing: set[str] = set()
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", list(required)):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        for table in sorted(required):
            assert table in msg, f"Table '{table}' should appear in error message"
        assert "alembic upgrade head" in msg
