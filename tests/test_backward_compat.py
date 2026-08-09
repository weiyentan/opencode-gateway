"""Backward-compatibility regression tests for the canonical event layer.

Issue #394 — verify that existing collectors, payloads, and dashboard
consumers keep working without changes after the canonical event layer
(issues #384–#393) landed:

1. **Legacy ingest payloads** without replay metadata produce outcomes
   identical to pre-change behaviour: first delivery is ``accepted`` with a
   canonical event; re-delivery is idempotent with no double-counting.
2. **Existing collector auth** (Two-Layer Auth) continues to work: the Admin
   API Key still passes both layers when its hash is registered as a
   collector credential, and the collector-credential layer is still
   enforced after the API-key middleware.
3. **Usage Record Consumer** treats every new outcome code (``accepted``,
   ``duplicate``, ``updated``, ``quarantined``, ``conflict``) as a
   successful delivery (2xx → commit, no DLQ), and DLQ routing is unchanged
   for 4xx / validation failures.
4. **Aurora Glass API contract** is unchanged: the field names/types the
   dashboard consumes for KPI totals, model mix, the sessions table,
   collector distribution, and collector health KPIs are all still present
   in the endpoint responses.

The cursor endpoint contract is verified by ``tests/test_cursor.py`` (7
tests, passing unmodified).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiokafka.structs import ConsumerRecord
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.consumer.consumer import Consumer
from app.core.factory import create_app
from app.db.session import get_session

# ── Shared test data ──────────────────────────────────────────────────────

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()
_SOURCE_DB_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()

_OUTCOME_CODES = ("accepted", "duplicate", "updated", "quarantined", "conflict")


def _mk_ts() -> datetime:
    return datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017


def _auth_row(
    client_id: uuid.UUID = _CLIENT_ID,
    credential_id: uuid.UUID = _CREDENTIAL_ID,
    client_name: str = "test-client",
) -> MagicMock:
    """Return a mock row that passes require_collector_token."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "credential_id": credential_id,
        "revoked_at": None,
        "last_used_at": None,
        "client_id": client_id,
        "client_name": client_name,
        "client_is_active": True,
    }.__getitem__
    return row


def _valid_legacy_payload() -> dict:
    """Return a pre-change ingest payload — NO replay metadata fields.

    Matches the exact payload shape collectors have always sent:
    schema_version/collector_version/source_database_id/records only.
    """
    return {
        "schema_version": "1.0",
        "collector_version": "0.1.0",
        "source_database_id": str(_SOURCE_DB_ID),
        "records": [
            {
                "source_record_id": "rec-legacy-001",
                "session_id": str(_SESSION_ID),
                "model": "gpt-4",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_tokens": 0,
                "estimated_cost_usd": "0.0035",
                "reported_at": _mk_ts().isoformat(),
            },
        ],
    }


def _add_transaction_support(mock_conn: AsyncMock) -> None:
    """Give ``mock_conn.transaction()`` an async-context-manager shape.

    The winner path and Replay Merge wrap their statements in
    ``async with conn.transaction():`` blocks.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=ctx)


def _winner_fetchrow_items() -> list:
    """fetchrow side-effect items for ONE winning first-delivery record.

    Order: [sd_check] + [model_upsert, atomic_insert, session_upsert,
    identity_select, identity_insert, model_lookup, session_lookup,
    event_lookup] — the canonical-event recording path (#387).
    """
    insert_row = MagicMock()
    insert_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    session_row = MagicMock()
    session_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    identity_insert_row = MagicMock()
    identity_insert_row.__getitem__.side_effect = {
        "id": uuid.uuid4(), "canonical_parent_id": None,
    }.__getitem__
    model_lookup_row = MagicMock()
    model_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    session_lookup_row = MagicMock()
    session_lookup_row.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
    return [
        None,                                  # sd check (new)
        None, insert_row, session_row,         # _process_one_record
        None, identity_insert_row,             # resolve_canonical_identity
        model_lookup_row,                      # model lookup
        session_lookup_row,                    # session lookup
        None,                                  # event lookup (no existing)
    ]


def _build_two_layer_app(
    mock_conn: AsyncMock,
    *,
    monkeypatch,
) -> AsyncClient:
    """Build an app with BOTH auth layers active (Two-Layer Auth).

    Unlike ``_build_ingest_app`` in ``test_ingest.py`` (which deletes
    ``GATEWAY_API_KEY`` to isolate the collector-token layer), this keeps
    the API key configured so every request must pass the
    ``ApiKeyMiddleware`` AND ``require_collector_token``.
    """
    _add_transaction_support(mock_conn)

    monkeypatch.setenv("GATEWAY_API_KEY", "test-api-key")
    monkeypatch.setenv("GATEWAY_ENV", "development")
    import importlib

    import app.core.config as _cfg

    importlib.reload(_cfg)

    app = create_app(configure_logging=False)

    async def _override(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════════════
#  1. Legacy ingest payloads without replay metadata → identical outcomes
# ══════════════════════════════════════════════════════════════════════════


class TestLegacyIngestPayloads:
    """Payloads without replay metadata process exactly as before #387."""

    @pytest.mark.asyncio
    async def test_first_delivery_accepted_with_canonical_event(self, monkeypatch):
        """A legacy payload (no replay metadata) is accepted and produces a
        canonical event + ingest attempt — the #387 behaviour applied to
        pre-change payloads."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            _auth_row(),
            *_winner_fetchrow_items(),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_legacy_payload(),
                headers={"Authorization": "Bearer test-api-key"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["accepted_count"] == 1
        assert data["rejected_count"] == 0
        result = data["results"][0]
        assert result["status"] == "accepted"
        assert result["event_id"] is not None
        assert result["attempt_id"] is not None

        # Canonical event + ingest attempt written for the legacy payload
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(event_inserts) == 1
        assert len(attempt_inserts) == 1
        # Replay metadata defaults to None — the attempt is a real-time delivery
        assert attempt_inserts[0].args[-2] is None  # replay_id column

    @pytest.mark.asyncio
    async def test_redelivery_idempotent_no_double_count(self, monkeypatch):
        """Re-posting the same legacy payload yields the same outcome
        (accepted, idempotent) with NO second canonical event and NO second
        usage record — replay-safety for pre-change re-deliveries."""
        mock_conn = AsyncMock()
        _add_transaction_support(mock_conn)

        existing_model = MagicMock()
        existing_model.__getitem__.side_effect = {"id": uuid.uuid4()}.__getitem__
        existing_dedup = MagicMock()
        existing_dedup.__getitem__.side_effect = {
            "id": uuid.uuid4(),
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "estimated_cost_usd": Decimal("0.0035"),
        }.__getitem__
        lock_row = MagicMock()
        lock_row.__getitem__.side_effect = {
            "provider": None,
            "mode": None,
            "finish_reason": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "session_id": _SESSION_ID,
        }.__getitem__

        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            # ── Delivery 1: winner path ──
            _auth_row(),
            *_winner_fetchrow_items(),
            # ── Delivery 2: identical loser path ──
            _auth_row(),
            None,              # sd check (exists)
            existing_model,    # model upsert (exists)
            None,              # atomic INSERT → ON CONFLICT (loser)
            existing_dedup,    # dedup query → identical match
            lock_row,          # _apply_replay_merge: SELECT FOR UPDATE
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)
        payload = _valid_legacy_payload()

        async with client as c:
            first = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer test-api-key"},
            )
            second = await c.post(
                "/ingest", json=payload,
                headers={"Authorization": "Bearer test-api-key"},
            )

        # Same outcome for the same input: accepted both times
        assert first.status_code == 200
        assert second.status_code == 200
        first_data = first.json()["data"]
        second_data = second.json()["data"]
        assert first_data["accepted_count"] == 1
        assert second_data["accepted_count"] == 1
        assert second_data["results"][0]["status"] == "accepted"
        assert "duplicate" in (second_data["results"][0]["reason"] or "").lower()

        # No double-counting: the atomic usage-record INSERT was attempted
        # both deliveries but produced only ONE canonical event and ONE
        # ingest attempt (the second delivery was idempotent).
        record_inserts = [
            call for call in mock_conn.fetchrow.call_args_list
            if "INSERT INTO opencode_usage_records" in str(call)
        ]
        event_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_events" in str(call)
        ]
        attempt_inserts = [
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO usage_ingest_attempts" in str(call)
        ]
        assert len(record_inserts) == 2  # attempted on both deliveries
        assert len(event_inserts) == 1  # created only on the first
        assert len(attempt_inserts) == 1


# ══════════════════════════════════════════════════════════════════════════
#  2. Consumer outcome-code vocabulary + unchanged DLQ routing
# ══════════════════════════════════════════════════════════════════════════


def _ingest_response_body(outcome: str) -> dict:
    """Simulate the gateway's 200 /ingest response for a given outcome.

    Mirrors the real envelope: ``{status: "ok", data: {batch_id,
    accepted_count, rejected_count, results: [...]}}`` with
    ``results[0].status`` set to the canonical outcome vocabulary.
    """
    return {
        "status": "ok",
        "data": {
            "batch_id": str(uuid.uuid4()),
            "accepted_count": 1 if outcome == "accepted" else 0,
            "rejected_count": 0 if outcome == "accepted" else 1,
            "results": [{"index": 0, "status": outcome}],
        },
    }


class TestConsumerOutcomeCodes:
    """The consumer must treat every new outcome code as a successful
    delivery (the gateway returns 2xx for all of them) — no code changes
    needed on the consumer side."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", _OUTCOME_CODES)
    async def test_outcome_code_is_successful_delivery(self, outcome):
        """A 200 response carrying any of the five outcome codes is
        committed and never routed to the DLQ."""
        consumer = Consumer(
            kafka_brokers="broker:9092",
            gateway_base_url="http://gateway:8000",
            gateway_collector_token="tok",
        )
        consumer._http_client = AsyncMock()
        resp = MagicMock(status_code=200)
        resp.text = json.dumps(_ingest_response_body(outcome))
        consumer._http_client.post = AsyncMock(return_value=resp)
        consumer._consumer = AsyncMock()
        consumer._consumer.commit = AsyncMock()
        consumer._producer = AsyncMock()
        consumer._producer.send_and_wait = AsyncMock()

        msg = MagicMock(spec=ConsumerRecord)
        msg.value = json.dumps(
            {
                "schema_version": "1.0",
                "collector_version": "0.1.0",
                "source_database_id": str(_SOURCE_DB_ID),
                "records": [
                    {
                        "source_record_id": "rec-c-001",
                        "session_id": "ses_abc",
                        "model": "gpt-4",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "reported_at": _mk_ts().isoformat(),
                    }
                ],
            }
        ).encode("utf-8")
        msg.offset = 42
        msg.partition = 0
        msg.topic = "opencode-usage"
        msg.key = None
        msg.headers = ()

        await consumer._process_message(msg)

        consumer._http_client.post.assert_called_once()
        consumer._consumer.commit.assert_called_once()
        consumer._producer.send_and_wait.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 422])
    async def test_dlq_routing_unchanged_for_4xx_and_validation(self, status_code):
        """4xx responses (including FastAPI 422 validation failures) still
        route to the DLQ with reason ``HTTP <code>`` and commit the offset —
        exactly as before the canonical event layer."""
        consumer = Consumer(
            kafka_brokers="broker:9092",
            gateway_base_url="http://gateway:8000",
            gateway_collector_token="tok",
        )
        consumer._http_client = AsyncMock()
        resp = MagicMock(status_code=status_code)
        resp.text = json.dumps(
            {"status": "error", "error": {"code": "VALIDATION_ERROR"}}
        )
        consumer._http_client.post = AsyncMock(return_value=resp)
        consumer._consumer = AsyncMock()
        consumer._consumer.commit = AsyncMock()
        consumer._producer = AsyncMock()
        consumer._producer.send_and_wait = AsyncMock()

        msg = MagicMock(spec=ConsumerRecord)
        msg.value = json.dumps(
            {
                "schema_version": "1.0",
                "collector_version": "0.1.0",
                "source_database_id": str(_SOURCE_DB_ID),
                "records": [
                    {
                        "source_record_id": "rec-dlq-001",
                        "session_id": "ses_abc",
                        "model": "gpt-4",
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "reported_at": _mk_ts().isoformat(),
                    }
                ],
            }
        ).encode("utf-8")
        msg.offset = 42
        msg.partition = 0
        msg.topic = "opencode-usage"
        msg.key = None
        msg.headers = ()

        await consumer._process_message(msg)

        consumer._http_client.post.assert_called_once()
        consumer._producer.send_and_wait.assert_called_once()
        consumer._consumer.commit.assert_called_once()
        (_topic, dlq_payload), _kwargs = (
            consumer._producer.send_and_wait.call_args
        )
        assert dlq_payload["reason"] == f"HTTP {status_code}"
        assert dlq_payload["original_topic"] == "opencode-usage"
        assert "payload" in dlq_payload


# ══════════════════════════════════════════════════════════════════════════
#  3. Existing collector auth (Two-Layer Auth) still works
# ══════════════════════════════════════════════════════════════════════════


class TestCollectorAuthBackwardCompat:
    """Collectors using the current auth flow keep working unchanged."""

    @pytest.mark.asyncio
    async def test_admin_api_key_passes_both_layers(self, monkeypatch):
        """The Admin API Key (with its hash registered as a collector
        credential) still passes BOTH auth layers on /ingest."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            _auth_row(),
            *_winner_fetchrow_items(),
        ]
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_legacy_payload(),
                headers={"Authorization": "Bearer test-api-key"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["accepted_count"] == 1

    @pytest.mark.asyncio
    async def test_collector_layer_still_enforced_after_middleware(self, monkeypatch):
        """Passing the API-key middleware is not enough: /ingest still
        requires a registered collector credential (second layer)."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # no credential match
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_legacy_payload(),
                headers={"Authorization": "Bearer test-api-key"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_api_key_still_rejected_by_middleware(self, monkeypatch):
        """Requests without the Admin API Key are rejected before the
        collector layer runs — unchanged middleware behaviour."""
        mock_conn = AsyncMock()
        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.post(
                "/ingest",
                json=_valid_legacy_payload(),
                headers={"Authorization": "Bearer collector-token"},
            )

        assert response.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
#  4. Aurora Glass API contract — field names the dashboard consumes
# ══════════════════════════════════════════════════════════════════════════


def _mk_aggregate_row(
    *,
    group_value: str = "total",
    total_input_tokens: int = 300,
    total_output_tokens: int = 150,
    total_cached_tokens: int = 10,
    total_reasoning_tokens: int = 5,
    total_cache_read_tokens: int = 3,
    total_cache_write_tokens: int = 2,
    cost: Decimal | None = Decimal("0.0105"),
    record_count: int = 3,
    session_count: int = 2,
    model_count: int = 1,
    project_label: str | None = None,
) -> MagicMock:
    """Return a MagicMock for an aggregate query result row."""
    row = MagicMock()
    data = {
        "group_value": group_value,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_reasoning_tokens": total_reasoning_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_estimated_cost_usd": cost,
        "record_count": record_count,
        "session_count": session_count,
        "model_count": model_count,
        "project_label": project_label,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


def _mk_session_row(
    *,
    session_id: uuid.UUID | None = None,
    session_title: str = "Legacy Session",
) -> MagicMock:
    """Return a MagicMock for a sessions-list query result row."""
    row = MagicMock()
    data = {
        "id": session_id or uuid.uuid4(),
        "client_id": _CLIENT_ID,
        "source_database_id": _SOURCE_DB_ID,
        "first_message_at": _mk_ts(),
        "last_message_at": _mk_ts(),
        "message_count": 5,
        "total_input_tokens": 500,
        "total_output_tokens": 250,
        "total_cached_tokens": 0,
        "total_cache_read_tokens": 10,
        "total_cache_write_tokens": 5,
        "project_id": None,
        "project_label": None,
        "workspace_id": None,
        "agent": None,
        "parent_session_id": None,
        "total_estimated_cost_usd": Decimal("0.0175"),
        "session_title": session_title,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.__iter__ = MagicMock(return_value=iter(data.keys()))
    return row


class TestAuroraGlassApiContract:
    """The usage/health response shapes the Aurora Glass dashboard consumes
    (KPI totals, model mix, sessions table, collector distribution, health
    KPIs) are unchanged after the canonical event layer.

    Aurora Glass lives in a separate repository — this suite verifies the
    API-contract side: every field name the dashboard's ``app.js`` reads
    must still be present in the endpoint responses.
    """

    @pytest.mark.asyncio
    async def test_kpi_totals_contract(self, client: AsyncClient, mock_conn: AsyncMock):
        """KPI totals row keeps the fields app.js reads for the token/cost
        cards: group_value, total_input_tokens, total_output_tokens,
        total_estimated_cost_usd."""
        mock_conn.fetchrow = AsyncMock(return_value=_mk_aggregate_row())
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        row = response.json()["data"][0]
        for field in (
            "group_value",
            "total_input_tokens",
            "total_output_tokens",
            "total_estimated_cost_usd",
            "record_count",
            "session_count",
            "model_count",
        ):
            assert field in row, f"KPI total field {field!r} missing"
        assert isinstance(row["total_input_tokens"], int)
        assert isinstance(row["total_output_tokens"], int)

    @pytest.mark.asyncio
    async def test_model_mix_contract(self, client: AsyncClient, mock_conn: AsyncMock):
        """Model mix rows keep the fields app.js reads for the bar chart:
        group_value (model name) + token totals."""
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_aggregate_row(group_value="gpt-4", record_count=2),
                _mk_aggregate_row(group_value="claude-3", record_count=1),
            ]
        )
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "model",
                },
            )

        assert response.status_code == 200
        rows = response.json()["data"]
        assert len(rows) == 2
        for row in rows:
            for field in (
                "group_value",
                "total_input_tokens",
                "total_output_tokens",
            ):
                assert field in row, f"Model-mix field {field!r} missing"

    @pytest.mark.asyncio
    async def test_sessions_table_contract(self, client: AsyncClient, mock_conn: AsyncMock):
        """The sessions response keeps the paginated envelope and the row
        fields app.js renders (id, session_title, token totals)."""
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetch = AsyncMock(return_value=[_mk_session_row()])

        async with client as c:
            response = await c.get(
                "/api/v1/usage/sessions",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert "total" in data and "items" in data
        assert data["total"] == 1
        item = data["items"][0]
        for field in (
            "id",
            "session_title",
            "total_input_tokens",
            "total_output_tokens",
            "total_cache_read_tokens",
            "total_cache_write_tokens",
            "total_estimated_cost_usd",
            "message_count",
        ):
            assert field in item, f"Sessions-table field {field!r} missing"

    @pytest.mark.asyncio
    async def test_collector_distribution_contract(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Client/project breakdown rows keep the fields app.js reads for the
        two-level distribution table: pipe-delimited group_value,
        project_label, session_count, model_count, token totals."""
        mock_conn.fetch = AsyncMock(
            return_value=[
                _mk_aggregate_row(
                    group_value="legacy-client|legacy-project",
                    project_label="Legacy Project",
                    session_count=2,
                    model_count=1,
                ),
            ]
        )
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            response = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                    "group_by": "client,project",
                },
            )

        assert response.status_code == 200
        row = response.json()["data"][0]
        for field in (
            "group_value",
            "project_label",
            "session_count",
            "model_count",
            "total_input_tokens",
            "total_output_tokens",
            "total_estimated_cost_usd",
        ):
            assert field in row, f"Distribution field {field!r} missing"
        assert row["group_value"] == "legacy-client|legacy-project"

    @pytest.mark.asyncio
    async def test_collector_health_kpis_contract(self):
        """The health endpoint keeps the collector/source-database shapes
        app.js reads for the Healthy Collectors / Source Databases KPI
        cards: collectors[].health and source_databases[].health."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        now = datetime.now(timezone.utc)  # noqa: UP017
        collector_row = MagicMock()
        collector_row.__getitem__.side_effect = {
            "credential_id": str(_CREDENTIAL_ID),
            "client_name": "legacy-client",
            "last_heartbeat": now,
            "total_records_ingested": 42,
        }.__getitem__
        source_db_row = MagicMock()
        source_db_row.__getitem__.side_effect = {
            "source_database_id": str(_SOURCE_DB_ID),
            "client_name": "legacy-client",
            "last_push": now,
            "record_count": 42,
        }.__getitem__
        mock_conn.fetch = AsyncMock(
            side_effect=[[collector_row], [source_db_row]]
        )
        mock_conn.fetchrow = AsyncMock(return_value=None)

        app = create_app(configure_logging=False)
        app.state.pool = mock_pool
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()["data"]
        assert "collectors" in data and "source_databases" in data
        assert data["collectors"][0]["health"] == "healthy"
        assert data["collectors"][0]["client_name"] == "legacy-client"
        assert data["source_databases"][0]["health"] == "healthy"
        assert data["source_databases"][0]["record_count"] == 42


# ══════════════════════════════════════════════════════════════════════════
#  5. Cursor endpoint — contract unchanged
# ══════════════════════════════════════════════════════════════════════════


class TestCursorEndpointBackwardCompat:
    """GET /cursor?source_database_id= keeps returning the pre-change
    contract.  (Full coverage lives in tests/test_cursor.py — this locks
    the response shape against the canonical-event era.)"""

    @pytest.mark.asyncio
    async def test_cursor_contract_unchanged(self, monkeypatch):
        """The cursor response still carries status/data envelope with
        source_database_id, last_seen_at, record_count, is_active."""
        mock_conn = AsyncMock()
        source_row = MagicMock()
        source_row.__getitem__.side_effect = {
            "last_seen_at": _mk_ts(),
            "record_count": 5000,
            "is_active": True,
        }.__getitem__
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchrow.side_effect = [_auth_row(), source_row]
        mock_conn.execute = AsyncMock()

        client = _build_two_layer_app(mock_conn, monkeypatch=monkeypatch)

        async with client as c:
            response = await c.get(
                f"/cursor?source_database_id={_SOURCE_DB_ID}",
                headers={"Authorization": "Bearer test-api-key"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["source_database_id"] == str(_SOURCE_DB_ID)
        assert data["last_seen_at"] == _mk_ts().strftime("%Y-%m-%dT%H:%M:%SZ")
        assert data["record_count"] == 5000
        assert data["is_active"] is True
