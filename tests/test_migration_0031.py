"""Migration tests for 0031 — reporting-ingestion delivery tables.

Mirrors ``test_migration_0029.py``: loads the 0031 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0030 ↔ 0031
up/downgrade deltas offline, and checks the two new ORM models are
registered in ``app.db.models``.

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

_REPORTING_TABLES = ("reporting_deliveries", "delivery_state_trails")
_REPORTING_CONSTRAINTS = (
    "uq_reporting_deliveries_provider_delivery",
    "uq_delivery_state_trails_delivery_state_time",
)
_REPORTING_INDEXES = (
    "ix_reporting_deliveries_received_at",
    "ix_delivery_state_trails_delivery",
)


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0031 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0031_add_reporting_delivery_tables.py"
    spec = importlib.util.spec_from_file_location("reporting_migration_0031", path)
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
            upgrade(cfg, "0030:0031", sql=True)
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
            downgrade(cfg, "0031:0030", sql=True)
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


def test_migration_module_declares_revision_0031() -> None:
    module = _load_migration_module()
    assert module.revision == "0031"
    assert module.down_revision == "0030"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — two tables + constraints + indexes ───────────────────────


def test_upgrade_creates_reporting_tables() -> None:
    sql = _render_upgrade_delta_guarded()
    for table in _REPORTING_TABLES:
        assert f"CREATE TABLE {table}" in sql, f"Expected CREATE TABLE {table}"


def test_upgrade_creates_unique_constraints() -> None:
    sql = _render_upgrade_delta_guarded()
    for constraint in _REPORTING_CONSTRAINTS:
        assert constraint in sql, f"Expected unique constraint {constraint}"


def test_upgrade_creates_secondary_indexes() -> None:
    sql = _render_upgrade_delta_guarded()
    for index in _REPORTING_INDEXES:
        assert index in sql, f"Expected index {index}"


def test_upgrade_uses_server_defaults() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "gen_random_uuid()" in sql
    assert "now()" in sql


def test_downgrade_drops_reporting_tables() -> None:
    sql = _render_downgrade_delta_guarded()
    # reverse dependency order: trail first, then deliveries
    for table in ("delivery_state_trails", "reporting_deliveries"):
        assert f"DROP TABLE {table}" in sql, f"Expected DROP TABLE {table}"


# ── ORM model registration ────────────────────────────────────────────────────


def test_reporting_models_registered_in_init() -> None:
    import app.db.models as models
    from app.db.models import DeliveryStateTrail, ReportingDelivery

    assert "ReportingDelivery" in models.__all__
    assert "DeliveryStateTrail" in models.__all__
    assert ReportingDelivery.__tablename__ == "reporting_deliveries"
    assert DeliveryStateTrail.__tablename__ == "delivery_state_trails"


def test_reporting_models_have_expected_columns() -> None:
    from app.db.models import DeliveryStateTrail, ReportingDelivery

    delivery_cols = {c.name for c in ReportingDelivery.__table__.columns}
    assert {
        "id",
        "provider",
        "delivery_id",
        "event_type",
        "client_id",
        "received_at",
        "payload",
    } <= delivery_cols

    trail_cols = {c.name for c in DeliveryStateTrail.__table__.columns}
    assert {
        "id",
        "provider",
        "delivery_id",
        "state",
        "occurred_at",
        "detail",
        "created_at",
    } <= trail_cols


def test_reporting_models_mirror_migration_server_defaults() -> None:
    """ORM server_defaults must match migration 0031 (autogenerate parity)."""
    from app.db.models import DeliveryStateTrail, ReportingDelivery

    delivery_cols = ReportingDelivery.__table__.columns
    assert str(delivery_cols["id"].server_default.arg) == "gen_random_uuid()"
    assert str(delivery_cols["received_at"].server_default.arg) == "now()"
    assert str(delivery_cols["payload"].server_default.arg) == "'{}'::jsonb"

    trail_cols = DeliveryStateTrail.__table__.columns
    assert str(trail_cols["id"].server_default.arg) == "gen_random_uuid()"
    assert str(trail_cols["created_at"].server_default.arg) == "now()"
