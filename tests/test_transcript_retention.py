"""Tests for the execution-transcript retention job (issue #470).

Drives ``scripts/retention_transcripts.py`` through its public seams:

* per-table boundary semantics — rows at / over / under the retention edge
  are deleted / retained / retained (strict ``<`` on ``source_created_at_tz``),
  with NULL source timestamps never deleted;
* differing per-table windows (a row of one age can be past one table's
  window yet inside another's);
* idempotent re-runs and empty-table no-ops;
* ``--dry-run`` reporting without deleting;
* bounded batched deletes (no unbounded single transaction) and the
  ``--limit`` total cap;
* accounting/usage table isolation — the job's SQL only ever references
  the three transcript tables and keys retention on ``source_created_at_tz``,
  never ingest time.

The boundary behaviour is exercised through a small in-memory connection
(:class:`FakeTranscriptConn`) that evaluates the job's two SQL shapes
(count query + batched ``DELETE ... IN (SELECT ... LIMIT)``) over rows
keyed by id with a ``source_created_at_tz`` value — so the tests assert
what the job actually deletes, not just what SQL it sends.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.retention_transcripts import (
    DEFAULT_BATCH_SIZE,
    TRANSCRIPT_TABLES,
    RetentionReport,
    _parse_args,
    _windows_from_settings,
    format_report,
    run_retention,
)

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
WINDOWS = {
    "observed_messages": timedelta(days=365),
    "observed_parts": timedelta(days=90),
    "observed_tool_calls": timedelta(days=90),
}
ACCOUNTING_TABLES = (
    "usage_events",
    "client_project_rollup",
    "opencode_usage_records",
    "sessions",
)


class FakeTranscriptConn:
    """In-memory asyncpg-shaped connection evaluating the job's SQL shapes.

    Supports exactly the two statements the retention job issues — the
    per-table count query (``SELECT count(*) ... FROM <table> WHERE
    source_created_at_tz < $1``) and the batched delete (``DELETE FROM
    <table> WHERE id IN (SELECT id ... LIMIT $n)``).  Rows with a ``None``
    source timestamp are never matched (unknown age is retained, never
    deleted).
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, datetime | None]] = {
            table: {} for table in TRANSCRIPT_TABLES
        }
        self.batch_sizes: dict[str, list[int]] = {table: [] for table in TRANSCRIPT_TABLES}

    def add_row(self, table: str, source_created_at_tz: datetime | None) -> str:
        """Insert one row; returns its id."""
        row_id = str(uuid.uuid4())
        self.tables[table][row_id] = source_created_at_tz
        return row_id

    def rows(self, table: str) -> dict[str, datetime | None]:
        """Snapshot of the rows currently stored in ``table``."""
        return dict(self.tables[table])

    async def fetchrow(self, sql: str, cutoff: datetime) -> dict[str, int]:
        """Evaluate the count query for the table named in ``sql``."""
        table = re.search(r"FROM (\w+)", sql).group(1)
        eligible = [
            row_id
            for row_id, tz in self.tables[table].items()
            if tz is not None and tz < cutoff
        ]
        return {"cnt": len(eligible)}

    async def execute(self, sql: str, cutoff: datetime, batch: int) -> str:
        """Evaluate the batched delete for the table named in ``sql``."""
        table = re.search(r"DELETE FROM (\w+)", sql).group(1)
        eligible = [
            row_id
            for row_id, tz in self.tables[table].items()
            if tz is not None and tz < cutoff
        ]
        selected = eligible[:batch]
        for row_id in selected:
            del self.tables[table][row_id]
        self.batch_sizes[table].append(len(selected))
        return f"DELETE {len(selected)}"


# ── Boundary semantics ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rows_over_edge_deleted_at_edge_and_under_retained() -> None:
    """Rows older than the window are deleted; rows exactly at the cutoff
    (strict ``<``) and rows newer than the cutoff are retained — for every
    transcript table."""
    conn = FakeTranscriptConn()
    for table in TRANSCRIPT_TABLES:
        cutoff = NOW - WINDOWS[table]
        conn.add_row(table, cutoff - timedelta(days=1))  # over the edge → deleted
        conn.add_row(table, cutoff)  # exactly at the edge → retained
        conn.add_row(table, cutoff + timedelta(days=1))  # under the edge → retained

    report = await run_retention(conn, windows=WINDOWS, now=NOW)

    for table in TRANSCRIPT_TABLES:
        cutoff = NOW - WINDOWS[table]
        assert report.deleted[table] == 1, table
        remaining = conn.rows(table)
        assert len(remaining) == 2, table
        assert all(tz is not None and tz >= cutoff for tz in remaining.values()), table


@pytest.mark.asyncio
async def test_null_source_timestamp_rows_never_deleted() -> None:
    """Rows without a source timestamp have unknown age and are retained."""
    conn = FakeTranscriptConn()
    conn.add_row("observed_parts", None)
    conn.add_row("observed_parts", NOW - WINDOWS["observed_parts"] - timedelta(days=1))

    report = await run_retention(conn, windows=WINDOWS, now=NOW)

    assert report.deleted["observed_parts"] == 1
    remaining = conn.rows("observed_parts")
    assert len(remaining) == 1
    assert next(iter(remaining.values())) is None


@pytest.mark.asyncio
async def test_differing_per_table_windows() -> None:
    """A row of one age can be past one table's window yet inside another's."""
    conn = FakeTranscriptConn()
    tz = NOW - timedelta(days=200)  # older than parts/tool_calls, newer than messages
    for table in TRANSCRIPT_TABLES:
        conn.add_row(table, tz)

    report = await run_retention(conn, windows=WINDOWS, now=NOW)

    assert report.deleted["observed_messages"] == 0
    assert report.deleted["observed_parts"] == 1
    assert report.deleted["observed_tool_calls"] == 1
    assert len(conn.rows("observed_messages")) == 1
    assert len(conn.rows("observed_parts")) == 0
    assert len(conn.rows("observed_tool_calls")) == 0


# ── Idempotency and no-ops ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rerun_is_idempotent() -> None:
    """Re-running the job deletes nothing further."""
    conn = FakeTranscriptConn()
    for table in TRANSCRIPT_TABLES:
        conn.add_row(table, NOW - WINDOWS[table] - timedelta(days=10))
        conn.add_row(table, NOW + timedelta(days=1))

    first = await run_retention(conn, windows=WINDOWS, now=NOW)
    second = await run_retention(conn, windows=WINDOWS, now=NOW)

    assert first.total == len(TRANSCRIPT_TABLES)
    assert second.total == 0
    for table in TRANSCRIPT_TABLES:
        assert len(conn.rows(table)) == 1


@pytest.mark.asyncio
async def test_empty_tables_are_a_noop() -> None:
    """An empty transcript store reports zero deletions and no errors."""
    conn = FakeTranscriptConn()

    report = await run_retention(conn, windows=WINDOWS, now=NOW)

    assert report.total == 0
    assert report.deleted == {table: 0 for table in TRANSCRIPT_TABLES}


# ── Dry-run, batching, and limits ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_counts_without_deleting() -> None:
    """``dry_run`` reports the would-be per-table counts and deletes nothing."""
    conn = FakeTranscriptConn()
    for table in TRANSCRIPT_TABLES:
        conn.add_row(table, NOW - WINDOWS[table] - timedelta(days=1))

    report = await run_retention(conn, windows=WINDOWS, now=NOW, dry_run=True)

    assert report.dry_run is True
    assert report.total == len(TRANSCRIPT_TABLES)
    for table in TRANSCRIPT_TABLES:
        assert report.deleted[table] == 1
        assert len(conn.rows(table)) == 1  # nothing deleted


@pytest.mark.asyncio
async def test_deletes_are_batched_not_one_unbounded_transaction() -> None:
    """Deletions run in bounded batches; the final partial batch ends the loop."""
    conn = FakeTranscriptConn()
    for _ in range(5):
        conn.add_row("observed_parts", NOW - timedelta(days=100))

    report = await run_retention(conn, windows=WINDOWS, now=NOW, batch_size=2)

    assert report.deleted["observed_parts"] == 5
    assert len(conn.rows("observed_parts")) == 0
    assert conn.batch_sizes["observed_parts"] == [2, 2, 1]


@pytest.mark.asyncio
async def test_limit_caps_total_deletions_across_tables() -> None:
    """``--limit`` caps the total rows deleted across all tables."""
    conn = FakeTranscriptConn()
    for table in TRANSCRIPT_TABLES:
        for _ in range(3):
            conn.add_row(table, NOW - WINDOWS[table] - timedelta(days=1))

    report = await run_retention(conn, windows=WINDOWS, now=NOW, limit=4)

    assert report.total == 4
    remaining = sum(len(conn.rows(table)) for table in TRANSCRIPT_TABLES)
    assert remaining == 9 - 4


# ── Accounting-table isolation ────────────────────────────────────────────────


def test_sql_only_references_transcript_tables() -> None:
    """The job's SQL must never touch usage/accounting tables."""
    from scripts.retention_transcripts import _count_sql, _delete_sql

    sql = " ".join(
        _count_sql(table) + " " + _delete_sql(table) for table in TRANSCRIPT_TABLES
    )
    for table in TRANSCRIPT_TABLES:
        assert table in sql
    for table in ACCOUNTING_TABLES:
        assert table not in sql, f"Retention SQL must not reference {table}"


def test_retention_keyed_on_source_created_at_tz_not_ingest_time() -> None:
    """Both SQL shapes filter on ``source_created_at_tz`` and never on the
    ingest-side timestamps (``first_seen_at`` / ``last_seen_at``)."""
    from scripts.retention_transcripts import _count_sql, _delete_sql

    sql = " ".join(
        _count_sql(table) + " " + _delete_sql(table) for table in TRANSCRIPT_TABLES
    )
    assert sql.count("source_created_at_tz") >= 2 * len(TRANSCRIPT_TABLES)
    assert "first_seen_at" not in sql
    assert "last_seen_at" not in sql
    assert "ingested_at" not in sql


# ── CLI surface ───────────────────────────────────────────────────────────────


def test_parse_args_defaults() -> None:
    args = _parse_args([])
    assert args.dry_run is False
    assert args.limit is None
    assert args.batch_size is None


def test_parse_args_flags() -> None:
    args = _parse_args(["--dry-run", "--limit", "10", "--batch-size", "50"])
    assert args.dry_run is True
    assert args.limit == 10
    assert args.batch_size == 50


def test_parse_args_rejects_invalid_values() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--limit", "0"])
    with pytest.raises(SystemExit):
        _parse_args(["--batch-size", "0"])


def test_parse_delete_count_asyncpg_style() -> None:
    """asyncpg execute() returns 'DELETE n' status strings."""
    from scripts.retention_transcripts import _parse_delete_count

    assert _parse_delete_count("DELETE 5") == 5
    assert _parse_delete_count("DELETE 0") == 0


# ── Settings wiring and report ────────────────────────────────────────────────


def test_windows_from_settings_maps_days() -> None:
    """Per-table windows come from the GATEWAY_ retention settings."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        transcript_retention_messages_days=365,
        transcript_retention_parts_days=90,
        transcript_retention_tool_calls_days=90,
    )
    windows = _windows_from_settings(settings)
    assert windows["observed_messages"] == timedelta(days=365)
    assert windows["observed_parts"] == timedelta(days=90)
    assert windows["observed_tool_calls"] == timedelta(days=90)


def test_format_report_contains_per_table_counts() -> None:
    """The printed report carries per-table counts, cutoffs, and mode."""
    report = RetentionReport(
        now=NOW,
        dry_run=True,
        cutoffs={table: NOW - WINDOWS[table] for table in TRANSCRIPT_TABLES},
        deleted={table: 1 for table in TRANSCRIPT_TABLES},
    )
    text = format_report(report)
    assert "observed_messages" in text
    assert "observed_parts" in text
    assert "observed_tool_calls" in text
    assert "total: 3" in text
    assert "dry-run" in text


def test_default_batch_size_is_positive() -> None:
    assert DEFAULT_BATCH_SIZE >= 1
