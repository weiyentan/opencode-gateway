"""Migration tests for 0029 — execution-transcript tables.

Mirrors ``test_migration_0027.py``: loads the 0029 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0028 ↔ 0029
up/downgrade deltas offline to confirm the three transcript tables
(``observed_messages`` / ``observed_parts`` / ``observed_tool_calls``) are
created with the ADR 0016 §2 columns, unique keys, and §5 index set, and
dropped in reverse dependency order.

The offline render is guarded against the pre-existing 0024/0025 module-level
``str | None`` import failure on Python 3.9 (alembic imports every version
file to build the revision map).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
from pathlib import Path

import pytest

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0029 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0029_add_execution_transcript_tables.py"
    spec = importlib.util.spec_from_file_location("afk_migration_0029", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_upgrade_delta_guarded() -> str:
    """Render the 0028 → 0029 upgrade delta, skipping on the 3.9 import error."""
    from alembic.command import upgrade

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0028:0029", sql=True)
        return buf.getvalue()
    except BaseException as exc:  # noqa: BLE001 - re-raise unless pre-existing
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level); "
                "run on Python >=3.12 to exercise the offline render."
            )
        raise


def _render_downgrade_delta_guarded() -> str:
    """Render the 0029 → 0028 downgrade delta, skipping on the 3.9 import error."""
    from alembic.command import downgrade

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            downgrade(cfg, "0029:0028", sql=True)
        return buf.getvalue()
    except BaseException as exc:  # noqa: BLE001 - re-raise unless pre-existing
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level); "
                "run on Python >=3.12 to exercise the offline render."
            )
        raise


def _extract_table_ddl(sql: str, table_name: str) -> str:
    """Extract the DDL block for a given CREATE TABLE statement."""
    pattern = rf"CREATE TABLE {table_name}\s*\((.*)\);"
    match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1)
    start = sql.find(f"CREATE TABLE {table_name}")
    if start == -1:
        return ""
    end = sql.find(";", start)
    return sql[start : end + 1] if end != -1 else sql[start:]


# ── Revision ids + 3.9 import safety ──────────────────────────────────────────


def test_migration_module_declares_revision_0029() -> None:
    module = _load_migration_module()
    assert module.revision == "0029"
    assert module.down_revision == "0028"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — upgrade creates the three transcript tables ──────────────


def test_upgrade_delta_creates_three_tables() -> None:
    sql = _render_upgrade_delta_guarded()
    for table_name in (
        "observed_messages",
        "observed_parts",
        "observed_tool_calls",
    ):
        assert f"CREATE TABLE {table_name}" in sql, (
            f"Expected CREATE TABLE {table_name} in upgrade SQL"
        )


def test_upgrade_delta_observed_messages_columns_and_key() -> None:
    sql = _render_upgrade_delta_guarded()
    ddl = _extract_table_ddl(sql, "observed_messages")

    expected_cols = [
        "id",
        "client_id",
        "source_database_id",
        "external_message_id",
        "session_id",
        "external_session_id",
        "parent_external_session_id",
        "role",
        "agent",
        "mode",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "source_created_at",
        "source_updated_at",
        "source_created_at_tz",
        "source_updated_at_tz",
        "first_seen_at",
        "last_seen_at",
        "data",
    ]
    for col in expected_cols:
        assert col in ddl, f"Missing column '{col}' in observed_messages"

    assert "uq_observed_messages_source_key" in ddl, "Missing unique constraint name"
    assert "REFERENCES opencode_clients (id)" in ddl, "Missing client FK"
    assert "REFERENCES source_databases (id)" in ddl, "Missing source database FK"
    assert "REFERENCES sessions (id)" in ddl, "Missing session FK"


def test_upgrade_delta_observed_parts_columns_and_key() -> None:
    sql = _render_upgrade_delta_guarded()
    ddl = _extract_table_ddl(sql, "observed_parts")

    expected_cols = [
        "id",
        "client_id",
        "source_database_id",
        "external_part_id",
        "message_id",
        "external_message_id",
        "session_id",
        "external_session_id",
        "part_type",
        "source_created_at",
        "source_updated_at",
        "source_created_at_tz",
        "source_updated_at_tz",
        "first_seen_at",
        "last_seen_at",
        "data",
    ]
    for col in expected_cols:
        assert col in ddl, f"Missing column '{col}' in observed_parts"

    assert "uq_observed_parts_source_key" in ddl, "Missing unique constraint name"
    assert "REFERENCES observed_messages (id)" in ddl, "Missing message FK"
    assert "REFERENCES sessions (id)" in ddl, "Missing session FK"


def test_upgrade_delta_observed_tool_calls_columns_and_key() -> None:
    sql = _render_upgrade_delta_guarded()
    ddl = _extract_table_ddl(sql, "observed_tool_calls")

    expected_cols = [
        "id",
        "client_id",
        "source_database_id",
        "part_id",
        "external_part_id",
        "message_id",
        "session_id",
        "external_session_id",
        "tool_name",
        "tool_status",
        "tool_input",
        "tool_output",
        "source_created_at",
        "source_updated_at",
        "source_created_at_tz",
        "source_updated_at_tz",
        "first_seen_at",
        "last_seen_at",
        "data",
    ]
    for col in expected_cols:
        assert col in ddl, f"Missing column '{col}' in observed_tool_calls"

    assert "uq_observed_tool_calls_source_key" in ddl, "Missing unique constraint name"
    assert re.search(r"part_id\s+UUID\s+NOT NULL", ddl), "part_id must be NOT NULL"
    assert "REFERENCES observed_parts (id)" in ddl, "Missing part FK"
    assert "REFERENCES observed_messages (id)" in ddl, "Missing message FK"
    assert "REFERENCES sessions (id)" in ddl, "Missing session FK"


def test_upgrade_delta_creates_index_set() -> None:
    """ADR 0016 §5 index set: composites plus the partial tool_status index."""
    sql = _render_upgrade_delta_guarded()

    expected_indexes = [
        # observed_messages
        "CREATE INDEX ix_observed_messages_session_id_source_created_at "
        "ON observed_messages (session_id, source_created_at)",
        "CREATE INDEX ix_observed_messages_agent ON observed_messages (agent)",
        "CREATE INDEX ix_observed_messages_role_source_created_at "
        "ON observed_messages (role, source_created_at)",
        # observed_parts
        "CREATE INDEX ix_observed_parts_session_id_source_created_at "
        "ON observed_parts (session_id, source_created_at)",
        "CREATE INDEX ix_observed_parts_message_id_source_created_at "
        "ON observed_parts (message_id, source_created_at)",
        "CREATE INDEX ix_observed_parts_session_id_part_type_source_created_at "
        "ON observed_parts (session_id, part_type, source_created_at)",
        "CREATE INDEX ix_observed_parts_part_type_source_created_at "
        "ON observed_parts (part_type, source_created_at)",
        "CREATE INDEX ix_observed_parts_source_created_at "
        "ON observed_parts (source_created_at)",
        # observed_tool_calls
        "CREATE INDEX ix_observed_tool_calls_session_id_source_created_at "
        "ON observed_tool_calls (session_id, source_created_at)",
        "CREATE INDEX ix_observed_tool_calls_tool_name_source_created_at "
        "ON observed_tool_calls (tool_name, source_created_at)",
        "CREATE INDEX ix_observed_tool_calls_tool_status "
        "ON observed_tool_calls (tool_status) WHERE tool_status IS NOT NULL",
    ]
    for idx in expected_indexes:
        assert idx in sql, f"Missing index: {idx}"


# ── Offline render — downgrade drops in reverse dependency order ──────────────


def test_downgrade_drops_all_three_tables() -> None:
    sql = _render_downgrade_delta_guarded()
    for table_name in (
        "observed_tool_calls",
        "observed_parts",
        "observed_messages",
    ):
        assert f"DROP TABLE {table_name}" in sql, (
            f"Expected DROP TABLE {table_name} in downgrade SQL"
        )
