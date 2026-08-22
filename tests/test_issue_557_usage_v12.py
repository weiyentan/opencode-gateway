"""Tests for issue #557 — raw token fields and provider semantics.

Covers the usage query API additions:
- RecordRow raw token fields + provider (null → JSON null).
- SessionSummary ``total_reasoning_tokens`` + ``primary_provider``.
- AggregateRow ``cache_hit_ratio`` + ``provider_breakdown``.
- Deprecated ``active_tokens`` field + ``Deprecation`` HTTP header window.
- Agent runs list/detail reasoning + primary provider fields.
- Ingest normalization of empty-string provider values to NULL.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from app.api.usage import _deprecation_header_value
from app.core.config import Settings
from tests.test_ingest import (
    _add_transaction_support,
    _auth_row,
    _build_ingest_app,
    _new_record_side_effect,
    _valid_ingest_payload,
)
from tests.test_usage import _mk_aggregate_row, _mk_record_row, _mk_session_row

_AGG_PARAMS = {
    "start_date": "2025-07-01T00:00:00Z",
    "end_date": "2025-07-31T23:59:59Z",
}

_SESSION_ID = uuid.uuid4()
_CLIENT_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()


def _row(data: dict) -> MagicMock:
    """Return a MagicMock that behaves like an asyncpg Record."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _future_sunset_settings(days: int = 45) -> Settings:
    return Settings(
        active_tokens_deprecation_sunset=datetime.now(timezone.utc)
        + timedelta(days=days)
    )


# ══════════════════════════════════════════════════════════════════════════
#  Deprecation window helper (pure)
# ══════════════════════════════════════════════════════════════════════════


class TestActiveTokensDeprecationWindow:
    """The Deprecation header is emitted only inside the 90-day window."""

    def test_header_emitted_within_window(self):
        sunset = datetime.now(timezone.utc) + timedelta(days=45)
        value = _deprecation_header_value(sunset)
        assert value == "active_tokens; sunset=" + sunset.isoformat()

    def test_header_omitted_after_sunset(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert _deprecation_header_value(past) is None

    def test_header_omitted_when_sunset_unset(self):
        assert _deprecation_header_value(None) is None

    def test_header_omitted_exactly_at_sunset(self):
        sunset = datetime.now(timezone.utc)
        assert _deprecation_header_value(sunset) is None


# ══════════════════════════════════════════════════════════════════════════
#  Records — raw token fields + provider + active_tokens deprecation
# ══════════════════════════════════════════════════════════════════════════


class TestRecordRowV12Fields:
    @pytest.mark.asyncio
    async def test_records_include_raw_token_fields_and_provider(
        self, client: AsyncClient, mock_conn: AsyncMock, monkeypatch
    ):
        monkeypatch.setattr(
            "app.api.usage.get_settings", lambda: _future_sunset_settings()
        )
        row = _mk_record_row(
            provider="openai",
            reasoning_tokens=12,
            cache_read_tokens=3,
            cache_write_tokens=2,
        )
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[row])

        async with client as c:
            response = await c.get("/api/v1/usage/records", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"]["items"][0]
        assert data["provider"] == "openai"
        assert data["reasoning_tokens"] == 12
        assert data["cache_read_tokens"] == 3
        assert data["cache_write_tokens"] == 2
        # Deprecated field: input + output (Active Tokens)
        assert data["active_tokens"] == 150
        assert response.headers.get("deprecation", "").startswith(
            "active_tokens; sunset="
        )

    @pytest.mark.asyncio
    async def test_null_provider_serializes_as_json_null(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _mk_record_row(provider=None)
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[row])

        async with client as c:
            response = await c.get("/api/v1/usage/records", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"]["items"][0]
        assert "provider" in data
        assert data["provider"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Sessions — reasoning aggregate + primary provider
# ══════════════════════════════════════════════════════════════════════════


class TestSessionSummaryV12Fields:
    @pytest.mark.asyncio
    async def test_sessions_include_reasoning_and_primary_provider(
        self, client: AsyncClient, mock_conn: AsyncMock, monkeypatch
    ):
        monkeypatch.setattr(
            "app.api.usage.get_settings", lambda: _future_sunset_settings()
        )
        row = _row(
            {
                "id": uuid.uuid4(),
                "client_id": _CLIENT_ID,
                "source_database_id": _SOURCE_DB_ID,
                "first_message_at": datetime(2025, 7, 1, tzinfo=timezone.utc),
                "last_message_at": datetime(2025, 7, 16, tzinfo=timezone.utc),
                "message_count": 5,
                "total_input_tokens": 500,
                "total_output_tokens": 250,
                "total_cached_tokens": 0,
                "total_cache_read_tokens": 10,
                "total_cache_write_tokens": 5,
                "total_estimated_cost_usd": Decimal("0.0175"),
                "project_id": None,
                "project_label": None,
                "workspace_id": None,
                "agent": None,
                "parent_session_id": None,
                "session_title": None,
                "code_change_count": 0,
                "code_change_additions": 0,
                "code_change_deletions": 0,
                "total_reasoning_tokens": 77,
                "primary_provider": "openai",
            }
        )
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[row])

        async with client as c:
            response = await c.get("/api/v1/usage/sessions", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"]["items"][0]
        assert data["total_reasoning_tokens"] == 77
        assert data["primary_provider"] == "openai"
        assert data["active_tokens"] == 750
        assert response.headers.get("deprecation", "").startswith(
            "active_tokens; sunset="
        )

    @pytest.mark.asyncio
    async def test_sessions_legacy_rows_default_reasoning_and_provider(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Rows without the new columns (legacy mock factories) still
        serialize with zeroed reasoning and null provider — additive."""
        row = _mk_session_row()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[row])

        async with client as c:
            response = await c.get("/api/v1/usage/sessions", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"]["items"][0]
        assert data["total_reasoning_tokens"] == 0
        assert data["primary_provider"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Aggregates — cache hit ratio + provider breakdown
# ══════════════════════════════════════════════════════════════════════════


class TestAggregateRowV12Fields:
    @pytest.mark.asyncio
    async def test_total_row_includes_cache_hit_ratio_and_provider_breakdown(
        self, client: AsyncClient, mock_conn: AsyncMock, monkeypatch
    ):
        monkeypatch.setattr(
            "app.api.usage.get_settings", lambda: _future_sunset_settings()
        )
        total_row = _row(
            {
                "group_value": "total",
                "total_input_tokens": 100,
                "total_output_tokens": 50,
                "total_cached_tokens": 10,
                "total_reasoning_tokens": 5,
                "total_cache_read_tokens": 25,
                "total_cache_write_tokens": 5,
                "total_estimated_cost_usd": Decimal("0.0105"),
                "record_count": 3,
                "session_count": 2,
                "model_count": 1,
                "provider_breakdown": json.dumps({"openai": 2, "anthropic": 1}),
            }
        )
        mock_conn.fetchrow = AsyncMock(return_value=total_row)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/usage/aggregates", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"][0]
        # cache hit ratio = cache_read / (input + cache_read) = 25/125
        assert data["cache_hit_ratio"] == 0.2
        assert data["provider_breakdown"] == {"openai": 2, "anthropic": 1}
        assert data["active_tokens"] == 150
        assert response.headers.get("deprecation", "").startswith(
            "active_tokens; sunset="
        )

    @pytest.mark.asyncio
    async def test_total_row_zero_denominator_yields_null_ratio(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        total_row = _mk_aggregate_row(
            total_input_tokens=0, total_cache_read_tokens=0
        )
        mock_conn.fetchrow = AsyncMock(return_value=total_row)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get("/api/v1/usage/aggregates", params=_AGG_PARAMS)

        assert response.status_code == 200
        data = response.json()["data"][0]
        assert data["cache_hit_ratio"] is None
        assert data["provider_breakdown"] == {}

    @pytest.mark.asyncio
    async def test_grouped_row_includes_breakdown_from_same_query(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        rows = [
            _row(
                {
                    "group_value": "gpt-4",
                    "total_input_tokens": 300,
                    "total_output_tokens": 150,
                    "total_cached_tokens": 10,
                    "total_reasoning_tokens": 5,
                    "total_cache_read_tokens": 3,
                    "total_cache_write_tokens": 2,
                    "total_estimated_cost_usd": Decimal("0.0105"),
                    "record_count": 3,
                    "session_count": 2,
                    "model_count": 1,
                    "project_label": None,
                    "agent": None,
                    "provider_breakdown": json.dumps({"openai": 3}),
                }
            )
        ]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={**_AGG_PARAMS, "group_by": "model"},
            )

        assert response.status_code == 200
        data = response.json()["data"][0]
        assert data["provider_breakdown"] == {"openai": 3}
        assert data["cache_hit_ratio"] is not None


# ══════════════════════════════════════════════════════════════════════════
#  Ingest — provider normalization
# ══════════════════════════════════════════════════════════════════════════


class TestIngestProviderNormalization:
    @pytest.mark.asyncio
    async def test_empty_string_provider_normalized_to_null(self, monkeypatch):
        """A v1.2 payload with provider='' stores NULL in both the legacy
        record table and the canonical usage_events table."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            _auth_row(),
            *_new_record_side_effect(1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": datetime(
                        2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc
                    ).isoformat(),
                    "provider": "",
                },
            ],
        )

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

        # Legacy table INSERT: provider is the 12th positional value.
        legacy_inserts = [
            call
            for call in mock_conn.fetchrow.call_args_list
            if str(call[0][0]).startswith("INSERT INTO opencode_usage_records")
        ]
        assert len(legacy_inserts) == 1
        assert legacy_inserts[0][0][12] is None

        # Canonical table INSERT: provider is the 15th positional value.
        canonical_inserts = [
            call
            for call in mock_conn.execute.call_args_list
            if str(call[0][0]).startswith("INSERT INTO usage_events")
        ]
        assert len(canonical_inserts) == 1
        assert canonical_inserts[0][0][15] is None

    @pytest.mark.asyncio
    async def test_non_empty_provider_persisted_verbatim(self, monkeypatch):
        """A v1.2 payload with provider='openai' persists it verbatim."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            _auth_row(),
            *_new_record_side_effect(1),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        client = _build_ingest_app(mock_conn, monkeypatch=monkeypatch)

        payload = _valid_ingest_payload(
            schema_version="1.2",
            records=[
                {
                    "source_record_id": "rec-001",
                    "session_id": str(_SESSION_ID),
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 0,
                    "estimated_cost_usd": "0.0035",
                    "reported_at": datetime(
                        2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc
                    ).isoformat(),
                    "provider": "openai",
                },
            ],
        )

        async with client as c:
            response = await c.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 200
        legacy_inserts = [
            call
            for call in mock_conn.fetchrow.call_args_list
            if str(call[0][0]).startswith("INSERT INTO opencode_usage_records")
        ]
        assert legacy_inserts[0][0][12] == "openai"
        canonical_inserts = [
            call
            for call in mock_conn.execute.call_args_list
            if str(call[0][0]).startswith("INSERT INTO usage_events")
        ]
        assert canonical_inserts[0][0][15] == "openai"


# ══════════════════════════════════════════════════════════════════════════
#  Agent runs — reasoning + primary provider (merged Sessions table)
# ══════════════════════════════════════════════════════════════════════════


def _mk_agent_run_v12_row() -> MagicMock:
    return _row(
        {
            "id": uuid.uuid4(),
            "client_id": _CLIENT_ID,
            "source_database_id": _SOURCE_DB_ID,
            "external_session_id": "ses_abc123",
            "project_id": None,
            "project_label": None,
            "workspace_id": None,
            "agent": "researcher",
            "parent_session_id": None,
            "message_count": 5,
            "total_input_tokens": 500,
            "total_output_tokens": 250,
            "total_cached_tokens": 0,
            "total_cache_read_tokens": 10,
            "total_cache_write_tokens": 5,
            "total_estimated_cost_usd": Decimal("0.0175"),
            "last_message_at": datetime(2025, 7, 16, tzinfo=timezone.utc),
            "_status": "completed",
            "child_run_count": 0,
            "session_title": None,
            "session_model": None,
            "code_change_count": 0,
            "code_change_additions": 0,
            "code_change_deletions": 0,
            "total_reasoning_tokens": 42,
            "primary_provider": "anthropic",
        }
    )


class TestAgentRunV12Fields:
    @pytest.mark.asyncio
    async def test_agent_runs_list_includes_provider_and_reasoning(
        self, client: AsyncClient, mock_conn: AsyncMock, monkeypatch
    ):
        monkeypatch.setattr(
            "app.api.usage.get_settings", lambda: _future_sunset_settings()
        )
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_agent_run_v12_row()])

        async with client as c:
            response = await c.get("/api/v1/usage/agent-runs")

        assert response.status_code == 200
        data = response.json()["data"]["items"][0]
        assert data["total_reasoning_tokens"] == 42
        assert data["primary_provider"] == "anthropic"
        assert data["active_tokens"] == 750
        assert response.headers.get("deprecation", "").startswith(
            "active_tokens; sunset="
        )

    @pytest.mark.asyncio
    async def test_agent_runs_detail_includes_provider_and_reasoning(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        session_id = uuid.uuid4()
        detail = {
            "id": session_id,
            "client_id": _CLIENT_ID,
            "source_database_id": _SOURCE_DB_ID,
            "external_session_id": "ses_abc123",
            "first_message_at": datetime(2025, 7, 1, tzinfo=timezone.utc),
            "last_message_at": datetime(2025, 7, 16, tzinfo=timezone.utc),
            "message_count": 5,
            "total_input_tokens": 500,
            "total_output_tokens": 250,
            "total_cached_tokens": 0,
            "total_cache_read_tokens": 10,
            "total_cache_write_tokens": 5,
            "total_estimated_cost_usd": Decimal("0.0175"),
            "project_id": None,
            "project_label": None,
            "workspace_id": None,
            "agent": "researcher",
            "parent_session_id": None,
            "parent_internal_id": None,
            "ctx_present": None,
            "code_change_count": None,
            "code_change_additions": None,
            "code_change_deletions": None,
            "ctx_session_model": None,
            "ctx_session_cost": None,
            "ctx_title": None,
            "ctx_source_directory": None,
            "ctx_source_path": None,
            "ctx_source_input_tokens": None,
            "ctx_source_output_tokens": None,
            "ctx_source_cached_tokens": None,
            "ctx_source_reasoning_tokens": None,
            "total_reasoning_tokens": 33,
            "primary_provider": "openai",
        }
        mock_conn.fetchrow = AsyncMock(return_value=_row(detail))
        mock_conn.fetch = AsyncMock(side_effect=[[], []])

        async with client as c:
            response = await c.get(f"/api/v1/usage/agent-runs/{session_id}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_reasoning_tokens"] == 33
        assert data["primary_provider"] == "openai"
        assert data["active_tokens"] == 750
