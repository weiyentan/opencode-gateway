"""Migration tests for 0032 — timeline parent-session lookup index.

Mirrors ``test_migration_0030.py``: loads the 0032 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0031 ↔ 0032
up/downgrade deltas offline to confirm the partial
``ix_observed_messages_parent_ext`` index on
``(parent_external_session_id, session_id)`` is created (upgrade) and
dropped (downgrade).

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

_PARENT_EXT_INDEX = "ix_observed_messages_parent_ext"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0032 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0032_add_observed_messages_parent_ext_index.py"
    spec = importlib.util.spec_from_file_location("parent_ext_migration_0032", path)
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
            upgrade(cfg, "0031:0032", sql=True)
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
            downgrade(cfg, "0032:0031", sql=True)
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


def test_migration_module_declares_revision_0032() -> None:
    module = _load_migration_module()
    assert module.revision == "0032"
    assert module.down_revision == "0031"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — partial parent-session index ─────────────────────────────


def test_upgrade_creates_parent_ext_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert f"CREATE INDEX CONCURRENTLY {_PARENT_EXT_INDEX}" in sql
    assert _PARENT_EXT_INDEX in sql
    assert "observed_messages" in sql
    assert "parent_external_session_id" in sql
    assert "session_id" in sql
    assert (
        "parent_external_session_id IS NOT NULL AND session_id IS NOT NULL" in sql
    )


def test_downgrade_drops_parent_ext_index() -> None:
    sql = _render_downgrade_delta_guarded()
    assert f"DROP INDEX CONCURRENTLY {_PARENT_EXT_INDEX}" in sql, (
        f"Expected DROP INDEX CONCURRENTLY {_PARENT_EXT_INDEX}"
    )
