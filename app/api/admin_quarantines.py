"""Admin API endpoints for viewing quarantined source identities.

All endpoints require API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).

The read query lives here rather than in :mod:`app.core.identity`
because the endpoint needs ``collector_source_id`` for **both** the
quarantined identity and the identity it overlaps — a double join to
``source_identities`` that the module's ``get_active_quarantines``
(:class:`~app.core.identity.QuarantineRow`) does not provide.
"""

from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.core.schemas.identity import QuarantineRead
from app.core.schemas.usage import PaginatedResponse
from app.db.session import get_session

router = APIRouter(prefix="/admin/quarantined-identities", tags=["admin"])


# ── Helpers ───────────────────────────────────────────────────────────────


def _row_to_quarantine_read(row: asyncpg.Record) -> QuarantineRead:
    return QuarantineRead(
        quarantine_id=row["quarantine_id"],
        source_identity_id=row["source_identity_id"],
        collector_source_id=row["collector_source_id"],
        overlapping_identity_id=row["overlapping_identity_id"],
        overlapping_collector_source_id=row["overlapping_collector_source_id"],
        overlap_count=row["overlap_count"],
        quarantined_at=row["quarantined_at"],
    )


# ── Quarantine listing ─────────────────────────────────────────────────────


@router.get("", response_model=PaginatedResponse[QuarantineRead])
async def list_quarantined_identities(
    client_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to quarantines owned by this client",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[QuarantineRead]:
    """List active (uncleared) quarantined source identities, paginated.

    Optionally narrows the result set to one client's quarantines via
    the *client_id* query parameter.  A quarantine belongs to the client
    that owns the quarantined (source) identity — the same scoping used
    by :func:`app.core.identity.get_active_quarantines`.
    """
    conditions = ["q.cleared_at IS NULL"]
    params: list = []
    if client_id is not None:
        conditions.append(f"si.client_id = ${len(params) + 1}")
        params.append(client_id)
    where = " AND ".join(conditions)

    total: int = await conn.fetchval(
        "SELECT COUNT(*) "
        "FROM source_identity_quarantine q "
        "JOIN source_identities si ON si.id = q.source_identity_id "
        f"WHERE {where}",
        *params,
    )

    params.extend([limit, offset])
    rows = await conn.fetch(
        "SELECT q.id AS quarantine_id, q.source_identity_id, "
        "si.collector_source_id, q.overlapping_identity_id, "
        "oi.collector_source_id AS overlapping_collector_source_id, "
        "q.overlap_count, q.quarantined_at "
        "FROM source_identity_quarantine q "
        "JOIN source_identities si ON si.id = q.source_identity_id "
        "JOIN source_identities oi ON oi.id = q.overlapping_identity_id "
        f"WHERE {where} "
        "ORDER BY q.quarantined_at "
        f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )
    items = [_row_to_quarantine_read(r) for r in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)
