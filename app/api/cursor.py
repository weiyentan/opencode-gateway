"""Cursor endpoint — returns last ingestion state for a source database.

Provides:
- Pydantic schema for the cursor response
- GET /cursor?source_database_id=<uuid> returning last_seen_at,
  record_count, and is_active from the source_databases table
- 404 when the source_database_id is unknown
- 401 via the existing require_collector_token dependency
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.auth import require_collector_token
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cursor", tags=["cursor"])


# ── Pydantic schemas ──────────────────────────────────────────────────────


class CursorResponse(BaseModel):
    """Response returned for a successful cursor lookup."""

    source_database_id: uuid.UUID = Field(
        description="The requested source database identifier"
    )
    last_seen_at: datetime = Field(
        description="The last time this source database was seen by the collector"
    )
    record_count: int = Field(
        description="Total number of usage records ingested for this source database"
    )
    is_active: bool = Field(
        description="Whether the source database is still considered active"
    )


# ── GET /cursor ───────────────────────────────────────────────────────────


@router.get("", response_model=CursorResponse)
async def get_cursor(
    source_database_id: uuid.UUID = Query(
        ..., description="Source database UUID to query cursor for"
    ),
    auth: dict = Depends(require_collector_token),
    conn: asyncpg.Connection = Depends(get_session),
) -> CursorResponse:
    """Return cursor state for a source database.

    The collector calls this on startup to determine where to begin
    reading from the SQLite database.  Returns ``last_seen_at``,
    ``record_count``, and ``is_active`` for the given
    ``source_database_id``.

    Returns 404 when the source_database_id is not found (collector
    falls back to local cache or starts from zero).
    """
    row = await conn.fetchrow(
        "SELECT last_seen_at, record_count, is_active "
        "FROM source_databases WHERE id = $1",
        source_database_id,
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"source_database_id '{source_database_id}' not found",
        )

    return CursorResponse(
        source_database_id=source_database_id,
        last_seen_at=row["last_seen_at"],
        record_count=row["record_count"],
        is_active=row["is_active"],
    )
