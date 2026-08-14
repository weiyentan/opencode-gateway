"""Migration tests for 0030 — transcript-table retention indexes.

Mirrors ``test_migration_0029.py``: loads the 0030 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0029 ↔ 0030
up/downgrade deltas offline to confirm the three partial retention indexes
on ``source_created_at_tz`` are created (upgrade) and dropped (downgrade).

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

_RETENTION_INDEXES = (
    "ix_observed_messages_retention",
    "ix_observed_parts_retention",
    "ix_observed_tool_calls_retention",
)


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0030 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0030_add_transcript_retention_indexes.py"
    spec = importlib.util.spec_from_file_location("retention_migration_0030", path)
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
            upgrade(cfg, "0029:0030", sql=True)
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
            downgrade(cfg, "0030:0029", sql=True)
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


def test_migration_module_declares_revision_0030() -> None:
    module = _load_migration_module()
    assert module.revision == "0030"
    assert module.down_revision == "0029"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — three partial retention indexes ──────────────────────────


def test_upgrade_creates_retention_indexes() -> None:
    sql = _render_upgrade_delta_guarded()
    for index in _RETENTION_INDEXES:
        assert index in sql, f"Expected index {index}"
    assert "source_created_at_tz IS NOT NULL" in sql
    for table in ("observed_messages", "observed_parts", "observed_tool_calls"):
        assert table in sql, f"Expected table {table}"


def test_downgrade_drops_retention_indexes() -> None:
    sql = _render_downgrade_delta_guarded()
    for index in _RETENTION_INDEXES:
        assert f"DROP INDEX {index}" in sql, f"Expected DROP INDEX {index}"
