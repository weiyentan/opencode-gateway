"""Migration tests for 0027 — run-scoped unresolved-correlation identity.

Mirrors ``test_afk_outcome_schema.py``: loads the 0027 migration module by
file path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0026 ↔ 0027
up/downgrade deltas offline to confirm the constraint swap and the
``afk_run_id`` NOT NULL change.

The offline render is guarded against the pre-existing 0024/0025 module-level
``str | None`` import failure on Python 3.9 (alembic imports every version
file to build the revision map).
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
    """Load the 0027 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0027_unresolved_correlations_run_identity.py"
    spec = importlib.util.spec_from_file_location("afk_migration_0027", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_upgrade_delta_guarded() -> str:
    """Render the 0026 → 0027 upgrade delta, skipping on the 3.9 import error."""
    from alembic.command import upgrade

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0026:0027", sql=True)
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
    """Render the 0027 → 0026 downgrade delta, skipping on the 3.9 import error."""
    from alembic.command import downgrade

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            downgrade(cfg, "0027:0026", sql=True)
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


def test_migration_module_declares_revision_0027() -> None:
    module = _load_migration_module()
    assert module.revision == "0027"
    assert module.down_revision == "0026"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — constraint swap + NOT NULL ───────────────────────────────


def test_upgrade_delta_replaces_constraint_and_not_null() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "DROP CONSTRAINT uq_unresolved_correlations_entity_method" in sql
    assert "ADD CONSTRAINT uq_unresolved_correlations_entity_run_method" in sql
    assert (
        "ALTER TABLE unresolved_correlations ALTER COLUMN afk_run_id SET NOT NULL"
        in sql
    )


def test_downgrade_restores_old_constraint_and_nullability() -> None:
    sql = _render_downgrade_delta_guarded()
    assert "DROP CONSTRAINT uq_unresolved_correlations_entity_run_method" in sql
    assert "ADD CONSTRAINT uq_unresolved_correlations_entity_method" in sql
    assert "DROP NOT NULL" in sql
