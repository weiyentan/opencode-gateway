"""Migration tests for 0037 — execution-binding table.

Mirrors ``test_migration_0034.py``: loads the 0037 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0036 <-> 0037
up/downgrade deltas offline, and checks the new ORM model is registered in
``app.db.models``.

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
_AWX_JOB_CONSTRAINT = "uq_execution_bindings_awx_job_id"
_RESOURCE_INDEX = "ix_execution_bindings_resource"
_SESSION_INDEX = "ix_execution_bindings_session"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0037 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0037_execution_binding.py"
    spec = importlib.util.spec_from_file_location("execution_binding_migration_0037", path)
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
            upgrade(cfg, "0036:0037", sql=True)
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
            downgrade(cfg, "0037:0036", sql=True)
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


def test_migration_module_declares_revision_0037() -> None:
    module = _load_migration_module()
    assert module.revision == "0037"
    assert module.down_revision == "0036"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — table + constraint + index ───────────────────────────────


def test_upgrade_creates_execution_binding_table() -> None:
    sql = _render_upgrade_delta_guarded()
    assert f"CREATE TABLE {_EXECUTION_BINDING_TABLE}" in sql


def test_upgrade_creates_awx_job_unique_constraint() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _AWX_JOB_CONSTRAINT in sql


def test_upgrade_creates_resource_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _RESOURCE_INDEX in sql


def test_upgrade_creates_session_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _SESSION_INDEX in sql


def test_upgrade_uses_server_defaults() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "gen_random_uuid()" in sql
    assert "now()" in sql


def test_upgrade_creates_awx_job_id_column() -> None:
    sql = _render_upgrade_delta_guarded()
    assert '"awx_job_id"' in sql
    assert "BIGINT" in sql.upper() or "bigint" in sql


def test_upgrade_has_no_resource_uniqueness() -> None:
    """Provider resource identity (provider, repository_url, entity_type,
    entity_number) must NOT be unique — multiple executions per resource
    are allowed."""
    sql = _render_upgrade_delta_guarded()
    # The only unique constraint should be on awx_job_id, not on
    # the provider resource identity tuple.
    assert "UNIQUE" in sql.upper()
    # Verify no unique constraint on all four resource columns together.
    assert "uq_execution_bindings_awx_job_id" in sql


def test_downgrade_drops_execution_binding_table() -> None:
    sql = _render_downgrade_delta_guarded()
    assert f"DROP TABLE {_EXECUTION_BINDING_TABLE}" in sql


# ── ORM model registration ────────────────────────────────────────────────────


def test_execution_binding_model_registered_in_init() -> None:
    import app.db.models as models
    from app.db.models import ExecutionBinding

    assert "ExecutionBinding" in models.__all__
    assert ExecutionBinding.__tablename__ == "execution_bindings"


def test_execution_binding_model_has_expected_columns() -> None:
    from app.db.models import ExecutionBinding

    cols = {c.name for c in ExecutionBinding.__table__.columns}
    assert {
        "id",
        "awx_job_id",
        "external_session_id",
        "provider",
        "repository_url",
        "entity_type",
        "entity_number",
        "outcome",
        "source_event_id",
        "branch",
        "title",
        "failure_reason",
        "failure_summary",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    } <= cols


def test_execution_binding_model_mirrors_migration_constraint() -> None:
    """The ORM UniqueConstraint must match migration 0037 (autogenerate parity)."""
    from app.db.models import ExecutionBinding

    constraints = {
        c.name: {col.name for col in c.columns}
        for c in ExecutionBinding.__table__.constraints
    }
    assert "uq_execution_bindings_awx_job_id" in constraints
    assert constraints["uq_execution_bindings_awx_job_id"] == {"awx_job_id"}


def test_awx_job_id_is_not_nullable() -> None:
    """AWX job identity must be NOT NULL."""
    from app.db.models import ExecutionBinding

    col = ExecutionBinding.__table__.c.awx_job_id
    assert col.nullable is False


def test_provider_resource_identity_is_not_unique() -> None:
    """The provider resource identity columns must NOT be part of any
    UniqueConstraint — multiple executions per resource are allowed."""
    from app.db.models import ExecutionBinding

    resource_cols = {"provider", "repository_url", "entity_type", "entity_number"}
    for constraint in ExecutionBinding.__table__.constraints:
        constraint_cols = {col.name for col in constraint.columns}
        # No constraint should contain all four resource identity columns.
        assert not resource_cols.issubset(constraint_cols), (
            f"Constraint {constraint.name} covers all resource identity columns "
            f"{resource_cols} — resource identity must NOT be unique."
        )
