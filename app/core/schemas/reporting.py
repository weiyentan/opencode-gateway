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
