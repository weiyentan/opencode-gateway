"""Tests for the canonical event schema migration 0021.

Migration-only schema (no ORM models yet), so this module verifies the
rendered SQL of ``alembic upgrade 0021 --sql`` and
``alembic downgrade 0021:0020 --sql``:

1. Upgrade creates all five tables (usage_events, usage_ingest_attempts,
   source_identities, source_identity_quarantine, source_identity_resolutions)
   with the expected columns, constraints, foreign keys, and indexes.
2. The circular FK pair (source_identity_quarantine.resolution_id ->
   source_identity_resolutions, source_identity_resolutions.quarantine_id ->
   source_identity_quarantine) is added via ALTER TABLE after both tables
   exist.
3. Downgrade drops the two circular FKs and all five tables in reverse
   dependency order.
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


def _alembic_cfg() -> Config:
    """Build a minimal Alembic Config pointing at the project's migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _run_alembic_upgrade_sql(revision: str = "0021") -> str:
    """Run ``alembic upgrade <revision> --sql`` and return the SQL string."""
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, revision, sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0020") -> str:
    """Run ``alembic downgrade 0021:<revision> --sql`` and return the SQL string."""
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0021:{revision}", sql=True)
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


class TestMigration0021Upgrade:
    """Verify Alembic migration 0021 upgrade creates all five tables."""

    _TABLE_NAMES = [
        "usage_events",
        "usage_ingest_attempts",
        "source_identities",
        "source_identity_quarantine",
        "source_identity_resolutions",
    ]

    def _get_upgrade_sql(self) -> str:
        """Run migration 0021 in offline mode and return SQL output."""
        return _run_alembic_upgrade_sql("0021")

    def test_upgrade_creates_all_five_tables(self):
        """Upgrade should emit CREATE TABLE for all five tables."""
        sql = self._get_upgrade_sql()

        for table_name in self._TABLE_NAMES:
            assert f"CREATE TABLE {table_name}" in sql, (
                f"Expected CREATE TABLE {table_name} in upgrade SQL"
            )

    def test_upgrade_does_not_touch_existing_tables(self):
        """Upgrade should not emit ALTER/DROP for pre-existing tables."""
        sql = self._get_upgrade_sql()
        # opencode_usage_records must be created exactly once by migration
        # 0012 and never altered or dropped by 0021.
        assert sql.count("CREATE TABLE opencode_usage_records") == 1
        assert "DROP TABLE opencode_usage_records" not in sql
        # 0021 contributes no ALTER on existing tables; only the two ALTER
        # TABLE ADD CONSTRAINT statements for the circular FK pair.
        alter_statements = re.findall(r"ALTER TABLE (\S+)", sql)
        assert alter_statements.count("source_identity_quarantine") == 1
        assert alter_statements.count("source_identity_resolutions") == 1

    def test_upgrade_creates_usage_events(self):
        """usage_events should have the canonical accounting-event columns."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_events")

        expected_cols = [
            "id",
            "canonical_source_identity_id",
            "source_record_id",
            "client_id",
            "session_id",
            "model_id",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated_cost_usd",
            "reported_at",
            "provider",
            "mode",
            "finish_reason",
            "project_id",
            "workspace_id",
            "agent",
            "parent_session_id",
            "first_ingested_at",
            "last_ingested_at",
            "created_at",
            "updated_at",
        ]
        for col in expected_cols:
            assert col in table_ddl, f"Missing column '{col}' in usage_events"

        assert "uq_usage_events_canonical_source_key" in table_ddl, (
            "Missing unique constraint uq_usage_events_canonical_source_key"
        )

    def test_upgrade_usage_events_foreign_keys(self):
        """usage_events FKs should reference the expected tables."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_events")

        assert "REFERENCES source_identities (id)" in table_ddl, (
            "Missing FK canonical_source_identity_id -> source_identities"
        )
        assert "REFERENCES opencode_clients (id)" in table_ddl, (
            "Missing FK client_id -> opencode_clients"
        )
        assert "REFERENCES sessions (id)" in table_ddl, (
            "Missing FK session_id -> sessions"
        )
        assert "REFERENCES observed_models (id)" in table_ddl, (
            "Missing FK model_id -> observed_models"
        )

    def test_upgrade_creates_usage_ingest_attempts(self):
        """usage_ingest_attempts should record every delivery."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "usage_ingest_attempts")

        expected_cols = [
            "id",
            "usage_event_id",
            "source_identity_id",
            "original_source_record_id",
            "record_jsonb",
            "ingest_batch_id",
            "outcome",
            "replay_id",
            "delivered_at",
            "created_at",
        ]
        for col in expected_cols:
            assert col in table_ddl, f"Missing column '{col}' in usage_ingest_attempts"

        assert "JSONB" in table_ddl.upper(), "record_jsonb should be JSONB"
        assert "REFERENCES usage_events (id)" in table_ddl, (
            "Missing FK usage_event_id -> usage_events"
        )
        assert "REFERENCES source_identities (id)" in table_ddl, (
            "Missing FK source_identity_id -> source_identities"
        )
        assert "REFERENCES ingest_batches (id)" in table_ddl, (
            "Missing FK ingest_batch_id -> ingest_batches"
        )

    def test_upgrade_creates_source_identities(self):
        """source_identities should map collector source IDs to clients."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "source_identities")

        expected_cols = [
            "id",
            "client_id",
            "collector_source_id",
            "is_canonical",
            "canonical_parent_id",
            "resolved_at",
            "created_at",
        ]
        for col in expected_cols:
            assert col in table_ddl, f"Missing column '{col}' in source_identities"

        assert "uq_source_identities_client_source_key" in table_ddl, (
            "Missing unique constraint uq_source_identities_client_source_key"
        )
        assert "REFERENCES opencode_clients (id)" in table_ddl, (
            "Missing FK client_id -> opencode_clients"
        )

    def test_upgrade_creates_source_identity_quarantine(self):
        """source_identity_quarantine should track overlapping identities."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "source_identity_quarantine")

        expected_cols = [
            "id",
            "source_identity_id",
            "overlapping_identity_id",
            "overlap_count",
            "quarantined_at",
            "cleared_at",
            "resolution_id",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in source_identity_quarantine"
            )

    def test_upgrade_creates_source_identity_resolutions(self):
        """source_identity_resolutions should provide an audit trail."""
        sql = self._get_upgrade_sql()
        table_ddl = _extract_table_ddl(sql, "source_identity_resolutions")

        expected_cols = [
            "id",
            "quarantine_id",
            "resolving_identity_id",
            "resolved_by_user_id",
            "reason",
            "resolved_at",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in source_identity_resolutions"
            )

    def test_upgrade_creates_usage_events_indexes(self):
        """usage_events should have the session/reported_at and session/model indexes."""
        sql = self._get_upgrade_sql()

        assert "CREATE INDEX ix_usage_events_session_reported_at" in sql, (
            "Missing index ix_usage_events_session_reported_at"
        )
        assert "CREATE INDEX ix_usage_events_session_model_id" in sql, (
            "Missing index ix_usage_events_session_model_id"
        )

    def test_upgrade_creates_usage_ingest_attempts_indexes(self):
        """usage_ingest_attempts should have its three indexes."""
        sql = self._get_upgrade_sql()

        assert (
            "CREATE INDEX ix_usage_ingest_attempts_source_identity_source_record"
            in sql
        ), "Missing index ix_usage_ingest_attempts_source_identity_source_record"
        assert "CREATE INDEX ix_usage_ingest_attempts_usage_event_id" in sql, (
            "Missing index ix_usage_ingest_attempts_usage_event_id"
        )
        assert "CREATE INDEX ix_usage_ingest_attempts_ingest_batch_id" in sql, (
            "Missing index ix_usage_ingest_attempts_ingest_batch_id"
        )

    def test_upgrade_creates_quarantine_index(self):
        """source_identity_quarantine should have its (identity, cleared) index."""
        sql = self._get_upgrade_sql()

        assert "CREATE INDEX ix_source_identity_quarantine_identity_cleared" in sql, (
            "Missing index ix_source_identity_quarantine_identity_cleared"
        )

    def test_upgrade_adds_circular_foreign_keys_via_alter(self):
        """The circular FK pair should be added with ALTER TABLE ADD CONSTRAINT."""
        sql = self._get_upgrade_sql()

        # Both circular FKs must be added after both tables exist.
        assert (
            "ADD CONSTRAINT fk_source_identity_quarantine_resolution_id_resolutions"
            " FOREIGN KEY(resolution_id) REFERENCES source_identity_resolutions (id)"
            in sql
        ), "Missing circular FK quarantine.resolution_id -> resolutions"
        assert (
            "ADD CONSTRAINT fk_source_identity_resolutions_quarantine_id_quarantine"
            " FOREIGN KEY(quarantine_id) REFERENCES source_identity_quarantine (id)"
            in sql
        ), "Missing circular FK resolutions.quarantine_id -> quarantine"

        # The ALTER statements must come after all five CREATE TABLE statements.
        # Creation order: source_identities, usage_events, usage_ingest_attempts,
        # source_identity_quarantine, source_identity_resolutions.
        positions = [
            sql.find("CREATE TABLE source_identities"),
            sql.find("CREATE TABLE usage_events"),
            sql.find("CREATE TABLE usage_ingest_attempts"),
            sql.find("CREATE TABLE source_identity_quarantine"),
            sql.find("CREATE TABLE source_identity_resolutions"),
            sql.find(
                "ADD CONSTRAINT fk_source_identity_quarantine_resolution_id_resolutions"
            ),
            sql.find(
                "ADD CONSTRAINT fk_source_identity_resolutions_quarantine_id_quarantine"
            ),
        ]
        assert -1 not in positions, "Could not locate all CREATE/ALTER statements"
        assert positions == sorted(positions), (
            "Circular FK ALTER statements must come after all CREATE TABLEs"
        )


class TestMigration0021Downgrade:
    """Verify Alembic migration 0021 downgrade is fully reversible."""

    _TABLE_NAMES = [
        "usage_ingest_attempts",
        "usage_events",
        "source_identity_resolutions",
        "source_identity_quarantine",
        "source_identities",
    ]

    def _get_downgrade_sql(self) -> str:
        """Run downgrade to 0020 in offline mode and return SQL output."""
        return _run_alembic_downgrade_sql("0020")

    def test_downgrade_drops_circular_foreign_keys_first(self):
        """Downgrade should drop the two circular FKs before the tables."""
        sql = self._get_downgrade_sql()

        assert "DROP CONSTRAINT fk_source_identity_quarantine_resolution_id_resolutions" in sql, (
            "Expected DROP CONSTRAINT quarantine.resolution_id FK"
        )
        assert "DROP CONSTRAINT fk_source_identity_resolutions_quarantine_id_quarantine" in sql, (
            "Expected DROP CONSTRAINT resolutions.quarantine_id FK"
        )

    def test_downgrade_drops_all_five_tables(self):
        """Downgrade should emit DROP TABLE for all five tables."""
        sql = self._get_downgrade_sql()

        for table_name in self._TABLE_NAMES:
            assert f"DROP TABLE {table_name}" in sql, (
                f"Expected DROP TABLE {table_name} in downgrade SQL"
            )

    def test_downgrade_drops_tables_in_reverse_dependency_order(self):
        """Children must be dropped before the parents they reference."""
        sql = self._get_downgrade_sql()

        positions = [sql.find(f"DROP TABLE {name}") for name in self._TABLE_NAMES]
        assert -1 not in positions, "Could not locate all DROP TABLE statements"
        assert positions == sorted(positions), (
            "DROP TABLE statements must be emitted in reverse dependency order"
        )
