# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Admin endpoint for historical usage reconciliation.

``POST /admin/reconcile-historical-duplicates``

Scans ``usage_events`` for duplicate ``source_record_id`` values,
deterministically selects the canonical row per duplicate group, and either
previews (``dry_run: true``) or performs (``dry_run: false``) reconciliation:
removing non-canonical rows and rebuilding affected session aggregates.

Requires Admin API Key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, status

from app.core.reconciliation import (
    RECONCILE_LOCK_CLASS,
    compute_reconcile_preview,
    perform_reconciliation,
    scan_duplicate_groups,
)
from app.core.schemas.reconciliation import ReconcileRequest, ReconcileResponse
from app.db.session import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _date_to_end_of_day(dt: datetime) -> datetime:
    """Convert a date to the end of that day in UTC (exclusive boundary)."""
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


@router.post(
    "/reconcile-historical-duplicates",
    response_model=ReconcileResponse,
    status_code=status.HTTP_200_OK,
)
async def reconcile_historical_duplicates(
    body: ReconcileRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> ReconcileResponse:
    """Trigger historical usage reconciliation.

    - **dry_run: true** — scans for duplicate events and returns a preview
      without modifying any data.
    - **dry_run: false** — performs the reconciliation: removes non-canonical
      duplicate ``usage_events`` rows, preserves ``usage_ingest_attempts``
      history (setting ``usage_event_id = NULL`` on referencing attempts),
      and rebuilds affected session aggregates from remaining canonical
      events.  The operation is transactional and serialised per-client
      with an advisory lock.

    Canonical row selection: earliest ``first_ingested_at``, lowest ``id``
    as tiebreaker.
    """
    # Convert date boundaries to full-day datetime ranges
    date_from_dt: datetime | None = None
    date_to_dt: datetime | None = None

    if body.date_from is not None and body.date_to is not None:
        date_from_dt = datetime.combine(
            body.date_from, datetime.min.time(), tzinfo=timezone.utc
        )
        date_to_dt = _date_to_end_of_day(
            datetime.combine(body.date_to, datetime.min.time(), tzinfo=timezone.utc)
        )

    # ── Scan for duplicate groups ───────────────────────────────────────
    groups = await scan_duplicate_groups(
        conn,
        client_id=body.client_id,
        date_from=date_from_dt,
        date_to=date_to_dt,
    )

    if not groups:
        # No duplicates found — return zero-impact result
        return ReconcileResponse(dry_run=body.dry_run)

    # ── Compute preview ─────────────────────────────────────────────────
    preview = compute_reconcile_preview(groups)

    if body.dry_run:
        return ReconcileResponse(
            dry_run=True,
            events_to_merge=preview.events_to_merge,
            aggregates_affected=preview.aggregates_affected,
            token_adjustment=preview.token_adjustment,
            cost_adjustment_usd=str(preview.cost_adjustment),
        )

    # ── Non-dry-run: lock + transaction + reconcile ──────────────────────
    # Use client_id as the lock key when available; fall back to a sentinel.
    lock_client_id = body.client_id or uuid.UUID("00000000-0000-0000-0000-000000000000")
    lock_key = lock_client_id.int & 0xFFFFFFFF

    # Acquire a transaction-scoped advisory lock to serialise concurrent
    # reconciliation runs per client.
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1, $2)",
        RECONCILE_LOCK_CLASS,
        lock_key,
    )

    # Re-scan inside the lock to ensure we see committed state
    groups = await scan_duplicate_groups(
        conn,
        client_id=body.client_id,
        date_from=date_from_dt,
        date_to=date_to_dt,
    )

    if not groups:
        return ReconcileResponse(dry_run=False)

    actual = await perform_reconciliation(conn, groups)

    return ReconcileResponse(
        dry_run=False,
        events_to_merge=actual.events_to_merge,
        aggregates_affected=actual.aggregates_affected,
        token_adjustment=actual.token_adjustment,
        cost_adjustment_usd=str(actual.cost_adjustment),
    )
