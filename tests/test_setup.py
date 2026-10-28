"""Tests for database setup — migrations and required relation checks.

Tests the ``check_required_tables()`` function which verifies that all
required relations exist in the database after Alembic migrations have been
applied.  The required set covers the identity layer and the canonical
usage/ingestion ledger relations selected in issue #825, plus the active AFK
reporting and event-consumer relations selected in issue #832.  Optional,
archival and derived/rebuildable relations are deliberately excluded (the
inventory and its rationale live in ``app/db/setup.py``).

Each test simulates different table sets by controlling what
``information_schema.tables`` returns — no live database is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.setup import _REQUIRED_TABLES, check_required_tables

# ── Helpers ──────────────────────────────────────────────────────────────

_IDENTITY_TABLES = frozenset({"opencode_clients", "collector_credentials"})
_COMPLETE_SCHEMA = frozenset(_REQUIRED_TABLES)

# Relations queried/written by the active AFK reporting paths (issue #832):
# the always-registered AFK API routers plus the AFK outcome consumer and
# execution-provisioning write paths.
_AFK_REQUIRED_TABLES = frozenset(
    {
        "afk_runs",
        "afk_run_sessions",
        "afk_run_entities",
        "afk_run_delivery_batches",
        "engineering_events",
        "delivery_log",
        "unresolved_correlations",
        "execution_bindings",
        "recovery_checkpoints",
        "reporting_deliveries",
        "delivery_state_trails",
    }
)

# Rebuildable projections, dashboard rollups, repair-only audit tables and
# repository seams with no active service caller must never gate startup.
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

# Execution-era relations removed by the observability refactor (issue #207)
# must never be required again.
_DEFUNCT_EXECUTION_ERA_TABLES = frozenset({"jobs", "runners", "workspaces"})


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


class TestRequiredTableInventory:
    """The selected inventory is identity plus the usage/ingestion ledger."""

    def test_identity_tables_are_required(self) -> None:
        """The identity layer remains part of the required set."""
        assert _IDENTITY_TABLES <= _COMPLETE_SCHEMA

    @pytest.mark.parametrize(
        "table",
        [
            "source_databases",
            "sessions",
            "usage_events",
            "opencode_usage_records",
            "observed_models",
            "source_identities",
            "source_identity_quarantine",
            "ingest_batches",
            "ingest_audit",
            "usage_ingest_attempts",
            "client_project_rollup",
        ],
    )
    def test_active_usage_ingestion_relations_are_required(self, table: str) -> None:
        """Every relation on the active usage/ingestion path is enforced."""
        assert table in _COMPLETE_SCHEMA

    @pytest.mark.parametrize(
        "table",
        [
            "alembic_version",
            "afk_dashboard_daily",
            "usage_dashboard_daily",
            "reporting_resource_aggregates",
        ],
    )
    def test_derived_and_migration_metadata_are_not_required(self, table: str) -> None:
        """Derived read-models and Alembic metadata never gate startup."""
        assert table not in _COMPLETE_SCHEMA


class TestAfkRequiredTableInventory:
    """The AFK reporting/event-consumer relations selected in issue #832."""

    @pytest.mark.parametrize("table", sorted(_AFK_REQUIRED_TABLES))
    def test_active_afk_relations_are_required(self, table: str) -> None:
        """Every relation on an active AFK reporting/consumer path is enforced."""
        assert table in _COMPLETE_SCHEMA

    @pytest.mark.parametrize("table", sorted(_OPTIONAL_OR_DERIVED_TABLES))
    def test_optional_derived_and_repair_relations_are_not_required(
        self, table: str
    ) -> None:
        """Rebuildable, optional and repair-only relations never gate startup."""
        assert table not in _COMPLETE_SCHEMA

    @pytest.mark.parametrize("table", sorted(_DEFUNCT_EXECUTION_ERA_TABLES))
    def test_defunct_execution_era_tables_are_not_required(self, table: str) -> None:
        """The observability refactor removed these relations permanently."""
        assert table not in _COMPLETE_SCHEMA


class TestCheckRequiredTables:
    """Tests for ``check_required_tables()`` startup validation."""

    @pytest.mark.asyncio
    async def test_success_when_all_required_present(self) -> None:
        """Should pass silently when the complete selected set exists."""
        mock_pool = _make_mock_pool(set(_COMPLETE_SCHEMA))

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_success_with_extra_unrelated_tables(self) -> None:
        """Extra unrelated/optional tables must not cause false failures."""
        existing = set(_COMPLETE_SCHEMA) | {
            "alembic_version",
            "some_other_table",
            "afk_dashboard_daily",
            "observed_messages",
        }
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_success_without_optional_and_derived_tables(self) -> None:
        """Optional/derived relations absent must not cause false failures.

        Only the selected required set is present; every rebuildable
        projection, rollup and repair-only relation is absent.
        """
        existing = set(_COMPLETE_SCHEMA) - _OPTIONAL_OR_DERIVED_TABLES
        mock_pool = _make_mock_pool(existing)

        result = await check_required_tables(mock_pool)

        assert result is None

    @pytest.mark.asyncio
    async def test_identity_and_usage_present_but_afk_event_relation_missing(
        self,
    ) -> None:
        """A missing AFK event relation fails even when usage is complete.

        This is the issue #832 regression: the usage/ingestion layer is
        fully present but the AFK outcome event ledger is absent.
        """
        existing = set(_COMPLETE_SCHEMA) - {"engineering_events"}
        mock_pool = _make_mock_pool(existing)

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "engineering_events" in msg
        assert "alembic upgrade head" in msg
        # Complete usage/ingestion relations must not be reported as missing.
        assert "usage_events" not in msg
        assert "sessions" not in msg

    @pytest.mark.asyncio
    async def test_identity_and_usage_present_but_afk_delivery_relation_missing(
        self,
    ) -> None:
        """A missing AFK delivery relation is named precisely."""
        existing = set(_COMPLETE_SCHEMA) - {"delivery_state_trails"}
        mock_pool = _make_mock_pool(existing)

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "delivery_state_trails" in msg
        assert "reporting_deliveries" not in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_identity_tables_alone_do_not_satisfy_usage_schema(self) -> None:
        """Identity tables present but the usage ledger absent must fail.

        This is the issue #825 regression: the old check logged success
        after seeing only the two identity tables.
        """
        mock_pool = _make_mock_pool(set(_IDENTITY_TABLES))

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "usage_events" in msg
        assert "sessions" in msg
        assert "alembic upgrade head" in msg
        # Present identity tables must not be reported as missing.
        assert "opencode_clients" not in msg
        assert "collector_credentials" not in msg

    @pytest.mark.asyncio
    async def test_identity_present_but_one_usage_relation_missing(self) -> None:
        """A single absent usage relation is named in the failure message."""
        existing = set(_COMPLETE_SCHEMA) - {"usage_events"}
        mock_pool = _make_mock_pool(existing)

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "usage_events" in msg
        assert "sessions" not in msg
        assert "collector_credentials" not in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_table", _REQUIRED_TABLES)
    async def test_every_required_table_is_enforced(self, missing_table: str) -> None:
        """Each selected relation is individually enforced."""
        existing = set(_COMPLETE_SCHEMA) - {missing_table}
        mock_pool = _make_mock_pool(existing)

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        assert missing_table in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_raises_when_required_tables_missing(self) -> None:
        """Should raise ``RuntimeError`` when required tables are missing."""
        mock_pool = _make_mock_pool(set())

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "opencode_clients" in msg
        assert "collector_credentials" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_raises_when_partial_tables_present(self) -> None:
        """Should raise when only some required tables exist."""
        existing = {"opencode_clients"}  # missing collector_credentials
        mock_pool = _make_mock_pool(existing)

        with pytest.raises(RuntimeError) as excinfo:
            await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        assert "collector_credentials" in msg
        assert "alembic upgrade head" in msg

    @pytest.mark.asyncio
    async def test_raises_when_patched_tables_missing(self) -> None:
        """Should raise ``RuntimeError`` when patched required tables are missing."""
        required = ["future_sessions"]
        mock_pool = _make_mock_pool(set())

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
        mock_pool = _make_mock_pool(set())

        with patch("app.db.setup._REQUIRED_TABLES", required):
            with pytest.raises(RuntimeError) as excinfo:
                await check_required_tables(mock_pool)

        msg = str(excinfo.value)
        for table in required:
            assert table in msg, f"Missing table '{table}' not found in error message"
        assert "alembic upgrade head" in msg
