# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the tool-call backfill script (issue #468).

Covers the acceptance criteria from the task contract:

1. ``scripts/backfill_tool_calls.py`` recomputes ``observed_tool_calls``
   from ``observed_parts`` (``part_type = 'tool'``) keyed by
   ``(client_id, source_database_id, external_part_id)``, applying the
   same redaction/truncation caps as live ingest
   (``GATEWAY_TOOL_PAYLOAD_MAX_CHARS``, default 4096).
2. The extraction logic is SHARED with the live ingest path — the script
   imports ``_extract_part_columns`` / ``_truncate_json_value`` from
   ``app.api.ingest`` (function-object identity, ADR 0015
   backfill↔live-equivalence): backfill and live extraction cannot drift.
3. Idempotent: re-running produces no duplicate or divergent rows; the
   upsert preserves ``first_seen_at`` (ADR 0016 idempotent-projection
   convention).
4. Bounded runs: ``--limit`` caps the rows repaired per run (deferred
   rows are counted for a later run); ``--since`` bounds the scan to
   parts first seen after a UTC-offset timestamp.
5. Verification: the run reports a re-extraction diff
   (``--dry-run`` preview / post-run re-verification), flagging missing
   and divergent ``observed_tool_calls`` rows plus stale rows with no
   backing tool part.

``observed_parts.data`` remains the authoritative verbatim store
(ADR 0016 §4): on disagreement the ``observed_tool_calls`` projection is
corrected toward re-extraction — never the reverse.

Tests follow the sibling backfill test pattern
(``tests/test_client_project_rollup_backfill.py``): SQL-content
assertions + AsyncMock connection, with the tool-part scan driven by a
fake async cursor.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.ingest import _extract_part_columns, _truncate_json_value
from app.core.secrets import redact_dict

# ══════════════════════════════════════════════════════════════════════════════
#  AC 2: Shared extraction — the SAME functions as live ingest
# ══════════════════════════════════════════════════════════════════════════════


class TestSharedExtraction:
    """The backfill must reuse the live-ingest extraction functions
    (ADR 0015 equivalence): importing the same function objects is
    equivalence by construction — backfill and live extraction cannot
    drift."""

    def test_backfill_imports_live_extraction_functions(self):
        import scripts.backfill_tool_calls as backfill

        assert backfill._extract_part_columns is _extract_part_columns
        assert backfill._truncate_json_value is _truncate_json_value
        assert backfill.redact_dict is redact_dict

    def test_recompute_follows_live_pipeline_shape(self):
        """redact → extract → truncate, the exact _process_part pipeline."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        tool_name, status, input_, output = _recompute_tool_call(
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"a": 1},
                    "output": "ok",
                },
            },
            tool_payload_max_chars=4096,
        )
        assert tool_name == "bash"
        assert status == "completed"
        assert input_ == {"a": 1}
        assert output == "ok"

    def test_recompute_applies_payload_truncation_cap(self):
        """Oversized tool_input / tool_output are truncated to the same
        GATEWAY_TOOL_PAYLOAD_MAX_CHARS cap live ingest applies."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        _, _, input_, output = _recompute_tool_call(
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "input": {"command": "x" * 5000},
                    "output": "y" * 5000,
                },
            },
            tool_payload_max_chars=100,
        )
        assert input_["truncated"] is True
        assert len(input_["content"]) == 100
        assert output["truncated"] is True
        assert len(output["content"]) == 100

    def test_recompute_redaction_is_idempotent_on_stored_data(self):
        """observed_parts.data is already redacted; re-redacting it must
        be a no-op, so the backfill pipeline is literally identical to
        live ingest (redact → extract → truncate)."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        stored = redact_dict(
            {
                "type": "tool",
                "tool": "bash",
                "state": {"input": {"GITHUB_TOKEN": "ghp_plaintext"}, "output": "ok"},
            }
        )
        assert stored["state"]["input"]["GITHUB_TOKEN"] == "***"
        _, _, input_, _ = _recompute_tool_call(stored, tool_payload_max_chars=4096)
        assert input_ == {"GITHUB_TOKEN": "***"}
        assert "ghp_plaintext" not in str(input_)

    def test_recompute_rejects_truncation_marker_data(self):
        """A part whose data was truncated at ingest (bounded retention)
        cannot be re-extracted — the marker document has no type."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        with pytest.raises(ValueError):
            _recompute_tool_call(
                {"truncated": True, "content": "{"}, tool_payload_max_chars=4096,
            )

    def test_recompute_rejects_non_tool_data(self):
        """Data whose stored type is not 'tool' cannot produce a tool-call
        row — live ingest would never have written one either."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        with pytest.raises(ValueError):
            _recompute_tool_call(
                {"type": "text", "text": "hello"}, tool_payload_max_chars=4096,
            )

    def test_recompute_rejects_missing_tool_name(self):
        from scripts.backfill_tool_calls import _recompute_tool_call

        with pytest.raises(ValueError):
            _recompute_tool_call(
                {"type": "tool", "state": {"status": "completed"}},
                tool_payload_max_chars=4096,
            )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: Scan SQL — tool parts joined to the derived tool-call rows
# ══════════════════════════════════════════════════════════════════════════════


class TestScanSql:
    """The scan reads observed_parts tool parts with their current
    observed_tool_calls projection (LEFT JOIN), in deterministic order."""

    def _scan_sql(self, since: datetime | None = None) -> str:
        from scripts.backfill_tool_calls import _scan_sql

        return _scan_sql(since)

    def test_scan_reads_tool_parts(self):
        sql = self._scan_sql()
        assert "FROM observed_parts" in sql
        assert "part_type = 'tool'" in sql

    def test_scan_joins_tool_calls_on_source_key(self):
        """The JOIN key is the projection's unique key
        (client_id, source_database_id, external_part_id)."""
        sql = self._scan_sql()
        assert "LEFT JOIN observed_tool_calls" in sql
        assert "ON t.client_id = p.client_id" in sql
        assert "AND t.source_database_id = p.source_database_id" in sql
        assert "AND t.external_part_id = p.external_part_id" in sql

    def test_scan_orders_deterministically(self):
        sql = self._scan_sql()
        assert "ORDER BY p.first_seen_at, p.id" in sql

    def test_scan_without_since_has_no_since_clause(self):
        sql = self._scan_sql()
        assert "first_seen_at >=" not in sql

    def test_scan_with_since_bounds_by_first_seen(self):
        """--since bounds the scan to parts first seen at/after the
        boundary (phased backfill by ingest window)."""
        sql = self._scan_sql(datetime(2026, 8, 1, tzinfo=timezone.utc))
        assert "AND p.first_seen_at >= $1" in sql

    def test_scan_exposes_part_data_and_stored_tool_columns(self):
        """The scan must carry the verbatim part data plus the stored
        projection columns for the re-extraction diff."""
        sql = self._scan_sql()
        assert "p.id AS part_id" in sql
        assert "p.data" in sql
        for col in ("t.tool_name", "t.tool_status", "t.tool_input", "t.tool_output"):
            assert col in sql


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Upsert SQL — keyed conflict, first_seen_at preserved
# ══════════════════════════════════════════════════════════════════════════════


class TestUpsertSql:
    """The upsert mirrors live ingest's observed_tool_calls write: keyed
    on (client_id, source_database_id, external_part_id), overwriting
    tool_name, COALESCE-filling the nullable derived columns (no
    erasure), and never touching first_seen_at on conflict."""

    def _upsert_sql(self) -> str:
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        return UPSERT_TOOL_CALL_SQL

    def test_upsert_inserts_into_observed_tool_calls(self):
        sql = self._upsert_sql()
        assert "INSERT INTO observed_tool_calls" in sql

    def test_upsert_conflicts_on_source_key(self):
        sql = self._upsert_sql()
        assert "ON CONFLICT (client_id, source_database_id, external_part_id)" in sql
        assert "DO UPDATE SET" in sql

    def test_upsert_preserves_first_seen_at(self):
        """first_seen_at must not be assigned in the DO UPDATE branch —
        re-running the backfill never resets the first-observation
        timestamp (ADR 0016 idempotent-projection convention)."""
        sql = self._upsert_sql()
        update_branch = sql.split("DO UPDATE SET")[1]
        assert "first_seen_at" in sql
        assert "first_seen_at =" not in update_branch

    def test_upsert_overwrites_tool_name_and_fills_rest(self):
        """tool_name is NOT NULL (always overwritten); the nullable
        derived columns are COALESCE-filled so a re-extraction can never
        erase a stored value — the same fill-absent semantics as live
        ingest."""
        update_branch = self._upsert_sql().split("DO UPDATE SET")[1]
        assert "tool_name = EXCLUDED.tool_name" in update_branch
        assert (
            "tool_status = COALESCE(EXCLUDED.tool_status, observed_tool_calls.tool_status)"
            in update_branch
        )
        assert (
            "tool_input = COALESCE(EXCLUDED.tool_input, observed_tool_calls.tool_input)"
            in update_branch
        )
        assert (
            "tool_output = COALESCE(EXCLUDED.tool_output, observed_tool_calls.tool_output)"
            in update_branch
        )

    def test_upsert_updates_last_seen_at(self):
        update_branch = self._upsert_sql().split("DO UPDATE SET")[1]
        assert "last_seen_at = EXCLUDED.last_seen_at" in update_branch


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: Bounded runs and safe defaults (arg parsing)
# ══════════════════════════════════════════════════════════════════════════════


class TestArgParsing:
    """The script follows the sibling pattern: --dry-run previews,
    --limit caps repairs per phased run, --since bounds the scan."""

    def test_dry_run_default_false(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args([])
        assert args.dry_run is False

    def test_dry_run_flag_true(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_limit_default_none(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args([])
        assert args.limit is None

    def test_limit_flag_value(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args(["--limit", "500"])
        assert args.limit == 500

    def test_since_default_none(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args([])
        assert args.since is None

    def test_since_flag_value(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args(["--since", "2026-08-01T00:00:00+00:00"])
        assert args.since == datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_since_requires_utc_offset(self):
        """A naive timestamp is ambiguous — reject rather than guess."""
        from scripts.backfill_tool_calls import _parse_args

        with pytest.raises(SystemExit):
            _parse_args(["--since", "2026-08-01T00:00:00"])


# ══════════════════════════════════════════════════════════════════════════════
#  Shared test fixtures
# ══════════════════════════════════════════════════════════════════════════════


def _part_row(**overrides) -> dict:
    """A scanned observed_parts row (LEFT JOINed to observed_tool_calls):
    a bash tool part whose stored projection is missing (t.* NULL)."""
    row = {
        "part_id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "source_database_id": uuid.uuid4(),
        "external_part_id": "part_tool_1",
        "message_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "external_session_id": "ses_1",
        "source_created_at": 1755000000000,
        "source_updated_at": 1755000000000,
        "source_created_at_tz": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "source_updated_at_tz": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "first_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "data": {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls"},
                "output": "ok",
            },
        },
        "tool_name": None,
        "tool_status": None,
        "tool_input": None,
        "tool_output": None,
    }
    row.update(overrides)
    return row


class _FakeCursor:
    """Minimal asyncpg-style cursor wrapper over an in-memory row list."""

    def __init__(self, rows):
        self._it = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: Per-row repair behaviour
# ══════════════════════════════════════════════════════════════════════════════


class TestToolCallMatches:
    """A stored projection row matches the re-extraction only when all
    four derived columns agree."""

    def _expected(self):
        from scripts.backfill_tool_calls import _recompute_tool_call

        return _recompute_tool_call(_part_row()["data"], tool_payload_max_chars=4096)

    def test_no_stored_row_never_matches(self):
        """A missing projection row (all t.* NULL) is a disagreement."""
        from scripts.backfill_tool_calls import _tool_call_matches

        assert _tool_call_matches(_part_row(), self._expected()) is False

    def test_identical_row_matches(self):
        from scripts.backfill_tool_calls import _tool_call_matches

        row = _part_row(
            tool_name="bash",
            tool_status="completed",
            tool_input={"command": "ls"},
            tool_output="ok",
        )
        assert _tool_call_matches(row, self._expected()) is True

    def test_divergent_status_does_not_match(self):
        from scripts.backfill_tool_calls import _tool_call_matches

        row = _part_row(
            tool_name="bash",
            tool_status="failed",
            tool_input={"command": "ls"},
            tool_output="ok",
        )
        assert _tool_call_matches(row, self._expected()) is False

    def test_divergent_input_does_not_match(self):
        from scripts.backfill_tool_calls import _tool_call_matches

        row = _part_row(
            tool_name="bash",
            tool_status="completed",
            tool_input={"command": "pwd"},
            tool_output="ok",
        )
        assert _tool_call_matches(row, self._expected()) is False


class TestUpsertHelper:
    """The upsert writes the recomputed values with live-ingest conflict
    semantics: part linkage, truncated payloads, first_seen_at carried
    from the part, last_seen_at refreshed to now."""

    @pytest.mark.asyncio
    async def test_upsert_writes_recomputed_values(self):
        from scripts.backfill_tool_calls import _recompute_tool_call, _upsert_tool_call

        mock_conn = AsyncMock()
        row = _part_row()
        expected = _recompute_tool_call(row["data"], tool_payload_max_chars=4096)
        now = datetime(2026, 8, 2, tzinfo=timezone.utc)

        await _upsert_tool_call(mock_conn, row, expected, now=now)

        sql, *params = mock_conn.execute.call_args[0]
        assert "INSERT INTO observed_tool_calls" in sql
        assert params[0] != row["part_id"]  # fresh id
        assert params[1] == row["client_id"]
        assert params[2] == row["source_database_id"]
        assert params[3] == row["part_id"]  # NOT NULL part linkage
        assert params[4] == row["external_part_id"]
        assert params[5] == row["message_id"]
        assert params[6] == row["session_id"]
        assert params[7] == row["external_session_id"]
        assert params[8] == "bash"  # tool_name
        assert params[9] == "completed"  # tool_status
        assert params[10] == {"command": "ls"}  # tool_input
        assert params[11] == "ok"  # tool_output
        assert params[16] == row["first_seen_at"]  # first_seen_at from the part
        assert params[17] == now  # last_seen_at = backfill time
        assert params[18] == row["data"]  # verbatim redacted part data

    @pytest.mark.asyncio
    async def test_upsert_truncates_oversized_payloads(self):
        from scripts.backfill_tool_calls import _recompute_tool_call, _upsert_tool_call

        mock_conn = AsyncMock()
        row = _part_row(
            data={
                "type": "tool",
                "tool": "bash",
                "state": {
                    "input": {"command": "x" * 5000},
                    "output": "y" * 5000,
                },
            }
        )
        expected = _recompute_tool_call(row["data"], tool_payload_max_chars=100)
        await _upsert_tool_call(
            mock_conn, row, expected, now=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

        params = mock_conn.execute.call_args[0][1:]
        assert params[10]["truncated"] is True
        assert len(params[10]["content"]) == 100
        assert params[11]["truncated"] is True
        assert len(params[11]["content"]) == 100


class TestRunBackfill:
    """The scan orchestrates per-row repair with idempotent, bounded
    behaviour."""

    async def _run(self, mock_conn, rows, **kwargs):
        mock_conn.cursor = Mock(return_value=_FakeCursor(rows))
        from scripts.backfill_tool_calls import _run_backfill

        return await _run_backfill(mock_conn, tool_payload_max_chars=4096, **kwargs)

    @pytest.mark.asyncio
    async def test_repairs_missing_tool_call_row(self):
        """A tool part with no projection row is upserted."""
        mock_conn = AsyncMock()
        counters = await self._run(mock_conn, [_part_row()])

        assert counters == {
            "scanned": 1, "unchanged": 0, "repaired": 1, "deferred": 0, "skipped": 0,
        }
        assert mock_conn.execute.call_count == 1
        assert "INSERT INTO observed_tool_calls" in str(mock_conn.execute.call_args)

    @pytest.mark.asyncio
    async def test_identical_rows_are_untouched(self):
        """A projection row that already matches re-extraction is never
        rewritten — re-running is a no-op (idempotency)."""
        mock_conn = AsyncMock()
        row = _part_row(
            tool_name="bash",
            tool_status="completed",
            tool_input={"command": "ls"},
            tool_output="ok",
        )
        counters = await self._run(mock_conn, [row])

        assert counters["unchanged"] == 1
        assert counters["repaired"] == 0
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_divergent_row_repaired_toward_re_extraction(self):
        """A divergent status is corrected toward the re-extracted value —
        the projection is corrected toward observed_parts.data, never the
        reverse (ADR 0016 §4)."""
        mock_conn = AsyncMock()
        row = _part_row(
            tool_name="bash",
            tool_status="failed",
            tool_input={"command": "ls"},
            tool_output="ok",
        )
        counters = await self._run(mock_conn, [row])

        assert counters["repaired"] == 1
        params = mock_conn.execute.call_args[0][1:]
        assert params[9] == "completed"

    @pytest.mark.asyncio
    async def test_truncation_marker_data_skipped(self):
        """A part whose data is a truncation marker cannot be
        re-extracted — skipped and counted, never written."""
        mock_conn = AsyncMock()
        row = _part_row(data={"truncated": True, "content": "{"})
        counters = await self._run(mock_conn, [row])

        assert counters["skipped"] == 1
        assert counters["repaired"] == 0
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writing(self):
        mock_conn = AsyncMock()
        counters = await self._run(mock_conn, [_part_row()], dry_run=True)

        assert counters["repaired"] == 1
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_caps_repairs_and_defers_rest(self):
        """--limit bounds the rows repaired per run; remaining
        disagreements are counted deferred for a later run (phased
        backfill converges by re-running)."""
        mock_conn = AsyncMock()
        rows = [_part_row(external_part_id=f"part_{i}") for i in range(3)]
        counters = await self._run(mock_conn, rows, limit=1)

        assert counters["repaired"] == 1
        assert counters["deferred"] == 2
        assert mock_conn.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_since_bounds_the_scan(self):
        """--since binds the first_seen_at boundary into the scan."""
        mock_conn = AsyncMock()
        since = datetime(2026, 8, 1, tzinfo=timezone.utc)
        await self._run(mock_conn, [], since=since)

        sql, param = mock_conn.cursor.call_args[0]
        assert "AND p.first_seen_at >= $1" in sql
        assert param == since

    @pytest.mark.asyncio
    async def test_no_limit_binds_no_since_when_absent(self):
        mock_conn = AsyncMock()
        await self._run(mock_conn, [])

        sql = mock_conn.cursor.call_args[0][0]
        assert "first_seen_at >=" not in sql
        assert len(mock_conn.cursor.call_args[0]) == 1  # sql only, no params

    @pytest.mark.asyncio
    async def test_rejects_non_positive_limit(self):
        mock_conn = AsyncMock()
        with pytest.raises(ValueError):
            await self._run(mock_conn, [], limit=0)
        with pytest.raises(ValueError):
            await self._run(mock_conn, [], limit=-3)


# ══════════════════════════════════════════════════════════════════════════════
#  Stale rows — no backing tool part
# ══════════════════════════════════════════════════════════════════════════════


class TestStaleToolCallSql:
    """A tool-call row with no backing tool part cannot be recomputed —
    it is flagged and corrected by deletion (no tool part, no derived
    row, mirroring the sibling rollup backfill)."""

    def test_stale_query_flags_missing_backing_part(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_SQL

        assert "FROM observed_tool_calls" in STALE_TOOL_CALL_SQL
        assert "LEFT JOIN observed_parts" in STALE_TOOL_CALL_SQL
        assert "p.id IS NULL" in STALE_TOOL_CALL_SQL

    def test_stale_query_joins_on_source_key(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_SQL

        assert "ON t.client_id = p.client_id" in STALE_TOOL_CALL_SQL
        assert "AND t.source_database_id = p.source_database_id" in STALE_TOOL_CALL_SQL
        assert "AND t.external_part_id = p.external_part_id" in STALE_TOOL_CALL_SQL

    def test_stale_query_flags_retyped_parts(self):
        """A part re-ingested with a different type is no longer a tool
        part — its projection row is stale."""
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_SQL

        assert "p.part_type <> 'tool'" in STALE_TOOL_CALL_SQL

    def test_stale_delete_uses_not_exists(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_DELETE_SQL

        assert "DELETE FROM observed_tool_calls" in STALE_TOOL_CALL_DELETE_SQL
        assert "NOT EXISTS" in STALE_TOOL_CALL_DELETE_SQL
        assert "p.part_type = 'tool'" in STALE_TOOL_CALL_DELETE_SQL

    def test_stale_delete_matches_source_key(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_DELETE_SQL

        assert "p.client_id = t.client_id" in STALE_TOOL_CALL_DELETE_SQL
        assert "p.source_database_id = t.source_database_id" in STALE_TOOL_CALL_DELETE_SQL
        assert "p.external_part_id = t.external_part_id" in STALE_TOOL_CALL_DELETE_SQL


class TestStaleHelpers:
    @pytest.mark.asyncio
    async def test_count_stale_returns_int(self):
        from scripts.backfill_tool_calls import _count_stale_tool_calls

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"cnt": 3})
        assert await _count_stale_tool_calls(mock_conn) == 3
        assert "COUNT" in mock_conn.fetchrow.call_args[0][0]

    @pytest.mark.asyncio
    async def test_count_stale_none_returns_zero(self):
        from scripts.backfill_tool_calls import _count_stale_tool_calls

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        assert await _count_stale_tool_calls(mock_conn) == 0

    @pytest.mark.asyncio
    async def test_delete_stale_returns_count(self):
        from scripts.backfill_tool_calls import _delete_stale_tool_calls

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 2")
        assert await _delete_stale_tool_calls(mock_conn) == 2


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2 + 5: Backfill↔live equivalence and idempotent re-running
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillLiveEquivalence:
    """Acceptance criterion: backfilled extraction equals live-ingest
    extraction for the same payloads.

    Simulates the two paths over the same raw part payload:

    - **Live** (``_process_part``): ``redact_dict`` → ``_extract_part_columns``
      → ``_truncate_json_value`` per field, before persistence.
    - **Backfill** (``_recompute_tool_call``): the same pipeline over the
      stored (already-redacted) ``observed_parts.data``.

    The backfill imports the same function objects live ingest uses
    (``TestSharedExtraction`` asserts identity), and ``redact_dict`` is
    idempotent on stored redacted payloads, so both paths must produce
    identical projection values — including the redaction and truncation
    caps.
    """

    def _live_extract(self, raw_data, *, tool_payload_max_chars):
        """The live-ingest path: redact → extract → truncate."""
        redacted = redact_dict(raw_data)
        part_type, tool_name, tool_status, tool_input, tool_output = _extract_part_columns(
            redacted
        )
        return (
            part_type,
            tool_name,
            tool_status,
            _truncate_json_value(tool_input, tool_payload_max_chars),
            _truncate_json_value(tool_output, tool_payload_max_chars),
        )

    def _backfill_extract(self, stored_data, *, tool_payload_max_chars):
        """The backfill path: re-extract the stored part data."""
        from scripts.backfill_tool_calls import _recompute_tool_call

        tool_name, tool_status, tool_input, tool_output = _recompute_tool_call(
            stored_data, tool_payload_max_chars=tool_payload_max_chars,
        )
        return ("tool", tool_name, tool_status, tool_input, tool_output)

    def test_backfilled_extraction_equals_live_extraction(self):
        """A payload with a secret value and an over-cap output: both
        paths must agree exactly (redaction + truncation identical)."""
        raw = {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls -la", "GITHUB_TOKEN": "ghp_plaintext"},
                "output": "total 0\n" + "x" * 6000,  # over the 4096 cap
            },
        }
        live = self._live_extract(raw, tool_payload_max_chars=4096)
        stored = redact_dict(raw)  # what live ingest persisted as part data
        backfilled = self._backfill_extract(stored, tool_payload_max_chars=4096)

        assert backfilled == live
        assert "ghp_plaintext" not in str(backfilled)

    def test_top_level_fallback_shape_equivalent(self):
        """A tool part without a ``state`` object (top-level
        status/input/output fallback) must extract identically."""
        raw = {
            "type": "tool",
            "tool": "edit",
            "status": "running",
            "input": "x",
            "output": None,
        }
        live = self._live_extract(raw, tool_payload_max_chars=4096)
        backfilled = self._backfill_extract(
            redact_dict(raw), tool_payload_max_chars=4096,
        )
        assert backfilled == live

    def test_small_payloads_pass_through_unchanged(self):
        """Under-cap payloads are stored verbatim on both paths."""
        raw = {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"a": 1}, "output": "ok"},
        }
        live = self._live_extract(raw, tool_payload_max_chars=4096)
        backfilled = self._backfill_extract(
            redact_dict(raw), tool_payload_max_chars=4096,
        )
        assert backfilled == live
        assert backfilled[3] == {"a": 1}
        assert backfilled[4] == "ok"

    @pytest.mark.asyncio
    async def test_rerun_after_repair_is_a_noop(self):
        """Idempotency: a second run over a store the first run repaired
        changes nothing — no duplicate rows, no rewrites."""
        from scripts.backfill_tool_calls import _run_backfill

        mock_conn = AsyncMock()
        row = _part_row()

        async def run():
            mock_conn.cursor = Mock(return_value=_FakeCursor([row]))
            return await _run_backfill(mock_conn, tool_payload_max_chars=4096)

        first = await run()
        assert first["repaired"] == 1
        assert mock_conn.execute.call_count == 1

        # The store now matches re-extraction (the repair landed).
        row["tool_name"] = "bash"
        row["tool_status"] = "completed"
        row["tool_input"] = {"command": "ls"}
        row["tool_output"] = "ok"

        second = await run()
        assert second["unchanged"] == 1
        assert second["repaired"] == 0
        assert mock_conn.execute.call_count == 1  # nothing new written
