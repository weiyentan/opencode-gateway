"""Pydantic schemas for the reporting-ingestion surface (issue #479).

These schemas shape the request/response contract of
``POST /api/v1/reporting/ingest/deliveries`` — the persistence surface for
normalized reporting deliveries from the producer
(``fast-api-eda-gateway``).

The response follows the same batch-level-2xx-with-per-delivery-outcomes
convention as ``/ingest`` (README "acceptance contract"): a batch always
succeeds at the HTTP level and reports per-delivery outcomes so the future
consumer can commit offsets; only unknown ``schema_version`` (400) and
missing/invalid collector tokens (401) fail the whole request.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReportingDeliveryIn(BaseModel):
    """One normalized reporting delivery to persist.

    ``delivery_id`` is the producer's own delivery UUID — the idempotency
    key (deduped by ``UNIQUE (provider, delivery_id)``).  ``event_type`` is
    stored verbatim (no type mapping / no locked vocabulary at this layer).
    ``occurred_at`` is the *message's* timestamp — the deterministic dedup
    anchor for the state trail across redeliveries.
    """

    provider: str = Field(description="Provider that emitted the delivery")
    delivery_id: str = Field(description="Producer delivery UUID — idempotency key")
    event_type: str = Field(
        description="Opaque event type (e.g. 'normalized'), stored verbatim"
    )
    occurred_at: datetime = Field(
        description="The message's timestamp — state-trail dedup anchor"
    )
    payload: dict = Field(
        default_factory=dict, description="Verbatim delivery payload"
    )

    @field_validator("occurred_at")
    @classmethod
    def _require_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return v.astimezone(timezone.utc)


class ReportingIngestRequest(BaseModel):
    """A batch of reporting deliveries pushed by an authenticated collector."""

    schema_version: str = Field(description="Schema version of the payload")
    deliveries: list[ReportingDeliveryIn] = Field(
        default_factory=list, description="Deliveries to ingest"
    )


class ReportingDeliveryResult(BaseModel):
    """Per-delivery result in the ingest response.

    Status values (issue #479):
    ``accepted`` — a fresh delivery was persisted (and its state trail written).
    ``duplicate`` — idempotent redelivery; no new row created.
    ``rejected`` — persistence failed (validation or internal error); no row.
    """

    index: int = Field(description="Zero-based index of the delivery in the batch")
    delivery_id: str = Field(description="Producer delivery UUID")
    status: str = Field(description="accepted | duplicate | rejected")
    reason: str | None = Field(
        default=None, description="Human-readable reason if rejected"
    )
    delivery_record_id: uuid.UUID | None = Field(
        default=None, description="reporting_deliveries.id on the accepted path"
    )


class ReportingIngestResponse(BaseModel):
    """Response returned after processing a reporting-ingest batch."""

    accepted_count: int = Field(description="Number of deliveries accepted")
    duplicate_count: int = Field(description="Number of deliveries that were duplicates")
    rejected_count: int = Field(description="Number of deliveries rejected")
    results: list[ReportingDeliveryResult] = Field(
        default_factory=list, description="Per-delivery results"
    )


# ── Read-side schemas (reporting read-only API, issue #484) ────────────────


class ResourceSummary(BaseModel):
    """One ingested resource with its current aggregate (read-only).

    Addressable by stable resource identity — ``provider`` +
    ``repository_url`` + ``resource_type`` + ``resource_number`` (the
    producer's partition-key vocabulary, PRD #478).  ``resource_id`` is the
    composite ``provider:repository_url:resource_type:resource_number`` key
    for display/client correlation.

    The aggregate carries the resource's current payload **verbatim** — the
    Gateway never derives a "completed"/"finished"/outcome state here
    (PRD Implementation Decision 13).  When the current-aggregate layer
    (#480) lands, ``last_occurred_at`` and per-key provenance slot in
    alongside ``payload``; until then ``delivery_count``,
    ``last_delivery_id`` and ``last_ingested_at`` are derived at read time
    from the immutable ``reporting_deliveries`` table.
    """

    resource_id: str = Field(description="Composite stable resource identity key")
    provider: str
    repository_url: str
    resource_type: str
    resource_number: str
    delivery_count: int = Field(
        default=0, ge=0, description="Number of deliveries observed for this resource"
    )
    last_delivery_id: str | None = Field(
        default=None, description="Producer delivery id of the most recent delivery"
    )
    last_ingested_at: datetime | None = Field(
        default=None, description="Gateway receive time of the most recent delivery"
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Verbatim current resource payload (never a derived completion state)",
    )


class StateTrailEntry(BaseModel):
    """One append-only state-trail row for a delivery.

    ``state`` is the delivery lifecycle state (e.g. ``received``,
    ``normalized``, ``published``, ``persisted``, ``rejected``, ``failed``)
    — a pipeline observation, never a resource completion claim.
    """

    provider: str
    delivery_id: str
    state: str
    occurred_at: datetime
    detail: dict[str, Any] | None = None
    created_at: datetime


class ReportingSessionLink(BaseModel):
    """A session link surfaced by the reporting read API.

    Exact session↔resource correlation (#481) is not yet implemented, so
    every link surfaced here is **provisional** (inferred — the existing
    ``afk_run_sessions`` heuristic) and carries an empty
    ``source_references`` list.  The shape is forward-compatible: when
    exact correlation lands, ``provisional`` becomes ``False`` and
    ``source_references`` carries the explicit reference that produced the
    link.  The API never fabricates an exact link that does not exist.
    """

    session_id: str | None = Field(default=None, description="Internal Gateway session UUID")
    external_session_id: str | None = Field(
        default=None, description="External OpenCode session ID (e.g. ses_* ID)"
    )
    afk_run_id: str | None = Field(default=None, description="Owning AFK run id")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    agent: str | None = None
    provisional: bool = Field(
        default=True,
        description="True while the link is inferred (exact correlation not yet available)",
    )
    source_references: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Explicit resource references that produced an exact link; empty until #481",
    )


class ResourceDetail(BaseModel):
    """Full detail for one resource: aggregate + state trail + session links.

    ``session_links`` is empty until exact correlation (#481) exists — the
    Gateway never invents a resource↔session link it cannot prove.  No
    completion/finished/outcome state is asserted anywhere in this shape.
    """

    resource: ResourceSummary
    state_trail: list[StateTrailEntry] = Field(default_factory=list)
    session_links: list[ReportingSessionLink] = Field(default_factory=list)
