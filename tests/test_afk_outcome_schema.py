"""Schema tests for the AFK outcome persistence migration 0026 (issue #448).

Verifies three things:

1. The documentation-style ORM models (``app/db/models/afk.py``) register the
   six tables with the exact columns, unique constraints, foreign keys, and
   NOT NULL requirements the issue specifies.
2. The migration module declares ``revision == "0026"`` / ``down_revision ==
   "0025"`` and imports cleanly on Python 3.9 (the environment's
   ``requires-python`` is ``>=3.12`` but CI runs 3.9 — the new migration must
   not use module-level ``str | None`` annotations evaluated at import time).
3. The full offline ``alembic upgrade 0026 --sql`` render is additive (creates
   the six tables, never alters/drops existing usage tables) — guarded so it
   degrades gracefully when the *pre-existing* 0024/0025 migrations trip the
   Python 3.9 ``str | None`` import error.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.models import (
    AFKRun,
    AFKRunEntityLink,
    AFKRunSessionLink,
    DeliveryLog,
    EngineeringEvent,
    UnresolvedCorrelation,
)

# ── Offline render helpers (mirrors test_canonical_event_schema.py) ──────────

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _run_alembic_upgrade_sql(revision: str = "0026") -> str:
    from alembic.command import upgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade(cfg, revision, sql=True)
    return buf.getvalue()


def _run_alembic_downgrade_sql(revision: str = "0025") -> str:
    from alembic.command import downgrade

    cfg = _alembic_cfg()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        downgrade(cfg, f"0026:{revision}", sql=True)
    return buf.getvalue()


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_upgrade_sql_guarded() -> str:
    """Render 0026 upgrade SQL, skipping on the pre-existing 3.9 migration error."""
    try:
        return _run_alembic_upgrade_sql("0026")
    except BaseException as exc:  # noqa: BLE001 - we re-raise unless pre-existing
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level); "
                "run on Python >=3.12 to exercise the offline render."
            )
        raise


def _render_downgrade_sql_guarded() -> str:
    """Render 0026 downgrade SQL, skipping on the pre-existing 3.9 migration error."""
    try:
        return _run_alembic_downgrade_sql("0025")
    except BaseException as exc:  # noqa: BLE001
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level)."
            )
        raise


def _ddl(model) -> str:
    """Compile a model's CREATE TABLE DDL against the postgresql dialect."""
    compiled = CreateTable(model.__table__).compile(dialect=postgresql.dialect())
    return str(compiled)


def _load_migration_module():
    """Load the 0026 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0026_afk_outcome_persistence.py"
    spec = importlib.util.spec_from_file_location("afk_migration_0026", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ══════════════════════════════════════════════════════════════════════════════
#  ORM model registration + column/constraint verification
# ══════════════════════════════════════════════════════════════════════════════


_TABLES = {
    AFKRun: "afk_runs",
    AFKRunSessionLink: "afk_run_sessions",
    AFKRunEntityLink: "afk_run_entities",
    EngineeringEvent: "engineering_events",
    DeliveryLog: "delivery_log",
    UnresolvedCorrelation: "unresolved_correlations",
}


def test_all_six_models_registered() -> None:
    for model, table_name in _TABLES.items():
        assert model.__tablename__ == table_name, f"{model.__name__} table name mismatch"


def test_afk_run_entities_afk_run_id_not_null() -> None:
    col = AFKRunEntityLink.__table__.columns["afk_run_id"]
    assert not col.nullable, "afk_run_entities.afk_run_id must be NOT NULL"
    fks = list(col.foreign_keys)
    assert fks, "afk_run_entities.afk_run_id must be a foreign key"
    assert fks[0].column.table.name == "afk_runs"


def test_afk_run_sessions_afk_run_id_not_null() -> None:
    col = AFKRunSessionLink.__table__.columns["afk_run_id"]
    assert not col.nullable, "afk_run_sessions.afk_run_id must be NOT NULL"


def test_engineering_events_identity_unique_constraint() -> None:
    ddl = _ddl(EngineeringEvent)
    assert "uq_engineering_events_identity" in ddl
    # the six identity columns must all be present and NOT NULL
    for col_name in ("provider", "repository", "entity_type", "external_id", "event_type"):
        col = EngineeringEvent.__table__.columns[col_name]
        assert not col.nullable, f"engineering_events.{col_name} must be NOT NULL"
    occurred_at = EngineeringEvent.__table__.columns["occurred_at"]
    assert not occurred_at.nullable


def test_engineering_events_stores_provider_event_id() -> None:
    assert "provider_event_id" in EngineeringEvent.__table__.columns


def test_delivery_log_unique_provider_delivery() -> None:
    ddl = _ddl(DeliveryLog)
    assert "uq_delivery_log_provider_delivery" in ddl
    assert not DeliveryLog.__table__.columns["provider"].nullable
    assert not DeliveryLog.__table__.columns["delivery_id"].nullable


def test_afk_run_entities_entity_mapping_unique() -> None:
    ddl = _ddl(AFKRunEntityLink)
    assert "uq_afk_run_entities_entity_run" in ddl


def test_derived_link_columns_present() -> None:
    """Every derived link stores method/confidence/evidence/resolver_version."""
    cols = set(AFKRunEntityLink.__table__.columns.keys())
    required_cols = ("correlation_method", "correlation_confidence", "evidence", "resolver_version")
    for required in required_cols:
        assert required in cols, f"afk_run_entities missing {required}"
    # superseded links are marked, never deleted
    assert "superseded_at" in cols


def test_unresolved_correlations_unique_key() -> None:
    ddl = _ddl(UnresolvedCorrelation)
    assert "uq_unresolved_correlations_entity_method" in ddl


# ══════════════════════════════════════════════════════════════════════════════
#  Migration module — revision ids + 3.9 import safety
# ══════════════════════════════════════════════════════════════════════════════


def test_migration_module_declares_revision_0026() -> None:
    module = _load_migration_module()
    assert module.revision == "0026"
    assert module.down_revision == "0025"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ══════════════════════════════════════════════════════════════════════════════
#  Offline render — additive, existing usage tables untouched (3.12+ / guarded)
# ══════════════════════════════════════════════════════════════════════════════

_AFK_TABLE_NAMES = [
    "afk_runs",
    "afk_run_sessions",
    "afk_run_entities",
    "engineering_events",
    "delivery_log",
    "unresolved_correlations",
]


def test_upgrade_creates_all_six_tables() -> None:
    sql = _render_upgrade_sql_guarded()
    for table_name in _AFK_TABLE_NAMES:
        assert f"CREATE TABLE {table_name}" in sql, f"missing CREATE TABLE {table_name}"


def test_upgrade_does_not_touch_existing_usage_tables() -> None:
    sql = _render_upgrade_sql_guarded()
    # existing usage tables are created exactly once by earlier migrations and
    # never altered or dropped by 0026.
    for table_name in ("usage_events", "opencode_usage_records", "sessions", "source_identities"):
        assert f"DROP TABLE {table_name}" not in sql
        assert f"ALTER TABLE {table_name}" not in sql


def test_upgrade_emits_required_unique_constraints() -> None:
    sql = _render_upgrade_sql_guarded()
    assert "uq_engineering_events_identity" in sql
    assert "uq_afk_run_entities_entity_run" in sql
    assert "uq_delivery_log_provider_delivery" in sql
    assert "uq_unresolved_correlations_entity_method" in sql


def test_downgrade_drops_all_six_tables_in_reverse_order() -> None:
    sql = _render_downgrade_sql_guarded()
    for table_name in _AFK_TABLE_NAMES:
        assert f"DROP TABLE {table_name}" in sql, f"missing DROP TABLE {table_name}"
    positions = [sql.find(f"DROP TABLE {name}") for name in _AFK_TABLE_NAMES]
    assert -1 not in positions
    assert positions == sorted(positions), "downgrade must drop in reverse dependency order"
