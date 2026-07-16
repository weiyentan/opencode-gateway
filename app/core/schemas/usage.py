"""Pydantic schemas for the usage reporting API.

Defines request/response models for aggregates, records, sessions, and
paginated responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ── Aggregate schemas ─────────────────────────────────────────────────────


class AggregateRow(BaseModel):
    """A single aggregate row — one group's token/cost totals."""

    group_value: str = Field(description="Value of the group-by dimension")
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_cached_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal | None = Field(default=None)
    record_count: int = Field(default=0, ge=0)


class AggregateQuery(BaseModel):
    """Query parameters for the aggregates endpoint.

    Note that ``group_by`` is accepted as comma-separated values in the
    query string; this model is used for response/validation but the
    actual parsing is done from query params.
    """

    client_id: uuid.UUID | None = Field(default=None)
    model: str | None = Field(default=None)
    session_id: uuid.UUID | None = Field(default=None)
    start_date: datetime
    end_date: datetime
    group_by: list[str] | None = Field(default=None)


# ── Record schemas ────────────────────────────────────────────────────────


class RecordRow(BaseModel):
    """A single usage record returned by the records endpoint."""

    id: uuid.UUID
    client_id: uuid.UUID
    source_database_id: uuid.UUID
    session_id: uuid.UUID
    model_name: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost_usd: Decimal | None = None
    reported_at: datetime
    ingested_at: datetime
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


class RecordQuery(BaseModel):
    """Query parameters for the records endpoint."""

    client_id: uuid.UUID | None = Field(default=None)
    model: str | None = Field(default=None)
    session_id: uuid.UUID | None = Field(default=None)
    start_date: datetime
    end_date: datetime
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    sort_by: str = Field(default="reported_at")
    sort_dir: str = Field(default="desc")


# ── Session schemas ───────────────────────────────────────────────────────


class SessionSummary(BaseModel):
    """A session-level summary returned by the sessions endpoint."""

    id: uuid.UUID
    client_id: uuid.UUID
    source_database_id: uuid.UUID
    first_message_at: datetime
    last_message_at: datetime
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_estimated_cost_usd: Decimal | None = None
    loki_search_url: str | None = Field(
        default=None,
        description="Grafana Explore URL for drill-down into Loki logs",
    )


# ── Paginated response ────────────────────────────────────────────────────


class PaginatedResponse(BaseModel, Generic[T]):  # noqa: UP046
    """A generic paginated response wrapper."""

    items: list[T] = Field(description="The items for the current page")
    total: int = Field(description="Total number of items across all pages")
    limit: int = Field(description="Maximum items per page")
    offset: int = Field(description="Zero-based offset of the current page")
