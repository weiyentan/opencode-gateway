"""SQLAlchemy ORM models for the AFK outcome persistence tables (migration 0026).

These models are used by Alembic for autogenerate (metadata reflection)
and serve as canonical type documentation.  Runtime database access is
raw asyncpg — the ``afk_outcomes.repository`` package is the only writer.

Write semantics are documented on the migration (0026): engineering
events are immutable facts (conflict-ignore); state rows are enrich-only
(raise confidence, append evidence, update first/last_seen, correct
derived outcome, mark superseded links, never hard-delete).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


def _utcnow() -> datetime:
    """Return the current UTC time — usable as a Python-side column default."""
    return datetime.now(timezone.utc)


class AFKRun(Base):
    """The aggregate root: one AFK run and its derived engineering outcome.

    ``afk_run_id`` is the ULID primary key carried by the domain
    ``afk_outcomes.models.AFKRun``.  ``outcome_status`` is the derived
    ``EngineeringOutcomeStatus``; ``outcome`` is the full JSONB projection
    of the ``EngineeringOutcome`` (change-request ids, resolved issue ids,
    merge event id, merged-at).  ``first_seen_at``/``last_seen_at`` are
    advanced by the enrich-only upsert.
    """

    __tablename__ = "afk_runs"

    afk_run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AFKRunSessionLink(Base):
    """An association between an AFK run and an OpenCode session.

    ``afk_run_id`` is ``NOT NULL``.  ``session_id`` (internal Gateway session
    UUID) and ``external_session_id`` (external OpenCode ``ses_*`` id) are
    both nullable and deliberately carry no foreign key toward ``sessions``
    so this schema does not couple to the exact session table shape.
    """

    __tablename__ = "afk_run_sessions"

    __table_args__ = (
        UniqueConstraint(
            "afk_run_id",
            "external_session_id",
            name="uq_afk_run_sessions_run_external_session",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    afk_run_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("afk_runs.afk_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    external_session_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AFKRunEntityLink(Base):
    """A derived association between an AFK run and one engineering entity.

    Keyed by ``UNIQUE (provider, repository, entity_type, external_id,
    afk_run_id)`` — one row per (entity, run).  ``afk_run_id`` is ``NOT
    NULL``.  Every derived link stores ``correlation_method``,
    ``correlation_confidence``, ``evidence`` (JSONB array of
    ``CorrelationEvidence``), and ``resolver_version``.  ``superseded_at``
    is set — never deleted — when a higher-confidence link supersedes this
    one.
    """

    __tablename__ = "afk_run_entities"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "afk_run_id",
            name="uq_afk_run_entities_entity_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    afk_run_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("afk_runs.afk_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    owning_change_request_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    correlation_method: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    correlation_source: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'direct'")
    )
    correlation_confidence: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class EngineeringEvent(Base):
    """An immutable, timestamped observation about an engineering entity.

    Identity is ``UNIQUE (provider, repository, entity_type, external_id,
    event_type, occurred_at)``.  Re-delivery no-ops via
    ``ON CONFLICT DO NOTHING``.  ``provider_event_id`` is stored when a
    provider emits one and is the authority for ``occurred_at`` in that case.
    """

    __tablename__ = "engineering_events"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "event_type",
            "occurred_at",
            name="uq_engineering_events_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_event_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    actor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    first_ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DeliveryLog(Base):
    """A replay-safe record of one AFK outcome delivery.

    Keyed by ``UNIQUE (provider, delivery_id)``; written with
    ``ON CONFLICT DO NOTHING`` so a delivery is processed at most once.
    """

    __tablename__ = "delivery_log"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "delivery_id",
            name="uq_delivery_log_provider_delivery",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    afk_run_id: Mapped[Optional[str]] = mapped_column(
        String(26),
        ForeignKey("afk_runs.afk_run_id"),
        nullable=True,
    )
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class UnresolvedCorrelation(Base):
    """A correlation that could not be confidently resolved to a run.

    Stored only here (never in ``afk_run_entities``) so unresolved
    correlations remain visible for later manual or algorithmic resolution.
    ``afk_run_id`` is NOT NULL — every unresolved correlation is attributed to
    the run that produced it.  Keyed by ``UNIQUE (provider, repository,
    entity_type, external_id, afk_run_id, method)`` so the run is part of the
    row identity (replay-safe across runs).

    Two kinds of row share this table under the enrich-only contract:

    * **Low-confidence links** — a ``Correlation`` below the resolved-role
      threshold (a ``referenced`` link).  ``entity_type``/``external_id``/
      ``method`` are the real entity identity + correlation method;
      ``reason`` is NULL and ``candidates`` is the empty array.
    * **Engine ambiguous/unmatched outcomes** — run-level results from the
      correlation engine with no single entity or method.  ``entity_type``
      is the ``afk_run`` sentinel, ``external_id`` is the run id, ``method``
      mirrors ``reason`` (``ambiguous``/``unmatched``), and ``candidates``
      holds the competing candidate entity ids (empty for ``unmatched``).
    """

    __tablename__ = "unresolved_correlations"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "afk_run_id",
            "method",
            name="uq_unresolved_correlations_entity_run_method",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    afk_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    correlation_confidence: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ResourceSessionAssociation(Base):
    """A deterministic many-to-many association between a resource and a session.

    One resource may link to many sessions and one session may link to many
    resources (migration 0034).  Each association derives only from an
    explicit stable resource reference carried in session metadata — never
    temporal/heuristic inference — and records ``source_reference`` (JSONB
    array of ``ReferenceSource``) so each link is provable and reproducible.

    Keyed by ``UNIQUE (provider, repository, resource_type, resource_number,
    external_session_id)`` — the stable resource identity plus the session's
    external identity — and written with
    ``ON CONFLICT (...) DO UPDATE SET last_seen_at = now()`` so the same
    explicit reference converging on the same association never duplicates a
    row, while re-observation advances ``last_seen_at`` (consistent with the
    AFK enrich-only upsert convention).  ``session_id`` (internal Gateway
    session UUID) is a
    nullable enrichment (no FK, mirroring ``afk_run_sessions``); the external
    session id is the deterministic anchor.  No completion/finished claim is
    carried (PRD Implementation Decision 13).
    """

    __tablename__ = "resource_session_associations"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "resource_type",
            "resource_number",
            "external_session_id",
            name="uq_resource_session_associations_resource_session",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    external_session_id: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    repository: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_number: Mapped[str] = mapped_column(String, nullable=False)
    source_reference: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
