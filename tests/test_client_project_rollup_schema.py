"""Tests for the client_project_rollup table migration 0022.

Migration-only schema (no ORM models yet, ADR 0015), so this module
verifies the rendered SQL of ``alembic upgrade 0021:0022 --sql`` and
``alembic downgrade 0022:0021 --sql``:

1. Upgrade creates the ``client_project_rollup`` table keyed by
   ``(client_id, project_id, day)`` — the stable project ID, not the
   volatile project label — with additive token/cost columns only.
2. No session counts, model counts, or reasoning-token total are stored
   in the rollup (they remain distinct-count/sum queries over raw
   ``usage_events`` records, ADR 0015 decision 3).
3. Indexes support the hybrid read path: the composite primary key serves
   per-(client, project, day) lookups and the ``(client_id, day)`` index
   serves client-scoped day-range scans.
4. Downgrade drops the index and the table.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from alembic.config import Config

# ── Helpers ──────────────────────────────────────────────────────────────────

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"


def _alembic_cfg() -> Config:
    """Build a minimal Alembic Config pointing at the project's migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _run_alembic_upgrade_sql(start: str = "0021", revision: str = "0022") -> str:
    """Run ``alembic upgrade <start>:<revision> --sql`` and return the SQL string."""
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, f"{start}:{revision}", sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0021") -> str:
    """Run ``alembic downgrade 0022:<revision> --sql`` and return the SQL string."""
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0022:{revision}", sql=True)
    return buf.getvalue()


def _extract_table_ddl(sql: str, table_name: str) -> str:
    """Extract the DDL block for a given CREATE TABLE statement."""
    start = sql.find(f"CREATE TABLE {table_name}")
    if start == -1:
        return ""
    # The table block ends at the first ")" that is immediately followed by
    # ";" after the opening "(".  Guard against unbalanced parens (e.g.
    # DEFAULT gen_random_uuid()) by extending to the next ");" until balanced.
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


# ══════════════════════════════════════════════════════════════════════════════
#  Migration — Offline SQL Verification
# ══════════════════════════════════════════════════════════════════════════════


class TestMigration0022Upgrade:
    """Verify Alembic migration 0022 upgrade creates the rollup table."""

    def _get_upgrade_sql(self) -> str:
        """Run migration 0022 in offline mode and return SQL output."""
        return _run_alembic_upgrade_sql()

    def test_upgrade_creates_client_project_rollup_table(self):
        """Upgrade should emit CREATE TABLE for client_project_rollup."""
        sql = self._get_upgrade_sql()

        assert "CREATE TABLE client_project_rollup" in sql, (
            "Expected CREATE TABLE client_project_rollup in upgrade SQL"
        )

    def test_upgrade_table_has_agreed_key_and_additive_columns(self):
        """The rollup should be keyed by (client_id, project_id, day) with
        additive token/cost columns only."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "client_project_rollup")

        expected_cols = [
            "client_id",
            "project_id",
            "day",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated_cost_usd",
        ]
        for col in expected_cols:
            assert col in table_ddl, f"Missing column '{col}' in client_project_rollup"

        # The agreed key is the composite primary key on the stable project ID
        # (ADR 0015 decision 2), not the volatile project label.
        assert "PRIMARY KEY (client_id, project_id, day)" in table_ddl, (
            "Missing composite PRIMARY KEY (client_id, project_id, day)"
        )

    def test_upgrade_does_not_store_session_or_model_counts(self):
        """No session counts, model counts, or reasoning totals in the rollup
        (ADR 0015 decision 3 — distinct counts do not aggregate additively)."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "client_project_rollup")

        forbidden = [
            "session_count",
            "model_count",
            "reasoning_tokens",
            "project_label",
            "total_reasoning",
        ]
        for token in forbidden:
            assert token not in table_ddl, (
                f"Rollup must not store '{token}' — additive token/cost totals only"
            )

    def test_upgrade_foreign_key_to_opencode_clients(self):
        """client_id should reference opencode_clients."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "client_project_rollup")

        assert "REFERENCES opencode_clients (id)" in table_ddl, (
            "Missing FK client_id -> opencode_clients"
        )

    def test_upgrade_creates_client_day_index(self):
        """The (client_id, day) index should support client-scoped day-range
        scans of the hybrid read path."""
        sql = self._get_upgrade_sql()

        assert "CREATE INDEX ix_client_project_rollup_client_day" in sql, (
            "Missing index ix_client_project_rollup_client_day"
        )

    def test_upgrade_does_not_touch_existing_tables(self):
        """0022 should create exactly one table and no other DDL."""
        sql = self._get_upgrade_sql()

        assert sql.count("CREATE TABLE") == 1, (
            "Expected exactly one CREATE TABLE from migration 0022"
        )
        assert "ALTER TABLE" not in sql, "Migration 0022 should not ALTER tables"
        assert "DROP TABLE" not in sql, "Migration 0022 should not DROP tables"


class TestMigration0022Downgrade:
    """Verify Alembic migration 0022 downgrade is fully reversible."""

    def _get_downgrade_sql(self) -> str:
        """Run downgrade to 0021 in offline mode and return SQL output."""
        return _run_alembic_downgrade_sql("0021")

    def test_downgrade_drops_client_day_index(self):
        """Downgrade should drop the rollup's (client_id, day) index."""
        sql = self._get_downgrade_sql()

        assert "DROP INDEX ix_client_project_rollup_client_day" in sql, (
            "Expected DROP INDEX ix_client_project_rollup_client_day"
        )

    def test_downgrade_drops_table(self):
        """Downgrade should emit DROP TABLE for client_project_rollup."""
        sql = self._get_downgrade_sql()

        assert "DROP TABLE client_project_rollup" in sql, (
            "Expected DROP TABLE client_project_rollup in downgrade SQL"
        )
