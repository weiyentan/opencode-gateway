"""Tests for OpenCode source-fact projection tables — ORM models and migration 0015.

Verifies that:
1. ORM model classes exist with correct table names and columns.
2. Alembic migration 0015 creates all four tables with the expected
   columns, constraints, and foreign keys.
3. The downgrade drops all four tables.
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


def _run_alembic_upgrade_sql(revision: str = "0015") -> str:
    """Run ``alembic upgrade <revision> --sql`` and return the SQL string."""
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, revision, sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0014") -> str:
    """Run ``alembic downgrade 0015:<revision> --sql`` and return the SQL string."""
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0015:{revision}", sql=True)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  ORM Model Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestORMProjectionModels:
    """Verify the ORM model classes are defined correctly."""

    def test_opencode_source_project_model_exists(self):
        """OpenCodeSourceProject model should exist with correct table name."""
        from app.db.models.projection import OpenCodeSourceProject

        assert OpenCodeSourceProject.__tablename__ == "opencode_source_projects"

    def test_opencode_session_context_model_exists(self):
        """OpenCodeSessionContext model should exist with correct table name."""
        from app.db.models.projection import OpenCodeSessionContext

        assert OpenCodeSessionContext.__tablename__ == "opencode_session_contexts"

    def test_opencode_project_directory_model_exists(self):
        """OpenCodeProjectDirectory model should exist with correct table name."""
        from app.db.models.projection import OpenCodeProjectDirectory

        assert OpenCodeProjectDirectory.__tablename__ == "opencode_project_directories"

    def test_opencode_session_todo_model_exists(self):
        """OpenCodeSessionTodo model should exist with correct table name."""
        from app.db.models.projection import OpenCodeSessionTodo

        assert OpenCodeSessionTodo.__tablename__ == "opencode_session_todos"

    def test_all_models_registered_in_init(self):
        """All four projection models should be importable from app.db.models."""
        from app.db.models import (
            OpenCodeProjectDirectory,
            OpenCodeSessionContext,
            OpenCodeSessionTodo,
            OpenCodeSourceProject,
        )

        assert OpenCodeSourceProject.__tablename__ == "opencode_source_projects"
        assert OpenCodeSessionContext.__tablename__ == "opencode_session_contexts"
        assert OpenCodeProjectDirectory.__tablename__ == "opencode_project_directories"
        assert OpenCodeSessionTodo.__tablename__ == "opencode_session_todos"

    def test_models_have_uuid_primary_key(self):
        """Each model should have a UUID primary key column 'id'."""
        from sqlalchemy.orm import class_mapper

        from app.db.models.projection import (
            OpenCodeProjectDirectory,
            OpenCodeSessionContext,
            OpenCodeSessionTodo,
            OpenCodeSourceProject,
        )

        for model in [
            OpenCodeSourceProject,
            OpenCodeSessionContext,
            OpenCodeProjectDirectory,
            OpenCodeSessionTodo,
        ]:
            mapper = class_mapper(model)
            pk_cols = [c.name for c in mapper.primary_key]
            assert "id" in pk_cols, f"{model.__name__} missing 'id' PK column"

    def test_models_have_client_id_and_source_database_id(self):
        """Each projection model should have client_id and source_database_id columns."""
        from app.db.models.projection import (
            OpenCodeProjectDirectory,
            OpenCodeSessionContext,
            OpenCodeSessionTodo,
            OpenCodeSourceProject,
        )

        for model in [
            OpenCodeSourceProject,
            OpenCodeSessionContext,
            OpenCodeProjectDirectory,
            OpenCodeSessionTodo,
        ]:
            cols = [c.name for c in model.__table__.columns]
            assert "client_id" in cols, f"{model.__name__} missing client_id"
            assert "source_database_id" in cols, (
                f"{model.__name__} missing source_database_id"
            )

    def test_models_have_first_seen_at_and_last_seen_at(self):
        """Each projection model should have first_seen_at and last_seen_at columns."""
        from app.db.models.projection import (
            OpenCodeProjectDirectory,
            OpenCodeSessionContext,
            OpenCodeSessionTodo,
            OpenCodeSourceProject,
        )

        for model in [
            OpenCodeSourceProject,
            OpenCodeSessionContext,
            OpenCodeProjectDirectory,
            OpenCodeSessionTodo,
        ]:
            cols = [c.name for c in model.__table__.columns]
            assert "first_seen_at" in cols, f"{model.__name__} missing first_seen_at"
            assert "last_seen_at" in cols, f"{model.__name__} missing last_seen_at"

    def test_models_have_source_payload_jsonb(self):
        """Each projection model should have a source_payload JSONB column."""
        from app.db.models.projection import (
            OpenCodeProjectDirectory,
            OpenCodeSessionContext,
            OpenCodeSessionTodo,
            OpenCodeSourceProject,
        )

        for model in [
            OpenCodeSourceProject,
            OpenCodeSessionContext,
            OpenCodeProjectDirectory,
            OpenCodeSessionTodo,
        ]:
            col = model.__table__.columns.get("source_payload")
            assert col is not None, f"{model.__name__} missing source_payload"
            assert "JSONB" in str(col.type).upper(), (
                f"{model.__name__}.source_payload should be JSONB"
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Migration — Offline SQL Verification
# ══════════════════════════════════════════════════════════════════════════════


class TestMigration0015Upgrade:
    """Verify Alembic migration 0015 upgrade creates all four tables."""

    _TABLE_NAMES = [
        "opencode_source_projects",
        "opencode_session_contexts",
        "opencode_project_directories",
        "opencode_session_todos",
    ]

    def _get_upgrade_sql(self) -> str:
        """Run migration 0015 in offline mode and return SQL output."""
        return _run_alembic_upgrade_sql("0015")

    def test_upgrade_creates_all_four_tables(self):
        """Upgrade should emit CREATE TABLE for all four projection tables."""
        sql = self._get_upgrade_sql()

        for table_name in self._TABLE_NAMES:
            assert f"CREATE TABLE {table_name}" in sql, (
                f"Expected CREATE TABLE {table_name} in upgrade SQL"
            )

    def test_upgrade_creates_opencode_source_projects(self):
        """opencode_source_projects should have expected columns."""
        sql = self._get_upgrade_sql()
        table_start = sql.find("CREATE TABLE opencode_source_projects")
        assert table_start != -1, "Missing CREATE TABLE opencode_source_projects"

        # Extract the DDL block for this table
        table_ddl = self._extract_table_ddl(sql, "opencode_source_projects")

        expected_cols = [
            "id",
            "client_id",
            "source_database_id",
            "external_project_id",
            "source_project_id",
            "worktree",
            "vcs",
            "sandboxes",
            "name",
            "display_name",
            "icon",
            "icon_color",
            "raw_commands",
            "parsed_commands",
            "source_created_at",
            "source_updated_at",
            "source_created_at_tz",
            "source_updated_at_tz",
            "first_seen_at",
            "last_seen_at",
            "source_payload",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in opencode_source_projects"
            )

        # Verify unique constraint
        assert "uq_opencode_source_projects_source_key" in table_ddl, (
            "Missing unique constraint name"
        )
        assert "source_database_id" in table_ddl, "Missing FK source_database_id"
        assert "ondelete CASCADE" in table_ddl.replace("\n", " ").lower() or (
            "on delete cascade" in table_ddl.lower()
        ), "Missing CASCADE on source_database_id FK"

    def test_upgrade_creates_opencode_session_contexts(self):
        """opencode_session_contexts should have expected columns."""
        sql = self._get_upgrade_sql()
        table_ddl = self._extract_table_ddl(sql, "opencode_session_contexts")

        expected_cols = [
            "id",
            "client_id",
            "source_database_id",
            "external_session_id",
            "session_id",
            "parent_external_session_id",
            "parent_session_id",
            "external_project_id",
            "source_project_id",
            "source_directory",
            "source_path",
            "title",
            "slug",
            "version",
            "session_model",
            "session_cost",
            "source_input_tokens",
            "source_output_tokens",
            "source_cached_tokens",
            "source_reasoning_tokens",
            "code_change_count",
            "code_change_additions",
            "code_change_deletions",
            "source_created_at",
            "source_updated_at",
            "source_started_at",
            "source_completed_at",
            "source_created_at_tz",
            "source_updated_at_tz",
            "source_started_at_tz",
            "source_completed_at_tz",
            "first_seen_at",
            "last_seen_at",
            "source_payload",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in opencode_session_contexts"
            )

        assert "uq_opencode_session_contexts_source_key" in table_ddl, (
            "Missing unique constraint name"
        )

    def test_upgrade_creates_opencode_project_directories(self):
        """opencode_project_directories should have expected columns."""
        sql = self._get_upgrade_sql()
        table_ddl = self._extract_table_ddl(sql, "opencode_project_directories")

        expected_cols = [
            "id",
            "client_id",
            "source_database_id",
            "directory",
            "directory_type",
            "strategy",
            "source_created_at",
            "source_updated_at",
            "source_created_at_tz",
            "source_updated_at_tz",
            "first_seen_at",
            "last_seen_at",
            "source_payload",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in opencode_project_directories"
            )

        assert "uq_opencode_project_directories_source_key" in table_ddl, (
            "Missing unique constraint name"
        )

    def test_upgrade_creates_opencode_session_todos(self):
        """opencode_session_todos should have expected columns."""
        sql = self._get_upgrade_sql()
        table_ddl = self._extract_table_ddl(sql, "opencode_session_todos")

        expected_cols = [
            "id",
            "client_id",
            "source_database_id",
            "external_session_id",
            "session_id",
            "position",
            "content",
            "status",
            "priority",
            "content_hash",
            "source_created_at",
            "source_updated_at",
            "source_created_at_tz",
            "source_updated_at_tz",
            "first_seen_at",
            "last_seen_at",
            "source_payload",
        ]
        for col in expected_cols:
            assert col in table_ddl, (
                f"Missing column '{col}' in opencode_session_todos"
            )

        assert "uq_opencode_session_todos_source_key" in table_ddl, (
            "Missing unique constraint name"
        )

    @staticmethod
    def _extract_table_ddl(sql: str, table_name: str) -> str:
        """Extract the DDL block for a given CREATE TABLE statement."""
        pattern = rf"CREATE TABLE {table_name}\s*\((.*)\);"
        match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
        # Fall back to returning the relevant section
        start = sql.find(f"CREATE TABLE {table_name}")
        if start == -1:
            return ""
        end = sql.find(";", start)
        return sql[start : end + 1] if end != -1 else sql[start:]


class TestMigration0015Downgrade:
    """Verify Alembic migration 0015 downgrade drops all four tables."""

    _TABLE_NAMES = [
        "opencode_session_todos",
        "opencode_project_directories",
        "opencode_session_contexts",
        "opencode_source_projects",
    ]

    def _get_downgrade_sql(self) -> str:
        """Run downgrade to 0014 in offline mode and return SQL output."""
        return _run_alembic_downgrade_sql("0014")

    def test_downgrade_drops_all_four_tables(self):
        """Downgrade should emit DROP TABLE for all four projection tables."""
        sql = self._get_downgrade_sql()

        for table_name in self._TABLE_NAMES:
            assert f"DROP TABLE {table_name}" in sql, (
                f"Expected DROP TABLE {table_name} in downgrade SQL"
            )
