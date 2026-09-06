"""Migration tests for the AFK-run execution binding lookup index."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

from alembic.command import downgrade, upgrade
from alembic.config import Config


ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = ROOT / "alembic"
MIGRATION = ALEMBIC_DIR / "versions" / "0044_add_execution_bindings_afk_run_lookup_index.py"


def _module():
    spec = importlib.util.spec_from_file_location("execution_binding_migration_0044", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return config


def _render(command, target: str) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command(_config(), target, sql=True)
    return output.getvalue()


def test_migration_revision_chain() -> None:
    module = _module()
    assert module.revision == "0044"
    assert module.down_revision == "0043"


def test_upgrade_creates_ordered_afk_run_lookup_index() -> None:
    sql = _render(upgrade, "0043:0044")
    assert (
        "CREATE INDEX ix_execution_bindings_afk_run_created ON execution_bindings "
        "(afk_run_id, created_at, id)" in sql
    )


def test_downgrade_drops_lookup_index() -> None:
    sql = _render(downgrade, "0044:0043")
    assert "DROP INDEX ix_execution_bindings_afk_run_created" in sql
