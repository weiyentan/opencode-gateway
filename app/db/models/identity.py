"""SQLAlchemy ORM models for the identity layer — OpenCode clients and
collector credentials.

These models are used by Alembic for autogenerate (metadata reflection)
and serve as canonical type documentation.  Runtime database access in
API endpoints uses asyncpg directly, consistent with the existing
Gateway architecture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


def _utcnow() -> datetime:
    """Return the current UTC time — usable as a Python-side column default."""
    return datetime.now(timezone.utc)


class OpenCodeClient(Base):
    """A registered OpenCode client (e.g. a Runner VM or Paperclip instance).

    Each client has a unique name and may hold zero or more collector
    credentials that authenticate telemetry collectors.
    """

    __tablename__ = "opencode_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    credentials: Mapped[list["CollectorCredential"]] = relationship(
        back_populates="client", lazy="selectin"
    )


class CollectorCredential(Base):
    """A hashed bearer token that authenticates a telemetry collector.

    The raw token is generated server-side, hashed with SHA-256, and
    stored here.  The raw value is only shown once — at provision time.
    Tokens can be revoked and are scoped to a single client.
    """

    __tablename__ = "collector_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opencode_clients.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    client: Mapped[OpenCodeClient] = relationship(back_populates="credentials")
