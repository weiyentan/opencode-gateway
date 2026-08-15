"""Tests for the execution-transcript slice (issue #217, ADR 0016).

Covers the ingest-time redaction/truncation helpers, tool-call extraction,
the observed-message/part/tool-call upsert handlers, keyset cursor
round-trips, and the ``/api/v1/execution`` read path.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.api.execution import (
    _fetch_messages,
    _fetch_parts,
    _fetch_timeline,
    _fetch_tool_calls,
    _message_row,
    _part_row,
    _timeline_row,
    _tool_call_row,
)
from app.api.ingest import (
    MessagePayload,
    PartPayload,
    _extract_tool_call_facts,
    _process_message,
    _process_part,
    _redact_and_truncate_payload,
    _truncate_json_field,
)
from app.api.pagination import (
    NULL_CURSOR_SENTINEL,
    decode_cursor,
    encode_cursor,
    next_cursor,
)
from app.core.schemas.execution import ObservedToolCall
from tests.conftest import create_client

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

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
        "data": json.dumps({"text": "hi"}),
    }
    data.update(overrides)
    return data


def _part_row_data(**overrides) -> dict:
    data = {
        "id": uuid.uuid4(),
        "external_part_id": "part_1",
        "external_message_id": "msg_1",
        "external_session_id": "ses_1",
        "message_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "part_type": "text",
        "source_created_at": 1000,
        "source_updated_at": 2000,
        "source_created_at_tz": None,
        "source_updated_at_tz": None,
        "data": json.dumps({"type": "text", "text": "hi"}),
    }
    data.update(overrides)
    return data


def _tool_call_row_data(**overrides) -> dict:
    data = {
        "id": uuid.uuid4(),
        "external_part_id": "part_1",
        "external_session_id": "ses_1",
        "part_id": uuid.uuid4(),
        "message_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "tool_name": "bash",
        "tool_status": "completed",
        "tool_input": json.dumps({"cmd": "ls"}),
        "tool_output": json.dumps("ok"),
        "source_created_at": 1000,
        "source_created_at_tz": None,
    }
    data.update(overrides)
    return data


def _timeline_row_data(**overrides) -> dict:
    data = {
        "part_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "external_session_id": "ses_1",
        "agent": None,
        "depth": 0,
        "part_type": "text",
        "source_created_at": 1000,
        "source_created_at_tz": None,
        "data": json.dumps({"type": "text", "text": "hi"}),
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


# ── Keyset cursor helper unit tests (app.api.pagination) ─────────────────────


class TestPaginationHelpers:
    def test_round_trip(self):
        row_id = uuid.uuid4()
        cursor = encode_cursor(12345, str(row_id))
        ms, decoded_id = decode_cursor(cursor)
        assert ms == 12345
        assert decoded_id == row_id

    def test_invalid_cursor_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            decode_cursor("not-a-valid-cursor!!!")
        assert exc.value.status_code == 400

    def test_null_timestamp_advances_via_sentinel(self):
        row_id = uuid.uuid4()
        cursor = next_cursor(None, str(row_id))
        ms, decoded_id = decode_cursor(cursor)
        assert ms == NULL_CURSOR_SENTINEL
        assert decoded_id == row_id

    def test_sentinel_value(self):
        assert NULL_CURSOR_SENTINEL == 2**62


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
            conn,
            msg,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
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
            conn,
            msg,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
        )
        insert_sql = str(conn.execute.call_args)
        assert "INSERT INTO observed_messages" in insert_sql
        # Message-level parent/child linkage is preserved on the projection.
        assert "ses_parent" in insert_sql

    @pytest.mark.asyncio
    async def test_message_binds_json_string_data(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        msg = MessagePayload(
            external_message_id="msg_1",
            external_session_id="ses_1",
            role="assistant",
            data={"text": "hello", "n": 1},
        )
        await _process_message(
            conn,
            msg,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
        )
        # asyncpg's JSONB codec accepts only str — the bound value must be a
        # JSON string, not a Python dict.
        data_arg = conn.execute.call_args.args[19]
        assert isinstance(data_arg, str)
        assert json.loads(data_arg) == {"text": "hello", "n": 1}


class TestProcessPart:
    @pytest.mark.asyncio
    async def test_tool_part_projects_tool_call(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
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
            conn,
            part,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        assert ok is True
        calls = [str(c) for c in conn.execute.call_args_list]
        assert any("INSERT INTO observed_parts" in c for c in calls)
        assert any("INSERT INTO observed_tool_calls" in c for c in calls)

    @pytest.mark.asyncio
    async def test_tool_part_redacts_secret_in_tool_payload(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        secret = "super-secret-xyz"
        part = PartPayload(
            external_part_id="part_1",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="tool",
            data={
                "type": "tool",
                "tool": "bash",
                "input": {"command": "echo $GITHUB_TOKEN", "env": {"GITHUB_TOKEN": secret}},
                "output": {"stdout": "done", "API_KEY": secret},
            },
        )
        await _process_part(
            conn,
            part,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        tool_call_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_tool_calls" in str(c)
        ]
        assert tool_call_calls, "expected an observed_tool_calls INSERT"
        tool_input = tool_call_calls[0].args[11]
        tool_output = tool_call_calls[0].args[12]
        # The durable tool-call store must never receive the plaintext secret.
        assert secret not in str(tool_input)
        assert secret not in str(tool_output)
        # The JSONB-bound values are JSON strings; decoding round-trips.
        assert json.loads(tool_input) == {
            "command": "echo $GITHUB_TOKEN",
            "env": {"GITHUB_TOKEN": "***"},
        }
        assert json.loads(tool_output) == {"stdout": "done", "API_KEY": "***"}

    @pytest.mark.asyncio
    async def test_tool_part_truncates_oversized_tool_payload(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        part = PartPayload(
            external_part_id="part_1",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="tool",
            data={"type": "tool", "tool": "bash", "input": {"command": "y" * 500}},
        )
        await _process_part(
            conn,
            part,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=50,
        )
        tool_call_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_tool_calls" in str(c)
        ]
        assert tool_call_calls, "expected an observed_tool_calls INSERT"
        tool_input = tool_call_calls[0].args[11]
        # Oversized tool input is bounded to the configured cap and stored as a
        # JSON-escaped string (asyncpg JSONB accepts str only).
        assert isinstance(tool_input, str)
        decoded = json.loads(tool_input)
        assert isinstance(decoded, str)
        assert len(decoded) <= 50

    @pytest.mark.asyncio
    async def test_non_tool_part_does_not_project_tool_call(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
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
            conn,
            part,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        calls = [str(c) for c in conn.execute.call_args_list]
        assert any("INSERT INTO observed_parts" in c for c in calls)
        assert not any("INSERT INTO observed_tool_calls" in c for c in calls)

    @pytest.mark.asyncio
    async def test_part_binds_json_string_data_and_tool_fields(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        part = PartPayload(
            external_part_id="part_1",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="tool",
            data={
                "type": "tool",
                "tool": "bash",
                "state": {"status": "ok", "input": {"cmd": "ls"}, "output": "ok"},
            },
        )
        await _process_part(
            conn,
            part,
            uuid.uuid4(),
            uuid.uuid4(),
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        part_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_parts" in str(c)
        ]
        tool_call_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_tool_calls" in str(c)
        ]
        assert part_calls, "expected an observed_parts INSERT"
        assert tool_call_calls, "expected an observed_tool_calls INSERT"
        # observed_parts.data is bound as a JSON string ($15).
        part_data = part_calls[0].args[15]
        assert isinstance(part_data, str)
        # observed_tool_calls.data is the same serialized JSON string ($18).
        tool_call_data = tool_call_calls[0].args[18]
        assert isinstance(tool_call_data, str)
        # tool_input/tool_output are serialized to JSON strings.
        assert isinstance(tool_call_calls[0].args[11], str)
        assert isinstance(tool_call_calls[0].args[12], str)

    @pytest.mark.asyncio
    async def test_replay_reuses_existing_part_id(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.transaction = MagicMock(return_value=_async_cm())

        part = PartPayload(
            external_part_id="part_1",
            external_message_id="msg_1",
            external_session_id="ses_1",
            part_type="tool",
            data={"type": "tool", "tool": "bash", "state": {"status": "ok"}},
        )
        client_id = uuid.uuid4()
        source_db_id = uuid.uuid4()
        # First delivery: no existing part row → a fresh id is generated.
        await _process_part(
            conn,
            part,
            client_id,
            source_db_id,
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        part_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_parts" in str(c)
        ]
        first_part_id = part_calls[0].args[1]

        # Second delivery (replay): the pre-query now resolves the stored id.
        conn.fetchval = AsyncMock(return_value=first_part_id)
        await _process_part(
            conn,
            part,
            client_id,
            source_db_id,
            datetime.now(UTC),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )
        tool_call_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO observed_tool_calls" in str(c)
        ]
        assert len(tool_call_calls) == 2
        # The replayed tool-call row references the stored part id, not a fresh
        # UUID that would violate the part_id FK.
        assert tool_call_calls[1].args[5] == first_part_id


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
    async def test_keyset_advances_on_null_last_timestamp(self):
        conn = AsyncMock()
        rows = [
            _row(_message_row_data(source_created_at=1000, id=uuid.uuid4())),
            _row(_message_row_data(source_created_at=None, id=uuid.uuid4())),
            _row(_message_row_data(source_created_at=3000, id=uuid.uuid4())),
        ]
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
        # A full page ending on a NULL timestamp must still advance (the cursor
        # encodes the NULL sentinel) rather than stalling on the first page.
        assert page.has_more is True
        assert page.next_cursor is not None
        sql = conn.fetch.call_args.args[0]
        assert "COALESCE(source_created_at" in sql

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


class TestFetchPartsNullCursor:
    @pytest.mark.asyncio
    async def test_keyset_advances_on_null_last_timestamp(self):
        conn = AsyncMock()
        rows = [
            _row(_part_row_data(source_created_at=1000, id=uuid.uuid4())),
            _row(_part_row_data(source_created_at=None, id=uuid.uuid4())),
            _row(_part_row_data(source_created_at=3000, id=uuid.uuid4())),
        ]
        conn.fetch = AsyncMock(return_value=rows)

        page = await _fetch_parts(
            conn,
            uuid.uuid4(),
            part_type=None,
            tool_name=None,
            from_ms=None,
            to_ms=None,
            limit=2,
            after_ms=None,
            after_id=None,
            db_timeout_seconds=5,
        )
        assert page.has_more is True
        assert page.next_cursor is not None
        sql = conn.fetch.call_args.args[0]
        assert "COALESCE(source_created_at" in sql


class TestFetchToolCallsNullCursor:
    @pytest.mark.asyncio
    async def test_keyset_advances_on_null_last_timestamp(self):
        conn = AsyncMock()
        rows = [
            _row(_tool_call_row_data(source_created_at=1000, id=uuid.uuid4())),
            _row(_tool_call_row_data(source_created_at=None, id=uuid.uuid4())),
            _row(_tool_call_row_data(source_created_at=3000, id=uuid.uuid4())),
        ]
        conn.fetch = AsyncMock(return_value=rows)

        page = await _fetch_tool_calls(
            conn,
            session_id=None,
            agent=None,
            tool_name=None,
            tool_status=None,
            from_ms=None,
            to_ms=None,
            limit=2,
            after_ms=None,
            after_id=None,
            db_timeout_seconds=5,
        )
        assert page.has_more is True
        assert page.next_cursor is not None
        sql = conn.fetch.call_args.args[0]
        assert "COALESCE(tc.source_created_at" in sql


class TestTimelineCte:
    @pytest.mark.asyncio
    async def test_cte_has_cycle_guard_and_dedup(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        await _fetch_timeline(
            conn,
            uuid.uuid4(),
            agent=None,
            max_depth=5,
            from_ms=None,
            to_ms=None,
            limit=10,
            after_ms=None,
            after_id=None,
            db_timeout_seconds=5,
        )
        sql = conn.fetch.call_args.args[0]
        # Cycle protection on the recursive step.
        assert "ANY(d.path)" in sql
        # Dedup each part to the shallowest depth.
        assert "DISTINCT ON" in sql
        # NULL-safe keyset ordering.
        assert "COALESCE(source_created_at" in sql


class TestRowBuilders:
    def test_message_row_decodes_json_data(self):
        row = _row(_message_row_data(data=json.dumps({"text": "hi"})))
        model = _message_row(row)
        assert model.data == {"text": "hi"}

    def test_message_row_handles_null_data(self):
        row = _row(_message_row_data(data=None))
        model = _message_row(row)
        assert model.data is None

    def test_part_row_decodes_json_data(self):
        row = _row(_part_row_data(data=json.dumps({"type": "text"})))
        model = _part_row(row)
        assert model.data == {"type": "text"}

    def test_tool_call_row_decodes_scalar_output(self):
        row = _row(_tool_call_row_data())
        model = _tool_call_row(row)
        assert model.tool_input == {"cmd": "ls"}
        assert model.tool_output == "ok"

    def test_tool_call_row_handles_null_fields(self):
        row = _row(_tool_call_row_data(tool_input=None, tool_output=None))
        model = _tool_call_row(row)
        assert model.tool_input is None
        assert model.tool_output is None

    def test_timeline_row_decodes_json_data(self):
        row = _row(_timeline_row_data(data=json.dumps({"type": "text"})))
        model = _timeline_row(row)
        assert model.data == {"type": "text"}


class TestToolCallSchema:
    def test_scalar_tool_output_validates(self):
        model = ObservedToolCall(
            id=uuid.uuid4(),
            external_part_id="part_1",
            external_session_id="ses_1",
            part_id=uuid.uuid4(),
            tool_name="bash",
            tool_output="ok",
        )
        assert model.tool_output == "ok"

    def test_scalar_tool_input_validates(self):
        model = ObservedToolCall(
            id=uuid.uuid4(),
            external_part_id="part_1",
            external_session_id="ses_1",
            part_id=uuid.uuid4(),
            tool_name="bash",
            tool_input=42,
        )
        assert model.tool_input == 42


# ── HTTP-level tests for /api/v1/execution ────────────────────────────────────

_SESSION_ID = uuid.uuid4()


class TestExecutionAuth:
    """All six execution endpoints require API-key auth (401 without it)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v1/execution/sessions/{_SESSION_ID}",
            f"/api/v1/execution/sessions/{_SESSION_ID}/children",
            f"/api/v1/execution/sessions/{_SESSION_ID}/messages",
            f"/api/v1/execution/sessions/{_SESSION_ID}/parts",
            f"/api/v1/execution/sessions/{_SESSION_ID}/timeline",
            "/api/v1/execution/tool-calls",
        ],
    )
    async def test_requires_auth(self, mock_conn: AsyncMock, path: str):
        async with create_client(mock_conn, api_key=None) as c:
            response = await c.get(path)
        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


class TestExecutionSessionHeader:
    """The session-header endpoint 404s on an unknown session."""

    @pytest.mark.asyncio
    async def test_unknown_session_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetchrow = AsyncMock(return_value=None)
        async with client as c:
            response = await c.get(f"/api/v1/execution/sessions/{_SESSION_ID}")
        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"


class TestExecutionEnvelope:
    """List endpoints return the ``{status, data}`` envelope with a CursorPage."""

    @pytest.mark.asyncio
    async def test_messages_envelope_shape(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetch = AsyncMock(return_value=[])
        async with client as c:
            response = await c.get(f"/api/v1/execution/sessions/{_SESSION_ID}/messages")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
        assert data["items"] == []
        assert data["next_cursor"] is None
        assert data["has_more"] is False


class TestExecutionPagination:
    """``after``/``limit`` handling and pagination stability through the HTTP layer."""

    @pytest.mark.asyncio
    async def test_after_and_limit_bound_params(self, client: AsyncClient, mock_conn: AsyncMock):
        mock_conn.fetch = AsyncMock(return_value=[])
        after_id = uuid.uuid4()
        cursor = encode_cursor(1000, str(after_id))
        async with client as c:
            response = await c.get(
                f"/api/v1/execution/sessions/{_SESSION_ID}/messages",
                params={"after": cursor, "limit": 5},
            )
        assert response.status_code == 200
        sql = mock_conn.fetch.call_args.args[0]
        args = mock_conn.fetch.call_args.args[1:]
        # LIMIT limit+1 is the has_more probe.
        assert "LIMIT $" in sql
        assert args[-1] == 6
        # The decoded cursor's (source_created_at, id) are bound as params.
        assert 1000 in args
        assert after_id in args

    @pytest.mark.asyncio
    async def test_malformed_after_returns_400(self, client: AsyncClient, mock_conn: AsyncMock):
        async with client as c:
            response = await c.get(
                f"/api/v1/execution/sessions/{_SESSION_ID}/messages",
                params={"after": "not-a-valid-cursor!!!"},
            )
        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_two_page_walk_disjoint_ids(self, client: AsyncClient, mock_conn: AsyncMock):
        page1_ids = [uuid.uuid4() for _ in range(3)]
        page2_ids = [uuid.uuid4() for _ in range(2)]
        page1_rows = [
            _row(_message_row_data(source_created_at=i, id=pid))
            for i, pid in enumerate(page1_ids)
        ]
        page2_rows = [
            _row(_message_row_data(source_created_at=100 + i, id=pid))
            for i, pid in enumerate(page2_ids)
        ]
        mock_conn.fetch = AsyncMock(side_effect=[page1_rows, page2_rows])

        async with client as c:
            r1 = await c.get(
                f"/api/v1/execution/sessions/{_SESSION_ID}/messages", params={"limit": 2}
            )
            p1 = r1.json()
            assert r1.status_code == 200
            assert p1["data"]["has_more"] is True
            next_cursor_value = p1["data"]["next_cursor"]
            assert next_cursor_value is not None
            page1_ids_returned = {item["id"] for item in p1["data"]["items"]}

            r2 = await c.get(
                f"/api/v1/execution/sessions/{_SESSION_ID}/messages",
                params={"limit": 2, "after": next_cursor_value},
            )
            p2 = r2.json()
            assert r2.status_code == 200
            page2_ids_returned = {item["id"] for item in p2["data"]["items"]}

        # Page 1 returns limit rows (the extra fetch row is the has_more probe),
        # and the two pages never overlap — stable under append-only ingest.
        assert len(page1_ids_returned) == 2
        assert page1_ids_returned.isdisjoint(page2_ids_returned)
        assert page2_ids_returned == {str(pid) for pid in page2_ids}
