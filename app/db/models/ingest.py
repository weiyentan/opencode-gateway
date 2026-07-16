"""SQLAlchemy ORM models for observability domain tables — usage, sessions,
source databases, ingest batches, and audit trails.

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
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


def _utcnow() -> datetime:
    """Return the current UTC time — usable as a Python-side column default."""
    return datetime.now(timezone.utc)


class SourceDatabase(Base):
    """A source database from which a collector pushes usage records.

    Each source database is tied to both an OpenCode client and the
    collector credential that first discovered it.
    """

    __tablename__ = "source_databases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    collector_credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_credentials.id"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ObservedModel(Base):
    """A model name encountered in ingested usage records."""

    __tablename__ = "observed_models"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Session(Base):
    """A coding session tracked across usage records.

    Aggregate counts (message_count, token totals, cost) are
    incremented as new usage records arrive for the session.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id"), nullable=False
    )
    external_session_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    first_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    total_output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    total_cached_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    total_estimated_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric, nullable=True
    )


class OpenCodeUsageRecord(Base):
    """A single usage record ingested from a collector.

    Deduplication is enforced via the unique constraint on
    ``(client_id, source_database_id, source_record_id)``.
    """

    __tablename__ = "opencode_usage_records"
    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "source_database_id",
            "source_record_id",
            name="uq_opencode_usage_records_dedup",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    source_database_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_databases.id"), nullable=False
    )
    source_record_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("observed_models.id"), nullable=False
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric, nullable=True
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class IngestBatch(Base):
    """Metadata for a single ingest request (batch of usage records)."""

    __tablename__ = "ingest_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    collector_credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_credentials.id"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    collector_version: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    schema_version: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class IngestAudit(Base):
    """Per-record audit trail within an ingest batch."""

    __tablename__ = "ingest_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ingest_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingest_batches.id"), nullable=False
    )
    record_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
