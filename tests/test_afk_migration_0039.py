"""Migration tests for 0039 — provisional AFK run lifecycle (issue #589).

Mirrors ``test_migration_0031.py``: loads the 0039 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0038 ↔ 0039
up/downgrade deltas offline.  The upgrade must be additive-only (nine
nullable columns) and reversible; the downgrade must restore the 0038 shape
exactly (drop the two partial unique indexes, the lookup index, and the
nine columns).

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

_MIGRATION_FILE = _ALEMBIC_DIR / "versions" / "0039_afk_run_provisional_lifecycle.py"

_LIFECYCLE_COLUMNS = (
    "host",
    "source_event_id",
    "repository",
    "trigger_type",
    "change_request_provider",
    "change_request_repository",
    "change_request_external_id",
    "recovered_from_afk_run_id",
)

_PARTIAL_UNIQUE_INDEXES = (
    "uq_afk_runs_provisioning_key",
    "uq_afk_runs_change_request_identity",
)


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0039 migration module by file path (versions/ is not a package)."""
    spec = importlib.util.spec_from_file_location("afk_lifecycle_migration_0039", _MIGRATION_FILE)
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
            upgrade(cfg, "0038:0039", sql=True)
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
            downgrade(cfg, "0039:0038", sql=True)
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


def test_migration_module_declares_revision_0039() -> None:
    module = _load_migration_module()
    assert module.revision == "0039"
    assert module.down_revision == "0038"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


def test_migration_module_exposes_reversible_upgrade_and_downgrade() -> None:
    """Both upgrade() and downgrade() callables exist (reversible migration)."""
    module = _load_migration_module()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


# ── Offline render — additive columns + partial unique indexes ────────────────


def test_upgrade_adds_lifecycle_columns_to_afk_runs() -> None:
    sql = _render_upgrade_delta_guarded()
    for column in _LIFECYCLE_COLUMNS:
        assert f"ADD COLUMN {column}" in sql, f"Expected ADD COLUMN {column}"
    assert "ALTER TABLE afk_runs" in sql


def test_upgrade_creates_partial_unique_indexes() -> None:
    sql = _render_upgrade_delta_guarded()
    for index in _PARTIAL_UNIQUE_INDEXES:
        assert f"CREATE UNIQUE INDEX {index}" in sql, f"Expected CREATE UNIQUE INDEX {index}"
    # The provisioning key excludes legacy rows (NULL host/source_event_id).
    assert "host IS NOT NULL AND source_event_id IS NOT NULL" in sql
    # The 1:1 lifecycle<->change_request index excludes unbound rows.
    assert "change_request_provider IS NOT NULL" in sql


def test_upgrade_creates_recovery_lookup_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "CREATE INDEX ix_afk_runs_recovered_from" in sql


def test_upgrade_is_additive_only() -> None:
    """The upgrade never drops columns, tables, or constraints."""
    sql = _render_upgrade_delta_guarded()
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
    assert "DROP CONSTRAINT" not in sql


# ── Offline render — downgrade restores the 0038 shape ────────────────────────


def test_downgrade_drops_indexes_and_columns() -> None:
    sql = _render_downgrade_delta_guarded()
    for index in ("ix_afk_runs_recovered_from",) + _PARTIAL_UNIQUE_INDEXES:
        assert f"DROP INDEX {index}" in sql, f"Expected DROP INDEX {index}"
    for column in reversed(_LIFECYCLE_COLUMNS):
        assert f"DROP COLUMN {column}" in sql, f"Expected DROP COLUMN {column}"


# ── ORM mirror ────────────────────────────────────────────────────────────────


def test_orm_model_mirrors_lifecycle_columns() -> None:
    """The AFKRun ORM model mirrors the migration 0039 columns and indexes."""
    from app.db.models.afk import AFKRun

    column_names = {c.name for c in AFKRun.__table__.columns}
    for column in _LIFECYCLE_COLUMNS:
        assert column in column_names, f"ORM model missing column: {column}"

    index_names = {idx.name for idx in AFKRun.__table__.indexes}
    assert "uq_afk_runs_provisioning_key" in index_names
    assert "uq_afk_runs_change_request_identity" in index_names
    assert "ix_afk_runs_recovered_from" in index_names
