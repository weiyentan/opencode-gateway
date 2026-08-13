"""Pydantic schemas for the usage reporting API.

Defines request/response models for aggregates, records, sessions, and
paginated responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ── Aggregate schemas ─────────────────────────────────────────────────────


class AggregateRow(BaseModel):
    """A single aggregate row — one group's token/cost totals."""

    group_value: str = Field(description="Value of the group-by dimension")
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_cached_tokens: int = Field(default=0, ge=0)
    total_reasoning_tokens: int = Field(default=0, ge=0)
    total_cache_read_tokens: int = Field(default=0, ge=0)
    total_cache_write_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal | None = Field(default=None)
    record_count: int = Field(default=0, ge=0)
    session_count: int = Field(default=0, ge=0)
    model_count: int = Field(default=0, ge=0)
    project_label: str | None = Field(
        default=None,
        description="Resolved project label (present when group includes 'project')",
    )
    agent: str | None = Field(
        default=None,
        description="Resolved agent identity (present when group includes 'agent')",
    )


class AggregateQuery(BaseModel):
    """Query parameters for the aggregates endpoint.

    Note that ``group_by`` is accepted as comma-separated values in the
    query string; this model is used for response/validation but the
    actual parsing is done from query params.
    """

    client_id: uuid.UUID | None = Field(default=None)
    model: str | None = Field(default=None)
    session_id: uuid.UUID | None = Field(default=None)
    start_date: datetime
    end_date: datetime
    group_by: list[str] | None = Field(default=None)


# ── Record schemas ────────────────────────────────────────────────────────


class RecordRow(BaseModel):
    """A single usage record returned by the records endpoint."""

    id: uuid.UUID
    client_id: uuid.UUID
    source_database_id: uuid.UUID
    session_id: uuid.UUID
    model_name: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    provider: str | None = None
    mode: str | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None
    reported_at: datetime
    ingested_at: datetime
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


class RecordQuery(BaseModel):
    """Query parameters for the records endpoint."""

    client_id: uuid.UUID | None = Field(default=None)
    model: str | None = Field(default=None)
    session_id: uuid.UUID | None = Field(default=None)
    start_date: datetime
    end_date: datetime
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    sort_by: str = Field(default="reported_at")
    sort_dir: str = Field(default="desc")


# ── Session schemas ───────────────────────────────────────────────────────


class SessionSummary(BaseModel):
    """A session-level summary returned by the sessions endpoint."""

    id: uuid.UUID
    client_id: uuid.UUID
    source_database_id: uuid.UUID
    first_message_at: datetime
    last_message_at: datetime
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    project_id: str | None = None
    project_label: str | None = Field(
        default=None,
        description="Resolved project label via COALESCE(display_name, name, "
        "basename(worktree), project_id, 'unknown')",
    )
    workspace_id: str | None = None
    agent: str | None = None
    parent_session_id: str | None = None
    session_title: str | None = Field(
        default=None,
        description="Session title from opencode_session_contexts (null if no context)",
    )
    code_change_count: int = Field(
        default=0,
        ge=0,
        description="Code change count from opencode_session_contexts (0 if no context)",
    )
    code_change_additions: int = Field(
        default=0,
        ge=0,
        description="Code change additions from opencode_session_contexts (0 if no context)",
    )
    code_change_deletions: int = Field(
        default=0,
        ge=0,
        description="Code change deletions from opencode_session_contexts (0 if no context)",
    )
    total_estimated_cost_usd: Decimal | None = None
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


# ── Agent Run schemas ─────────────────────────────────────────────────────


class TodoRow(BaseModel):
    """A single todo item snapshot within an agent run detail view.

    Note: Todo snapshots are not yet persisted by the ingest pipeline.
    These fields return empty results until the Todo Snapshot projection
    table is introduced in a future schema migration.
    """

    description: str = Field(description="Todo description text")
    status: str = Field(
        description="Todo status: pending, in_progress, completed, blocked"
    )
    priority: str | None = Field(
        default=None,
        description="Todo priority (e.g. high, medium, low)",
    )


class ChildRunSummary(BaseModel):
    """A summary of a child agent run — used in the detail view."""

    id: uuid.UUID = Field(description="Internal Gateway session UUID")
    external_session_id: str | None = Field(
        default=None,
        description="External OpenCode session identifier",
    )
    status: str = Field(
        description="Computed status: running, stale, completed, blocked, unknown"
    )
    currentStatus: str = Field(
        description="Current computed status (same derivation as status). "
        "Preferred field for UI badge rendering per ADR 0010."
    )
    agent: str | None = Field(default=None)
    message_count: int = Field(default=0, ge=0)


class AgentRunSummary(BaseModel):
    """A single row in the paginated Agent Run list.

    Provides both internal Gateway identifiers and external OpenCode
    identifiers.  Status and child_run_count are computed on read —
    they are never stored.
    """

    id: uuid.UUID = Field(description="Internal Gateway session UUID")
    external_session_id: str | None = Field(
        default=None,
        description="External OpenCode session identifier (e.g. ses_* ID)",
    )
    client_id: uuid.UUID = Field(description="OpenCode client UUID")
    source_database_id: uuid.UUID = Field(description="Source database UUID")
    title: str | None = Field(
        default=None,
        description="Derived title from agent name and external session ID",
    )
    status: str = Field(
        description="Computed status on read: running, stale, completed, blocked, unknown"
    )
    currentStatus: str = Field(
        description="Current computed status (same derivation as status). "
        "Preferred field for UI badge rendering per ADR 0010."
    )
    agent: str | None = Field(default=None, description="Agent name")
    project_id: str | None = Field(default=None, description="Project identifier")
    project_label: str | None = Field(
        default=None,
        description="Resolved project label via COALESCE(display_name, name, "
        "basename(worktree), project_id, 'unknown')",
    )
    workspace_id: str | None = Field(
        default=None, description="Worktree/workspace identifier"
    )
    todo_total: int = Field(
        default=0,
        ge=0,
        description="Total todo items (populated from opencode_session_todos queries)",
    )
    todo_completed: int = Field(
        default=0,
        ge=0,
        description="Completed todo items (populated from opencode_session_todos queries)",
    )
    todo_blocked: int = Field(
        default=0,
        ge=0,
        description="Blocked todo items (populated from opencode_session_todos queries)",
    )
    code_changes_total: int = Field(
        default=0,
        ge=0,
        description="Total code changes (populated from opencode_session_contexts queries)",
    )
    code_change_count: int = Field(
        default=0,
        ge=0,
        description="Code change count (populated from opencode_session_contexts queries)",
    )
    code_change_additions: int = Field(
        default=0,
        ge=0,
        description="Code change additions (populated from opencode_session_contexts queries)",
    )
    code_change_deletions: int = Field(
        default=0,
        ge=0,
        description="Code change deletions (populated from opencode_session_contexts queries)",
    )
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_cached_tokens: int = Field(default=0, ge=0)
    total_cache_read_tokens: int = Field(default=0, ge=0)
    total_cache_write_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal | None = Field(default=None)
    message_count: int = Field(default=0, ge=0)
    last_updated_at: datetime = Field(
        description="Timestamp of the last message (last_message_at)"
    )
    child_run_count: int = Field(
        default=0,
        ge=0,
        description="Number of child sessions whose parent_session_id references this session",
    )
    session_title: str | None = Field(
        default=None,
        description="Session title from opencode_session_contexts (null if no context)",
    )
    model: str | None = Field(
        default=None,
        description="LLM model identifier from opencode_session_contexts (null if no context)",
    )


class AgentRunDetail(BaseModel):
    """Full detail view for a single agent run, keyed by internal session UUID.

    Includes parent identifiers, child summaries, todo rows, project
    details, Session Context data, and usage totals.
    """

    id: uuid.UUID = Field(description="Internal Gateway session UUID")
    external_session_id: str | None = Field(
        default=None,
        description="External OpenCode session identifier",
    )
    client_id: uuid.UUID = Field(description="OpenCode client UUID")
    source_database_id: uuid.UUID = Field(description="Source database UUID")
    title: str | None = Field(
        default=None,
        description="Derived title from agent name and external session ID",
    )
    status: str = Field(
        description="Computed status on read: running, stale, completed, blocked, unknown"
    )
    currentStatus: str = Field(
        description="Current computed status (same derivation as status). "
        "Preferred field for UI badge rendering per ADR 0010."
    )
    agent: str | None = Field(default=None, description="Agent name")
    project_id: str | None = Field(default=None, description="Project identifier")
    project_label: str | None = Field(
        default=None,
        description="Resolved project label via COALESCE(display_name, name, "
        "basename(worktree), project_id, 'unknown')",
    )
    workspace_id: str | None = Field(
        default=None, description="Worktree/workspace identifier"
    )
    parent_session_id: str | None = Field(
        default=None,
        description="External session ID of the parent run, if any",
    )
    parent_internal_id: uuid.UUID | None = Field(
        default=None,
        description="Internal Gateway UUID of the parent session, if resolved",
    )
    child_summaries: list[ChildRunSummary] = Field(
        default_factory=list,
        description="Summaries of child agent runs",
    )
    todo_rows: list[TodoRow] = Field(
        default_factory=list,
        description="Todo items for this run (populated from opencode_session_todos queries)",
    )
    todo_total: int = Field(
        default=0, ge=0, description="Total todo items (populated from opencode_session_todos queries)"
    )
    todo_completed: int = Field(
        default=0, ge=0, description="Completed todo items (populated from opencode_session_todos queries)"
    )
    todo_blocked: int = Field(
        default=0, ge=0, description="Blocked todo items (populated from opencode_session_todos queries)"
    )
    code_changes_total: int = Field(
        default=0, ge=0, description="Total code changes (populated from opencode_session_contexts queries)"
    )
    code_change_count: int = Field(
        default=0, ge=0, description="Code change count (populated from opencode_session_contexts queries)"
    )
    code_change_additions: int = Field(
        default=0, ge=0, description="Code change additions (populated from opencode_session_contexts queries)"
    )
    code_change_deletions: int = Field(
        default=0, ge=0, description="Code change deletions (populated from opencode_session_contexts queries)"
    )
    session_context: dict[str, object] | None = Field(
        default=None,
        description="Session Context data (populated from opencode_session_contexts queries)",
    )
    message_count: int = Field(default=0, ge=0)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_cached_tokens: int = Field(default=0, ge=0)
    total_cache_read_tokens: int = Field(default=0, ge=0)
    total_cache_write_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal | None = Field(default=None)
    first_message_at: datetime | None = Field(
        default=None, description="Timestamp of the first message"
    )
    last_message_at: datetime | None = Field(
        default=None, description="Timestamp of the last message"
    )
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


VALID_AGENT_RUN_STATUSES: frozenset[str] = frozenset(
    {"running", "stale", "completed", "blocked", "unknown"}
)


# ── Records-with-context schemas ──────────────────────────────────────────


class RecordWithContextRow(BaseModel):
    """A single usage record enriched with session title, project label, and agent.

    This is the per-message row returned when ``group_by`` is *not* used.
    """

    id: uuid.UUID
    client_id: uuid.UUID
    source_database_id: uuid.UUID
    session_id: uuid.UUID
    model_name: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    provider: str | None = None
    mode: str | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None
    reported_at: datetime
    ingested_at: datetime
    session_title: str | None = Field(
        default=None,
        description="Session title from opencode_session_contexts",
    )
    project_label: str = Field(
        description="Resolved project label via COALESCE(display_name, name, "
        "basename(worktree), external_project_id)",
    )
    agent: str | None = Field(
        default=None,
        description="Agent name from sessions table",
    )
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


class RecordWithContextGroupedRow(BaseModel):
    """An aggregated row returned when ``group_by`` is used.

    Only the fields relevant to the requested group-by dimensions are
    populated — additional context fields (``project_label``,
    ``session_title``, ``agent``, ``model_name``) are included when the
    respective dimension is in the group set.
    """

    group_value: str = Field(
        description="Value of the group-by dimension(s), pipe-separated for multi-dimension"
    )
    project_label: str | None = Field(
        default=None,
        description="Resolved project label (present when group includes 'project')",
    )
    session_title: str | None = Field(
        default=None,
        description="Session title (present when group includes 'session')",
    )
    agent: str | None = Field(
        default=None,
        description="Agent name (present when group includes 'agent')",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name (present when group includes 'model')",
    )
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_cached_tokens: int = Field(default=0, ge=0)
    total_reasoning_tokens: int = Field(default=0, ge=0)
    total_cache_read_tokens: int = Field(default=0, ge=0)
    total_cache_write_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal | None = Field(default=None)
    record_count: int = Field(default=0, ge=0)


# ── Paginated response ────────────────────────────────────────────────────


class PaginatedResponse(BaseModel, Generic[T]):  # noqa: UP046
    """A generic paginated response wrapper."""

    items: list[T] = Field(description="The items for the current page")
    total: int = Field(description="Total number of items across all pages")
    limit: int = Field(description="Maximum items per page")
    offset: int = Field(description="Zero-based offset of the current page")
