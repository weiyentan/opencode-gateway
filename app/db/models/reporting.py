"""SQLAlchemy ORM models for the reporting-ingestion tables (migration 0031).

These models back the normalized-event reporting ingestion surface
(issue #479, cross-repo PRD #478): the producer (``fast-api-eda-gateway``)
emits ``afk.events`` messages carrying ``event_type: "normalized"``, and the
Gateway persists each delivery to an immutable ``reporting_deliveries`` row
plus an append-only ``delivery_state_trails`` entry.

These models are used by Alembic for autogenerate (metadata reflection)
and serve as canonical type documentation.  Runtime database access is
raw asyncpg — the ``app.api.reporting_ingest`` endpoint is the only writer.

The ``reporting_*`` table family is deliberately distinct from the AFK
outcome tables (``delivery_log`` / ``engineering_events``, migration 0026)
so the "only writer" contract owned by ``afk_outcomes.repository`` is
preserved by construction: this write path never touches those tables.

Write semantics (enforced by the endpoint, guaranteed by the constraints
on migration 0031):

* **Delivery dedup** — ``reporting_deliveries`` is keyed by
  ``UNIQUE (provider, delivery_id)`` and written with
  ``ON CONFLICT (provider, delivery_id) DO NOTHING`` so a redelivered
  message is absorbed (outcome ``duplicate``) rather than duplicated.
* **State trail** — ``delivery_state_trails`` is keyed by
  ``UNIQUE (provider, delivery_id, state, occurred_at)`` and written with
  ``ON CONFLICT DO NOTHING`` so an identical redelivered message appends
  nothing.  The trail key is anchored on the *message's* ``occurred_at``
  (not ingest time) so the dedup is deterministic across redeliveries.
* **No locked vocabulary** — ``event_type`` is stored verbatim as an
  opaque string (the producer currently sends ``"normalized"``); no type
  mapping is performed at this layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


def _utcnow() -> datetime:
    """Return the current UTC time — usable as a Python-side column default."""
    return datetime.now(timezone.utc)


class ReportingDelivery(Base):
    """An immutable, deduplicated record of one reporting delivery.

    Keyed by ``UNIQUE (provider, delivery_id)`` (the producer's own
    delivery UUID) and written with ``ON CONFLICT DO NOTHING`` so a
    delivery is persisted at most once.  ``client_id`` is the attribution
    from ``require_collector_token`` (a loose reference — no FK — so this
    schema does not couple to the exact identity table shape).
    """

    __tablename__ = "reporting_deliveries"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "delivery_id",
            name="uq_reporting_deliveries_provider_delivery",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DeliveryStateTrail(Base):
    """An append-only lifecycle entry for one delivery.

    Keyed by ``UNIQUE (provider, delivery_id, state, occurred_at)`` where
    ``occurred_at`` is the *message's* timestamp — the deterministic dedup
    anchor across redeliveries.  There is no FK to ``reporting_deliveries``
    (a loose reference, mirroring ``afk_run_sessions``) so the trail stays
    writable regardless of the delivery insert outcome.  ``state`` is
    bounded to ``persisted`` / ``rejected`` (the first-delivery outcome);
    duplicates append nothing.
    """

    __tablename__ = "delivery_state_trails"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "delivery_id",
            "state",
            "occurred_at",
            name="uq_delivery_state_trails_delivery_state_time",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
