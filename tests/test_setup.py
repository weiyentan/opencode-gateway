"""Tests for database setup — migrations and required table checks.

Tests the ``check_required_tables()`` function which verifies that all
required tables exist in the database after Alembic migrations have been
applied.  After the execution-era cleanup (issue #207), the required
tables list is empty — observability tables will be added in future
slices.  Each test simulates different table sets by controlling what
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

    After the execution-era cleanup, the required tables list is empty.
    The function should always pass regardless of what tables exist.
    """

    @pytest.mark.asyncio
    async def test_success_when_required_list_is_empty(self) -> None:
        """Should pass silently when _REQUIRED_TABLES is empty (current state)."""
        existing: set[str] = set()
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_success_even_when_database_is_empty(self) -> None:
        """Should pass when database has no tables and required list is empty."""
        existing: set[str] = set()
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_success_when_database_has_extra_tables(self) -> None:
        """Should pass when database has tables but required list is empty."""
        existing = {"alembic_version", "some_other_table"}
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_when_tables_required_but_missing(self) -> None:
        """Should raise ``RuntimeError`` when required tables are missing.

        Patches ``_REQUIRED_TABLES`` to include a table that doesn't exist
        (future-slice behavior, not current state).
        """
        required = ["future_sessions"]
        existing: set[str] = set()
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", required):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "future_sessions" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_error_message_lists_all_missing_tables(self) -> None:
        """Error message should name **all** missing tables, not just the first."""
        required = ["table_a", "table_b", "table_c"]
        existing: set[str] = set()
        mock_pool = _make_mock_pool(existing)

        with patch("app.db.setup._REQUIRED_TABLES", required):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        for table in required:
            assert table in msg, f"Missing table '{table}' not found in error message"
        assert "alembic upgrade head" in msg
