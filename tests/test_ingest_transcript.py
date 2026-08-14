# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for execution-transcript ingest collections (issue #465, ADR 0016).

Covers the optional ``messages`` and ``parts`` batch collections added to the
``/ingest`` endpoint at schema version 1.3:

- schema-version gate: ``"1.3"`` accepted, unknown versions still rejected
- idempotent upserts into ``observed_messages`` / ``observed_parts``
- same-transaction ``observed_tool_calls`` extraction for tool parts
- ingest-time ``redact_dict`` redaction of message/part/tool payloads
- truncation caps (``GATEWAY_TOOL_PAYLOAD_MAX_CHARS`` /
  ``GATEWAY_PART_DATA_MAX_CHARS``) with a ``truncated`` marker
- partial-success: malformed transcript items never block accepted records

Follows the mock pattern from ``tests/test_ingest.py`` (AsyncMock connection,
side-effect lists for ``fetchrow``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.ingest import (
    MessagePayload,
    PartPayload,
    _extract_message_columns,
    _extract_part_columns,
    _process_message,
    _process_part,
    _truncate_json_value,
)
from app.core.config import Settings
from tests.test_ingest import (
    _CLIENT_ID,
    _SOURCE_DB_ID,
    _add_transaction_support,
    _auth_row,
    _build_ingest_app,
    _new_record_side_effect,
    _valid_ingest_payload,
)

# ── Shared test data ────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _mk_message_payload(
    *,
    external_message_id: str = "mes_test",
    external_session_id: str = "ses_transcript",
    data: dict | None = None,
    **kwargs,
) -> dict:
    """Return an execution-transcript message projection dict."""
    defaults: dict = {
        "external_message_id": external_message_id,
        "external_session_id": external_session_id,
        "data": data
        if data is not None
        else {
            "role": "assistant",
            "agent": "general",
            "mode": "code",
            "cost": 0.0035,
            "tokens": {"input": 100, "output": 50},
        },
    }
    defaults.update(kwargs)
    return defaults


def _mk_part_payload(
    *,
    external_part_id: str = "part_test",
    external_message_id: str = "mes_test",
    external_session_id: str = "ses_transcript",
    data: dict | None = None,
    **kwargs,
) -> dict:
    """Return an execution-transcript part projection dict."""
    defaults: dict = {
        "external_part_id": external_part_id,
        "external_message_id": external_message_id,
        "external_session_id": external_session_id,
        "data": data if data is not None else {"type": "text", "text": "hello"},
    }
    defaults.update(kwargs)
    return defaults


def _mk_tool_part_payload(**kwargs) -> dict:
    """Return a ``part_type = "tool"`` projection with a bash tool call."""
    return _mk_part_payload(
        data={
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls -la"},
                "output": "total 0",
            },
        },
        **kwargs,
    )


def _row_with_id() -> MagicMock:
    row = MagicMock()
    row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    return row


# ════════════════════════════════════════════════════════════════════════════
#  Schema-version gate
# ════════════════════════════════════════════════════════════════════════════


class TestSchemaVersionGate:
    """schema_version 1.3 is accepted; unknown versions are still rejected."""

    @pytest.mark.asyncio
    async def test_schema_version_1_3_accepted(self, monkeypatch):
        """A payload with schema_version 1.3 is accepted (not 400)."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, None]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="1.3", records=[])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 0

    @pytest.mark.asyncio
    async def test_unknown_schema_version_rejected(self, monkeypatch):
        """An unrecognized schema_version is still rejected with 400."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock(return_value=auth)
        mock_conn.execute = AsyncMock()

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(schema_version="9.9")

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
#  Endpoint-level message / part projection processing
# ════════════════════════════════════════════════════════════════════════════


class TestMessagesEndpoint:
    """messages collections flow through the ingest endpoint projection path."""

    @pytest.mark.asyncio
    async def test_single_message_accepted(self, monkeypatch):
        """A single message projection is upserted and counted accepted."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,   # 1. auth
            None,   # 2. source_database check
            None,   # 3. resolve session_id for message → not found
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])
        payload["messages"] = [_mk_message_payload()]

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        msg_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO observed_messages" in str(call)
        ]
        assert len(msg_inserts) == 1
        assert "ON CONFLICT" in str(msg_inserts[0])

    @pytest.mark.asyncio
    async def test_ingest_without_messages_behaves_as_before(self, monkeypatch):
        """Omitting messages/parts issues no transcript writes."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [auth, None]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 0

        msg_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO observed_messages" in str(call)
        ]
        part_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO observed_parts" in str(call)
        ]
        assert msg_inserts == []
        assert part_inserts == []


class TestPartsEndpoint:
    """parts collections flow through the ingest endpoint projection path."""

    @pytest.mark.asyncio
    async def test_single_part_accepted(self, monkeypatch):
        """A single non-tool part is upserted via RETURNING id, no tool call."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check
            None,             # 3. resolve session_id → not found
            None,             # 4. resolve observed message id → not found
            _row_with_id(),   # 5. observed_parts INSERT ... RETURNING id
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])
        payload["parts"] = [_mk_part_payload()]

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        part_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO observed_parts" in str(call)
        ]
        assert len(part_inserts) == 1
        assert "RETURNING id" in str(part_inserts[0])

        tool_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(call)
        ]
        assert tool_inserts == []

    @pytest.mark.asyncio
    async def test_tool_part_produces_tool_call(self, monkeypatch):
        """A tool part writes observed_parts AND observed_tool_calls."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,             # 1. auth
            None,             # 2. source_database check
            None,             # 3. resolve session_id → not found
            None,             # 4. resolve observed message id → not found
            _row_with_id(),   # 5. observed_parts INSERT ... RETURNING id
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])
        payload["parts"] = [_mk_tool_part_payload()]

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 0

        tool_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(call)
        ]
        assert len(tool_inserts) == 1


# ════════════════════════════════════════════════════════════════════════════
#  _process_message handler
# ════════════════════════════════════════════════════════════════════════════


class TestProcessMessageHandler:
    """Redaction, truncation, and idempotent upsert for messages."""

    @pytest.mark.asyncio
    async def test_message_redacts_secret_values(self):
        """Secret-like values in message.data are ***-replaced before persistence."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_conn.fetchrow = AsyncMock(return_value=None)

        msg = MessagePayload(
            external_message_id="mes_secret",
            external_session_id="ses_x",
            data={
                "role": "assistant",
                "env": {"GITHUB_TOKEN": "ghp_plaintext_secret"},
            },
        )

        await _process_message(
            mock_conn, msg, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
        )

        insert_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_messages" in str(c)
        ][0]
        data_arg = insert_call.args[-1]
        assert data_arg["env"]["GITHUB_TOKEN"] == "***"
        assert "ghp_plaintext_secret" not in str(insert_call.args)

    @pytest.mark.asyncio
    async def test_message_truncates_data_with_marker(self):
        """Over-cap message.data is stored truncated with a truncated marker."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_conn.fetchrow = AsyncMock(return_value=None)

        msg = MessagePayload(
            external_message_id="mes_big",
            external_session_id="ses_x",
            data={"role": "assistant", "text": "x" * 1000},
        )

        await _process_message(
            mock_conn, msg, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=50,
        )

        insert_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_messages" in str(c)
        ][0]
        data_arg = insert_call.args[-1]
        assert data_arg["truncated"] is True
        assert len(data_arg["content"]) == 50

    @pytest.mark.asyncio
    async def test_message_upsert_preserves_first_seen(self):
        """The upsert updates last_seen_at but never first_seen_at."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_conn.fetchrow = AsyncMock(return_value=None)

        msg = MessagePayload(
            external_message_id="mes_upsert",
            external_session_id="ses_x",
            data={"role": "assistant"},
        )

        await _process_message(
            mock_conn, msg, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
        )

        insert_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_messages" in str(c)
        ][0]
        sql = str(insert_call)
        assert "last_seen_at = EXCLUDED.last_seen_at" in sql
        do_update = sql.split("DO UPDATE SET")[1]
        assert "first_seen_at" not in do_update

    @pytest.mark.asyncio
    async def test_message_missing_role_raises(self):
        """A message without data.role raises ValueError (projection-rejected)."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_conn.fetchrow = AsyncMock(return_value=None)

        msg = MessagePayload(
            external_message_id="mes_norole",
            external_session_id="ses_x",
            data={"agent": "general"},
        )

        with pytest.raises(ValueError, match="role"):
            await _process_message(
                mock_conn, msg, _CLIENT_ID, _SOURCE_DB_ID, _now(),
                part_data_max_chars=65536,
            )

    @pytest.mark.asyncio
    async def test_message_missing_data_raises(self):
        """A message without data raises ValueError (projection-rejected)."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        mock_conn.fetchrow = AsyncMock(return_value=None)

        msg = MessagePayload(
            external_message_id="mes_nodata",
            external_session_id="ses_x",
            data=None,
        )

        with pytest.raises(ValueError, match="data"):
            await _process_message(
                mock_conn, msg, _CLIENT_ID, _SOURCE_DB_ID, _now(),
                part_data_max_chars=65536,
            )


# ════════════════════════════════════════════════════════════════════════════
#  _process_part handler
# ════════════════════════════════════════════════════════════════════════════


class TestProcessPartHandler:
    """Redaction, truncation, and same-transaction tool-call extraction."""

    def _setup_part_conn(self) -> tuple[AsyncMock, MagicMock]:
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        part_row = _row_with_id()
        mock_conn.fetchrow = AsyncMock(side_effect=[None, None, part_row])
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        return mock_conn, part_row

    @pytest.mark.asyncio
    async def test_part_redacts_secret_values(self):
        """Secret-like values in tool input are ***-replaced before persistence."""
        mock_conn, _ = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_secret",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"env": {"AWS_SECRET_ACCESS_KEY": "plaintext"}},
                    "output": "ok",
                },
            },
        )

        await _process_part(
            mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )

        tool_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(c)
        ][0]
        tool_input = tool_call.args[11]
        assert tool_input["env"]["AWS_SECRET_ACCESS_KEY"] == "***"
        assert "plaintext" not in str(tool_call.args)

    @pytest.mark.asyncio
    async def test_part_truncates_tool_input_output(self):
        """Tool input/output fields are bounded per field with a marker."""
        mock_conn, _ = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_big",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "x" * 100},
                    "output": "y" * 100,
                },
            },
        )

        await _process_part(
            mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
            tool_payload_max_chars=30,
        )

        tool_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(c)
        ][0]
        tool_input = tool_call.args[11]
        tool_output = tool_call.args[12]
        assert tool_input["truncated"] is True
        assert tool_output["truncated"] is True
        assert len(tool_input["content"]) == 30
        assert len(tool_output["content"]) == 30

    @pytest.mark.asyncio
    async def test_tool_part_produces_tool_call_with_part_id(self):
        """The tool call references the observed_parts id in the same write."""
        mock_conn, part_row = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_tool",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={"type": "tool", "tool": "bash", "state": {"input": {}, "output": ""}},
        )

        await _process_part(
            mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )

        # The part upsert must go through fetchrow (RETURNING id) inside a
        # transaction, and the tool call must carry that returned id.
        assert mock_conn.transaction.called
        part_insert = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO observed_parts" in str(c)
        ][0]
        assert "RETURNING id" in str(part_insert)

        tool_call = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(c)
        ][0]
        assert tool_call.args[4] == part_row["id"]
        assert tool_call.args[9] == "bash"

    @pytest.mark.asyncio
    async def test_non_tool_part_no_tool_call(self):
        """A text part writes only observed_parts, no observed_tool_calls."""
        mock_conn, _ = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_text",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={"type": "text", "text": "hello"},
        )

        await _process_part(
            mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
            part_data_max_chars=65536,
            tool_payload_max_chars=4096,
        )

        tool_calls = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO observed_tool_calls" in str(c)
        ]
        assert tool_calls == []

    @pytest.mark.asyncio
    async def test_part_missing_type_raises(self):
        """A part without data.type raises ValueError (projection-rejected)."""
        mock_conn, _ = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_notype",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={"text": "no type here"},
        )

        with pytest.raises(ValueError, match="type"):
            await _process_part(
                mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
                part_data_max_chars=65536,
                tool_payload_max_chars=4096,
            )

    @pytest.mark.asyncio
    async def test_tool_part_missing_tool_name_raises(self):
        """A tool part without a tool name raises ValueError."""
        mock_conn, _ = self._setup_part_conn()

        part = PartPayload(
            external_part_id="part_noname",
            external_message_id="mes_x",
            external_session_id="ses_x",
            data={"type": "tool", "state": {"input": {}, "output": ""}},
        )

        with pytest.raises(ValueError, match="tool"):
            await _process_part(
                mock_conn, part, _CLIENT_ID, _SOURCE_DB_ID, _now(),
                part_data_max_chars=65536,
                tool_payload_max_chars=4096,
            )


# ════════════════════════════════════════════════════════════════════════════
#  Extraction + truncation helpers
# ════════════════════════════════════════════════════════════════════════════


class TestExtractionHelpers:
    """Promoted-column extraction from redacted message/part data."""

    def test_extract_message_columns_promotes_fields(self):
        role, agent, mode, cost, in_tok, out_tok, parent = _extract_message_columns(
            {
                "role": "assistant",
                "agent": "general",
                "mode": "code",
                "cost": 0.0035,
                "tokens": {"input": 100, "output": 50},
                "parentID": "ses_parent",
            }
        )
        assert role == "assistant"
        assert agent == "general"
        assert mode == "code"
        assert float(cost) == 0.0035
        assert in_tok == 100
        assert out_tok == 50
        assert parent == "ses_parent"

    def test_extract_message_columns_missing_optional_fields(self):
        role, agent, mode, cost, in_tok, out_tok, parent = _extract_message_columns(
            {"role": "user"}
        )
        assert role == "user"
        assert agent is None
        assert mode is None
        assert cost is None
        assert in_tok is None
        assert out_tok is None
        assert parent is None

    def test_extract_part_columns_tool(self):
        part_type, name, status, input_, output = _extract_part_columns(
            {
                "type": "tool",
                "tool": "bash",
                "state": {"status": "completed", "input": {"a": 1}, "output": "ok"},
            }
        )
        assert part_type == "tool"
        assert name == "bash"
        assert status == "completed"
        assert input_ == {"a": 1}
        assert output == "ok"

    def test_extract_part_columns_text(self):
        part_type, name, status, input_, output = _extract_part_columns(
            {"type": "text", "text": "hello"}
        )
        assert part_type == "text"
        assert name is None
        assert status is None
        assert input_ is None
        assert output is None


class TestTruncationHelper:
    """JSON-value truncation with a truncated marker."""

    def test_short_value_unchanged(self):
        value = {"a": "b"}
        assert _truncate_json_value(value, 100) == value

    def test_long_value_marked_and_bounded(self):
        result = _truncate_json_value({"text": "x" * 100}, 20)
        assert result["truncated"] is True
        assert len(result["content"]) == 20


# ════════════════════════════════════════════════════════════════════════════
#  Partial-success semantics
# ════════════════════════════════════════════════════════════════════════════


class TestTranscriptPartialFailure:
    """Malformed transcript items never block accepted usage records."""

    @pytest.mark.asyncio
    async def test_malformed_part_does_not_block_usage_records(self, monkeypatch):
        """A malformed part is projection-rejected while the record is accepted."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,
            *_new_record_side_effect(record_count=1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(uuid.uuid4()),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": _now().isoformat(),
                },
            ],
        )
        payload["parts"] = [_mk_part_payload(data={"text": "no type"})]

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        assert data["projection_accepted_count"] == 0
        assert data["projection_rejected_count"] == 1

    @pytest.mark.asyncio
    async def test_malformed_message_counted_rejected(self, monkeypatch):
        """A valid message is accepted; a malformed one is counted rejected."""
        mock_conn = AsyncMock()
        auth = _auth_row()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            auth,   # 1. auth
            None,   # 2. source_database check
            None,   # 3. resolve session_id for valid message → not found
            None,   # 4. resolve session_id for malformed message → not found
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(records=[])
        payload["messages"] = [
            _mk_message_payload(external_message_id="mes_ok"),
            _mk_message_payload(external_message_id="mes_bad", data={"agent": "no-role"}),
        ]

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["projection_accepted_count"] == 1
        assert data["projection_rejected_count"] == 1


# ════════════════════════════════════════════════════════════════════════════
#  Config defaults for the truncation caps
# ════════════════════════════════════════════════════════════════════════════


class TestTruncationCapConfig:
    """GATEWAY_TOOL_PAYLOAD_MAX_CHARS / GATEWAY_PART_DATA_MAX_CHARS defaults."""

    def test_truncation_caps_defaults(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
        monkeypatch.delenv("GATEWAY_TOOL_PAYLOAD_MAX_CHARS", raising=False)
        monkeypatch.delenv("GATEWAY_PART_DATA_MAX_CHARS", raising=False)

        settings = Settings()
        assert settings.tool_payload_max_chars == 4096
        assert settings.part_data_max_chars == 65536

    def test_truncation_caps_env_override(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
        monkeypatch.setenv("GATEWAY_TOOL_PAYLOAD_MAX_CHARS", "100")
        monkeypatch.setenv("GATEWAY_PART_DATA_MAX_CHARS", "200")

        settings = Settings()
        assert settings.tool_payload_max_chars == 100
        assert settings.part_data_max_chars == 200
