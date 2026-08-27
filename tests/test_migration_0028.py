"""Migration tests for 0028 — the durable AFK backfill job queue.

Mirrors ``test_migration_0027.py``: loads the 0028 migration module by file
path (``alembic/versions`` is not a package), verifies its revision
identifiers and Python 3.9 import safety, and renders the 0027 ↔ 0028
up/downgrade deltas offline to confirm the table, the status CHECK
constraint, the three indexes, and the drop order.

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


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0028 migration module by file path (versions/ is not a package)."""
    path = _ALEMBIC_DIR / "versions" / "0028_backfill_jobs.py"
    spec = importlib.util.spec_from_file_location("afk_migration_0028", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_delta_guarded(target: str, command_name: str) -> str:
    from alembic import command

    command_fn = getattr(command, command_name)
    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            command_fn(cfg, target, sql=True)
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


def test_migration_module_declares_revision_0028() -> None:
    module = _load_migration_module()
    assert module.revision == "0028"
    assert module.down_revision == "0027"


def test_migration_module_imports_on_py39() -> None:
    """Importing the new migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


# ── Offline render — table + constraint + indexes ─────────────────────────────


def test_upgrade_delta_creates_job_table_and_indexes() -> None:
    sql = _render_delta_guarded("0027:0028", "upgrade")
    assert "CREATE TABLE afk_backfill_jobs" in sql
    assert "ck_afk_backfill_jobs_status" in sql
    assert "'queued'" in sql and "'cancelled'" in sql
    assert "ix_afk_backfill_jobs_status_created" in sql
    assert "ix_afk_backfill_jobs_repo_status_created" in sql
    assert "ix_afk_backfill_jobs_completed_at" in sql
    assert "completed_at IS NOT NULL" in sql


def test_upgrade_delta_never_touches_existing_tables() -> None:
    sql = _render_delta_guarded("0027:0028", "upgrade")
    assert "ALTER TABLE sessions" not in sql
    assert "ALTER TABLE afk_runs" not in sql
    assert "ALTER TABLE usage_events" not in sql


def test_downgrade_delta_drops_table_and_indexes() -> None:
    sql = _render_delta_guarded("0028:0027", "downgrade")
    assert "DROP TABLE afk_backfill_jobs" in sql
    assert "DROP INDEX ix_afk_backfill_jobs_completed_at" in sql
    assert "DROP INDEX ix_afk_backfill_jobs_repo_status_created" in sql
    assert "DROP INDEX ix_afk_backfill_jobs_status_created" in sql
