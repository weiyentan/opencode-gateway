"""Migration tests for 0035 — fact identity provenance on engineering_events.

Mirrors ``test_migration_0034.py``: loads the 0035 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, renders the 0034 <-> 0035
up/downgrade deltas offline, and pins the migration's self-contained
observation-key derivation against the canonical
``afk_outcomes.models.build_observation_key``.

The offline render is guarded against the pre-existing 0024/0025
module-level ``str | None`` import failure on Python 3.9 (alembic imports
every version file to build the revision map).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"
_MIGRATION_PATH = (
    _ALEMBIC_DIR
    / "versions"
    / "0035_observation_key_observed_via_snapshot_at.py"
)

PLUS_05_30 = timezone(timedelta(hours=5, minutes=30))


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0035 migration module by file path (versions/ is not a package)."""
    spec = importlib.util.spec_from_file_location("observation_key_migration_0035", _MIGRATION_PATH)
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
            upgrade(cfg, "0034:0035", sql=True)
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
            downgrade(cfg, "0035:0034", sql=True)
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


def test_migration_module_declares_revision_0035() -> None:
    module = _load_migration_module()
    assert module.revision == "0035"
    assert module.down_revision == "0034"


def test_migration_module_imports_on_py39() -> None:
    """Importing the migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


def test_migration_source_is_self_contained() -> None:
    """The migration must not import live application code (frozen snapshot)."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from afk_outcomes" not in source
    assert "import afk_outcomes" not in source


# ── Offline render — columns, server default, unique constraint ──────────────


def test_upgrade_adds_provenance_columns() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "observation_key" in sql
    assert "observed_via" in sql
    assert "snapshot_at" in sql


def test_upgrade_sets_webhook_server_default() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "'webhook'" in sql


def test_upgrade_creates_observation_key_unique_constraint() -> None:
    sql = _render_upgrade_delta_guarded()
    assert "uq_engineering_events_observation_key" in sql


def test_downgrade_drops_provenance_columns() -> None:
    sql = _render_downgrade_delta_guarded()
    assert "snapshot_at" in sql
    assert "observed_via" in sql
    assert "observation_key" in sql


# ── Parity: frozen derivation matches the canonical build_observation_key ────


@pytest.mark.parametrize(
    "occurred_at",
    [
        datetime(2026, 8, 1, 10, 30, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 16, 0, 0, tzinfo=PLUS_05_30),
        datetime(2026, 8, 1, 10, 30, 0),
    ],
    ids=["aware-utc", "aware-plus-0530", "naive"],
)
def test_migration_observation_key_matches_canonical(occurred_at) -> None:
    from afk_outcomes.models import build_observation_key

    module = _load_migration_module()
    assert module._observation_key(
        provider="github",
        repository="github.com/owner/repo",
        entity_type="change_request",
        external_id="442",
        event_type="change_request.merged",
        occurred_at=occurred_at,
    ) == build_observation_key(
        provider="github",
        repository="github.com/owner/repo",
        entity_type="change_request",
        external_id="442",
        event_type="change_request.merged",
        occurred_at=occurred_at,
    )
