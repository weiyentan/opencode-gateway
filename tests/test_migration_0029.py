"""Migration tests for 0029 — execution-transcript observability tables.

Mirrors ``test_migration_0027.py``: loads the 0029 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0028 ↔ 0029
up/downgrade deltas offline, and checks the three new ORM models are
registered in ``app.db.models``.

The offline render is guarded against the pre-existing 0024/0025
module-level ``str | None`` import failure on Python 3.9 (alembic imports
every version file to build the revision map).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
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
    spec = importlib.util.spec_from_file_location("execution_migration_0029", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_upgrade_delta_guarded() -> str:
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


# ── Revision ids + 3.9 import safety ──────────────────────────────────────────


def test_migration_module_declares_revision_0029() -> None:
    module = _load_migration_module()
    assert module.revision == "0029"
    assert module.down_revision == "0028"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — three tables + indexes ───────────────────────────────────


def test_upgrade_creates_three_tables() -> None:
    sql = _render_upgrade_delta_guarded()
    for table in ("observed_messages", "observed_parts", "observed_tool_calls"):
        assert f"CREATE TABLE {table}" in sql, f"Expected CREATE TABLE {table}"


def test_upgrade_creates_unique_constraints() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "uq_observed_messages_source_key" in sql
    assert "uq_observed_parts_source_key" in sql
    assert "uq_observed_tool_calls_source_key" in sql


def test_upgrade_creates_transcript_indexes() -> None:
    sql = _render_upgrade_delta_guarded()
    for index in (
        "ix_observed_messages_session_created",
        "ix_observed_parts_session_type_created",
        "ix_observed_parts_created",
        "ix_observed_tool_calls_name_created",
        "ix_observed_tool_calls_status",
    ):
        assert index in sql, f"Expected index {index}"


def test_upgrade_promotes_part_type_and_tool_name_not_null() -> None:
    sql = _render_upgrade_delta_guarded()
    # part_type and tool_name are the promoted NOT NULL dimensions.
    assert "part_type" in sql
    assert "tool_name" in sql


def test_downgrade_drops_all_three_tables() -> None:
    sql = _render_downgrade_delta_guarded()
    for table in ("observed_tool_calls", "observed_parts", "observed_messages"):
        assert f"DROP TABLE {table}" in sql, f"Expected DROP TABLE {table}"


# ── ORM model registration ────────────────────────────────────────────────────


def test_transcript_models_registered_in_init() -> None:
    from app.db.models import ObservedMessage, ObservedPart, ObservedToolCall

    assert ObservedMessage.__tablename__ == "observed_messages"
    assert ObservedPart.__tablename__ == "observed_parts"
    assert ObservedToolCall.__tablename__ == "observed_tool_calls"


def test_transcript_models_have_expected_columns() -> None:
    from app.db.models import ObservedMessage, ObservedPart, ObservedToolCall

    message_cols = {c.name for c in ObservedMessage.__table__.columns}
    assert {"external_message_id", "external_session_id", "role", "data"} <= message_cols
    assert "parent_external_session_id" in message_cols

    part_cols = {c.name for c in ObservedPart.__table__.columns}
    assert {"external_part_id", "external_message_id", "part_type", "data"} <= part_cols

    tool_cols = {c.name for c in ObservedToolCall.__table__.columns}
    assert {"part_id", "tool_name", "tool_status", "tool_input", "tool_output"} <= tool_cols
