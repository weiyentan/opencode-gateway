"""Pydantic schemas for the historical usage reconciliation admin endpoint.

Request and response models for ``POST /admin/reconcile-historical-duplicates``.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field, model_validator


class ReconcileRequest(BaseModel):
    """Request body for triggering historical usage reconciliation.

    Scans ``usage_events`` for duplicate ``source_record_id`` values within
    the specified client/date range, deterministically selects the canonical
    row per group, and either previews (``dry_run: true``) or performs
    (``dry_run: false``) the reconciliation.
    """

    dry_run: bool = Field(description="If true, preview only — no data is modified")
    client_id: uuid.UUID | None = Field(
        default=None,
        description="Restrict reconciliation to a single OpenCode client",
    )
    date_from: date | None = Field(
        default=None,
        description="Start date for the duplicate scan window (inclusive)",
    )
    date_to: date | None = Field(
        default=None,
        description="End date for the duplicate scan window (inclusive)",
    )

    @model_validator(mode="after")
    def _validate_date_range(self) -> ReconcileRequest:
        if self.date_from is not None and self.date_to is not None:
            if self.date_from > self.date_to:
                raise ValueError("date_from must be on or before date_to")
        elif (self.date_from is None) != (self.date_to is None):
            raise ValueError("date_from and date_to must both be provided or both omitted")
        return self


class ReconcileResponse(BaseModel):
    """Response body for the reconciliation endpoint — both preview and actual."""

    dry_run: bool = Field(description="Whether this was a dry-run preview")
    events_to_merge: int = Field(
        default=0,
        description="Number of non-canonical duplicate events to remove",
    )
    aggregates_affected: int = Field(
        default=0,
        description="Number of session aggregates that will be or were rebuilt",
    )
    token_adjustment: int = Field(
        default=0,
        description="Net token change if reconciliation is applied "
        "(negative = tokens removed from duplicate events)",
    )
    cost_adjustment_usd: str = Field(
        default="0",
        description="Net cost change if reconciliation is applied "
        "(negative = cost removed, as a decimal string)",
    )
