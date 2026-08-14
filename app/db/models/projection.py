"""SQLAlchemy ORM models for OpenCode source-fact projection tables.

These tables store denormalised projections of OpenCode source data:
session context, source projects, project directories, and session
todos.  They are populated at ingest time by extracting facts from
the collector payload.

These models are used by Alembic for autogenerate (metadata reflection)
and serve as canonical type documentation.  Runtime database access in
API endpoints uses asyncpg directly, consistent with the existing
Gateway architecture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


def _utcnow() -> datetime:
    """Return the current UTC time — usable as a Python-side column default."""
    return datetime.now(timezone.utc)


class OpenCodeSourceProject(Base):
    """A source project extracted from OpenCode collector payloads.

    Each row represents a unique project as identified by the source
    system (e.g. an OpenCode workspace project).  The source-key
    uniqueness is enforced by ``(client_id, source_database_id,
    external_project_id)``.
    """

    __tablename__ = "opencode_source_projects"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_project_id",
            name="uq_opencode_source_projects_source_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    external_project_id: Mapped[str] = mapped_column(String, nullable=False)
    source_project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    worktree: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    vcs: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sandboxes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    icon_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_commands: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    parsed_commands: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class OpenCodeSessionContext(Base):
    """A session context extracted from OpenCode collector payloads.

    Stores the runtime context of a coding session: the project it
    belongs to, its directory, title/slug/version metadata, the model
    and cost, source token totals, code-change summary counts, and
    lifecycle timestamps.

    Each row maps to an ``external_session_id`` within a source
    database.  When the internal session UUID has been resolved the
    ``session_id`` column is populated.
    """

    __tablename__ = "opencode_session_contexts"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_session_id",
            name="uq_opencode_session_contexts_source_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    parent_external_session_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    parent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    external_project_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    source_project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opencode_source_projects.id"),
        nullable=True,
    )
    source_directory: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    session_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    session_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric, nullable=True)
    source_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_output_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_cached_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_reasoning_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    code_change_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    code_change_additions: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    code_change_deletions: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_started_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_completed_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_started_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_completed_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class OpenCodeProjectDirectory(Base):
    """A project directory extracted from OpenCode collector payloads.

    Each row represents a directory associated with a source project,
    optionally typed (e.g. workspace root, sandbox) and linked to a
    strategy.
    """

    __tablename__ = "opencode_project_directories"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "directory",
            name="uq_opencode_project_directories_source_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    directory: Mapped[str] = mapped_column(String, nullable=False)
    directory_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    strategy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class ObservedMessage(Base):
    """An observed OpenCode ``message`` row (ADR 0016 execution transcript).

    Projects one OpenCode message's identity, session linkage, and the
    promoted role/agent/mode/cost/token facts plus parent linkage, with
    the full ``message.data`` payload preserved verbatim (redacted) in
    the ``data`` JSONB column.  Keyed by ``(client_id,
    source_database_id, external_message_id)``.
    """

    __tablename__ = "observed_messages"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_message_id",
            name="uq_observed_messages_source_key",
        ),
        Index("ix_observed_messages_session_created", "session_id", "source_created_at"),
        Index("ix_observed_messages_agent", "agent"),
        Index("ix_observed_messages_role_created", "role", "source_created_at"),
        Index(
            "ix_observed_messages_retention",
            "source_created_at_tz",
            postgresql_where=text("source_created_at_tz IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    external_message_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_external_session_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    agent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class ObservedPart(Base):
    """An observed OpenCode ``part`` row (ADR 0016 execution transcript).

    Projects one OpenCode part's identity, owning message and session, and
    its explicit Transcript Event Type (``part_type``), with the full
    ``part.data`` payload preserved verbatim (redacted) in ``data``.
    Keyed by ``(client_id, source_database_id, external_part_id)``.
    """

    __tablename__ = "observed_parts"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_part_id",
            name="uq_observed_parts_source_key",
        ),
        Index("ix_observed_parts_session_created", "session_id", "source_created_at"),
        Index("ix_observed_parts_message_created", "message_id", "source_created_at"),
        Index(
            "ix_observed_parts_session_type_created",
            "session_id",
            "part_type",
            "source_created_at",
        ),
        Index("ix_observed_parts_type_created", "part_type", "source_created_at"),
        Index("ix_observed_parts_created", "source_created_at"),
        Index(
            "ix_observed_parts_retention",
            "source_created_at_tz",
            postgresql_where=text("source_created_at_tz IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    external_part_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observed_messages.id"), nullable=True
    )
    external_message_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    part_type: Mapped[str] = mapped_column(String, nullable=False)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class ObservedToolCall(Base):
    """A normalized tool-call projection of an ``observed_parts`` tool part.

    Extracts tool name, status, and truncated input/output from the tool
    part's ``data``.  It is a derived query surface, not a source of truth —
    keyed by ``(client_id, source_database_id, external_part_id)`` (one row
    per tool part).
    """

    __tablename__ = "observed_tool_calls"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_part_id",
            name="uq_observed_tool_calls_source_key",
        ),
        Index(
            "ix_observed_tool_calls_session_created",
            "session_id",
            "source_created_at",
        ),
        Index(
            "ix_observed_tool_calls_name_created",
            "tool_name",
            "source_created_at",
        ),
        Index(
            "ix_observed_tool_calls_status",
            "tool_status",
            postgresql_where=text("tool_status IS NOT NULL"),
        ),
        Index(
            "ix_observed_tool_calls_retention",
            "source_created_at_tz",
            postgresql_where=text("source_created_at_tz IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observed_parts.id"), nullable=False
    )
    external_part_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("observed_messages.id"), nullable=True
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tool_input: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    tool_output: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class OpenCodeSessionTodo(Base):
    """A session todo extracted from OpenCode collector payloads.

    Each row represents an action item / todo item associated with a
    coding session, identified by its position within the session and
    optionally deduplicated via content_hash.
    """

    __tablename__ = "opencode_session_todos"

    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_session_id",
            "position",
            name="uq_opencode_session_todos_source_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id", ondelete="CASCADE"), nullable=False
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    priority: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_created_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_updated_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_created_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at_tz: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
