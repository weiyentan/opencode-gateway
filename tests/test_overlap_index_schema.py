"""Tests for migrations 0024 and 0025 — batch-overlap lookup indexes."""

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


def test_upgrade_0025_creates_concurrent_attempts_index() -> None:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        upgrade(_config(), "0024:0025", sql=True)

    sql = output.getvalue()
    assert "CREATE INDEX CONCURRENTLY ix_usage_ingest_attempts_original_source_record_id" in sql
    assert "ON usage_ingest_attempts (original_source_record_id)" in sql


def test_downgrade_0025_drops_index_concurrently() -> None:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        downgrade(_config(), "0025:0024", sql=True)

    assert "DROP INDEX CONCURRENTLY ix_usage_ingest_attempts_original_source_record_id" in output.getvalue()
