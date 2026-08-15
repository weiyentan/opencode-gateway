"""Migration tests for 0032 — occurred_at/ingested_at + current aggregates.

Mirrors ``test_migration_0031.py``: loads the 0032 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0031 ↔ 0032
up/downgrade deltas offline, asserts the backfill-before-NOT-NULL ordering,
and checks the new columns/table/constraint and the ORM model registration.

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

_NEW_COLUMNS = ("occurred_at", "ingested_at")
_AGGREGATE_TABLE = "reporting_resource_aggregates"
_AGGREGATE_CONSTRAINT = "uq_reporting_resource_aggregates_identity"
_AGGREGATE_INDEX = "ix_reporting_resource_aggregates_provider_url"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0032 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0032_add_reporting_aggregates.py"
    spec = importlib.util.spec_from_file_location("reporting_migration_0032", path)
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


# ── Offline render — columns, table, constraint, index, ordering ──────────────


def test_upgrade_adds_occurred_and_ingested_at() -> None:
    sql = _render_upgrade_delta_guarded()
    for col in _NEW_COLUMNS:
        assert col in sql, f"Expected column {col} in upgrade SQL"
    assert "reporting_deliveries" in sql
    assert "delivery_state_trails" in sql


def test_upgrade_backfills_before_not_null() -> None:
    """The occurred_at backfill UPDATE must precede the SET NOT NULL alter."""
    sql = _render_upgrade_delta_guarded()
    backfill_idx = sql.find("UPDATE reporting_deliveries")
    not_null_idx = sql.find("SET NOT NULL")
    assert backfill_idx != -1, "expected a backfill UPDATE"
    assert not_null_idx != -1, "expected a SET NOT NULL alter"
    assert backfill_idx < not_null_idx, (
        "backfill must run before the NOT NULL alter"
    )


def test_upgrade_creates_aggregate_table() -> None:
    sql = _render_upgrade_delta_guarded()
    assert f"CREATE TABLE {_AGGREGATE_TABLE}" in sql


def test_upgrade_creates_aggregate_constraint_and_index() -> None:
    sql = _render_upgrade_delta_guarded()
    assert _AGGREGATE_CONSTRAINT in sql
    assert _AGGREGATE_INDEX in sql


def test_downgrade_drops_aggregate_table_and_columns() -> None:
    sql = _render_downgrade_delta_guarded()
    assert f"DROP TABLE {_AGGREGATE_TABLE}" in sql
    for col in _NEW_COLUMNS:
        assert col in sql, f"Expected {col} dropped in downgrade SQL"


# ── ORM model registration ────────────────────────────────────────────────────


def test_aggregate_model_registered_in_init() -> None:
    import app.db.models as models
    from app.db.models import ReportingResourceAggregate

    assert "ReportingResourceAggregate" in models.__all__
    assert ReportingResourceAggregate.__tablename__ == "reporting_resource_aggregates"


def test_reporting_models_have_new_columns() -> None:
    from app.db.models import DeliveryStateTrail, ReportingDelivery

    delivery_cols = {c.name for c in ReportingDelivery.__table__.columns}
    assert {"occurred_at", "ingested_at"} <= delivery_cols

    trail_cols = {c.name for c in DeliveryStateTrail.__table__.columns}
    assert "ingested_at" in trail_cols


def test_aggregate_model_has_expected_columns() -> None:
    from app.db.models import ReportingResourceAggregate

    cols = {c.name for c in ReportingResourceAggregate.__table__.columns}
    assert {
        "id",
        "provider",
        "repository_url",
        "resource_type",
        "resource_number",
        "last_occurred_at",
        "last_delivery_id",
        "last_ingested_at",
        "payload",
        "updated_at",
    } <= cols


def test_aggregate_model_mirrors_migration_server_defaults() -> None:
    """ORM server_defaults must match migration 0032 (autogenerate parity)."""
    from app.db.models import ReportingResourceAggregate

    cols = ReportingResourceAggregate.__table__.columns
    assert str(cols["id"].server_default.arg) == "gen_random_uuid()"
    assert str(cols["last_ingested_at"].server_default.arg) == "now()"
    assert str(cols["updated_at"].server_default.arg) == "now()"
    assert str(cols["payload"].server_default.arg) == "'{}'::jsonb"
