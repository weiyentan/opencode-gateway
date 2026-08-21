"""SQLAlchemy ORM models for the AFK outcome persistence tables (migration 0026).

These models are used by Alembic for autogenerate (metadata reflection)
and serve as canonical type documentation.  Runtime database access is
raw asyncpg — the ``afk_outcomes.repository`` package is the only writer.

Write semantics are documented on the migration (0026): engineering
events are immutable facts (conflict-ignore); state rows are enrich-only
(raise confidence, append evidence, update first/last_seen, correct
derived outcome, mark superseded links, never hard-delete).

Retention tiers (issue #483, ADR 0022 — configurable via ``GATEWAY_RETENTION_*``):

* **Aggregates (indefinite)** — ``afk_runs``, ``afk_run_sessions``: the
  reconstructed run read-model is never swept by default.
* **Metadata (12 months)** — ``engineering_events`` (the event rows),
  ``delivery_log``, ``afk_run_entities``, ``unresolved_correlations``.
* **Redacted payload (90 days)** — the ``engineering_events.payload``
  redacted projection (the event row itself is 12-month metadata).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, UniqueConstraint, text
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

    ``observation_key`` (issue #523) is the fact's deterministic, NOT NULL,
    UNIQUE natural key derived from the six identity fields; ``observed_via``
    (``webhook``/``backfill``) and ``snapshot_at`` record observation
    provenance.  The 6-column identity UNIQUE is unchanged — the key is
    additive, never a replacement.

    Retention (issue #483, ADR 0022): the event row is metadata (12 months,
    ``GATEWAY_RETENTION_AFK_METADATA_DAYS``); the ``payload`` column is the
    redacted-payload tier (90 days, ``GATEWAY_RETENTION_AFK_PAYLOAD_DAYS``).
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
        UniqueConstraint(
            "observation_key",
            name="uq_engineering_events_observation_key",
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
    observation_key: Mapped[str] = mapped_column(String, nullable=False)
    observed_via: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'webhook'")
    )
    snapshot_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DeliveryLog(Base):
    """A replay-safe record of one AFK outcome delivery.

    Keyed by ``UNIQUE (provider, delivery_id)``; written with
    ``ON CONFLICT DO NOTHING`` so a delivery is processed at most once.

    Retention (issue #483, ADR 0022): metadata tier — 12 months
    (``GATEWAY_RETENTION_AFK_METADATA_DAYS``).
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


class ExecutionBinding(Base):
    """A durable Gateway record linking one AWX execution to one OpenCode
    external session and one provider resource identity (migration 0037).

    One row per AWX job.  ``awx_job_id`` carries a ``UNIQUE`` constraint
    so repeating the same binding is a no-op.  The provider resource
    identity (``provider``, ``repository_url``, ``entity_type``,
    ``entity_number``) is NOT unique — multiple failed and successful
    executions for the same change request are allowed.

    ``failure_reason`` and ``failure_summary`` carry bounded diagnostic
    information.  Raw ``extra_vars``, stdout, prompts, tokens, or
    arbitrary AWX payloads are never stored.
    """

    __tablename__ = "execution_bindings"

    __table_args__ = (
        UniqueConstraint(
            "awx_job_id",
            name="uq_execution_bindings_awx_job_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    awx_job_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    repository_url: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_number: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failure_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ClosureLink(Base):
    """The derived current state of one change-request->issue link (migration 0036).

    Keyed by ``UNIQUE (change_request_provider, change_request_repository,
    change_request_external_id, issue_provider, issue_repository,
    issue_external_id, kind)`` — both endpoint identities (flattened stable
    resource identities, no ``engineering_resources`` registry) plus the
    relationship kind (``references`` vs ``declares_closure``, never
    conflated).  ``state`` is ``active`` / ``revoked`` (explicit
    snapshot-diff revocation) / ``parked`` (conflicting same-timestamp
    snapshots, never arbitrarily won); ``revoked_at`` stamps the revocation
    and is cleared on re-activation.  The projection is a recomputed view
    over the immutable ``engineering_events`` facts: state is corrected
    toward the latest derivation on every recompute (conflict-update),
    never a separate source of truth.  Revoked rows are retained so the
    declaring set of an issue stays complete for incremental recomputes.
    """

    __tablename__ = "closure_links"

    __table_args__ = (
        UniqueConstraint(
            "change_request_provider",
            "change_request_repository",
            "change_request_external_id",
            "issue_provider",
            "issue_repository",
            "issue_external_id",
            "kind",
            name="uq_closure_links_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    change_request_provider: Mapped[str] = mapped_column(String, nullable=False)
    change_request_repository: Mapped[str] = mapped_column(String, nullable=False)
    change_request_external_id: Mapped[str] = mapped_column(String, nullable=False)
    issue_provider: Mapped[str] = mapped_column(String, nullable=False)
    issue_repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_external_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    derived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ClosureEpisode(Base):
    """One immutable closure episode: an open->close interval for one issue
    (migration 0036).

    Keyed by the issue endpoint identity (its flattened stable resource
    identity) plus the close observation time; ``closed_at IS NULL`` marks
    the currently-open interval.  ``status`` carries the fixed episode
    vocabulary (``pending``, ``awaiting_closure``, ``unmatched``,
    ``ambiguous``, ``inferred``, ``superseded``); the attributed
    change-request tuple is set only for ``inferred`` episodes.  The partial
    unique index ``uq_closure_episodes_current_issue`` (``superseded_at IS
    NULL``) guarantees at most one current episode per issue; a
    reopen/reclose cycle marks the earlier episode ``superseded`` — never
    deleted.  Episode identity and historical attribution are immutable; the
    outcome is recomputed toward the latest facts.
    """

    __tablename__ = "closure_episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    issue_provider: Mapped[str] = mapped_column(String, nullable=False)
    issue_repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_external_id: Mapped[str] = mapped_column(String, nullable=False)
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    change_request_provider: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    change_request_repository: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    change_request_external_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    derived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    superseded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ClosureUnresolved(Base):
    """A versioned unresolved record for one closed closure episode
    (migration 0036).

    Keyed by ``UNIQUE (issue_provider, issue_repository, issue_external_id,
    closed_at, reason)`` — one record per (episode, outcome), versioned via
    ``resolver_version`` / ``derived_at``.  ``reason`` is ``unmatched``
    (zero candidates) or ``ambiguous`` (multiple candidates, or an eligible
    parked declaration); ``candidates`` holds the competing change-request
    identities (empty for unmatched).  Never tie-broken, never scored;
    historical records of episodes that later resolved are retained (no
    hard delete anywhere in the projection).
    """

    __tablename__ = "closure_unresolved"

    __table_args__ = (
        UniqueConstraint(
            "issue_provider",
            "issue_repository",
            "issue_external_id",
            "closed_at",
            "reason",
            name="uq_closure_unresolved_episode_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    issue_provider: Mapped[str] = mapped_column(String, nullable=False)
    issue_repository: Mapped[str] = mapped_column(String, nullable=False)
    issue_external_id: Mapped[str] = mapped_column(String, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(String, nullable=False)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    resolver_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    derived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

