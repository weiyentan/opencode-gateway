# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the historical usage reconciliation admin endpoint.

Covers:
- Dry-run preview returns correct counts, no writes
- Non-dry-run performs reconciliation and returns actual counts
- Session aggregates are rebuilt correctly after reconciliation
- Non-canonical rows are removed from usage_events
- Idempotency — running twice produces the same result
- Admin API Key authentication is enforced
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import create_client, mock_row

# ── Shared test data ───────────────────────────────────────────────────────

_CLIENT_ID = uuid.UUID("a0000000-0000-0000-0000-000000000001")
_SESSION_A = uuid.UUID("b0000000-0000-0000-0000-000000000001")
_SESSION_B = uuid.UUID("b0000000-0000-0000-0000-000000000002")
_MODEL_ID = uuid.UUID("c0000000-0000-0000-0000-000000000001")
_SOURCE_ID = uuid.UUID("d0000000-0000-0000-0000-000000000001")

_EARLY = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_DATE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DATE_TO = datetime(2026, 1, 31, tzinfo=timezone.utc)


# ── Row builders ────────────────────────────────────────────────────────────


def _mk_event(
    *,
    event_id: uuid.UUID | None = None,
    source_record_id: str = "rec-001",
    client_id: uuid.UUID = _CLIENT_ID,
    session_id: uuid.UUID = _SESSION_A,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int | None = None,
    estimated_cost_usd: Decimal | None = Decimal("0.0100"),
    first_ingested_at: datetime = _LATER,
    reported_at: datetime = _LATER,
) -> MagicMock:
    return mock_row({
        "id": event_id or uuid.uuid4(),
        "source_record_id": source_record_id,
        "client_id": client_id,
        "session_id": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "first_ingested_at": first_ingested_at,
        "reported_at": reported_at,
    })


def _mk_session_agg(
    *,
    session_id: uuid.UUID = _SESSION_A,
    total_input_tokens: int = 200,
    total_output_tokens: int = 100,
    total_cached_tokens: int = 0,
    total_cache_read_tokens: int = 0,
    total_cache_write_tokens: int = 0,
    total_estimated_cost_usd: Decimal | None = Decimal("0.0200"),
) -> MagicMock:
    return mock_row({
        "id": session_id,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": total_cached_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_estimated_cost_usd": total_estimated_cost_usd,
    })


def _mk_empty_scan() -> list:
    return []


# ── Reconcile request helper ───────────────────────────────────────────────


def _reconcile_body(
    *,
    dry_run: bool = True,
    client_id: str | None = str(_CLIENT_ID),
    date_from: str | None = "2026-01-01",
    date_to: str | None = "2026-01-31",
) -> dict:
    body: dict = {"dry_run": dry_run}
    if client_id is not None:
        body["client_id"] = client_id
    if date_from is not None:
        body["date_from"] = date_from
    if date_to is not None:
        body["date_to"] = date_to
    return body


# ════════════════════════════════════════════════════════════════════════════
#  Dry-run preview
# ════════════════════════════════════════════════════════════════════════════


class TestDryRunPreview:
    """Dry-run preview returns correct counts without modifying data."""

    async def test_dry_run_returns_preview_with_zero_impact_when_no_duplicates(
        self, mock_conn: AsyncMock
    ):
        """When no duplicates exist, dry-run returns zero events_to_merge."""
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=True),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["dry_run"] is True
        assert data["events_to_merge"] == 0
        assert data["aggregates_affected"] == 0
        assert data["token_adjustment"] == 0
        assert data["cost_adjustment_usd"] == "0"

        # Verify no writes occurred
        mock_conn.execute.assert_not_called()

    async def test_dry_run_detects_duplicate_groups_and_computes_preview(
        self, mock_conn: AsyncMock
    ):
        """Two duplicates for rec-001 → 1 non-canonical row to merge."""
        canonical = _mk_event(
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=50,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        non_canonical = _mk_event(
            source_record_id="rec-001",
            input_tokens=200,
            output_tokens=60,
            cache_read_tokens=10,
            first_ingested_at=_LATER,
            reported_at=_LATER,
            estimated_cost_usd=Decimal("0.0200"),
        )

        # First fetch returns duplicate groups
        mock_conn.fetch = AsyncMock(return_value=[canonical, non_canonical])
        # Second fetch for the no-duplicates-scan
        mock_conn.fetchrow = AsyncMock()

        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=True),
        )

        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["dry_run"] is True
        # 2 events in duplicate group, 1 non-canonical → 1 to merge
        assert data["events_to_merge"] == 1
        # 1 session affected (all belong to same session)
        assert data["aggregates_affected"] == 1
        # Non-canonical contributes 200 input + 60 output = 260 tokens → adjustment -260
        # (removing the non-canonical row subtracts all its tokens from aggregates)
        assert data["token_adjustment"] == -260
        # Cost: removing the non-canonical row subtracts its cost 0.0200
        assert data["cost_adjustment_usd"] == "-0.0200"

        # Verify no writes occurred (dry_run)
        mock_conn.execute.assert_not_called()

    async def test_dry_run_no_data_modification(self, mock_conn: AsyncMock):
        """Dry-run must not execute any writes."""
        canonical = _mk_event(
            source_record_id="rec-001",
            input_tokens=100,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        non_canonical = _mk_event(
            source_record_id="rec-001",
            input_tokens=200,
            first_ingested_at=_LATER,
            reported_at=_LATER,
        )
        mock_conn.fetch = AsyncMock(return_value=[canonical, non_canonical])
        mock_conn.fetchrow = AsyncMock()
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=True),
        )

        assert resp.status_code == 200
        mock_conn.execute.assert_not_called()
        mock_conn.fetchval.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
#  Authentication enforcement
# ════════════════════════════════════════════════════════════════════════════


class TestReconcileAuth:
    """Admin API Key auth is enforced on the reconciliation endpoint."""

    async def test_reject_without_api_key(self, mock_conn: AsyncMock):
        """Unauthenticated request returns 401."""
        client = create_client(mock_conn, api_key=None)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(),
        )

        assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════════════════
#  Actual reconciliation (non-dry-run)
# ════════════════════════════════════════════════════════════════════════════


class TestActualReconciliation:
    """Non-dry-run performs reconciliation and returns actual counts."""

    async def test_non_dry_run_deletes_non_canonical_and_returns_counts(
        self, mock_conn: AsyncMock
    ):
        """Non-dry-run deletes non-canonical rows and returns actual counts."""
        canonical = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=50,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        non_canonical = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=200,
            output_tokens=60,
            cache_read_tokens=10,
            first_ingested_at=_LATER,
            reported_at=_LATER,
            estimated_cost_usd=Decimal("0.0200"),
        )

        # The scan query uses fetch(); the rebuild also uses fetch().
        # Use side_effect: first call returns scan results, second returns rebuild.
        rebuild_agg = mock_row({
            "session_id": _SESSION_A,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_cached_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_estimated_cost_usd": Decimal("0.0100"),
        })
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [canonical, non_canonical],  # scan before lock
                [canonical, non_canonical],  # scan after lock
                [rebuild_agg],               # rebuild
            ]
        )
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=False),
        )

        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["dry_run"] is False
        assert data["events_to_merge"] == 1
        assert data["aggregates_affected"] == 1
        assert data["token_adjustment"] == -260
        assert data["cost_adjustment_usd"] == "-0.0200"

    async def test_non_dry_run_acquires_advisory_lock(self, mock_conn: AsyncMock):
        """Non-dry-run acquires the reconcile lock to serialize concurrent runs."""
        canonical = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=50,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        non_canonical = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=200,
            output_tokens=60,
            first_ingested_at=_LATER,
            reported_at=_LATER,
        )

        rebuild_agg = mock_row({
            "session_id": _SESSION_A,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_cached_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_estimated_cost_usd": Decimal("0.0100"),
        })
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [canonical, non_canonical],  # scan before lock
                [canonical, non_canonical],  # scan after lock
                [rebuild_agg],               # rebuild
            ]
        )
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        client = create_client(mock_conn)

        await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=False),
        )

        # Verify the advisory lock was acquired
        lock_calls = [
            c for c in mock_conn.fetchval.call_args_list
            if "pg_advisory_xact_lock" in str(c.args[0])
        ]
        assert len(lock_calls) == 1, "Expected one advisory lock acquisition"

    async def test_canonical_selection_earliest_first_ingested_at(
        self, mock_conn: AsyncMock
    ):
        """Canonical row is the one with earliest first_ingested_at.
        
        When three events share the same source_record_id, the one with
        earliest first_ingested_at is canonical; the other two are merged.
        """
        canon = _mk_event(
            event_id=uuid.UUID("e0000000-0000-0000-0000-000000000001"),
            source_record_id="rec-001",
            input_tokens=50,
            output_tokens=20,
            first_ingested_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            reported_at=_LATER,
        )
        dup1 = _mk_event(
            event_id=uuid.UUID("e0000000-0000-0000-0000-000000000002"),
            source_record_id="rec-001",
            input_tokens=150,
            output_tokens=80,
            first_ingested_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            reported_at=_LATER,
        )
        dup2 = _mk_event(
            event_id=uuid.UUID("e0000000-0000-0000-0000-000000000003"),
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=60,
            first_ingested_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            reported_at=_LATER,
        )

        rebuild_agg = mock_row({
            "session_id": _SESSION_A,
            "total_input_tokens": 50,
            "total_output_tokens": 20,
            "total_cached_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_estimated_cost_usd": None,
        })
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [canon, dup1, dup2],  # scan before lock
                [canon, dup1, dup2],  # scan after lock
                [rebuild_agg],        # rebuild
            ]
        )
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=False),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        # 3 events → 2 non-canonical to merge
        assert data["events_to_merge"] == 2
        # Non-canonical tokens: dup1 (150+80=230) + dup2 (100+60=160) = 390 removed
        assert data["token_adjustment"] == -390


# ════════════════════════════════════════════════════════════════════════════
#  Idempotency
# ════════════════════════════════════════════════════════════════════════════


class TestIdempotency:
    """After reconciliation, a second run reports zero impact."""

    async def test_second_run_reports_zero_impact(self, mock_conn: AsyncMock):
        """After first run removes duplicates, second run finds nothing."""
        # First run: one canonical per source_record_id
        single_event = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=50,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        mock_conn.fetch = AsyncMock(return_value=[single_event])
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        client = create_client(mock_conn)

        # Second run — only the canonical row remains, one row per source_record_id
        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=False),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["events_to_merge"] == 0
        assert data["aggregates_affected"] == 0
        assert data["token_adjustment"] == 0
        assert data["cost_adjustment_usd"] == "0"

    async def test_idempotent_dry_run_after_reconciliation(self, mock_conn: AsyncMock):
        """Dry run after reconciliation also reports zero impact."""
        single_event = _mk_event(
            event_id=uuid.uuid4(),
            source_record_id="rec-001",
            input_tokens=100,
            output_tokens=50,
            first_ingested_at=_EARLY,
            reported_at=_LATER,
        )
        mock_conn.fetch = AsyncMock(return_value=[single_event])
        mock_conn.fetchrow = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(dry_run=True),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["events_to_merge"] == 0
        assert data["token_adjustment"] == 0
        mock_conn.execute.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
#  Validation — bad requests
# ════════════════════════════════════════════════════════════════════════════


class TestReconcileValidation:
    """Input validation on the reconciliation endpoint."""

    async def test_missing_dry_run_field(self, mock_conn: AsyncMock):
        """dry_run is required."""
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json={"client_id": str(_CLIENT_ID)},
        )

        assert resp.status_code == 422

    async def test_invalid_date_range(self, mock_conn: AsyncMock):
        """date_from after date_to is rejected."""
        client = create_client(mock_conn)

        resp = await client.post(
            "/admin/reconcile-historical-duplicates",
            json=_reconcile_body(date_from="2026-02-01", date_to="2026-01-01"),
        )

        assert resp.status_code == 422
