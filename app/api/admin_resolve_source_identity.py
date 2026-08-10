"""Admin API endpoint for resolving quarantined source identities.

``POST /admin/resolve-source-identity`` links a quarantined source
identity to a canonical parent identity via
:func:`~app.core.identity.resolve_identity`, which clears the quarantine
and records the decision in ``source_identity_resolutions``.  Requires
API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.identity import resolve_identity
from app.db.session import get_session

router = APIRouter(prefix="/admin/resolve-source-identity", tags=["admin"])


class ResolveSourceIdentityRequest(BaseModel):
    """Payload for resolving a quarantined source identity."""

    quarantine_id: uuid.UUID
    resolving_identity_id: uuid.UUID
    reason: str | None = None


class ResolveSourceIdentityResponse(BaseModel):
    """Details of a completed source identity resolution."""

    resolution_id: uuid.UUID
    quarantine_id: uuid.UUID
    resolved_identity_id: uuid.UUID
    linked_to_identity_id: uuid.UUID
    resolved_at: datetime


@router.post("", response_model=ResolveSourceIdentityResponse)
async def resolve_source_identity(
    body: ResolveSourceIdentityRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> ResolveSourceIdentityResponse:
    """Resolve a quarantined source identity into a canonical parent.

    Returns 404 when the quarantine is unknown, 400 when the quarantine
    has already been cleared, and 400 when the resolving identity does
    not exist.
    """
    quarantine = await conn.fetchrow(
        "SELECT id, source_identity_id, cleared_at "
        "FROM source_identity_quarantine WHERE id = $1",
        body.quarantine_id,
    )
    if quarantine is None:
        raise HTTPException(status_code=404, detail="Quarantine not found")

    if quarantine["cleared_at"] is not None:
        raise HTTPException(status_code=400, detail="Quarantine already resolved")

    identity = await conn.fetchrow(
        "SELECT id FROM source_identities WHERE id = $1",
        body.resolving_identity_id,
    )
    if identity is None:
        raise HTTPException(status_code=400, detail="Resolving identity not found")

    await resolve_identity(
        conn,
        body.quarantine_id,
        body.resolving_identity_id,
        body.reason,
        resolved_by=None,
    )

    # resolve_identity() returns None — read the recorded resolution back
    # from the audit table for the response.
    resolution = await conn.fetchrow(
        "SELECT id, resolved_at FROM source_identity_resolutions "
        "WHERE quarantine_id = $1 ORDER BY resolved_at DESC LIMIT 1",
        body.quarantine_id,
    )
    return ResolveSourceIdentityResponse(
        resolution_id=resolution["id"],
        quarantine_id=body.quarantine_id,
        resolved_identity_id=quarantine["source_identity_id"],
        linked_to_identity_id=body.resolving_identity_id,
        resolved_at=resolution["resolved_at"],
    )
