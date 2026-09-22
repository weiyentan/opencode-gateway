"""Pydantic response schemas for the AFK Dashboard summary API (issue #719).

The summary endpoint hides the internal ``afk_dashboard_daily`` table
structure — the summary is the contract.  These view models expose the
additive AFK operational metrics per bucket (a calendar day, or a calendar
month when the caller asks for ``interval=monthly``) plus the response-level
freshness metadata (``derived_at``).

The metrics mirror the additive columns of the daily rollup (issue #714):
run counts, change-request counts, execution outcome counts, session counts,
token category totals, and estimated cost.  Only additive values are
exposed — monthly aggregation is a plain sum of the daily rows performed at
query time.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class SummaryInterval(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """The bucket granularity of a summary query."""

    DAILY = "daily"
    MONTHLY = "monthly"


class AFKDashboardSummaryBucket(BaseModel):
    """One aggregated summary bucket — a day (``daily``) or a month
    (``monthly``) for one ``(provider, repository)`` pair.

    ``period_start`` is the bucket's first calendar day: the day itself for
    ``daily``, the first of the month for ``monthly``.  All metric fields are
    additive provider/rollup observations.
    """

    period_start: date = Field(
        description="Bucket start: the day (daily) or first of the month (monthly)"
    )
    provider: str
    repository: str
    runs_started: int = 0
    change_requests_opened: int = 0
    change_requests_merged: int = 0
    change_requests_closed: int = 0
    execution_count: int = 0
    successful_execution_count: int = 0
    failed_execution_count: int = 0
    cancelled_execution_count: int = 0
    session_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Decimal = Field(
        default=Decimal("0"), description="Summed estimated cost in USD"
    )
    derived_at: datetime | None = Field(
        default=None,
        description="Latest rollup recompute observed for this bucket",
    )
    oldest_derived_at: datetime | None = Field(
        default=None,
        description="Oldest rollup recompute observed for this bucket (freshness floor)",
    )


class AFKDashboardSummary(BaseModel):
    """The AFK Dashboard summary response.

    Carries the effective query (``interval``, resolved ``from_date`` /
    ``to_date``, optional ``provider`` / ``repository`` filters) and the
    ordered buckets.  ``derived_at`` is the latest rollup recompute across the
    returned buckets — the freshness marker — and is ``None`` when the window
    contains no data.
    """

    interval: str = Field(description="daily | monthly")
    from_date: date
    to_date: date
    provider: str | None = None
    repository: str | None = None
    buckets: list[AFKDashboardSummaryBucket] = Field(default_factory=list)
    derived_at: datetime | None = Field(
        default=None,
        description="Latest rollup recompute across the returned buckets",
    )
    oldest_derived_at: datetime | None = Field(
        default=None,
        description="Oldest rollup recompute across the returned buckets (freshness floor)",
    )
