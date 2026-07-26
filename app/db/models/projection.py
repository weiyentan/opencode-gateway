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
    Integer,
    Numeric,
    String,
    UniqueConstraint,
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
