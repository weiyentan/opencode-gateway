"""Tests for migration 0024's batch-overlap lookup index."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from alembic.command import downgrade, upgrade
from alembic.config import Config

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return config


def test_upgrade_creates_concurrent_source_record_index() -> None:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        upgrade(_config(), "0023:0024", sql=True)

    sql = output.getvalue()
    assert "CREATE INDEX CONCURRENTLY ix_usage_events_source_record_id" in sql
    assert "ON usage_events (source_record_id)" in sql


def test_downgrade_drops_index_concurrently() -> None:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        downgrade(_config(), "0024:0023", sql=True)

    assert "DROP INDEX CONCURRENTLY ix_usage_events_source_record_id" in output.getvalue()
