"""Migration tests for 0034 — exact resource<->session association table.

Mirrors ``test_migration_0031.py``: loads the 0034 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0033 <-> 0034
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

_ASSOCIATION_TABLE = "resource_session_associations"
_ASSOCIATION_CONSTRAINT = "uq_resource_session_associations_resource_session"
_ASSOCIATION_INDEX = "ix_resource_session_associations_session"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0034 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0034_exact_resource_session_associations.py"
    spec = importlib.util.spec_from_file_location("association_migration_0034", path)
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
            upgrade(cfg, "0033:0034", sql=True)
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
            downgrade(cfg, "0034:0033", sql=True)
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


def test_migration_module_declares_revision_0034() -> None:
    module = _load_migration_module()
    assert module.revision == "0034"
    assert module.down_revision == "0033"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — table + constraint + index ───────────────────────────────


def test_upgrade_creates_association_table() -> None:
    sql = _render_upgrade_delta_guarded()
    assert f"CREATE TABLE {_ASSOCIATION_TABLE}" in sql


def test_upgrade_creates_unique_constraint() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _ASSOCIATION_CONSTRAINT in sql


def test_upgrade_creates_secondary_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _ASSOCIATION_INDEX in sql


def test_upgrade_uses_server_defaults() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "gen_random_uuid()" in sql
    assert "now()" in sql


def test_downgrade_drops_association_table() -> None:
    sql = _render_downgrade_delta_guarded()
    assert f"DROP TABLE {_ASSOCIATION_TABLE}" in sql


# ── ORM model registration ────────────────────────────────────────────────────


def test_association_model_registered_in_init() -> None:
    import app.db.models as models
    from app.db.models import ResourceSessionAssociation

    assert "ResourceSessionAssociation" in models.__all__
    assert ResourceSessionAssociation.__tablename__ == "resource_session_associations"


def test_association_model_has_expected_columns() -> None:
    from app.db.models import ResourceSessionAssociation

    cols = {c.name for c in ResourceSessionAssociation.__table__.columns}
    assert {
        "id",
        "session_id",
        "external_session_id",
        "provider",
        "repository",
        "resource_type",
        "resource_number",
        "source_reference",
        "resolver_version",
        "first_seen_at",
        "last_seen_at",
    } <= cols


def test_association_model_mirrors_migration_constraint() -> None:
    """The ORM UniqueConstraint must match migration 0034 (autogenerate parity)."""
    from app.db.models import ResourceSessionAssociation

    constraints = {
        c.name: {col.name for col in c.columns}
        for c in ResourceSessionAssociation.__table__.constraints
    }
    assert "uq_resource_session_associations_resource_session" in constraints
    assert constraints["uq_resource_session_associations_resource_session"] == {
        "provider",
        "repository",
        "resource_type",
        "resource_number",
        "external_session_id",
    }
