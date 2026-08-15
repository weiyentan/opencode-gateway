"""Reporting ingestion endpoint — persists normalized reporting deliveries.

Provides the consumer-side persistence surface for cross-repo PRD #478:
the producer (``fast-api-eda-gateway``) emits ``afk.events`` messages with
``event_type: "normalized"``; the future normalized-event consumer POSTs
them here.  This endpoint performs **no type mapping and enforces no locked
vocabulary** — ``event_type`` is stored verbatim as an opaque string.

Write semantics (idempotent + transactional, enforced by the constraints
on migration 0031):

* One explicit transaction per delivery (``async with conn.transaction()``).
* ``reporting_deliveries`` dedup via
  ``INSERT ... ON CONFLICT (provider, delivery_id) DO NOTHING RETURNING id``
  — a fresh row → outcome ``accepted``; a redelivery → ``duplicate``.
* Every accepted delivery appends a ``delivery_state_trails`` row (state
  ``persisted``) via
  ``ON CONFLICT (provider, delivery_id, state, occurred_at) DO NOTHING`` so
  an identical redelivered message appends nothing.

The response follows the ``/ingest`` convention: a batch always succeeds at
the HTTP level and reports per-delivery outcomes (``accepted | duplicate |
rejected``) so the future consumer can commit offsets.  Only unknown
``schema_version`` (400) and missing/invalid collector tokens (401) fail
the whole request.
"""

from __future__ import annotations

import json
import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.auth import require_collector_token
from app.core.reporting_aggregates import (
    enrich_aggregate,
    resource_identity_from_payload,
)
from app.core.secrets import redact_dict
from app.core.schemas.reporting import (
    ReportingDeliveryIn,
    ReportingDeliveryResult,
    ReportingIngestRequest,
    ReportingIngestResponse,
)
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/reporting/ingest", tags=["reporting-ingest"])

# ── Known schema versions ─────────────────────────────────────────────────

KNOWN_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})

# ── Write-path SQL (raw asyncpg, conflict-ignore) ──────────────────────────

_INSERT_DELIVERY_SQL = """
    INSERT INTO reporting_deliveries
        (provider, delivery_id, event_type, client_id, received_at, payload,
         occurred_at, ingested_at)
    VALUES ($1, $2, $3, $4, now(), $5::jsonb, $6, now())
    ON CONFLICT (provider, delivery_id) DO NOTHING
    RETURNING id
"""

_INSERT_TRAIL_SQL = """
    INSERT INTO delivery_state_trails
        (provider, delivery_id, state, occurred_at, detail, created_at)
    VALUES ($1, $2, $3, $4, $5::jsonb, now())
    ON CONFLICT (provider, delivery_id, state, occurred_at) DO NOTHING
"""

# ── Write path ─────────────────────────────────────────────────────────────


async def _persist_delivery(
    conn: asyncpg.Connection,
    client_id: uuid.UUID | None,
    idx: int,
    d: ReportingDeliveryIn,
) -> ReportingDeliveryResult:
    """Persist one delivery transactionally; return its outcome.

    The delivery insert is conflict-ignored on ``(provider, delivery_id)``
    and returns the row ``id`` only when a fresh row was created — that
    discriminates ``accepted`` from ``duplicate``.  On the accepted path a
    ``delivery_state_trails`` row (state ``persisted``) is written in the
    same transaction and conflict-ignored on the message-timestamp anchor,
    and the current aggregate is enriched forward-only (issue #480).
    A missing/malformed ``resource`` skips enrichment without rejecting the
    delivery.
    """
    async with conn.transaction():
        redacted_payload = redact_dict(d.payload)
        row = await conn.fetchrow(
            _INSERT_DELIVERY_SQL,
            d.provider,
            d.delivery_id,
            d.event_type,
            client_id,
            json.dumps(redacted_payload),
            d.occurred_at,
        )
        if row is None:
            return ReportingDeliveryResult(
                index=idx, delivery_id=d.delivery_id, status="duplicate"
            )
        await conn.execute(
            _INSERT_TRAIL_SQL,
            d.provider,
            d.delivery_id,
            "persisted",
            d.occurred_at,
            None,
        )
        identity = resource_identity_from_payload(
            redacted_payload, provider=d.provider
        )
        if identity is not None:
            await enrich_aggregate(conn, identity, d)
        return ReportingDeliveryResult(
            index=idx,
            delivery_id=d.delivery_id,
            status="accepted",
            delivery_record_id=row["id"],
        )


# ── POST /api/v1/reporting/ingest/deliveries ───────────────────────────────


@router.post("/deliveries", response_model=ReportingIngestResponse)
async def ingest_deliveries(
    body: ReportingIngestRequest,
    request: Request,
    auth: dict = Depends(require_collector_token),
    conn: asyncpg.Connection = Depends(get_session),
) -> ReportingIngestResponse:
    """Accept a batch of reporting deliveries from an authenticated collector.

    **Idempotency**: deliveries are deduplicated by ``(provider,
    delivery_id)``.  Re-posting the same batch returns ``duplicate`` for
    every delivery without inserting new rows.

    **Partial success**: individual deliveries may be accepted, duplicated,
    or rejected (persistence error).  The overall response reports
    per-delivery status and the request succeeds at the HTTP level.
    """
    client_id = uuid.UUID(auth["client_id"])

    # ── Schema version validation ────────────────────────────────────
    if body.schema_version not in KNOWN_SCHEMA_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown schema version: {body.schema_version}. "
            f"Known versions: {', '.join(sorted(KNOWN_SCHEMA_VERSIONS))}",
        )

    results: list[ReportingDeliveryResult] = []
    accepted = 0
    duplicate = 0
    rejected = 0

    for idx, delivery in enumerate(body.deliveries):
        try:
            result = await _persist_delivery(conn, client_id, idx, delivery)
        except Exception as exc:  # noqa: BLE001 - per-delivery partial success
            logger.warning("Reporting delivery rejected: %s", exc, exc_info=True)
            result = ReportingDeliveryResult(
                index=idx,
                delivery_id=delivery.delivery_id,
                status="rejected",
                reason=str(exc),
            )
        results.append(result)
        if result.status == "accepted":
            accepted += 1
        elif result.status == "duplicate":
            duplicate += 1
        else:
            rejected += 1

    return ReportingIngestResponse(
        accepted_count=accepted,
        duplicate_count=duplicate,
        rejected_count=rejected,
        results=results,
    )
