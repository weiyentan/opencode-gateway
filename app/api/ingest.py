"""Ingest endpoint — accepts normalized usage-record batches from collectors.

Provides:
- Pydantic schemas for request/response validation
- POST /ingest with first-write-wins idempotency
- Partial-success semantics (per-record accepted/rejected/conflict)
- Empty-batch heartbeat support
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AliasChoices, BaseModel, Field

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
    session_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("model", "session_model"),
        description="Model used for the session",
    )
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
    """A project directory projection from an OpenCode collector.

    **Blank-path filtering (issue #413)**: ``directory`` is required but is
    not constrained to a minimum length.  Empty (``""``) and whitespace-only
    (``"  "``) paths pass validation and are filtered per projection item at
    processing time in ``_process_project_directories`` — a blank entry can
    never reject the *entire* /ingest batch.  Filtered items are reported in
    ``IngestResponse.projection_rejected_count`` so the response shows how
    many projection items were dropped.
    """

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
    # ── Replay metadata (optional, backward-compatible) ──────────────
    replay_id: uuid.UUID | None = Field(
        default=None, description="Replay run UUID (absent for real-time deliveries)"
    )
    replay_requested_start: date | None = Field(
        default=None, description="Start of the date window requested for this replay"
    )
    replay_delivery_mode: str | None = Field(
        default=None, description="How the replay was delivered (e.g. latest, at-least-once)"
    )


class IngestRecordResult(BaseModel):
    """Per-record result in the ingest response.

    Status values (issue #387 — canonical event vocabulary):
    ``accepted`` — record was processed and a canonical event was created.
    ``duplicate`` — idempotent delivery; no new event created.
    ``updated`` — replay delivery reconciled against the stored event.
    ``quarantined`` — record rejected because the source identity is
        quarantined (overlaps an existing identity).
    ``conflict`` — divergent duplicate (same dedup key, different values).
    ``rejected`` — validation failure or internal error.
    """

    index: int = Field(description="Zero-based index of the record in the batch")
    status: str = Field(
        description="accepted | duplicate | updated | quarantined | conflict | rejected"
    )
    reason: str | None = Field(default=None, description="Human-readable reason if not accepted")
    event_id: uuid.UUID | None = Field(
        default=None, description="Canonical usage_events.id on accepted path"
    )
    attempt_id: uuid.UUID | None = Field(
        default=None, description="usage_ingest_attempts.id on accepted path"
    )


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


def _normalise_optional_text(value: str | None) -> str | None:
    """Normalise an optional text value for Replay Merge.
    
    Whitespace-only strings are treated as missing and become ``None``.
    Non-empty strings and ``None`` pass through unchanged.
    """
    if value is not None and isinstance(value, str) and value.strip() == "":
        return None
    return value


def _validate_tokens(record: IngestRecord) -> tuple[int, int, int]:
    """Validate the token fields on an ingest record.

    Single source of truth for token validation, shared by the ``/ingest``
    handler's pre-routing Step 1 check and by ``_process_one_record`` so the
    two validation copies cannot drift — both call sites reject the record
    when this helper raises, with the exact reason string it carries.

    Checks performed, in order:
    - ``input_tokens`` / ``output_tokens`` / ``cached_tokens`` must be
      int-convertible; a ``ValueError`` / ``TypeError`` from ``int()`` is
      re-raised as ``ValueError`` carrying the exact
      ``Non-numeric token value: ...`` reason text.
    - The three base token values must be non-negative.
    - The enrichment tokens (``reasoning_tokens``, ``cache_read_tokens``,
      ``cache_write_tokens``) must be non-negative when present.

    Raises:
        ValueError: with the exact rejection reason string (``Non-numeric
            token value: ...`` or ``Negative token value``) that callers
            surface verbatim on the per-record :class:`IngestRecordResult`.

    Returns:
        The validated ``(input_tokens, output_tokens, cached_tokens)`` as
        ints.
    """
    try:
        input_tokens = int(record.input_tokens)
        output_tokens = int(record.output_tokens)
        cached_tokens = int(record.cached_tokens)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Non-numeric token value: {exc}") from exc

    if input_tokens < 0 or output_tokens < 0 or cached_tokens < 0:
        raise ValueError("Negative token value")

    if (record.reasoning_tokens is not None and record.reasoning_tokens < 0) \
        or (record.cache_read_tokens is not None and record.cache_read_tokens < 0) \
        or (record.cache_write_tokens is not None and record.cache_write_tokens < 0):
        raise ValueError("Negative token value")

    return input_tokens, output_tokens, cached_tokens


async def _apply_replay_merge(
    conn: asyncpg.Connection,
    record: IngestRecord,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
) -> bool:
    """Apply Replay Merge: fill absent enrichment fields on the stored record.

    Reads the stored enrichment columns under ``SELECT ... FOR UPDATE``
    within an explicit transaction, determines which columns are actually
    fillable (stored NULL + incoming non-NULL), and issues a COALESCE
    UPDATE only for those columns.  Returns ``True`` if at least one
    column was filled (i.e. an enrichment UPDATE was issued).

    The FOR UPDATE lock makes the stored-state read race-free — no
    concurrent writer can change the row between read and write.
    Whitespace-only text values are treated as missing (normalised to
    None before comparison).  Numeric zero is a valid observed value and
    is never treated as missing.

    The entire read+repair sequence — ``SELECT ... FOR UPDATE``, the
    fillable-only COALESCE UPDATE, and the session aggregate repair
    UPDATEs — runs inside an explicit ``async with conn.transaction()``
    block.  Under auto-commit mode the FOR UPDATE lock is released at
    the end of a single statement, so without a transaction two
    concurrent replays can both read NULL, both compute a delta, and
    both apply it (double-count).  The explicit transaction holds the
    row lock across all statements so exactly one replay fills the
    column and applies the session-aggregate delta.

    A ``session_id`` that is NULL in the locked record row (winner has
    not backfilled yet) is resolved via ``_resolve_internal_session_id``
    within the same transaction so the aggregate repair is not silently
    skipped.
    """

    # ── Build incoming enrichment map ─────────────────────────────────
    _incoming: dict[str, object] = {}

    _TEXT_ENRICHMENT: list[str] = ["provider", "mode", "finish_reason"]
    for field_name in _TEXT_ENRICHMENT:
        raw = getattr(record, field_name, None)
        _incoming[field_name] = _normalise_optional_text(raw)

    _NUMERIC_ENRICHMENT: list[str] = [
        "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    ]
    for field_name in _NUMERIC_ENRICHMENT:
        _incoming[field_name] = getattr(record, field_name, None)

    # estimated_cost_usd is excluded from enrichment: it is part of the
    # dedup identity (compared via _decimal_equal at the merge gate).
    # At this point the stored value is either non-NULL (populated — no
    # fill needed) or both stored and incoming are NULL (nothing to
    # fill).  A stored-NULL + incoming-populated cost fails the identity
    # check and goes to conflict (preserving TestDivergentDuplicate), so
    # including it in the SET clauses would be unreachable dead code.

    # ── Enclose in an explicit transaction ────────────────────────────
    # Under auto-commit, FOR UPDATE releases the lock at statement end,
    # so the enrichment UPDATE and the aggregate repair UPDATEs would
    # run without the lock held — two concurrent replays could both read
    # NULL and both apply the delta.  An explicit transaction holds the
    # row lock across all statements, serialising concurrent replays.
    async with conn.transaction():
        # Lock the record row and read stored enrichment columns
        current = await conn.fetchrow(
            """SELECT provider, mode, finish_reason,
                      reasoning_tokens, cache_read_tokens, cache_write_tokens,
                      session_id
               FROM opencode_usage_records
               WHERE client_id = $1
                 AND source_database_id = $2
                 AND source_record_id = $3
               FOR UPDATE""",
            client_id,
            source_db_id,
            record.source_record_id,
        )

        # The record must exist at this point (the caller verified it via
        # the dedup query); treat a missing row as a no-op.
        if current is None:
            return False

        # ── Determine actually fillable columns ────────────────────────
        # A column is fillable when the stored value is NULL AND the
        # incoming value is non-NULL (for text: already normalised via
        # _normalise_optional_text; for numerics: incoming is not None —
        # zero is valid).  A stored non-NULL value (including whitespace-
        # only text that was stored verbatim) is NOT fillable because
        # COALESCE would preserve it.
        fillable_columns: dict[str, object] = {}
        for field_name, incoming in _incoming.items():
            stored = current[field_name]
            if stored is None and incoming is not None:
                fillable_columns[field_name] = incoming

        if not fillable_columns:
            return False  # nothing to fill — race-free accurate reason

        # ── Build COALESCE SET clauses for fillable columns ───────────
        set_clauses: list[str] = []
        params: list = []
        param_idx = 1
        for field_name, incoming in fillable_columns.items():
            set_clauses.append(f"{field_name} = COALESCE({field_name}, ${param_idx})")
            params.append(incoming)
            param_idx += 1

        resolved_session_id = current["session_id"]

        # Handle session_id race: the winning request may not have
        # backfilled session_id on the usage record yet (backfill is a
        # separate statement after _resolve_session).  Resolve from the
        # sessions table within the same transaction so the aggregate
        # repair is not silently lost.
        if resolved_session_id is None:
            resolved_session_id = await _resolve_internal_session_id(
                conn, source_db_id, record.session_id,
            )

        params.extend([client_id, source_db_id, record.source_record_id])
        await conn.execute(
            f"""UPDATE opencode_usage_records
                SET {', '.join(set_clauses)}
                WHERE client_id = ${param_idx}
                  AND source_database_id = ${param_idx + 1}
                  AND source_record_id = ${param_idx + 2}""",
            *params,
        )

        # ── Repair session aggregate enrichment totals ────────────────
        # Compute the delta for cache token fields that were backfilled
        # (previously NULL, now non-NULL).  A non-NULL before value means
        # the column was already populated by the original write or a
        # prior replay — no delta is needed because that population was
        # already counted (either by the original _resolve_session call
        # or by a prior aggregate repair).
        cr_delta: int = 0
        cw_delta: int = 0
        if "cache_read_tokens" in fillable_columns:
            cr_delta = int(fillable_columns["cache_read_tokens"])  # type: ignore[arg-type]
        if "cache_write_tokens" in fillable_columns:
            cw_delta = int(fillable_columns["cache_write_tokens"])  # type: ignore[arg-type]

        # Apply session aggregate repair — only for positive deltas and
        # only when session_id is resolved.
        if resolved_session_id is not None:
            if cr_delta > 0:
                await conn.execute(
                    "UPDATE sessions SET total_cache_read_tokens = "
                    "total_cache_read_tokens + $1 WHERE id = $2",
                    cr_delta,
                    resolved_session_id,
                )
            if cw_delta > 0:
                await conn.execute(
                    "UPDATE sessions SET total_cache_write_tokens = "
                    "total_cache_write_tokens + $1 WHERE id = $2",
                    cw_delta,
                    resolved_session_id,
                )

    return True


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
    """Process a single ingest record — validate, deduplicate, upsert.

    Uses an atomic ``INSERT … ON CONFLICT … DO NOTHING RETURNING id``
    on ``(client_id, source_database_id, source_record_id)`` inside an
    explicit ``async with conn.transaction()`` block to determine the
    winner under concurrent replay.  The winning request runs all
    first-time side effects (session resolution with aggregate
    increment, source-database record count bump) within the same
    transaction so a crash between statements cannot permanently
    under-count.  A losing identical duplicate returns ``accepted``
    (with optional Replay Merge); a losing divergent duplicate returns
    ``conflict``.

    Returns an :class:`IngestRecordResult` regardless of outcome so the
    caller can implement partial-success semantics.
    """

    # ── 1. Validate token / cost fields ──────────────────────────────
    try:
        input_tokens, output_tokens, cached_tokens = _validate_tokens(record)
    except ValueError as exc:
        return IngestRecordResult(
            index=index,
            status="rejected",
            reason=str(exc),
        )

    # ── v1.2 cached_tokens computation ────────────────────────────
    # For v1.2 payloads, cached_tokens = cache_read_tokens + cache_write_tokens
    # For v1.0/v1.1 payloads, use the wire value directly
    if record.cache_read_tokens is not None and record.cache_write_tokens is not None:
        effective_cached_tokens = record.cache_read_tokens + record.cache_write_tokens
    else:
        effective_cached_tokens = cached_tokens

    # ── 2. Upsert observed model (idempotent — harmless for losers) ──
    model_id = await _upsert_model(conn, record.model, now)

    # ── 3. Atomic dedup INSERT + winner side effects in ONE transaction ──
    # Under concurrent replay, two requests can both pass a SELECT check
    # before either commits.  The atomic INSERT ... ON CONFLICT ... DO
    # NOTHING determines the winner in a single statement with no race
    # window.  All winner first-time side effects run inside an explicit
    # transaction so the INSERT is atomic with session resolution and
    # aggregate increment — a crash between statements cannot permanently
    # under-count (if the transaction rolls back the INSERT is also
    # rolled back and a retried replay re-enters as winner).
    record_uuid = uuid.uuid4()
    async with conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO opencode_usage_records
               (id, client_id, source_database_id, source_record_id, session_id,
                model_id, input_tokens, output_tokens, cached_tokens,
                estimated_cost_usd, reported_at, ingested_at,
                provider, mode, finish_reason, reasoning_tokens,
                cache_read_tokens, cache_write_tokens)
               VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10, $11,
                       $12, $13, $14, $15, $16, $17)
               ON CONFLICT (client_id, source_database_id, source_record_id)
               DO NOTHING
               RETURNING id""",
            record_uuid,
            client_id,
            source_db_id,
            record.source_record_id,
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

        if row is not None:
            # WINNER — first writer, run first-time side effects
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
            await conn.execute(
                "UPDATE opencode_usage_records SET session_id = $1 WHERE id = $2",
                internal_session_id, record_uuid,
            )
            await _increment_source_database_record_count(conn, source_db_id, now)

    if row is not None:
        return IngestRecordResult(index=index, status="accepted")

    # ── LOSER — dedup match, discriminate identical vs divergent ──────
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
            # ── Replay Merge: fill absent enrichment fields ──────────
            enrichment_applied = await _apply_replay_merge(
                conn, record, client_id, source_db_id,
            )
            reason = "Duplicate (idempotent)"
            if enrichment_applied:
                reason += " — enrichment applied"
            return IngestRecordResult(
                index=index,
                status="accepted",
                reason=reason,
            )
        # Different values → conflict
        return IngestRecordResult(
            index=index,
            status="conflict",
            reason="Divergent duplicate: same dedup key but different values",
        )

    # Should not reach here: if ON CONFLICT fired, the row must exist.
    return IngestRecordResult(
        index=index,
        status="rejected",
        reason="Unexpected dedup state: conflict detected but row not found",
    )


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
        _normalise_optional_text(ctx.parent_external_session_id),
        resolved_parent_session_id,
        _normalise_optional_text(ctx.external_project_id),
        resolved_source_project_id,
        _normalise_optional_text(ctx.source_directory),
        _normalise_optional_text(ctx.source_path),
        _normalise_optional_text(ctx.title),
        _normalise_optional_text(ctx.slug),
        _normalise_optional_text(ctx.version),
        _normalise_optional_text(ctx.session_model),
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
        _normalise_optional_text(proj.worktree),
        _normalise_optional_text(proj.vcs),
        proj.sandboxes,
        _normalise_optional_text(proj.name),
        _normalise_optional_text(proj.display_name),
        _normalise_optional_text(proj.icon),
        _normalise_optional_text(proj.icon_color),
        _normalise_optional_text(proj.raw_commands),
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
) -> tuple[int, int]:
    """Replace project directory rows for a source database and client.

    **Replace semantics**: deletes all existing directory rows scoped
    to ``(client_id, source_database_id)``, then inserts the provided
    batch.  All operations run within the caller's transaction.

    Each path is whitespace-trimmed before processing and storage.
    The incoming batch is normalised before insertion: blank, empty, or
    whitespace-only directory paths are filtered out, and duplicate
    paths (after trimming) within the batch are collapsed to a single
    entry stored with the canonical stripped form.  Replaying a snapshot
    therefore cannot produce duplicate or empty rows.

    Returns ``(inserted_count, filtered_count)``: the number of rows
    actually inserted, and the number of blank/empty/whitespace-only
    items dropped by filtering.  Filtered items are logged per item with
    their batch index and the offending field name — never the raw
    path value (issue #413).

    A no-op guard protects existing rows: when the incoming batch is
    non-empty but every entry filters out as blank/whitespace-only, the
    function returns early with no DELETE — a collector snapshot bug
    must not destroy previously-valid directory rows.  An explicit
    empty array (``directories == []``) is still treated as an
    authoritative "no directories" snapshot and runs the DELETE.
    """
    # ── Normalise batch: trim, drop blank paths, collapse duplicates ──
    seen: set[str] = set()
    batch: list[ProjectDirectoryPayload] = []
    filtered = 0
    for index, entry in enumerate(directories):
        path = entry.directory.strip()
        if not path:
            logger.warning(
                "Projection directory filtered: projection=project_directories "
                "index=%d field='directory' value is empty or whitespace-only",
                index,
            )
            filtered += 1
            continue
        if path in seen:
            continue
        seen.add(path)
        entry.directory = path
        batch.append(entry)

    # ── Delete existing directories for this scope ───────────────────
    # If the batch contained entries but every one was filtered out as
    # blank, treat it as a no-op rather than an authoritative
    # "no directories" snapshot — a collector snapshot bug must not
    # destroy previously-valid directory rows.  An explicit empty array
    # (directories == []) still runs the DELETE, preserving the
    # pre-existing replace/clear semantics.
    if not batch and directories:
        return 0, filtered
    await conn.execute(
        "DELETE FROM opencode_project_directories WHERE client_id = $1 AND source_database_id = $2",
        client_id,
        source_db_id,
    )

    # ── Insert new batch ─────────────────────────────────────────────
    count = 0
    for entry in batch:
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

    return count, filtered


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


# ── Canonical event helpers (issue #388) ────────────────────────────────────


def _canonical_fields_identical(
    stored: asyncpg.Record, record: IngestRecord, effective_cached_tokens: int,
) -> bool:
    """Compare all observable collector fields against the stored canonical event.

    Returns ``True`` when every compared field of the stored event matches
    the incoming record — the replay is an identical delivery.  Returns
    ``False`` when any field differs (including when the incoming value is
    non-null and authoritative).

    Compared fields (executor design decision for issue #388):
    - Token/cost fields: ``input_tokens``, ``output_tokens``,
      ``cached_tokens``, ``reasoning_tokens``, ``cache_read_tokens``,
      ``cache_write_tokens``, ``estimated_cost_usd``.
    - Text enrichment: ``provider``, ``mode``, ``finish_reason``.

    ``reported_at`` is deliberately excluded — replays may carry slightly
    different timestamps for the same logical record, so timestamp drift
    must not force a spurious update.
    """
    if stored["input_tokens"] != record.input_tokens:
        return False
    if stored["output_tokens"] != record.output_tokens:
        return False
    if stored["cached_tokens"] != effective_cached_tokens:
        return False
    # Nullable numeric fields: when the incoming value is None, the field
    # was omitted from the replay — treat as identical (ADR 0011: null
    # produces zero delta, never erases).  Only a non-None incoming that
    # differs from the stored value is a difference.  Numeric zero is a
    # valid observed value and is never treated as missing.
    if record.reasoning_tokens is not None and (
        stored["reasoning_tokens"] or 0
    ) != record.reasoning_tokens:
        return False
    if record.cache_read_tokens is not None and (
        stored["cache_read_tokens"] or 0
    ) != record.cache_read_tokens:
        return False
    if record.cache_write_tokens is not None and (
        stored["cache_write_tokens"] or 0
    ) != record.cache_write_tokens:
        return False
    # estimated_cost_usd: only compare when incoming is non-None
    if record.estimated_cost_usd is not None and not _decimal_equal(
        stored["estimated_cost_usd"], record.estimated_cost_usd,
    ):
        return False

    # Text enrichment — normalised comparison (whitespace-only → None).
    # **Non-erasing semantics (ADR 0011)**: a None/omitted incoming value
    # can never erase a populated stored value, so those are treated as
    # identical.  Only a non-None incoming that differs from the stored
    # value is considered a difference.
    stored_provider = _normalise_optional_text(stored["provider"])
    incoming_provider = _normalise_optional_text(record.provider)
    if incoming_provider is not None and incoming_provider != stored_provider:
        return False
    stored_mode = _normalise_optional_text(stored["mode"])
    incoming_mode = _normalise_optional_text(record.mode)
    if incoming_mode is not None and incoming_mode != stored_mode:
        return False
    stored_fr = _normalise_optional_text(stored["finish_reason"])
    incoming_fr = _normalise_optional_text(record.finish_reason)
    if incoming_fr is not None and incoming_fr != stored_fr:
        return False

    return True


async def _fill_canonical_text_enrichment(
    conn: asyncpg.Connection,
    event_id: uuid.UUID,
    record: IngestRecord,
) -> bool:
    """COALESCE-fill text enrichment fields on a canonical event (non-erasing).

    For each of ``provider``, ``mode``, ``finish_reason``: when the stored
    value is NULL and the incoming value is non-NULL (after whitespace
    normalisation), the column is filled via ``COALESCE(col, $n)`` —
    populated stored values are never erased.  Numeric zero semantics do
    not apply to text fields.

    Returns ``True`` when at least one enrichment column was filled.
    """
    enrichment: dict[str, str] = {}
    for field_name in ("provider", "mode", "finish_reason"):
        incoming = _normalise_optional_text(getattr(record, field_name, None))
        if incoming is not None:
            enrichment[field_name] = incoming

    if not enrichment:
        return False

    # Read stored values under FOR UPDATE (caller owns the transaction)
    current = await conn.fetchrow(
        "SELECT provider, mode, finish_reason FROM usage_events WHERE id = $1 FOR UPDATE",
        event_id,
    )
    if current is None:
        return False

    fillable: dict[str, str] = {}
    for field_name, incoming in enrichment.items():
        stored = current[field_name]
        if stored is None:
            fillable[field_name] = incoming

    if not fillable:
        return False

    set_clauses: list[str] = []
    params: list[object] = []
    param_idx = 1
    for field_name, value in fillable.items():
        set_clauses.append(f"{field_name} = COALESCE({field_name}, ${param_idx})")
        params.append(value)
        param_idx += 1
    params.append(event_id)

    await conn.execute(
        f"UPDATE usage_events SET {', '.join(set_clauses)} WHERE id = ${param_idx}",
        *params,
    )
    return True


# ── Canonical event recording (issue #387) ──────────────────────────────────


async def _record_canonical_event(
    conn: asyncpg.Connection,
    record: IngestRecord,
    client_id: uuid.UUID,
    source_db_id: uuid.UUID,
    batch_id: uuid.UUID,
    replay_id: uuid.UUID | None,
    now: datetime,
    *,
    canonical_identity_id: uuid.UUID,
) -> dict[str, uuid.UUID | None]:
    """Record a canonical event for an accepted first-delivery record.

    Quarantine, overlap detection, and cross-identity conflict checks
    are handled upstream in the ingest handler BEFORE ``_process_one_record()``
    (issue #389 — Findings 1 & 2).  This function assumes the record has
    already passed those checks and only handles: model/session lookup,
    canonical event creation, and ingest attempt recording.

    Concurrent first-delivery attempts for the same canonical identity
    and source record are serialised with a per-transaction advisory
    lock (``pg_advisory_xact_lock``) on ``hashtext(canonical_source_identity_id
    || source_record_id)``.  The second delivery blocks until the first
    commits, then re-reads and finds the event already present
    (re-read-after-commit pattern — issue #395).

    Resolves ``model_id`` and ``internal_session_id`` from the database
    (these were just upserted by ``_process_one_record``), so the
    canonical event has the same model and session references.

    **Replay Merge (issue #388):** When a canonical event already exists
    for ``(canonical_source_identity_id, source_record_id)``, the incoming
    record's observable fields are compared against the stored event:

    * All compared fields identical → outcome ``"duplicate"`` — no event
      modification; the attempt is recorded with the ``"duplicate"``
      outcome.
    * Any differing non-null collector field → the replay merge path:
      ``apply_replay_merge()`` (from ``app.core.reconciliation``) computes
      per-field deltas and adjusts session aggregates;
      ``_fill_canonical_text_enrichment()`` COALESCE-fills text
      enrichment fields (provider, mode, finish_reason) without erasing;
      ``last_ingested_at`` is bumped; outcome is ``"updated"``.

    **Compared field set**: ``input_tokens``, ``output_tokens``,
    ``cached_tokens``, ``reasoning_tokens``, ``cache_read_tokens``,
    ``cache_write_tokens``, ``estimated_cost_usd``, ``provider``,
    ``mode``, ``finish_reason``.  These are the collector-furnished
    fields observable on a canonical event and represent the full set
    of collector data captured in the event row.  ``reported_at`` is
    deliberately excluded — replays may carry slightly different
    timestamps for the same logical record.

    Returns a dict with ``event_id``, ``attempt_id``, and ``status``
    (``"accepted"``, ``"duplicate"``, or ``"updated"``) for the caller
    to attach to the :class:`IngestRecordResult`.
    """
    from app.core.reconciliation import (
        IngestOutcome,
        _rollup_day,
        _upsert_client_project_rollup,
        acquire_canonical_event_lock,
        apply_replay_merge,
    )
    from app.core.telemetry import EVENT_LOCK_ACQUIRED, timed_operation

    # ── 1. Resolve model and session (already upserted by _process_one_record) ──
    model_row = await conn.fetchrow(
        "SELECT id FROM observed_models WHERE model_name = $1",
        record.model,
    )
    if model_row is None:
        return {"event_id": None, "attempt_id": None}
    model_id = model_row["id"]

    session_row = await conn.fetchrow(
        "SELECT id FROM sessions WHERE source_database_id = $1 AND external_session_id = $2",
        source_db_id,
        record.session_id,
    )
    if session_row is None:
        return {"event_id": None, "attempt_id": None}
    internal_session_id = session_row["id"]

    # ── v1.2 cached_tokens computation (same logic as _process_one_record) ──
    if record.cache_read_tokens is not None and record.cache_write_tokens is not None:
        effective_cached_tokens = record.cache_read_tokens + record.cache_write_tokens
    else:
        effective_cached_tokens = int(record.cached_tokens)

    # ── 3. Serialise concurrent first-delivery attempts ──────────
    # Wrap the SELECT+INSERT+attempt in an EXPLICIT transaction so the
    # xact-scoped advisory lock spans the entire critical section.
    # Before the lock the event may not exist (first delivery) or it
    # may have been created by a concurrent request that committed
    # while we were blocked (re-read-after-commit pattern).
    event_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    outcome_str: str = "accepted"
    reason: str | None = None
    record_jsonb = json.dumps(record.model_dump(mode="json"))

    async with conn.transaction():
        # Lock wait time tracked via telemetry — duration_ms in the
        # "lock.acquired" event is the time spent blocked waiting for
        # the concurrent transaction to commit.
        async with timed_operation(EVENT_LOCK_ACQUIRED, "lock"):
            await acquire_canonical_event_lock(
                conn, canonical_identity_id, record.source_record_id,
            )

        # ── Re-read after lock: another tx may have inserted ─────
        existing = await conn.fetchrow(
            """SELECT id FROM usage_events
               WHERE canonical_source_identity_id = $1 AND source_record_id = $2
               FOR UPDATE""",
            canonical_identity_id,
            record.source_record_id,
        )

        if existing is None:
            # ── 4a. NEW — insert canonical event ──────────────────
            event_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO usage_events
                   (id, canonical_source_identity_id, source_record_id,
                    client_id, session_id, model_id,
                    input_tokens, output_tokens, cached_tokens,
                    reasoning_tokens, cache_read_tokens, cache_write_tokens,
                    estimated_cost_usd, reported_at,
                    provider, mode, finish_reason,
                    project_id, workspace_id, agent, parent_session_id,
                    first_ingested_at, last_ingested_at)
                   VALUES ($1, $2, $3,
                           $4, $5, $6,
                           $7, $8, $9,
                           $10, $11, $12,
                           $13, $14,
                           $15, $16, $17,
                           $18, $19, $20, $21,
                           $22, $22)""",
                event_id,
                canonical_identity_id,
                record.source_record_id,
                client_id,
                internal_session_id,
                model_id,
                record.input_tokens,
                record.output_tokens,
                effective_cached_tokens,
                record.reasoning_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.estimated_cost_usd,
                record.reported_at,
                record.provider,
                record.mode,
                record.finish_reason,
                record.project_id,
                record.workspace_id,
                record.agent,
                record.parent_session_id,
                now,
            )
            outcome_str = "accepted"

            # ── Maintain Client Project Rollup (first insert: full increment) ──
            if record.project_id is not None:
                day = _rollup_day(record.reported_at)
                cost = record.estimated_cost_usd
                if cost is None:
                    cost = Decimal("0")
                await _upsert_client_project_rollup(
                    conn,
                    client_id=client_id,
                    project_id=record.project_id,
                    day=day,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cache_read_tokens=record.cache_read_tokens or 0,
                    cache_write_tokens=record.cache_write_tokens or 0,
                    estimated_cost_usd=cost,
                )
        else:
            # ── 4b. EXISTING canonical event — compare for duplicate vs update ─
            event_id = existing["id"]

            # Read full canonical event for comparison (FOR UPDATE holds the lock)
            stored = await conn.fetchrow(
                """SELECT input_tokens, output_tokens, cached_tokens,
                          reasoning_tokens, cache_read_tokens, cache_write_tokens,
                          estimated_cost_usd, provider, mode, finish_reason
                   FROM usage_events WHERE id = $1""",
                event_id,
            )

            if _canonical_fields_identical(stored, record, effective_cached_tokens):
                # All compared fields identical → duplicate
                outcome_str = "duplicate"
            else:
                # Any differing non-null collector field → apply replay merge
                # The advisory lock on the event row spans the read-compute-
                # write across apply_replay_merge and the enrichment fill.
                new_values: dict[str, object] = {
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "cached_tokens": effective_cached_tokens,
                    "cache_read_tokens": record.cache_read_tokens,
                    "cache_write_tokens": record.cache_write_tokens,
                    "reasoning_tokens": record.reasoning_tokens,
                    "estimated_cost_usd": record.estimated_cost_usd,
                }
                merge_outcome = await apply_replay_merge(
                    conn, event_id, new_values,
                    client_id=client_id,
                )

                # COALESCE-fill text enrichment fields (non-erasing)
                enrichment_filled = await _fill_canonical_text_enrichment(
                    conn, event_id, record,
                )

                # Update last_ingested_at to reflect this replay delivery
                await conn.execute(
                    "UPDATE usage_events SET last_ingested_at = $1 WHERE id = $2",
                    now,
                    event_id,
                )

                # Outcome: "updated" if either token/cost or enrichment changed
                if merge_outcome == IngestOutcome.UPDATED or enrichment_filled:
                    outcome_str = "updated"
                else:
                    # Rare: pre-check saw a difference but under the lock
                    # nothing actually changed (concurrent merge already
                    # applied the same correction).  Treat as duplicate.
                    outcome_str = "duplicate"

        # ── 5. Record Ingest Attempt (inside transaction) ─────────
        attempt_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO usage_ingest_attempts
               (id, usage_event_id, source_identity_id,
                original_source_record_id, record_jsonb,
                ingest_batch_id, outcome, replay_id, delivered_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            attempt_id,
            event_id,
            canonical_identity_id,
            record.source_record_id,
            record_jsonb,
            batch_id,
            outcome_str,
            replay_id,
            now,
        )


    result: dict[str, uuid.UUID | None] = {
        "event_id": event_id,
        "attempt_id": attempt_id,
        "status": outcome_str,
    }
    if reason:
        result["reason"] = reason
    return result


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

    # ── Create ingest batch row BEFORE the records loop ──────────────
    # Must exist before any _record_canonical_event() inserts a
    # usage_ingest_attempts row referencing ingest_batches.id (FK is
    # immediate and non-deferrable; the batch row must be written first).
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
        0,  # accepted_count — updated after the records loop
        0,  # rejected_count — updated after the records loop
        now,
    )

    # ── Process records ──────────────────────────────────────────────
    results: list[IngestRecordResult] = []
    accepted = 0
    rejected = 0

    from app.core.identity import (
        check_batch_overlap,
        is_quarantined,
        quarantine_identity,
        resolve_canonical_identity,
    )

    canonical_identity_id: uuid.UUID | None = None
    quarantined = False
    newly_quarantined = False
    validation_errors: dict[int, str] = {}
    valid_records: list[IngestRecord] = []
    for idx, record in enumerate(body.records):
        try:
            _validate_tokens(record)
            valid_records.append(record)
        except ValueError as exc:
            validation_errors[idx] = str(exc)

    if valid_records:
        collector_source_id = str(source_db_id)
        canonical_identity_id = await resolve_canonical_identity(
            conn, client_id, collector_source_id,
        )
        quarantined = await is_quarantined(conn, canonical_identity_id)

        if not quarantined:
            overlaps = await check_batch_overlap(
                conn,
                client_id,
                canonical_identity_id,
                [record.source_record_id for record in valid_records],
            )
            if overlaps:
                primary_overlap = overlaps[0]
                await quarantine_identity(
                    conn,
                    canonical_identity_id,
                    primary_overlap.overlapping_identity_id,
                    primary_overlap.overlap_count,
                )
                quarantined = True
                newly_quarantined = True

    if canonical_identity_id is not None and quarantined:
        # Quarantine the whole batch before legacy or canonical accounting.
        for idx, record in enumerate(body.records):
            if idx in validation_errors:
                results.append(
                    IngestRecordResult(
                        index=idx,
                        status="rejected",
                        reason=validation_errors[idx],
                    )
                )
                continue
            attempt_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO usage_ingest_attempts
                   (id, usage_event_id, source_identity_id,
                    original_source_record_id, record_jsonb,
                    ingest_batch_id, outcome, replay_id, delivered_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                attempt_id,
                None,
                canonical_identity_id,
                record.source_record_id,
                json.dumps(record.model_dump(mode="json")),
                batch_id,
                "quarantined",
                body.replay_id,
                now,
            )
            results.append(
                IngestRecordResult(
                    index=idx,
                    status="quarantined",
                    reason=(
                        "Record quarantined — source identity overlap detected"
                        if newly_quarantined
                        else "Record quarantined — source identity has active quarantine"
                    ),
                    attempt_id=attempt_id,
                    event_id=None,
                )
            )
        rejected = len(body.records)
    elif canonical_identity_id is not None:
        for idx, record in enumerate(body.records):
            try:
                # Record-specific routing remains per record, but the historical
                # overlap scan has already run once for the entire batch.
                if idx in validation_errors:
                    result = IngestRecordResult(
                        index=idx,
                        status="rejected",
                        reason=validation_errors[idx],
                    )
                    results.append(result)
                    rejected += 1
                    continue

                cross_event = await conn.fetchrow(
                    """SELECT ue.id, ue.canonical_source_identity_id
                       FROM usage_events ue
                       JOIN source_identities si ON si.id = ue.canonical_source_identity_id
                       WHERE si.client_id = $1
                         AND ue.source_record_id = $2
                         AND ue.canonical_source_identity_id <> $3
                         AND ue.canonical_source_identity_id NOT IN (
                             SELECT id FROM source_identities
                             WHERE canonical_parent_id IS NOT NULL
                         )
                       LIMIT 1""",
                    client_id,
                    record.source_record_id,
                    canonical_identity_id,
                )
                if cross_event is not None:
                    attempt_id = uuid.uuid4()
                    await conn.execute(
                        """INSERT INTO usage_ingest_attempts
                           (id, usage_event_id, source_identity_id,
                            original_source_record_id, record_jsonb,
                            ingest_batch_id, outcome, replay_id, delivered_at)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                        attempt_id,
                        None,
                        canonical_identity_id,
                        record.source_record_id,
                        json.dumps(record.model_dump(mode="json")),
                        batch_id,
                        "conflict",
                        body.replay_id,
                        now,
                    )
                    result = IngestRecordResult(
                        index=idx,
                        status="conflict",
                        reason="Cross-identity conflict — canonical event owned by"
                               " a different unresolved identity",
                        attempt_id=attempt_id,
                        event_id=None,
                    )
                    results.append(result)
                    rejected += 1
                    continue

                result = await _process_one_record(
                    conn, record, idx, client_id, source_db_id, now,
                )
            except Exception as exc:
                logger.warning("Record %s failed processing: %s", idx, exc)
                result = IngestRecordResult(
                    index=idx, status="rejected", reason="Processing error — record skipped",
                )
            results.append(result)
            if result.status == "accepted":
                is_new = result.reason is None or not result.reason.startswith("Duplicate")
                if not is_new:
                    existing = await conn.fetchrow(
                        "SELECT 1 FROM usage_events"
                        " WHERE canonical_source_identity_id = $1"
                        "   AND source_record_id = $2",
                        canonical_identity_id,
                        record.source_record_id,
                    )
                    if existing is None:
                        is_new = True
                if is_new:
                    try:
                        canonical = await _record_canonical_event(
                            conn, record, client_id, source_db_id,
                            batch_id, body.replay_id, now,
                            canonical_identity_id=canonical_identity_id,
                        )
                        result.event_id = canonical["event_id"]
                        result.attempt_id = canonical["attempt_id"]
                        canonical_status = canonical.get("status")
                        if canonical_status and canonical_status != "accepted":
                            result.status = canonical_status
                            if canonical.get("reason"):
                                result.reason = canonical["reason"]
                    except Exception as exc:
                        logger.error(
                            "Record %s canonical event recording failed: %s", idx, exc,
                        )

            if result.status in ("accepted", "duplicate", "updated"):
                accepted += 1
            else:
                rejected += 1
    elif validation_errors:
        for idx, reason in validation_errors.items():
            results.append(
                IngestRecordResult(index=idx, status="rejected", reason=reason)
            )
        rejected = len(validation_errors)

    # ── Update ingest batch with final counts ────────────────────────
    await conn.execute(
        """UPDATE ingest_batches
           SET accepted_count = $1, rejected_count = $2
           WHERE id = $3""",
        accepted,
        rejected,
        batch_id,
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
        inserted, filtered = await _process_project_directories(
            conn, body.project_directories, client_id, source_db_id, now,
        )
        projection_accepted += inserted
        projection_rejected += filtered
    except Exception as exc:
        logger.warning(
            "Projection directories rejected: projection=project_directories "
            "field='directory' error=%s",
            exc,
        )
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
