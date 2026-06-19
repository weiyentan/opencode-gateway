"""Runner and observation domain models — SQLAlchemy ORM."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Runner(Base):
    """A Runner VM registered with the Gateway.

    Each runner hosts workspace directories and systemd-managed
    OpenCode Serve instances.  The executor plugin polls runner state
    and writes observations into the observation tables.
    """

    __tablename__ = "runners"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    executor_type: Mapped[str] = mapped_column(Text, nullable=False)
    labels: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="UNKNOWN")
    # admin_status: operator-set (online, offline, maintenance), NULL initially
    admin_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    # health_status: observation-derived (HEALTHY, BLOCKED_DISK_PRESSURE, BLOCKED_MEMORY_PRESSURE, UNKNOWN), NULL initially
    health_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    runner_observations: Mapped[list[RunnerObservation]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )
    workspace_observations: Mapped[list[WorkspaceObservation]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )
    opencode_instance_observations: Mapped[list[OpenCodeInstanceObservation]] = (
        relationship(back_populates="runner", cascade="all, delete-orphan")
    )
    runner_events: Mapped[list[RunnerEvent]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )


class RunnerObservation(Base):
    """Periodic snapshot of runner-level resource utilisation."""

    __tablename__ = "runner_observations"
    __table_args__ = (
        Index("ix_runner_observations_runner_observed", "runner_id", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    disk_used_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_used_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_15m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationship
    runner: Mapped[Runner] = relationship(back_populates="runner_observations")


class WorkspaceObservation(Base):
    """Periodic snapshot of a workspace on a Runner VM."""

    __tablename__ = "workspace_observations"
    __table_args__ = (
        Index(
            "ix_workspace_observations_runner_observed", "runner_id", "observed_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    opencode_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationship
    runner: Mapped[Runner] = relationship(back_populates="workspace_observations")


class OpenCodeInstanceObservation(Base):
    """Periodic snapshot of an OpenCode Serve instance on a Runner VM."""

    __tablename__ = "opencode_instance_observations"
    __table_args__ = (
        Index(
            "ix_opencode_instance_obs_runner_observed", "runner_id", "observed_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    instance_name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationship
    runner: Mapped[Runner] = relationship(back_populates="opencode_instance_observations")


class RunnerEvent(Base):
    """An event recording a runner status change.

    Replaces the previous pattern of writing runner status changes into
    ``job_events`` with a fake zero-UUID job_id.  Each status transition
    (system-driven or operator-driven) is recorded here.
    """

    __tablename__ = "runner_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    # Relationship
    runner: Mapped[Runner] = relationship(back_populates="runner_events")
