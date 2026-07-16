"""Ingest endpoint — accepts normalized usage-record batches from collectors.

Provides:
- Pydantic schemas for request/response validation
- POST /ingest with first-write-wins idempotency
- Partial-success semantics (per-record accepted/rejected/conflict)
- Empty-batch heartbeat support
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.auth import require_collector_token
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

# ── Known schema versions ─────────────────────────────────────────────────

KNOWN_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})


# ── Pydantic schemas ──────────────────────────────────────────────────────


class IngestRecord(BaseModel):
    """A single usage record from a collector."""

    source_record_id: str = Field(description="Unique record ID within the source database")
    session_id: uuid.UUID = Field(description="Session this record belongs to")
    model: str = Field(description="Model name used for this request")
    input_tokens: int = Field(ge=0, description="Prompt tokens consumed")
    output_tokens: int = Field(ge=0, description="Completion tokens produced")
    cached_tokens: int = Field(default=0, ge=0, description="Cached/prompt-cache tokens")
    estimated_cost_usd: Decimal | None = Field(
        default=None, description="Estimated cost in USD (nullable)"
    )
    reported_at: datetime = Field(description="When the collector recorded this usage")


class IngestRequest(BaseModel):
    """A batch of usage records pushed by a collector."""

    schema_version: str = Field(description="Schema version of the payload")
    collector_version: str = Field(description="Version of the collector software")
    source_database_id: uuid.UUID = Field(
        description="Source database identifier assigned by the collector"
    )
    records: list[IngestRecord] = Field(
        default_factory=list, description="Usage records to ingest"
    )


class IngestRecordResult(BaseModel):
    """Per-record result in the ingest response."""

    index: int = Field(description="Zero-based index of the record in the batch")
    status: str = Field(description="accepted | rejected | conflict")
    reason: str | None = Field(default=None, description="Human-readable reason if not accepted")


class IngestResponse(BaseModel):
    """Response returned after processing an ingest batch."""

    batch_id: uuid.UUID = Field(description="UUID of the ingest_batches row")
    accepted_count: int = Field(description="Number of records accepted")
    rejected_count: int = Field(description="Number of records rejected or conflicted")
    results: list[IngestRecordResult] = Field(
        default_factory=list, description="Per-record results"
    )


# ── Internal helpers ──────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_equal(a: Decimal | None, b: Decimal | None) -> bool:
    """Compare two optional decimals for approximate equality."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(a - b) < Decimal('0.0001')
    except (ValueError, TypeError, InvalidOperation):
        return False


async def _upsert_source_database(
    conn: asyncpg.Connection,
    source_db_id: uuid.UUID,
    client_id: uuid.UUID,
    credential_id: uuid.UUID,
    now: datetime,
) -> None:
    """Create a source_database row if it doesn't exist; always touch last_seen_at."""
    existing = await conn.fetchrow(
        "SELECT id FROM source_databases WHERE id = $1", source_db_id
    )
    if existing is None:
        await conn.execute(
            """INSERT INTO source_databases
               (id, collector_credential_id, client_id,
                first_seen_at, last_seen_at, record_count, is_active)
               VALUES ($1, $2, $3, $4, $4, 0, true)""",
            source_db_id,
            credential_id,
            client_id,
            now,
        )
    else:
        await conn.execute(
            "UPDATE source_databases SET last_seen_at = $2 WHERE id = $1",
            source_db_id,
            now,
        )


async def _increment_source_database_record_count(
    conn: asyncpg.Connection,
    source_db_id: uuid.UUID,
    now: datetime,
) -> None:
    """Bump record_count and last_seen_at on the source database."""
    await conn.execute(
        """UPDATE source_databases
           SET record_count = record_count + 1, last_seen_at = $2
           WHERE id = $1""",
        source_db_id,
        now,
    )


async def _upsert_model(
    conn: asyncpg.Connection,
    model_name: str,
    now: datetime,
) -> uuid.UUID:
    """Create an observed_model if new; update last_seen_at.  Returns the model's UUID."""
    row = await conn.fetchrow(
        "SELECT id FROM observed_models WHERE model_name = $1", model_name
    )
    if row is None:
        model_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO observed_models (id, model_name, first_seen_at, last_seen_at)
               VALUES ($1, $2, $3, $3)""",
            model_id,
            model_name,
            now,
        )
        return model_id
    else:
        await conn.execute(
            "UPDATE observed_models SET last_seen_at = $2 WHERE id = $1",
            row["id"],
            now,
        )
        return row["id"]


async def _upsert_session(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost: Decimal | None,
    now: datetime,
) -> None:
    """Create or update a session row, incrementing aggregate counters."""
    existing = await conn.fetchrow(
        "SELECT id FROM sessions WHERE id = $1", session_id
    )
    if existing is None:
        await conn.execute(
            """INSERT INTO sessions
               (id, client_id, source_database_id, first_message_at, last_message_at,
                message_count, total_input_tokens, total_output_tokens,
                total_cached_tokens, total_estimated_cost_usd)
               VALUES ($1, $2, $3, $4, $4, 1, $5, $6, $7, $8)""",
            session_id,
            client_id,
            source_db_id,
            now,
            input_tokens,
            output_tokens,
            cached_tokens,
            cost,
        )
    else:
        await conn.execute(
            """UPDATE sessions
               SET last_message_at = $2,
                   message_count = message_count + 1,
                   total_input_tokens = total_input_tokens + $3,
                   total_output_tokens = total_output_tokens + $4,
                   total_cached_tokens = total_cached_tokens + $5,
                   total_estimated_cost_usd = COALESCE(total_estimated_cost_usd, 0)
                                              + COALESCE($6, 0)
               WHERE id = $1""",
            session_id,
            now,
            input_tokens,
            output_tokens,
            cached_tokens,
            cost,
        )


# ── Record processor ──────────────────────────────────────────────────────


async def _process_one_record(
    conn: asyncpg.Connection,
    record: IngestRecord,
    index: int,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    now: datetime,
) -> IngestRecordResult:
    """Process a single ingest record — idempotency, validation, upsert.

    Returns an :class:`IngestRecordResult` regardless of outcome so the
    caller can implement partial-success semantics.
    """

    # ── 1. Validate token / cost fields ──────────────────────────────
    try:
        input_tokens = int(record.input_tokens)
        output_tokens = int(record.output_tokens)
        cached_tokens = int(record.cached_tokens)
    except (ValueError, TypeError) as exc:
        return IngestRecordResult(
            index=index,
            status="rejected",
            reason=f"Non-numeric token value: {exc}",
        )

    # ── 2. Idempotency check ─────────────────────────────────────────
    existing = await conn.fetchrow(
        """SELECT id, input_tokens, output_tokens, cached_tokens, estimated_cost_usd
           FROM opencode_usage_records
           WHERE client_id = $1 AND source_database_id = $2 AND source_record_id = $3""",
        client_id,
        source_db_id,
        record.source_record_id,
    )

    if existing is not None:
        # Identical values → idempotent accept
        if (
            existing["input_tokens"] == input_tokens
            and existing["output_tokens"] == output_tokens
            and existing["cached_tokens"] == cached_tokens
            and _decimal_equal(existing["estimated_cost_usd"], record.estimated_cost_usd)
        ):
            return IngestRecordResult(
                index=index,
                status="accepted",
                reason="Duplicate (idempotent)",
            )
        # Different values → conflict
        return IngestRecordResult(
            index=index,
            status="conflict",
            reason="Divergent duplicate: same dedup key but different values",
        )

    # ── 3. Upsert observed model ─────────────────────────────────────
    model_id = await _upsert_model(conn, record.model, now)

    # ── 4. Upsert session ────────────────────────────────────────────
    await _upsert_session(
        conn,
        record.session_id,
        client_id,
        source_db_id,
        input_tokens,
        output_tokens,
        cached_tokens,
        record.estimated_cost_usd,
        now,
    )

    # ── 5. Insert usage record ───────────────────────────────────────
    record_uuid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO opencode_usage_records
           (id, client_id, source_database_id, source_record_id, session_id,
            model_id, input_tokens, output_tokens, cached_tokens,
            estimated_cost_usd, reported_at, ingested_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
        record_uuid,
        client_id,
        source_db_id,
        record.source_record_id,
        record.session_id,
        model_id,
        input_tokens,
        output_tokens,
        cached_tokens,
        record.estimated_cost_usd,
        record.reported_at,
        now,
    )

    # ── 6. Bump source database record count ─────────────────────────
    await _increment_source_database_record_count(conn, source_db_id, now)

    return IngestRecordResult(index=index, status="accepted")


# ── POST /ingest ──────────────────────────────────────────────────────────


@router.post("", response_model=IngestResponse)
async def ingest_usage(
    body: IngestRequest,
    request: Request,
    auth: dict = Depends(require_collector_token),
    conn: asyncpg.Connection = Depends(get_session),
) -> IngestResponse:
    """Accept a batch of usage records from an authenticated collector.

    **Idempotency**: records are deduplicated by ``(client_id,
    source_database_id, source_record_id)``.  Re-posting the same batch
    returns ``accepted`` for every record without inserting new rows.

    **Partial success**: individual records may be accepted, rejected
    (malformed), or conflicted (divergent duplicate).  The overall
    response reports per-record status.

    **Heartbeat**: an empty ``records`` array updates source-database
    health timestamps without creating any usage rows.
    """
    client_id = uuid.UUID(auth["client_id"])
    credential_id = uuid.UUID(auth["credential_id"])
    source_db_id = body.source_database_id
    now = _utcnow()

    # ── Schema version validation ────────────────────────────────────
    if body.schema_version not in KNOWN_SCHEMA_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown schema version: {body.schema_version}. "
            f"Known versions: {', '.join(sorted(KNOWN_SCHEMA_VERSIONS))}",
        )

    batch_id = uuid.uuid4()

    # ── Upsert source database (create if first time) ────────────────
    await _upsert_source_database(conn, source_db_id, client_id, credential_id, now)

    # ── Process records ──────────────────────────────────────────────
    results: list[IngestRecordResult] = []
    accepted = 0
    rejected = 0

    for idx, record in enumerate(body.records):
        result = await _process_one_record(conn, record, idx, client_id, source_db_id, now)
        results.append(result)
        if result.status == "accepted":
            accepted += 1
        else:
            rejected += 1

    # ── Record ingest batch ──────────────────────────────────────────
    await conn.execute(
        """INSERT INTO ingest_batches
           (id, collector_credential_id, client_id, collector_version,
            schema_version, record_count, accepted_count, rejected_count, ingested_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        batch_id,
        credential_id,
        client_id,
        body.collector_version,
        body.schema_version,
        len(body.records),
        accepted,
        rejected,
        now,
    )

    # ── Record per-record audit ──────────────────────────────────────
    for i, result in enumerate(results):
        await conn.execute(
            """INSERT INTO ingest_audit
               (ingest_batch_id, record_index, status, reason, ingested_at)
               VALUES ($1, $2, $3, $4, $5)""",
            batch_id,
            i,
            result.status,
            result.reason,
            now,
        )

    return IngestResponse(
        batch_id=batch_id,
        accepted_count=accepted,
        rejected_count=rejected,
        results=results,
    )
