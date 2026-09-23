"""Tests for the usage_dashboard_daily table migration 0048 (issue #735).

The usage dashboard daily rollup is a pre-aggregated operational read-model
keyed by ``(day, provider)`` (afk_dashboard_daily precedent, migration 0046).
This module verifies:

1. Upgrade creates ``usage_dashboard_daily`` with the agreed composite
   primary key ``(day, provider)``.
2. Every additive metric column is present — token totals, estimated cost,
   and record count.  No non-additive columns are stored.
3. The metadata columns ``derived_at`` (timestamptz), ``rollup_version``
   (text), and ``updated_at`` (timestamptz) are present.
4. Indexes support the summary read path: the composite primary key serves
   date-range scans, the ``(provider, day)`` index serves provider-scoped
   scans, and the ``day`` index serves unfiltered date-range scans.
5. Downgrade drops the indexes and the table.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from alembic.config import Config

# ── Helpers ──────────────────────────────────────────────────────────────────

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"

_ADDITIVE_COLUMNS = [
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "estimated_cost_usd",
    "record_count",
]

_METADATA_COLUMNS = ["derived_at", "rollup_version", "updated_at"]


def _alembic_cfg() -> Config:
    """Build a minimal Alembic Config pointing at the project's migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _run_alembic_upgrade_sql(start: str = "0047", revision: str = "0048") -> str:
    """Run ``alembic upgrade <start>:<revision> --sql`` and return the SQL string."""
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, f"{start}:{revision}", sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0047") -> str:
    """Run ``alembic downgrade 0048:<revision> --sql`` and return the SQL string."""
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0048:{revision}", sql=True)
    return buf.getvalue()


def _extract_table_ddl(sql: str, table_name: str) -> str:
    """Extract the DDL block for a given CREATE TABLE statement."""
    start = sql.find(f"CREATE TABLE {table_name}")
    if start == -1:
        return ""
    open_paren = sql.find("(", start)
    if open_paren == -1:
        return sql[start:]
    pos = open_paren + 1
    depth = 0
    while True:
        closer = sql.find(")", pos)
        if closer == -1:
            return sql[start:]
        depth = sql.count("(", pos, closer + 1) - 1 + depth
        if depth <= 0 and closer + 1 < len(sql) and sql[closer + 1] == ";":
            return sql[start : closer + 1]
        pos = closer + 1


def _has_column(table_ddl: str, column: str) -> bool:
    """Word-boundary column match — prevents ``input_tokens`` matching
    inside ``cache_read_tokens``."""
    return re.search(rf"\b{re.escape(column)}\b", table_ddl) is not None


# ══════════════════════════════════════════════════════════════════════════════
#  Migration — Offline SQL Verification
# ══════════════════════════════════════════════════════════════════════════════


class TestMigration0048Upgrade:
    """Verify Alembic migration 0048 upgrade creates the usage dashboard rollup."""

    def _get_upgrade_sql(self) -> str:
        return _run_alembic_upgrade_sql()

    def test_upgrade_creates_usage_dashboard_daily_table(self):
        """Upgrade should emit CREATE TABLE for usage_dashboard_daily."""
        sql = self._get_upgrade_sql()

        assert "CREATE TABLE usage_dashboard_daily" in sql, (
            "Expected CREATE TABLE usage_dashboard_daily in upgrade SQL"
        )

    def test_upgrade_table_has_composite_primary_key(self):
        """The rollup is keyed by (day, provider)."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_dashboard_daily")

        assert "PRIMARY KEY (day, provider)" in table_ddl, (
            "Missing composite PRIMARY KEY (day, provider)"
        )

    def test_upgrade_table_has_all_additive_metric_columns(self):
        """Every additive metric column is present exactly once."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_dashboard_daily")

        for col in _ADDITIVE_COLUMNS:
            assert _has_column(table_ddl, col), (
                f"Missing additive column '{col}' in usage_dashboard_daily"
            )

    def test_upgrade_table_has_metadata_columns(self):
        """derived_at/rollup_version/updated_at metadata columns are present."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_dashboard_daily")

        for col in _METADATA_COLUMNS:
            assert _has_column(table_ddl, col), (
                f"Missing metadata column '{col}' in usage_dashboard_daily"
            )

        assert "derived_at TIMESTAMP WITH TIME ZONE" in table_ddl
        assert "updated_at TIMESTAMP WITH TIME ZONE" in table_ddl
        assert re.search(r"\brollup_version VARCHAR", table_ddl), (
            "rollup_version should be a text column"
        )

    def test_upgrade_does_not_store_non_additive_metrics(self):
        """No distinct-count/ratio columns — only additive totals belong here."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_dashboard_daily")

        forbidden = [
            "avg_",
            "median_",
            "p95_",
            "distinct_",
            "unique_",
            "success_rate",
            "repository_url",
        ]
        lowered = table_ddl.lower()
        for token in forbidden:
            assert token not in lowered, (
                f"Rollup must not store non-additive column '{token}'"
            )

    def test_upgrade_creates_provider_day_index(self):
        """The (provider, day) index supports provider-scoped scans."""
        sql = self._get_upgrade_sql()

        assert (
            "CREATE INDEX ix_usage_dashboard_daily_provider_day" in sql
        ), "Missing index ix_usage_dashboard_daily_provider_day"

    def test_upgrade_creates_day_index(self):
        """The day index supports unfiltered date-range scans."""
        sql = self._get_upgrade_sql()

        assert (
            "CREATE INDEX ix_usage_dashboard_daily_day" in sql
        ), "Missing index ix_usage_dashboard_daily_day"

    def test_upgrade_does_not_touch_existing_tables(self):
        """0048 should create exactly one table and no other DDL."""
        sql = self._get_upgrade_sql()

        assert sql.count("CREATE TABLE") == 1, (
            "Expected exactly one CREATE TABLE from migration 0048"
        )
        assert "ALTER TABLE" not in sql, "Migration 0048 should not ALTER tables"
        assert "DROP TABLE" not in sql, "Migration 0048 should not DROP tables"


class TestMigration0048Downgrade:
    """Verify Alembic migration 0048 downgrade is fully reversible."""

    def _get_downgrade_sql(self) -> str:
        return _run_alembic_downgrade_sql("0047")

    def test_downgrade_drops_provider_day_index(self):
        """Downgrade should drop the provider-scoped index."""
        sql = self._get_downgrade_sql()

        assert "DROP INDEX ix_usage_dashboard_daily_provider_day" in sql

    def test_downgrade_drops_day_index(self):
        """Downgrade should drop the day index."""
        sql = self._get_downgrade_sql()

        assert "DROP INDEX ix_usage_dashboard_daily_day" in sql

    def test_downgrade_drops_table(self):
        """Downgrade should emit DROP TABLE for usage_dashboard_daily."""
        sql = self._get_downgrade_sql()

        assert "DROP TABLE usage_dashboard_daily" in sql, (
            "Expected DROP TABLE usage_dashboard_daily in downgrade SQL"
        )
