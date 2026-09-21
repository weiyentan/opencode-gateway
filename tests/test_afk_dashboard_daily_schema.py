"""Tests for the afk_dashboard_daily table migration 0046 (issue #714).

The AFK dashboard daily rollup is a pre-aggregated operational read-model
keyed by ``(day, provider, repository)`` (client_project_rollup precedent,
migration 0023 / ADR 0014/0015).  This module verifies:

1. Upgrade creates ``afk_dashboard_daily`` with the agreed composite primary
   key ``(day, provider, repository)``.
2. Every additive metric column is present — run counts, change-request
   counts, execution counts, session count, token totals, and estimated
   cost.  No non-additive (distinct-count / ratio) columns are stored.
3. The metadata columns ``derived_at`` (timestamptz), ``rollup_version``
   (text), and ``updated_at`` (timestamptz) are present.
4. Indexes support the summary read path: the composite primary key serves
   date-range scans and the ``(provider, repository, day)`` index serves
   provider/repository-scoped scans.
5. Downgrade drops the index and the table.
6. The SQLAlchemy ORM model (Alembic-autogenerate only) mirrors the DDL.
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
    "runs_started",
    "change_requests_opened",
    "change_requests_merged",
    "change_requests_closed",
    "execution_count",
    "successful_execution_count",
    "failed_execution_count",
    "cancelled_execution_count",
    "session_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
]

_METADATA_COLUMNS = ["derived_at", "rollup_version", "updated_at"]


def _alembic_cfg() -> Config:
    """Build a minimal Alembic Config pointing at the project's migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _run_alembic_upgrade_sql(start: str = "0045", revision: str = "0046") -> str:
    """Run ``alembic upgrade <start>:<revision> --sql`` and return the SQL string."""
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, f"{start}:{revision}", sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0045") -> str:
    """Run ``alembic downgrade 0046:<revision> --sql`` and return the SQL string."""
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0046:{revision}", sql=True)
    return buf.getvalue()


def _extract_table_ddl(sql: str, table_name: str) -> str:
    """Extract the DDL block for a given CREATE TABLE statement."""
    start = sql.find(f"CREATE TABLE {table_name}")
    if start == -1:
        return ""
    # Guard against unbalanced parens (e.g. DEFAULT gen_random_uuid()) by
    # extending to the next ");" until the paren depth is balanced.
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
    """Word-boundary column match — prevents ``execution_count`` matching
    inside ``successful_execution_count``."""
    return re.search(rf"\b{re.escape(column)}\b", table_ddl) is not None


# ══════════════════════════════════════════════════════════════════════════════
#  Migration — Offline SQL Verification
# ══════════════════════════════════════════════════════════════════════════════


class TestMigration0046Upgrade:
    """Verify Alembic migration 0046 upgrade creates the dashboard rollup."""

    def _get_upgrade_sql(self) -> str:
        return _run_alembic_upgrade_sql()

    def test_upgrade_creates_afk_dashboard_daily_table(self):
        """Upgrade should emit CREATE TABLE for afk_dashboard_daily."""
        sql = self._get_upgrade_sql()

        assert "CREATE TABLE afk_dashboard_daily" in sql, (
            "Expected CREATE TABLE afk_dashboard_daily in upgrade SQL"
        )

    def test_upgrade_table_has_composite_primary_key(self):
        """The rollup is keyed by (day, provider, repository)."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "afk_dashboard_daily")

        assert "PRIMARY KEY (day, provider, repository)" in table_ddl, (
            "Missing composite PRIMARY KEY (day, provider, repository)"
        )

    def test_upgrade_table_has_all_additive_metric_columns(self):
        """Every additive metric column is present exactly once."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "afk_dashboard_daily")

        for col in _ADDITIVE_COLUMNS:
            assert _has_column(table_ddl, col), (
                f"Missing additive column '{col}' in afk_dashboard_daily"
            )

    def test_upgrade_table_has_metadata_columns(self):
        """derived_at/rollup_version/updated_at metadata columns are present."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "afk_dashboard_daily")

        for col in _METADATA_COLUMNS:
            assert _has_column(table_ddl, col), (
                f"Missing metadata column '{col}' in afk_dashboard_daily"
            )

        assert "derived_at TIMESTAMP WITH TIME ZONE" in table_ddl
        assert "updated_at TIMESTAMP WITH TIME ZONE" in table_ddl
        assert re.search(r"\brollup_version VARCHAR", table_ddl), (
            "rollup_version should be a text column"
        )

    def test_upgrade_does_not_store_non_additive_metrics(self):
        """No distinct-count/ratio columns — only additive totals belong here."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "afk_dashboard_daily")

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

    def test_upgrade_creates_provider_repository_day_index(self):
        """The (provider, repository, day) index supports scoped scans."""
        sql = self._get_upgrade_sql()

        assert (
            "CREATE INDEX ix_afk_dashboard_daily_provider_repository_day" in sql
        ), "Missing index ix_afk_dashboard_daily_provider_repository_day"

    def test_upgrade_does_not_touch_existing_tables(self):
        """0046 should create exactly one table and no other DDL."""
        sql = self._get_upgrade_sql()

        assert sql.count("CREATE TABLE") == 1, (
            "Expected exactly one CREATE TABLE from migration 0046"
        )
        assert "ALTER TABLE" not in sql, "Migration 0046 should not ALTER tables"
        assert "DROP TABLE" not in sql, "Migration 0046 should not DROP tables"


class TestMigration0046Downgrade:
    """Verify Alembic migration 0046 downgrade is fully reversible."""

    def _get_downgrade_sql(self) -> str:
        return _run_alembic_downgrade_sql("0045")

    def test_downgrade_drops_provider_repository_day_index(self):
        """Downgrade should drop the rollup's scoped index."""
        sql = self._get_downgrade_sql()

        assert "DROP INDEX ix_afk_dashboard_daily_provider_repository_day" in sql

    def test_downgrade_drops_table(self):
        """Downgrade should emit DROP TABLE for afk_dashboard_daily."""
        sql = self._get_downgrade_sql()

        assert "DROP TABLE afk_dashboard_daily" in sql, (
            "Expected DROP TABLE afk_dashboard_daily in downgrade SQL"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ORM Model — Alembic autogenerate parity
# ══════════════════════════════════════════════════════════════════════════════


class TestAFKDashboardDailyModel:
    """The SQLAlchemy model mirrors the migration (autogenerate source)."""

    def test_model_table_name_and_registration(self):
        from app.db.models import Base
        from app.db.models.afk import AFKDashboardDaily

        assert AFKDashboardDaily.__tablename__ == "afk_dashboard_daily"
        assert "afk_dashboard_daily" in Base.metadata.tables

    def test_model_primary_key_matches_migration(self):
        from app.db.models.afk import AFKDashboardDaily

        pk = [c.name for c in AFKDashboardDaily.__table__.primary_key.columns]
        assert pk == ["day", "provider", "repository"]

    def test_model_has_additive_and_metadata_columns(self):
        from app.db.models.afk import AFKDashboardDaily

        columns = set(AFKDashboardDaily.__table__.columns.keys())
        for col in _ADDITIVE_COLUMNS + _METADATA_COLUMNS:
            assert col in columns, f"Model missing column '{col}'"

    def test_model_registers_scoped_index(self):
        from app.db.models.afk import AFKDashboardDaily

        index_names = {idx.name for idx in AFKDashboardDaily.__table__.indexes}
        assert "ix_afk_dashboard_daily_provider_repository_day" in index_names
