"""Migration tests for 0041 — two-phase execution-binding nullable resource.

Mirrors ``test_migration_0037.py``: loads the 0041 migration module by file
path, verifies its revision identifiers and Python 3.9 import safety,
renders the 0040 <-> 0041 up/downgrade deltas offline, and checks the ORM
model mirrors the nullable change-request identity columns.

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

_EXECUTION_BINDING_TABLE = "execution_bindings"
_RESOURCE_COLUMNS = ("provider", "repository_url", "entity_type", "entity_number")


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0041 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0041_two_phase_execution_binding.py"
    spec = importlib.util.spec_from_file_location(
        "execution_binding_migration_0041", path
    )
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
            upgrade(cfg, "0040:0041", sql=True)
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
            downgrade(cfg, "0041:0040", sql=True)
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


def test_migration_module_declares_revision_0041() -> None:
    module = _load_migration_module()
    assert module.revision == "0041"
    assert module.down_revision == "0040"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — additive nullability change ──────────────────────────────


def test_upgrade_drops_not_null_on_resource_columns() -> None:
    sql = _render_upgrade_delta_guarded()
    for column in _RESOURCE_COLUMNS:
        assert (
            f"ALTER TABLE {_EXECUTION_BINDING_TABLE} ALTER COLUMN {column} "
            f"DROP NOT NULL" in sql
        ), f"upgrade must drop NOT NULL on {column}"


def test_upgrade_does_not_touch_other_tables() -> None:
    """The migration is additive — only execution_bindings nullability changes."""
    sql = _render_upgrade_delta_guarded()
    assert sql.count("ALTER TABLE") == len(_RESOURCE_COLUMNS)


def test_downgrade_restores_not_null_on_resource_columns() -> None:
    sql = _render_downgrade_delta_guarded()
    for column in _RESOURCE_COLUMNS:
        assert (
            f"ALTER TABLE {_EXECUTION_BINDING_TABLE} ALTER COLUMN {column} "
            f"SET NOT NULL" in sql
        ), f"downgrade must restore NOT NULL on {column}"


def _downgrade_with_null_count(monkeypatch, count: int):
    """Run the 0041 ``downgrade()`` with a mocked bind reporting ``count`` NULL rows.

    The downgrade counts NULL rows in the four resource-identity columns and
    refuses to proceed when any exist, so it cannot be exercised through the
    offline render (which cannot return a row count).  We drive it directly
    with a fake connection whose ``execute`` returns the desired count.
    """
    module = _load_migration_module()

    class _FakeResult:
        def __init__(self, count: int) -> None:
            self._count = count

        def scalar(self) -> int:
            return self._count

    class _FakeConn:
        def __init__(self, count: int) -> None:
            self._count = count

        def execute(self, *args, **kwargs) -> _FakeResult:
            return _FakeResult(self._count)

    monkeypatch.setattr(module.op, "get_bind", lambda: _FakeConn(count))
    return module


def test_downgrade_refuses_when_null_rows_exist(monkeypatch) -> None:
    """Downgrade raises RuntimeError when resource-less rows exist."""
    module = _downgrade_with_null_count(monkeypatch, count=1)
    with pytest.raises(RuntimeError):
        module.downgrade()


def test_downgrade_succeeds_when_no_null_rows(monkeypatch) -> None:
    """Downgrade restores NOT NULL on each column when no NULL rows exist."""
    module = _downgrade_with_null_count(monkeypatch, count=0)
    alter_calls: list[tuple] = []
    monkeypatch.setattr(
        module.op, "alter_column", lambda *args, **kwargs: alter_calls.append((args, kwargs))
    )
    module.downgrade()
    assert len(alter_calls) == len(_RESOURCE_COLUMNS)
    for (table, column), kwargs in alter_calls:
        assert table == _EXECUTION_BINDING_TABLE
        assert column in _RESOURCE_COLUMNS
        assert kwargs["nullable"] is False


# ── ORM model parity ──────────────────────────────────────────────────────────


def test_orm_resource_columns_nullable() -> None:
    """The ORM must mirror migration 0041 (autogenerate parity)."""
    from app.db.models import ExecutionBinding

    for column in _RESOURCE_COLUMNS:
        col = ExecutionBinding.__table__.c[column]
        assert col.nullable is True, f"ORM column {column} must be nullable"


def test_orm_awx_job_identity_still_not_null() -> None:
    """AWX job identity remains NOT NULL through the two-phase change."""
    from app.db.models import ExecutionBinding

    assert ExecutionBinding.__table__.c.awx_job_id.nullable is False
    assert ExecutionBinding.__table__.c.job_template_id.nullable is False
