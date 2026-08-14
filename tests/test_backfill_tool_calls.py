# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the observed_tool_calls backfill script (issue #468).

``observed_tool_calls`` is a *derived query surface* over ``observed_parts``
(ADR 0016 §4), not an independent source of truth.  A disagreement is
repaired by re-extraction — ``scripts/backfill_tool_calls.py`` recomputes
``observed_tool_calls`` rows from ``observed_parts`` rows with
``part_type = 'tool'`` using the SAME extraction logic as live ingest
(the ADR 0015 backfill↔live-equivalence pattern).

Covers the acceptance criteria from the task contract:

1. The extraction logic is SHARED with live ingest by import — the script
   imports ``_extract_tool_call_facts``, ``_redact_json_value`` and
   ``_truncate_json_field`` from ``app.api.ingest`` (no duplication), and
   the backfill derivation is byte-equivalent to the live ingest pipeline.
2. Redaction/truncation caps match live ingest
   (``GATEWAY_TOOL_PAYLOAD_MAX_CHARS``, default 4096).
3. Idempotency — the upsert preserves ``first_seen_at`` and COALESCE-fills
   nullable derived columns, so re-running never produces divergent rows.
4. ``--dry-run``, ``--limit`` and ``--since`` support bounded progress.
5. Backfill↔live equivalence: re-extracting from the stored (redacted)
   ``observed_parts.data`` equals live extraction from the raw payload.

Tests follow the mock pattern from ``tests/test_client_project_rollup_backfill.py``
(SQL-content assertions + AsyncMock connection) and the private-helper import
pattern from ``tests/test_execution_transcript.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.ingest import (
    _extract_tool_call_facts,
    _redact_and_truncate_payload,
    _redact_json_value,
    _truncate_json_field,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+


def _live_expected(data: dict | None, tool_payload_max_chars: int) -> tuple:
    """Simulate the live ingest extraction pipeline (ingest.py:1575 + 1632-1637).

    Extracts tool-call facts from the RAW part payload, redacts the
    input/output, and truncates each to ``tool_payload_max_chars`` — the
    exact function sequence live ingest applies before writing
    ``observed_tool_calls``.
    """
    tool_name, tool_status, tool_input, tool_output = _extract_tool_call_facts(data)
    tool_input = _truncate_json_field(_redact_json_value(tool_input), tool_payload_max_chars)
    tool_output = _truncate_json_field(_redact_json_value(tool_output), tool_payload_max_chars)
    return tool_name, tool_status, tool_input, tool_output


def _stored_data(raw: dict, part_data_max_chars: int = 65536) -> str:
    """Return the redacted+truncated JSON string live ingest persists to
    ``observed_parts.data`` for a raw part payload."""
    return json.dumps(_redact_and_truncate_payload(raw, part_data_max_chars))


def _part_row(**overrides) -> dict:
    """A scan row as returned by the backfill's SCAN_SQL LEFT JOIN."""
    row = {
        "part_id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "source_database_id": uuid.uuid4(),
        "external_part_id": "part_1",
        "message_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "external_session_id": "ses_1",
        "source_created_at": 1000,
        "source_updated_at": 2000,
        "source_created_at_tz": None,
        "source_updated_at_tz": None,
        "first_seen_at": datetime(2026, 8, 1, tzinfo=UTC),
        "data": json.dumps({"type": "tool", "tool": "bash", "state": {"status": "ok"}}),
        "tool_name": "bash",
        "tool_status": "ok",
        "tool_input": json.dumps({"cmd": "ls"}),
        "tool_output": json.dumps("done"),
    }
    row.update(overrides)
    return row


class _FakeCursor:
    """Minimal async iterator standing in for ``asyncpg.Connection.cursor()``."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        row = self._rows[self._i]
        self._i += 1
        return row


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1 + 5: Backfill↔live extraction equivalence
# ══════════════════════════════════════════════════════════════════════════════


class TestRecomputeEquivalence:
    """Acceptance criteria 1 + 5: the backfill's re-extraction from the stored
    (redacted) ``observed_parts.data`` equals live ingest's extraction from
    the raw payload — the shared helpers guarantee no drift (ADR 0015)."""

    def _recompute(self, stored: str, tool_payload_max_chars: int = 4096):
        from scripts.backfill_tool_calls import _recompute_tool_call

        return _recompute_tool_call(
            stored, tool_payload_max_chars=tool_payload_max_chars,
        )

    def test_backfill_equals_live_for_state_shape(self):
        raw = {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"cmd": "ls"}, "output": "ok"},
        }
        expected = _live_expected(raw, 4096)
        assert self._recompute(_stored_data(raw)) == expected

    def test_backfill_equals_live_after_redaction(self):
        secret = "super-secret-xyz"
        raw = {
            "type": "tool",
            "tool": "bash",
            "input": {"command": "echo $GITHUB_TOKEN", "env": {"GITHUB_TOKEN": secret}},
            "output": {"stdout": "done", "API_KEY": secret},
        }
        expected = _live_expected(raw, 4096)
        result = self._recompute(_stored_data(raw))
        assert result == expected
        # The durable re-extraction must never carry the plaintext secret.
        _, _, tool_input, tool_output = result
        assert secret not in json.dumps(tool_input)
        assert secret not in json.dumps(tool_output)

    def test_backfill_equals_live_after_truncation(self):
        raw = {
            "type": "tool",
            "tool": "bash",
            "input": {"command": "y" * 500},
            "output": "z" * 500,
        }
        expected = _live_expected(raw, 50)
        result = self._recompute(_stored_data(raw), tool_payload_max_chars=50)
        assert result == expected
        _, _, tool_input, tool_output = result
        assert isinstance(tool_input, str) and len(tool_input) <= 50
        assert isinstance(tool_output, str) and len(tool_output) <= 50

    def test_backfill_equals_live_for_list_values(self):
        # redact_dict does not descend into list elements (both paths agree).
        raw = {
            "type": "tool",
            "tool": "bash",
            "state": {"input": [{"token": "abc"}], "output": [1, 2]},
        }
        expected = _live_expected(raw, 4096)
        assert self._recompute(_stored_data(raw)) == expected

    def test_backfill_equals_live_for_scalar_input_output(self):
        raw = {"type": "tool", "tool": "bash", "state": {"input": 42, "output": "ok"}}
        expected = _live_expected(raw, 4096)
        assert self._recompute(_stored_data(raw)) == expected

    def test_recompute_raises_when_no_tool_name(self):
        from scripts.backfill_tool_calls import _recompute_tool_call

        with pytest.raises(ValueError):
            _recompute_tool_call(
                _stored_data({"type": "text", "text": "hi"}),
                tool_payload_max_chars=4096,
            )

    def test_recompute_raises_when_data_is_null(self):
        from scripts.backfill_tool_calls import _recompute_tool_call

        with pytest.raises(ValueError):
            _recompute_tool_call(None, tool_payload_max_chars=4096)

    def test_recompute_raises_on_truncation_marker(self):
        # A payload truncated at ingest (beyond part_data_max_chars) stores the
        # ``truncated`` marker and cannot be re-extracted.
        from scripts.backfill_tool_calls import _recompute_tool_call

        raw = {"type": "tool", "tool": "bash", "input": "x" * 5000}
        stored = _stored_data(raw, part_data_max_chars=20)
        assert "truncated" in stored
        with pytest.raises(ValueError):
            _recompute_tool_call(stored, tool_payload_max_chars=4096)


# ══════════════════════════════════════════════════════════════════════════════
#  AC 3: Idempotency — comparison treats stored JSONB as JSON-equivalent
# ══════════════════════════════════════════════════════════════════════════════


class TestToolCallMatches:
    """Acceptance criterion 3: already-correct rows are never rewritten, so
    re-running the backfill produces no divergent rows."""

    def _matches(self, row: dict, expected: tuple) -> bool:
        from scripts.backfill_tool_calls import _tool_call_matches

        return _tool_call_matches(row, expected)

    def test_matches_when_values_equal(self):
        row = _part_row(
            tool_name="bash",
            tool_status="completed",
            tool_input=json.dumps({"cmd": "ls"}),
            tool_output=json.dumps("ok"),
        )
        assert self._matches(row, ("bash", "completed", {"cmd": "ls"}, "ok"))

    def test_matches_json_objects_regardless_of_key_order(self):
        # JSONB round-trips re-serialize objects in canonical key order; the
        # comparison must be JSON-equivalent, not string-equal.
        row = _part_row(tool_input=json.dumps({"a": 1, "b": 2}))
        assert self._matches(row, ("bash", "ok", {"b": 2, "a": 1}, "done"))

    def test_matches_handles_null_fields(self):
        row = _part_row(tool_input=None, tool_output=None)
        assert self._matches(row, ("bash", "ok", None, None))

    def test_detects_divergent_tool_name(self):
        row = _part_row(tool_name="bash")
        assert not self._matches(row, ("different", "ok", {"cmd": "ls"}, "done"))

    def test_detects_divergent_status(self):
        row = _part_row(tool_status="completed")
        assert not self._matches(row, ("bash", "failed", {"cmd": "ls"}, "done"))

    def test_detects_divergent_input(self):
        row = _part_row(tool_input=json.dumps({"cmd": "ls"}))
        assert not self._matches(row, ("bash", "ok", {"cmd": "rm -rf /"}, "done"))

    def test_detects_divergent_output(self):
        row = _part_row(tool_output=json.dumps("done"))
        assert not self._matches(row, ("bash", "ok", {"cmd": "ls"}, "boom"))


# ══════════════════════════════════════════════════════════════════════════════
#  SQL content assertions
# ══════════════════════════════════════════════════════════════════════════════


class TestScanSql:
    def test_scan_sql_selects_tool_parts_joined_to_tool_calls(self):
        from scripts.backfill_tool_calls import SCAN_SQL

        assert "FROM observed_parts p" in SCAN_SQL
        assert "LEFT JOIN observed_tool_calls t" in SCAN_SQL
        assert "p.part_type = 'tool'" in SCAN_SQL

    def test_scan_sql_joins_on_source_key(self):
        from scripts.backfill_tool_calls import SCAN_SQL

        assert "t.client_id = p.client_id" in SCAN_SQL
        assert "t.source_database_id = p.source_database_id" in SCAN_SQL
        assert "t.external_part_id = p.external_part_id" in SCAN_SQL

    def test_scan_sql_orders_deterministically(self):
        from scripts.backfill_tool_calls import SCAN_SQL

        assert "ORDER BY p.first_seen_at, p.id" in SCAN_SQL

    def test_scan_sql_applies_since_bound(self):
        from scripts.backfill_tool_calls import _scan_sql

        sql = _scan_sql(datetime(2026, 8, 1, tzinfo=UTC))
        assert "p.first_seen_at >= $1" in sql
        # The marker must be fully replaced (no dangling comment).
        assert "__SINCE__" not in sql

    def test_scan_sql_no_since_bound_by_default(self):
        from scripts.backfill_tool_calls import _scan_sql

        sql = _scan_sql(None)
        assert "p.first_seen_at >= $1" not in sql
        assert "__SINCE__" not in sql


class TestUpsertSql:
    def test_upsert_on_source_key(self):
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        assert "INSERT INTO observed_tool_calls" in UPSERT_TOOL_CALL_SQL
        assert (
            "ON CONFLICT (client_id, source_database_id, external_part_id)"
            in UPSERT_TOOL_CALL_SQL
        )

    def test_upsert_overwrites_tool_name(self):
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        assert "tool_name = EXCLUDED.tool_name" in UPSERT_TOOL_CALL_SQL

    def test_upsert_coalesce_fills_nullable_derived_columns(self):
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        assert "tool_status = COALESCE(EXCLUDED.tool_status" in UPSERT_TOOL_CALL_SQL
        assert "tool_input = COALESCE(EXCLUDED.tool_input" in UPSERT_TOOL_CALL_SQL
        assert "tool_output = COALESCE(EXCLUDED.tool_output" in UPSERT_TOOL_CALL_SQL

    def test_upsert_preserves_first_seen_at(self):
        # first_seen_at must never be overwritten on conflict (idempotency).
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        assert "first_seen_at = EXCLUDED.first_seen_at" not in UPSERT_TOOL_CALL_SQL

    def test_upsert_refreshes_last_seen_at(self):
        from scripts.backfill_tool_calls import UPSERT_TOOL_CALL_SQL

        assert "last_seen_at = EXCLUDED.last_seen_at" in UPSERT_TOOL_CALL_SQL


class TestStaleSql:
    def test_stale_query_flags_rows_without_tool_part(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_SQL

        assert "FROM observed_tool_calls t" in STALE_TOOL_CALL_SQL
        assert "LEFT JOIN observed_parts p" in STALE_TOOL_CALL_SQL
        assert "p.id IS NULL" in STALE_TOOL_CALL_SQL
        assert "p.part_type <> 'tool'" in STALE_TOOL_CALL_SQL

    def test_stale_delete_removes_rows_without_tool_part(self):
        from scripts.backfill_tool_calls import STALE_TOOL_CALL_DELETE_SQL

        assert "DELETE FROM observed_tool_calls" in STALE_TOOL_CALL_DELETE_SQL
        assert "NOT EXISTS" in STALE_TOOL_CALL_DELETE_SQL
        assert "p.part_type = 'tool'" in STALE_TOOL_CALL_DELETE_SQL


# ══════════════════════════════════════════════════════════════════════════════
#  AC 4: CLI flags — --dry-run / --limit / --since
# ══════════════════════════════════════════════════════════════════════════════


class TestArgParsing:
    def test_defaults(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args([])
        assert args.dry_run is False
        assert args.limit is None
        assert args.since is None

    def test_dry_run_flag(self):
        from scripts.backfill_tool_calls import _parse_args

        assert _parse_args(["--dry-run"]).dry_run is True

    def test_limit_flag(self):
        from scripts.backfill_tool_calls import _parse_args

        assert _parse_args(["--limit", "500"]).limit == 500

    def test_since_requires_utc_offset(self):
        from scripts.backfill_tool_calls import _parse_args

        args = _parse_args(["--since", "2026-08-01T00:00:00+00:00"])
        assert args.since == datetime(2026, 8, 1, tzinfo=UTC)

    def test_since_rejects_naive_timestamp(self):
        from scripts.backfill_tool_calls import _parse_args

        with pytest.raises(SystemExit):
            _parse_args(["--since", "2026-08-01T00:00:00"])


# ══════════════════════════════════════════════════════════════════════════════
#  Helper behaviour against a mock connection
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillHelpers:
    @pytest.mark.asyncio
    async def test_count_stale_tool_calls_returns_int(self):
        from scripts.backfill_tool_calls import _count_stale_tool_calls

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"cnt": 5})
        assert await _count_stale_tool_calls(conn) == 5

    @pytest.mark.asyncio
    async def test_count_stale_tool_calls_none_returns_zero(self):
        from scripts.backfill_tool_calls import _count_stale_tool_calls

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        assert await _count_stale_tool_calls(conn) == 0

    @pytest.mark.asyncio
    async def test_delete_stale_tool_calls_returns_count(self):
        from scripts.backfill_tool_calls import _delete_stale_tool_calls

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 2")
        assert await _delete_stale_tool_calls(conn) == 2

    @pytest.mark.asyncio
    async def test_upsert_tool_call_serializes_jsonb_fields(self):
        # asyncpg's JSONB codec accepts str only — tool_input/tool_output must
        # be JSON strings, and first_seen_at is carried over from the part.
        from scripts.backfill_tool_calls import _upsert_tool_call

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        row = _part_row(first_seen_at=datetime(2026, 8, 1, tzinfo=UTC))
        expected = ("bash", "ok", {"cmd": "ls"}, "done")
        await _upsert_tool_call(conn, row, expected, now=datetime(2026, 8, 2, tzinfo=UTC))

        args = conn.execute.call_args.args
        # $11 tool_input and $12 tool_output are bound as JSON strings.
        assert isinstance(args[11], str)
        assert isinstance(args[12], str)
        assert json.loads(args[11]) == {"cmd": "ls"}
        assert json.loads(args[12]) == "done"
        # $17 first_seen_at is the part's first_seen_at; $18 last_seen_at is now.
        assert args[17] == row["first_seen_at"]
        assert args[18] == datetime(2026, 8, 2, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_run_backfill_repairs_divergent_rows(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        conn.cursor = MagicMock(
            return_value=_FakeCursor(
                [_part_row(tool_name="stale_name")],
            )
        )
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        counters = await _run_backfill(conn, tool_payload_max_chars=4096)
        assert counters["scanned"] == 1
        assert counters["repaired"] == 1
        assert conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_run_backfill_skips_unchanged_rows(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        conn.cursor = MagicMock(
            return_value=_FakeCursor(
                [_part_row(
                    tool_name="bash",
                    tool_status="ok",
                    tool_input=json.dumps({"cmd": "ls"}),
                    tool_output=json.dumps("done"),
                    data=json.dumps(
                        {"type": "tool", "tool": "bash",
                         "state": {"status": "ok", "input": {"cmd": "ls"}, "output": "done"}}
                    ),
                )],
            )
        )
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        counters = await _run_backfill(conn, tool_payload_max_chars=4096)
        assert counters["unchanged"] == 1
        assert counters["repaired"] == 0
        assert conn.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_run_backfill_respects_limit(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        rows = [_part_row(tool_name=f"stale_{i}") for i in range(5)]
        conn.cursor = MagicMock(return_value=_FakeCursor(rows))
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        counters = await _run_backfill(conn, tool_payload_max_chars=4096, limit=2)
        assert counters["repaired"] == 2
        assert counters["deferred"] == 3
        assert conn.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_run_backfill_dry_run_does_not_write(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=_FakeCursor([_part_row(tool_name="stale")]))
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        counters = await _run_backfill(conn, tool_payload_max_chars=4096, dry_run=True)
        assert counters["repaired"] == 1
        assert conn.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_run_backfill_skips_unrecoverable_rows(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        conn.cursor = MagicMock(
            return_value=_FakeCursor([_part_row(data=json.dumps({"truncated": True}))])
        )
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        counters = await _run_backfill(conn, tool_payload_max_chars=4096)
        assert counters["skipped"] == 1
        assert counters["repaired"] == 0
        assert conn.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_run_backfill_rejects_non_positive_limit(self):
        from scripts.backfill_tool_calls import _run_backfill

        conn = AsyncMock()
        with pytest.raises(ValueError):
            await _run_backfill(conn, tool_payload_max_chars=4096, limit=0)
        with pytest.raises(ValueError):
            await _run_backfill(conn, tool_payload_max_chars=4096, limit=-3)
