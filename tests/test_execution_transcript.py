"""Tests for the execution-transcript slice (issue #217, ADR 0016).

Covers the ingest-time redaction/truncation helpers, tool-call extraction,
the observed-message/part/tool-call upsert handlers, keyset cursor
round-trips, and the ``/api/v1/execution`` read path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.execution import _decode_cursor, _encode_cursor, _fetch_messages
from app.api.ingest import (
    MessagePayload,
    PartPayload,
    _extract_tool_call_facts,
    _process_message,
    _process_part,
    _redact_and_truncate_payload,
    _truncate_json_field,
)

# ── Row builder for asyncpg.Record-like mocks ─────────────────────────────────


def _row(data: dict) -> MagicMock:
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def _message_row_data(**overrides) -> dict:
    data = {
        "id": uuid.uuid4(),
        "external_message_id": "msg_1",
        "external_session_id": "ses_1",
        "session_id": uuid.uuid4(),
        "parent_external_session_id": None,
        "role": "assistant",
        "agent": None,
        "mode": None,
        "cost_usd": None,
        "input_tokens": 10,
        "output_tokens": 20,
        "source_created_at": 1000,
        "source_updated_at": 2000,
        "source_created_at_tz": None,
        "source_updated_at_tz": None,
        "data": {"text": "hi"},
    }
    data.update(overrides)
    return data


# ── Redaction / truncation helpers ────────────────────────────────────────────


class TestRedactionAndTruncation:
    def test_redact_and_truncate_payload_redacts_secret_keys(self):
        payload = {"REPO_URL": "https://example", "GITHUB_TOKEN": "ghp_secret_value"}
        result = _redact_and_truncate_payload(payload, 65536)
        assert result["REPO_URL"] == "https://example"
        assert result["GITHUB_TOKEN"] == "***"
        assert "ghp_secret_value" not in str(result)

    def test_redact_and_truncate_payload_sets_truncated_marker_when_oversized(self):
        payload = {"text": "x" * 100}
        result = _redact_and_truncate_payload(payload, max_chars=20)
        assert result["truncated"] is True
        assert "prefix" in result
        assert len(result["prefix"]) <= 20

    def test_redact_and_truncate_payload_returns_unchanged_when_fits(self):
        payload = {"text": "short"}
        result = _redact_and_truncate_payload(payload, max_chars=100)
        assert result == payload
        assert "truncated" not in result

    def test_truncate_json_field_returns_value_when_fits(self):
        value = {"a": 1}
        assert _truncate_json_field(value, max_chars=100) == value

    def test_truncate_json_field_truncates_string_when_oversized(self):
        value = {"a": "y" * 100}
        result = _truncate_json_field(value, max_chars=20)
        assert isinstance(result, str)
        assert len(result) <= 20


# ── Tool-call extraction ──────────────────────────────────────────────────────


class TestToolCallExtraction:
    def test_extract_from_state_shape(self):
        data = {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"cmd": "ls"}, "output": "ok"},
        }
        name, status, tool_input, tool_output = _extract_tool_call_facts(data)
        assert name == "bash"
        assert status == "completed"
        assert tool_input == {"cmd": "ls"}
        assert tool_output == "ok"

    def test_extract_returns_none_when_no_tool_name(self):
        assert _extract_tool_call_facts({"type": "text"}) == (None, None, None, None)
        assert _extract_tool_call_facts(None) == (None, None, None, None)


# ── Keyset cursor round-trip ──────────────────────────────────────────────────


class TestCursor:
    def test_round_trip(self):
        cursor = _encode_cursor(12345, str(uuid.uuid4()))
        ms, row_id = _decode_cursor(cursor)
        assert ms == 12345

    def test_invalid_cursor_raises_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            _decode_cursor("not-a-valid-cursor!!!")
        assert exc.value.status_code == 400


# ── Ingest handlers ───────────────────────────────────────────────────────────


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_message_redacts_and_upserts(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        msg = MessagePayload(
            external_message_id="msg_1",
            external_session_id="ses_1",
            role="assistant",
            data={"GITHUB_TOKEN": "ghp_abc", "text": "hello"},
        )
        ok = await _process_message(
            conn, msg, uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc),
            part_data_max_chars=65536,
        )
        assert ok is True
        insert_sql = str(conn.execute.call_args)
        assert "INSERT INTO observed_messages" in insert_sql
        assert "ON CONFLICT" in insert_sql
        # The durable store must never receive the plaintext secret.
        assert "ghp_abc" not in insert_sql
        assert "***" in insert_sql

    @pytest.mark.asyncio
    async def test_message_preserves_parent_external_session_id(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        msg = MessagePayload(
            external_message_id="msg_1",
            external_session_id="ses_child",
            role="assistant",
            parent_external_session_id="ses_parent",
            data={"text": "hello"},
        )
        await _process_message(
            conn, msg, uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc),
            part_data_max_chars=65536,
        )
        insert_sql = str(conn.execute.call_args)
        assert "INSERT INTO observed_messages" in insert_sql
        # Message-level parent/child linkage is preserved on the projection.
        assert "ses_parent" in insert_sql


class TestProcessPart:
    @pytest.mark.asyncio
    async def test_tool_part_projects_tool_call(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        part = PartPayload(
            external_part_id="part_1",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="tool",
            data={"type": "tool", "tool": "bash", "state": {"status": "ok"}},
        )
        ok = await _process_part(
            conn, part, uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        assert ok is True
        calls = [str(c) for c in conn.execute.call_args_list]
        assert any("INSERT INTO observed_parts" in c for c in calls)
        assert any("INSERT INTO observed_tool_calls" in c for c in calls)

    @pytest.mark.asyncio
    async def test_non_tool_part_does_not_project_tool_call(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        part = PartPayload(
            external_part_id="part_2",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="text",
            data={"type": "text", "text": "hello"},
        )
        await _process_part(
            conn, part, uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        calls = [str(c) for c in conn.execute.call_args_list]
        assert any("INSERT INTO observed_parts" in c for c in calls)
        assert not any("INSERT INTO observed_tool_calls" in c for c in calls)


def _async_cm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ── Read path: keyset pagination ──────────────────────────────────────────────


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_keyset_page_returns_cursor_and_has_more(self):
        conn = AsyncMock()
        rows = [_row(_message_row_data(source_created_at=i, id=uuid.uuid4())) for i in range(3)]
        conn.fetch = AsyncMock(return_value=rows)

        page = await _fetch_messages(
            conn,
            uuid.uuid4(),
            agent=None,
            role=None,
            from_ms=None,
            to_ms=None,
            limit=2,
            after_ms=None,
            after_id=None,
            db_timeout_seconds=5,
        )
        assert page.has_more is True
        assert page.next_cursor is not None
        assert len(page.items) == 2

    @pytest.mark.asyncio
    async def test_empty_page(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        page = await _fetch_messages(
            conn,
            uuid.uuid4(),
            agent=None,
            role=None,
            from_ms=None,
            to_ms=None,
            limit=2,
            after_ms=None,
            after_id=None,
            db_timeout_seconds=5,
        )
        assert page.has_more is False
        assert page.next_cursor is None
        assert page.items == []
