"""Migration guard tests for the original AFK trigger actor (issue #859).

Migration 0052 adds the nullable ``afk_runs.actor`` column — the
provider-qualified username of the account that originally triggered the
AFK run, bound **once** at provisioning time.  These tests guard the two
invariants the ORM metadata must uphold:

* the migration revision chain (0052 follows 0051), and
* the ``AFKRun`` ORM model mirrors the column exactly (``String(255)``,
  nullable) so Alembic autogenerate (``alembic/env.py`` ``target_metadata``)
  never emits a spurious ``DROP COLUMN`` against the migrated shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0052_afk_run_actor.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0052", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_follows_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0052"
    assert migration.down_revision == "0051"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_orm_model_mirrors_actor() -> None:
    """The ORM mirrors the column so autogenerate never drops it."""
    from app.db.models.afk import AFKRun

    column = AFKRun.__table__.columns["actor"]
    assert isinstance(column.type, sa.String)
    assert column.type.length == 255
    assert column.nullable is True
