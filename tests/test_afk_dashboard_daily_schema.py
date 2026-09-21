"""Tests for the ``afk_dashboard_daily`` migration 0047 and ORM model
(issue #714/#720).

The AFK dashboard daily rollup is the pre-aggregated operational read-model the
AFK Dashboard summary endpoint (issue #719) reads and the recompute engine
(``app.core.afk_dashboard_daily``, issue #715) writes.  It follows the
``client_project_rollup`` precedent (migration 0023, ADR 0014/0015): a composite
primary key on the bucket identity, additive-only metric columns, and a
reversible downgrade.

This module covers the parts unique to this change set: the migration 0047
addition of the day index used by unfiltered date-range scans, and the
SQLAlchemy ORM model that mirrors the DDL for Alembic autogenerate.  Migration
0046 (table creation) is covered by its own test module.

Migration-only schema, so the migration tests verify the rendered SQL of
``alembic upgrade 0046:0047 --sql`` and ``alembic downgrade 0047:0046 --sql``
(Alembic offline mode):

1. Upgrade adds the ``(day)`` index used by unfiltered date-range scans.
2. Upgrade creates no table — ``afk_dashboard_daily`` was created by 0046.
3. The downgrade drops the day index.
4. The SQLAlchemy ORM model (Alembic-autogenerate only) mirrors the DDL,
   including both indexes.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

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


# ══════════════════════════════════════════════════════════════════════════════
#  Migration — Offline SQL Verification
# ══════════════════════════════════════════════════════════════════════════════


class TestMigration0047DayIndex:
    """Verify migration 0047 adds the day index for unfiltered date-range scans."""

    def test_upgrade_creates_day_index(self):
        """Migration 0047 upgrade should emit CREATE INDEX ix_afk_dashboard_daily_day."""
        from alembic.command import upgrade
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
        cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0046:0047", sql=True)
        sql = buf.getvalue()
        assert "CREATE INDEX ix_afk_dashboard_daily_day" in sql

    def test_upgrade_does_not_create_table(self):
        """Migration 0047 only adds an index — the table was created by 0046."""
        from alembic.command import upgrade
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
        cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0046:0047", sql=True)
        sql = buf.getvalue()
        assert "CREATE TABLE" not in sql

    def test_downgrade_drops_day_index(self):
        """Migration 0047 downgrade should drop the day index."""
        from alembic.command import downgrade
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
        cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            downgrade(cfg, "0047:0046", sql=True)
        sql = buf.getvalue()
        assert "DROP INDEX ix_afk_dashboard_daily_day" in sql


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

    def test_model_registers_day_index(self):
        from app.db.models.afk import AFKDashboardDaily

        index_names = {idx.name for idx in AFKDashboardDaily.__table__.indexes}
        assert "ix_afk_dashboard_daily_day" in index_names
