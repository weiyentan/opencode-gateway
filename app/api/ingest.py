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

KNOWN_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0", "1.1", "1.2"})


# ── Pydantic schemas ──────────────────────────────────────────────────────


class IngestRecord(BaseModel):
    """A single usage record from a collector."""

    source_record_id: str = Field(description="Unique record ID within the source database")
    session_id: str = Field(description="External session identifier (e.g. OpenCode ses_* ID)")
    model: str = Field(description="Model name used for this request")
    input_tokens: int = Field(description="Prompt tokens consumed")
    output_tokens: int = Field(description="Completion tokens produced")
    cached_tokens: int = Field(default=0, description="Cached/prompt-cache tokens")
    estimated_cost_usd: Decimal | None = Field(
        default=None, description="Estimated cost in USD (nullable)"
    )
    reported_at: datetime = Field(description="When the collector recorded this usage")

    # ── Optional enrichment fields (v1.2+) ────────────────────────────
    provider: str | None = Field(default=None, description="LLM provider name")
    mode: str | None = Field(default=None, description="Execution mode (e.g. code, chat)")
    finish_reason: str | None = Field(default=None, description="Reason the LLM finished")
    reasoning_tokens: int | None = Field(default=None, description="Reasoning tokens used")
    cache_read_tokens: int | None = Field(default=None, description="Cache read tokens")
    cache_write_tokens: int | None = Field(default=None, description="Cache write tokens")

    # ── Optional session-level fields (v1.2+) ────────────────────────
    project_id: str | None = Field(default=None, description="Project identifier for the session")
    workspace_id: str | None = Field(default=None, description="Workspace identifier for the session")
    agent: str | None = Field(default=None, description="Agent name used for the session")
    parent_session_id: str | None = Field(default=None, description="Parent session identifier, if any")


# ── Projection payload schemas ─────────────────────────────────────────


class SessionContextPayload(BaseModel):
    """A session context projection from an OpenCode collector."""

    external_session_id: str = Field(description="External session ID (e.g. ses_*)")
    parent_external_session_id: str | None = Field(default=None, description="Parent session external ID")
    external_project_id: str | None = Field(default=None, description="External project identifier")
    source_directory: str | None = Field(default=None, description="Source directory path")
    source_path: str | None = Field(default=None, description="Source path")
    title: str | None = Field(default=None, description="Session title")
    slug: str | None = Field(default=None, description="Session slug")
    version: str | None = Field(default=None, description="Session version")
    session_model: str | None = Field(default=None, description="Model used for the session")
    session_cost: Decimal | None = Field(default=None, description="Session cost in USD")
    source_input_tokens: int | None = Field(default=None, description="Source-reported input tokens")
    source_output_tokens: int | None = Field(default=None, description="Source-reported output tokens")
    source_cached_tokens: int | None = Field(default=None, description="Source-reported cached tokens")
    source_reasoning_tokens: int | None = Field(default=None, description="Source-reported reasoning tokens")
    code_change_count: int | None = Field(default=None, description="Code change count")
    code_change_additions: int | None = Field(default=None, description="Code change additions")
    code_change_deletions: int | None = Field(default=None, description="Code change deletions")
    source_created_at: int | None = Field(default=None, description="Source created-at millisecond timestamp")
    source_updated_at: int | None = Field(default=None, description="Source updated-at millisecond timestamp")
    source_started_at: int | None = Field(default=None, description="Source started-at millisecond timestamp")
    source_completed_at: int | None = Field(default=None, description="Source completed-at millisecond timestamp")
    source_created_at_tz: datetime | None = Field(default=None, description="Source created-at with timezone")
    source_updated_at_tz: datetime | None = Field(default=None, description="Source updated-at with timezone")
    source_started_at_tz: datetime | None = Field(default=None, description="Source started-at with timezone")
    source_completed_at_tz: datetime | None = Field(default=None, description="Source completed-at with timezone")
    source_payload: dict | None = Field(default=None, description="Redacted source payload snapshot")


class ProjectPayload(BaseModel):
    """A source project projection from an OpenCode collector."""

    external_project_id: str = Field(description="External project identifier")
    source_project_id: uuid.UUID | None = Field(default=None, description="Source system's own project UUID")
    worktree: str | None = Field(default=None, description="Worktree identifier")
    vcs: str | None = Field(default=None, description="VCS type")
    sandboxes: dict | None = Field(default=None, description="Sandbox configuration")
    name: str | None = Field(default=None, description="Project name")
    display_name: str | None = Field(default=None, description="Project display name")
    icon: str | None = Field(default=None, description="Project icon")
    icon_color: str | None = Field(default=None, description="Project icon color")
    raw_commands: str | None = Field(default=None, description="Raw commands string")
    parsed_commands: dict | None = Field(default=None, description="Parsed commands dict")
    source_created_at: int | None = Field(default=None, description="Source created-at millisecond timestamp")
    source_updated_at: int | None = Field(default=None, description="Source updated-at millisecond timestamp")
    source_created_at_tz: datetime | None = Field(default=None, description="Source created-at with timezone")
    source_updated_at_tz: datetime | None = Field(default=None, description="Source updated-at with timezone")
    source_payload: dict | None = Field(default=None, description="Redacted source payload snapshot")


class ProjectDirectoryPayload(BaseModel):
    """A project directory projection from an OpenCode collector."""

    directory: str = Field(description="Directory path")
    directory_type: str | None = Field(default=None, description="Directory type")
    strategy: str | None = Field(default=None, description="Directory strategy")
    source_created_at: int | None = Field(default=None, description="Source created-at millisecond timestamp")
    source_updated_at: int | None = Field(default=None, description="Source updated-at millisecond timestamp")
    source_created_at_tz: datetime | None = Field(default=None, description="Source created-at with timezone")
    source_updated_at_tz: datetime | None = Field(default=None, description="Source updated-at with timezone")
    source_payload: dict | None = Field(default=None, description="Redacted source payload snapshot")


class SessionTodoPayload(BaseModel):
    """A session todo projection from an OpenCode collector."""

    external_session_id: str = Field(description="External session ID")
    content: str = Field(description="Todo content")
    position: int | None = Field(default=None, description="Todo position within the session")
    status: str | None = Field(default=None, description="Todo status")
    priority: str | None = Field(default=None, description="Todo priority")
    content_hash: str | None = Field(default=None, description="Content hash for deduplication")
    source_created_at: int | None = Field(default=None, description="Source created-at millisecond timestamp")
    source_updated_at: int | None = Field(default=None, description="Source updated-at millisecond timestamp")
    source_created_at_tz: datetime | None = Field(default=None, description="Source created-at with timezone")
    source_updated_at_tz: datetime | None = Field(default=None, description="Source updated-at with timezone")
    source_payload: dict | None = Field(default=None, description="Redacted source payload snapshot")


class IngestRequest(BaseModel):
    """A batch of usage records pushed by a collector."""

    schema_version: str = Field(description="Schema version of the payload")
    collector_version: str = Field(description="Version of the collector software")
    client_hostname: str = Field(
        default="", description="Hostname of the collector that sent this batch"
    )
    # TODO: store client_hostname in ingest_batches table for operational visibility
    source_database_id: uuid.UUID = Field(
        description="Source database identifier assigned by the collector"
    )
    records: list[IngestRecord] = Field(
        default_factory=list, description="Usage records to ingest"
    )
    # ── Projection arrays (optional) ──────────────────────────────────
    session_contexts: list[SessionContextPayload] = Field(
        default_factory=list, description="Session context projections"
    )
    projects: list[ProjectPayload] = Field(
        default_factory=list, description="Source project projections"
    )
    project_directories: list[ProjectDirectoryPayload] = Field(
        default_factory=list, description="Project directory projections"
    )
    session_todos: list[SessionTodoPayload] = Field(
        default_factory=list, description="Session todo projections"
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
    # ── Projection counts ─────────────────────────────────────────────
    projection_accepted_count: int = Field(
        default=0, description="Number of projection items accepted"
    )
    projection_rejected_count: int = Field(
        default=0, description="Number of projection items rejected"
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


async def _resolve_session(
    conn: asyncpg.Connection,
    source_database_id: uuid.UUID,
    client_id: uuid.UUID,
    external_session_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    estimated_cost_usd: Decimal | None,
    now: datetime,
    project_id: str | None = None,
    workspace_id: str | None = None,
    agent: str | None = None,
    parent_session_id: str | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> uuid.UUID:
    """Map (source_database_id, external_session_id) to internal sessions.id UUID.

    Uses an ``INSERT … ON CONFLICT … DO UPDATE … RETURNING`` pattern
    per ADR 0006 — safe for concurrent callers.  Increments session
    aggregate counters (message_count, token totals, cost) on every
    successful resolution so the counters always reflect the most
    recent state.

    Scoped per source database so the same external session ID resolves
    to different internal UUIDs when originating from different databases.
    """
    new_id = uuid.uuid4()
    row = await conn.fetchrow(
        """INSERT INTO sessions
           (id, client_id, source_database_id, external_session_id,
            first_message_at, last_message_at, message_count,
            total_input_tokens, total_output_tokens, total_cached_tokens,
            total_cache_read_tokens, total_cache_write_tokens,
            total_estimated_cost_usd,
            project_id, workspace_id, agent, parent_session_id)
           VALUES ($1, $2, $3, $4, $5, $5, 1, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
           ON CONFLICT (source_database_id, external_session_id)
               WHERE external_session_id IS NOT NULL
           DO UPDATE SET
               last_message_at = GREATEST(sessions.last_message_at, $5),
               message_count = sessions.message_count + 1,
               total_input_tokens = sessions.total_input_tokens + $6,
               total_output_tokens = sessions.total_output_tokens + $7,
               total_cached_tokens = sessions.total_cached_tokens + $8,
               total_cache_read_tokens = sessions.total_cache_read_tokens + $9,
               total_cache_write_tokens = sessions.total_cache_write_tokens + $10,
               total_estimated_cost_usd =
                   COALESCE(sessions.total_estimated_cost_usd, 0)
                   + COALESCE($11, 0),
               project_id = COALESCE($12, sessions.project_id),
               workspace_id = COALESCE($13, sessions.workspace_id),
               agent = COALESCE($14, sessions.agent),
               parent_session_id = COALESCE($15, sessions.parent_session_id)
           RETURNING id""",
        new_id,
        client_id,
        source_database_id,
        external_session_id,
        now,
        input_tokens,
        output_tokens,
        cached_tokens,
        cache_read_tokens,
        cache_write_tokens,
        estimated_cost_usd,
        project_id,
        workspace_id,
        agent,
        parent_session_id,
    )
    return row["id"]


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

    if input_tokens < 0 or output_tokens < 0 or cached_tokens < 0:
        return IngestRecordResult(
            index=index,
            status="rejected",
            reason="Negative token value",
        )

    # ── Negative validation for enrichment token fields ───────────────
    if (record.reasoning_tokens is not None and record.reasoning_tokens < 0) \
        or (record.cache_read_tokens is not None and record.cache_read_tokens < 0) \
        or (record.cache_write_tokens is not None and record.cache_write_tokens < 0):
        return IngestRecordResult(
            index=index,
            status="rejected",
            reason="Negative token value",
        )

    # ── v1.2 cached_tokens computation ────────────────────────────
    # For v1.2 payloads, cached_tokens = cache_read_tokens + cache_write_tokens
    # For v1.0/v1.1 payloads, use the wire value directly
    if record.cache_read_tokens is not None and record.cache_write_tokens is not None:
        effective_cached_tokens = record.cache_read_tokens + record.cache_write_tokens
    else:
        effective_cached_tokens = cached_tokens

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
            and existing["cached_tokens"] == effective_cached_tokens
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

    # ── 4. Resolve session (upsert + increment aggregates) ───────────
    internal_session_id = await _resolve_session(
        conn, source_db_id, client_id, record.session_id,
        input_tokens, output_tokens, effective_cached_tokens,
        record.estimated_cost_usd, now,
        project_id=record.project_id,
        workspace_id=record.workspace_id,
        agent=record.agent,
        parent_session_id=record.parent_session_id,
        cache_read_tokens=record.cache_read_tokens or 0,
        cache_write_tokens=record.cache_write_tokens or 0,
    )

    # ── 5. Insert usage record ───────────────────────────────────────
    record_uuid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO opencode_usage_records
           (id, client_id, source_database_id, source_record_id, session_id,
            model_id, input_tokens, output_tokens, cached_tokens,
            estimated_cost_usd, reported_at, ingested_at,
            provider, mode, finish_reason, reasoning_tokens,
            cache_read_tokens, cache_write_tokens)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                   $13, $14, $15, $16, $17, $18)""",
        record_uuid,
        client_id,
        source_db_id,
        record.source_record_id,
        internal_session_id,
        model_id,
        input_tokens,
        output_tokens,
        effective_cached_tokens,
        record.estimated_cost_usd,
        record.reported_at,
        now,
        record.provider,
        record.mode,
        record.finish_reason,
        record.reasoning_tokens,
        record.cache_read_tokens,
        record.cache_write_tokens,
    )

    # ── 6. Bump source database record count ─────────────────────────
    await _increment_source_database_record_count(conn, source_db_id, now)

    return IngestRecordResult(index=index, status="accepted")


# ── Projection processing helpers ─────────────────────────────────────────

def _normalise_ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert a millisecond epoch timestamp to a UTC datetime.

    Returns ``None`` when the input is ``None``.
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


async def _resolve_internal_session_id(
    conn: asyncpg.Connection,
    source_database_id: uuid.UUID,
    external_session_id: str,
) -> uuid.UUID | None:
    """Resolve an external session ID to an internal sessions.id UUID.

    Returns ``None`` when no matching session row exists.
    """
    row = await conn.fetchrow(
        "SELECT id FROM sessions WHERE source_database_id = $1 AND external_session_id = $2",
        source_database_id,
        external_session_id,
    )
    return row["id"] if row else None


async def _resolve_source_project_id(
    conn: asyncpg.Connection,
    client_id: uuid.UUID,
    source_database_id: uuid.UUID,
    external_project_id: str,
) -> uuid.UUID | None:
    """Resolve an external project ID to an opencode_source_projects.id UUID.

    Returns ``None`` when no matching source project row exists.
    """
    row = await conn.fetchrow(
        """SELECT id FROM opencode_source_projects
           WHERE client_id = $1 AND source_database_id = $2 AND external_project_id = $3""",
        client_id,
        source_database_id,
        external_project_id,
    )
    return row["id"] if row else None


async def _process_session_context(
    conn: asyncpg.Connection,
    ctx: SessionContextPayload,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Upsert a single session context projection.

    Uses ``INSERT … ON CONFLICT … DO UPDATE`` on the unique key
    ``(client_id, source_database_id, external_session_id)``.
    Preserves ``first_seen_at`` on conflict; updates ``last_seen_at``.

    Resolves ``session_id`` and ``parent_session_id`` from the
    ``sessions`` table when matching rows exist.  Resolves
    ``source_project_id`` from ``opencode_source_projects``.

    Returns ``True`` when the insert/update succeeded.
    """
    resolved_session_id = await _resolve_internal_session_id(
        conn, source_db_id, ctx.external_session_id,
    )
    resolved_parent_session_id: uuid.UUID | None = None
    if ctx.parent_external_session_id:
        resolved_parent_session_id = await _resolve_internal_session_id(
            conn, source_db_id, ctx.parent_external_session_id,
        )
    resolved_source_project_id: uuid.UUID | None = None
    if ctx.external_project_id:
        resolved_source_project_id = await _resolve_source_project_id(
            conn, client_id, source_db_id, ctx.external_project_id,
        )

    # Normalise source millisecond timestamps to datetimes
    source_created_at_tz = (
        ctx.source_created_at_tz
        if ctx.source_created_at_tz is not None
        else _normalise_ms_to_datetime(ctx.source_created_at)
    )
    source_updated_at_tz = (
        ctx.source_updated_at_tz
        if ctx.source_updated_at_tz is not None
        else _normalise_ms_to_datetime(ctx.source_updated_at)
    )
    source_started_at_tz = (
        ctx.source_started_at_tz
        if ctx.source_started_at_tz is not None
        else _normalise_ms_to_datetime(ctx.source_started_at)
    )
    source_completed_at_tz = (
        ctx.source_completed_at_tz
        if ctx.source_completed_at_tz is not None
        else _normalise_ms_to_datetime(ctx.source_completed_at)
    )

    new_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO opencode_session_contexts
           (id, client_id, source_database_id, external_session_id,
            session_id, parent_external_session_id, parent_session_id,
            external_project_id, source_project_id,
            source_directory, source_path, title, slug, version,
            session_model, session_cost,
            source_input_tokens, source_output_tokens, source_cached_tokens,
            source_reasoning_tokens,
            code_change_count, code_change_additions, code_change_deletions,
            source_created_at, source_updated_at, source_started_at, source_completed_at,
            source_created_at_tz, source_updated_at_tz, source_started_at_tz, source_completed_at_tz,
            first_seen_at, last_seen_at, source_payload)
           VALUES ($1, $2, $3, $4,
                   $5, $6, $7,
                   $8, $9,
                   $10, $11, $12, $13, $14,
                   $15, $16,
                   $17, $18, $19,
                   $20,
                   $21, $22, $23,
                   $24, $25, $26, $27,
                   $28, $29, $30, $31,
                   $32, $32, $33)
            ON CONFLICT (client_id, source_database_id, external_session_id)
            DO UPDATE SET
               session_id = COALESCE(EXCLUDED.session_id, opencode_session_contexts.session_id),
               parent_external_session_id = COALESCE(EXCLUDED.parent_external_session_id, opencode_session_contexts.parent_external_session_id),
               parent_session_id = COALESCE(EXCLUDED.parent_session_id, opencode_session_contexts.parent_session_id),
               external_project_id = COALESCE(EXCLUDED.external_project_id, opencode_session_contexts.external_project_id),
               source_project_id = COALESCE(EXCLUDED.source_project_id, opencode_session_contexts.source_project_id),
               source_directory = COALESCE(EXCLUDED.source_directory, opencode_session_contexts.source_directory),
               source_path = COALESCE(EXCLUDED.source_path, opencode_session_contexts.source_path),
               title = COALESCE(EXCLUDED.title, opencode_session_contexts.title),
               slug = COALESCE(EXCLUDED.slug, opencode_session_contexts.slug),
               version = COALESCE(EXCLUDED.version, opencode_session_contexts.version),
               session_model = COALESCE(EXCLUDED.session_model, opencode_session_contexts.session_model),
               session_cost = COALESCE(EXCLUDED.session_cost, opencode_session_contexts.session_cost),
               source_input_tokens = COALESCE(EXCLUDED.source_input_tokens, opencode_session_contexts.source_input_tokens),
               source_output_tokens = COALESCE(EXCLUDED.source_output_tokens, opencode_session_contexts.source_output_tokens),
               source_cached_tokens = COALESCE(EXCLUDED.source_cached_tokens, opencode_session_contexts.source_cached_tokens),
               source_reasoning_tokens = COALESCE(EXCLUDED.source_reasoning_tokens, opencode_session_contexts.source_reasoning_tokens),
               code_change_count = COALESCE(EXCLUDED.code_change_count, opencode_session_contexts.code_change_count),
               code_change_additions = COALESCE(EXCLUDED.code_change_additions, opencode_session_contexts.code_change_additions),
               code_change_deletions = COALESCE(EXCLUDED.code_change_deletions, opencode_session_contexts.code_change_deletions),
               source_created_at = COALESCE(EXCLUDED.source_created_at, opencode_session_contexts.source_created_at),
               source_updated_at = COALESCE(EXCLUDED.source_updated_at, opencode_session_contexts.source_updated_at),
               source_started_at = COALESCE(EXCLUDED.source_started_at, opencode_session_contexts.source_started_at),
               source_completed_at = COALESCE(EXCLUDED.source_completed_at, opencode_session_contexts.source_completed_at),
               source_created_at_tz = COALESCE(EXCLUDED.source_created_at_tz, opencode_session_contexts.source_created_at_tz),
               source_updated_at_tz = COALESCE(EXCLUDED.source_updated_at_tz, opencode_session_contexts.source_updated_at_tz),
               source_started_at_tz = COALESCE(EXCLUDED.source_started_at_tz, opencode_session_contexts.source_started_at_tz),
               source_completed_at_tz = COALESCE(EXCLUDED.source_completed_at_tz, opencode_session_contexts.source_completed_at_tz),
               last_seen_at = EXCLUDED.last_seen_at,
               source_payload = COALESCE(EXCLUDED.source_payload, opencode_session_contexts.source_payload)""",
        new_id,
        client_id,
        source_db_id,
        ctx.external_session_id,
        resolved_session_id,
        ctx.parent_external_session_id,
        resolved_parent_session_id,
        ctx.external_project_id,
        resolved_source_project_id,
        ctx.source_directory,
        ctx.source_path,
        ctx.title,
        ctx.slug,
        ctx.version,
        ctx.session_model,
        ctx.session_cost,
        ctx.source_input_tokens,
        ctx.source_output_tokens,
        ctx.source_cached_tokens,
        ctx.source_reasoning_tokens,
        ctx.code_change_count,
        ctx.code_change_additions,
        ctx.code_change_deletions,
        ctx.source_created_at,
        ctx.source_updated_at,
        ctx.source_started_at,
        ctx.source_completed_at,
        source_created_at_tz,
        source_updated_at_tz,
        source_started_at_tz,
        source_completed_at_tz,
        now,
        ctx.source_payload,
    )
    return True


async def _process_project(
    conn: asyncpg.Connection,
    proj: ProjectPayload,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Upsert a single source project projection.

    Uses ``INSERT … ON CONFLICT … DO UPDATE`` on the unique key
    ``(client_id, source_database_id, external_project_id)``.
    Preserves ``first_seen_at`` on conflict; updates ``last_seen_at``.

    Returns ``True`` when the insert/update succeeded.
    """
    source_created_at_tz = (
        proj.source_created_at_tz
        if proj.source_created_at_tz is not None
        else _normalise_ms_to_datetime(proj.source_created_at)
    )
    source_updated_at_tz = (
        proj.source_updated_at_tz
        if proj.source_updated_at_tz is not None
        else _normalise_ms_to_datetime(proj.source_updated_at)
    )

    new_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO opencode_source_projects
           (id, client_id, source_database_id, external_project_id,
            source_project_id, worktree, vcs, sandboxes, name, display_name,
            icon, icon_color, raw_commands, parsed_commands,
            source_created_at, source_updated_at,
            source_created_at_tz, source_updated_at_tz,
            first_seen_at, last_seen_at, source_payload)
           VALUES ($1, $2, $3, $4,
                   $5, $6, $7, $8, $9, $10,
                   $11, $12, $13, $14,
                   $15, $16, $17, $18,
                   $19, $19, $20)
            ON CONFLICT (client_id, source_database_id, external_project_id)
            DO UPDATE SET
               source_project_id = COALESCE(EXCLUDED.source_project_id, opencode_source_projects.source_project_id),
               worktree = COALESCE(EXCLUDED.worktree, opencode_source_projects.worktree),
               vcs = COALESCE(EXCLUDED.vcs, opencode_source_projects.vcs),
               sandboxes = COALESCE(EXCLUDED.sandboxes, opencode_source_projects.sandboxes),
               name = COALESCE(EXCLUDED.name, opencode_source_projects.name),
               display_name = COALESCE(EXCLUDED.display_name, opencode_source_projects.display_name),
               icon = COALESCE(EXCLUDED.icon, opencode_source_projects.icon),
               icon_color = COALESCE(EXCLUDED.icon_color, opencode_source_projects.icon_color),
               raw_commands = COALESCE(EXCLUDED.raw_commands, opencode_source_projects.raw_commands),
               parsed_commands = COALESCE(EXCLUDED.parsed_commands, opencode_source_projects.parsed_commands),
               source_created_at = COALESCE(EXCLUDED.source_created_at, opencode_source_projects.source_created_at),
               source_updated_at = COALESCE(EXCLUDED.source_updated_at, opencode_source_projects.source_updated_at),
               source_created_at_tz = COALESCE(EXCLUDED.source_created_at_tz, opencode_source_projects.source_created_at_tz),
               source_updated_at_tz = COALESCE(EXCLUDED.source_updated_at_tz, opencode_source_projects.source_updated_at_tz),
               last_seen_at = EXCLUDED.last_seen_at,
               source_payload = COALESCE(EXCLUDED.source_payload, opencode_source_projects.source_payload)""",
        new_id,
        client_id,
        source_db_id,
        proj.external_project_id,
        proj.source_project_id,
        proj.worktree,
        proj.vcs,
        proj.sandboxes,
        proj.name,
        proj.display_name,
        proj.icon,
        proj.icon_color,
        proj.raw_commands,
        proj.parsed_commands,
        proj.source_created_at,
        proj.source_updated_at,
        source_created_at_tz,
        source_updated_at_tz,
        now,
        proj.source_payload,
    )
    return True


async def _process_project_directories(
    conn: asyncpg.Connection,
    directories: list[ProjectDirectoryPayload],
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    now: datetime,
) -> int:
    """Replace project directory rows for a source database and client.

    **Replace semantics**: deletes all existing directory rows scoped
    to ``(client_id, source_database_id)``, then inserts the provided
    batch.  All operations run within the caller's transaction.

    Returns the number of inserted rows.
    """
    # ── Delete existing directories for this scope ───────────────────
    await conn.execute(
        "DELETE FROM opencode_project_directories WHERE client_id = $1 AND source_database_id = $2",
        client_id,
        source_db_id,
    )

    # ── Insert new batch ─────────────────────────────────────────────
    count = 0
    for entry in directories:
        source_created_at_tz = (
            entry.source_created_at_tz
            if entry.source_created_at_tz is not None
            else _normalise_ms_to_datetime(entry.source_created_at)
        )
        source_updated_at_tz = (
            entry.source_updated_at_tz
            if entry.source_updated_at_tz is not None
            else _normalise_ms_to_datetime(entry.source_updated_at)
        )

        new_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO opencode_project_directories
               (id, client_id, source_database_id, directory,
                directory_type, strategy,
                source_created_at, source_updated_at,
                source_created_at_tz, source_updated_at_tz,
                first_seen_at, last_seen_at, source_payload)
               VALUES ($1, $2, $3, $4,
                       $5, $6,
                       $7, $8, $9, $10,
                       $11, $11, $12)""",
            new_id,
            client_id,
            source_db_id,
            entry.directory,
            entry.directory_type,
            entry.strategy,
            entry.source_created_at,
            entry.source_updated_at,
            source_created_at_tz,
            source_updated_at_tz,
            now,
            entry.source_payload,
        )
        count += 1

    return count


async def _process_session_todos(
    conn: asyncpg.Connection,
    todos: list[SessionTodoPayload],
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    now: datetime,
) -> int:
    """Replace session todo rows for each distinct session in the batch.

    **Replace semantics**: for each distinct ``external_session_id``
    in the batch, deletes existing todo rows scoped to
    ``(client_id, source_database_id, external_session_id)``, then
    inserts the new items for that session.  All operations run within
    the caller's transaction.

    Returns the number of inserted rows.
    """
    # ── Group todos by external session ID ────────────────────────────
    todos_by_session: dict[str, list[SessionTodoPayload]] = {}
    for entry in todos:
        todos_by_session.setdefault(entry.external_session_id, []).append(entry)

    total_inserted = 0
    for external_session_id, entries in todos_by_session.items():
        # ── Delete existing todos for this session ──────────────────
        await conn.execute(
            """DELETE FROM opencode_session_todos
               WHERE client_id = $1
                 AND source_database_id = $2
                 AND external_session_id = $3""",
            client_id,
            source_db_id,
            external_session_id,
        )

        # ── Resolve internal session UUID (best-effort) ──────────────
        resolved_session_id = await _resolve_internal_session_id(
            conn, source_db_id, external_session_id,
        )

        # ── Insert new items for this session ────────────────────────
        for entry in entries:
            source_created_at_tz = (
                entry.source_created_at_tz
                if entry.source_created_at_tz is not None
                else _normalise_ms_to_datetime(entry.source_created_at)
            )
            source_updated_at_tz = (
                entry.source_updated_at_tz
                if entry.source_updated_at_tz is not None
                else _normalise_ms_to_datetime(entry.source_updated_at)
            )

            new_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO opencode_session_todos
                   (id, client_id, source_database_id, external_session_id,
                    session_id, position, content, status, priority, content_hash,
                    source_created_at, source_updated_at,
                    source_created_at_tz, source_updated_at_tz,
                    first_seen_at, last_seen_at, source_payload)
                   VALUES ($1, $2, $3, $4,
                           $5, $6, $7, $8, $9, $10,
                           $11, $12, $13, $14,
                           $15, $15, $16)""",
                new_id,
                client_id,
                source_db_id,
                external_session_id,
                resolved_session_id,
                entry.position,
                entry.content,
                entry.status,
                entry.priority,
                entry.content_hash,
                entry.source_created_at,
                entry.source_updated_at,
                source_created_at_tz,
                source_updated_at_tz,
                now,
                entry.source_payload,
            )
            total_inserted += 1

    return total_inserted


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

    if body.client_hostname:
        logger.info("Ingest from collector hostname=%s", body.client_hostname)

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

    # ── Process projections (independent path) ────────────────────────
    projection_accepted = 0
    projection_rejected = 0

    # ── Projects first — session contexts may reference them ──────────
    for proj in body.projects:
        try:
            await _process_project(conn, proj, client_id, source_db_id, now)
            projection_accepted += 1
        except Exception as exc:
            logger.warning(
                "Projection project rejected: external_project_id=%s error=%s",
                getattr(proj, "external_project_id", "?"),
                exc,
            )
            projection_rejected += 1

    # ── Session contexts ──────────────────────────────────────────────
    for ctx in body.session_contexts:
        try:
            await _process_session_context(conn, ctx, client_id, source_db_id, now)
            projection_accepted += 1
        except Exception as exc:
            logger.warning(
                "Projection session context rejected: external_session_id=%s error=%s",
                getattr(ctx, "external_session_id", "?"),
                exc,
            )
            projection_rejected += 1

    # ── Project directories (replace per batch) ───────────────────────
    try:
        inserted = await _process_project_directories(
            conn, body.project_directories, client_id, source_db_id, now,
        )
        projection_accepted += inserted
    except Exception as exc:
        logger.warning("Projection directories rejected: error=%s", exc)
        projection_rejected += len(body.project_directories)

    # ── Session todos (replace per session) ───────────────────────────
    try:
        inserted = await _process_session_todos(
            conn, body.session_todos, client_id, source_db_id, now,
        )
        projection_accepted += inserted
    except Exception as exc:
        logger.warning("Projection todos rejected: error=%s", exc)
        projection_rejected += len(body.session_todos)

    return IngestResponse(
        batch_id=batch_id,
        accepted_count=accepted,
        rejected_count=rejected,
        results=results,
        projection_accepted_count=projection_accepted,
        projection_rejected_count=projection_rejected,
    )
