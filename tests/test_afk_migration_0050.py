"""Migration guard tests for GitHub case-folded lifecycle ownership."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _ROOT / "alembic" / "versions" / "0050_github_change_request_identity_casefold.py"
)


def test_migration_revision_follows_current_head() -> None:
    spec = importlib.util.spec_from_file_location("migration_0050", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0050"
    assert module.down_revision == "0049"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_detects_collisions_before_creating_index() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    malformed_guard = source.index("malformed historical repository identities")
    collision_guard = source.index("HAVING count(*) > 1")
    fail_closed = source.index("RAISE EXCEPTION", collision_guard)
    create_index = source.index("CREATE UNIQUE INDEX")
    assert malformed_guard < collision_guard < fail_closed < create_index
    assert "lower(regexp_replace(" in source
    assert "rtrim(change_request_repository, '/')" in source
    assert "(\\\\.git)+$" in source
    assert "^([^/]+):(80|443)/" in source
    assert "change_request_provider = 'github'" in source
    assert "no records were changed" in source
    assert "malformed historical repository identities" in source
    assert "UPDATE afk_runs" not in source


def test_orm_model_declares_matching_partial_index() -> None:
    spec = importlib.util.spec_from_file_location("migration_0050", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    from app.db.models.afk import AFKRun

    index = next(
        idx
        for idx in AFKRun.__table__.indexes
        if idx.name == "uq_afk_runs_github_change_request_identity_casefold"
    )
    assert index.unique is True
    assert "change_request_provider = 'github'" in str(index.dialect_options["postgresql"]["where"])
    def normalize_sql(sql: str) -> str:
        return re.sub(r"\s+", "", sql)

    assert normalize_sql(str(index.expressions[0])) == normalize_sql(
        migration._CANONICAL_REPOSITORY
    )
