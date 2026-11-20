"""Migration guard tests for the immutable logical execution target (issue #856).

Migration 0051 adds the nullable ``afk_runs.logical_target`` column — the
explicitly selected worker target bound **once** to the canonical
``afk_run_id`` at provisioning time.  These tests guard the two invariants
the ORM metadata must uphold:

* the migration revision chain (0051 follows 0050), and
* the ``AFKRun`` ORM model mirrors the column exactly (``String(100)``,
  nullable) so Alembic autogenerate (``alembic/env.py`` ``target_metadata``)
  never emits a spurious ``DROP COLUMN`` against the migrated shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _ROOT / "alembic" / "versions" / "0051_afk_run_logical_target.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0051", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_follows_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0051"
    assert migration.down_revision == "0050"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_orm_model_mirrors_logical_target() -> None:
    """The ORM mirrors the column so autogenerate never drops it."""
    from app.db.models.afk import AFKRun

    column = AFKRun.__table__.columns["logical_target"]
    assert isinstance(column.type, sa.String)
    assert column.type.length == 100
    assert column.nullable is True
