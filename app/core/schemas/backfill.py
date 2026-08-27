"""Request/response schemas for the API-triggered AFK backfill endpoints.

The request model mirrors the CLI contract: one provider, one repository,
explicit ISO-8601 ``from``/``until`` bounds (max window enforced by the
endpoint against ``GATEWAY_BACKFILL_MAX_WINDOW_DAYS``), and opt-in
``dry_run``/``show_evidence`` flags.

``extra="forbid"`` guarantees provider credentials (``GITHUB_TOKEN`` /
``GITLAB_TOKEN`` or anything like them) are never accepted in request data —
tokens remain server-side environment secrets.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BackfillRequest(BaseModel):
    """A bounded backfill request for one provider and repository."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: Literal["github", "gitlab"]
    repository: str = Field(min_length=1, max_length=255)
    from_: Annotated[datetime, Field(alias="from")]
    until: datetime
    dry_run: bool = True
    show_evidence: bool = False

    @field_validator("from_", "until")
    @classmethod
    def _assume_utc_when_naive(cls, value: datetime) -> datetime:
        """Normalise naive timestamps to UTC (mirrors the CLI's semantics)."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)  # noqa: UP017
        return value

    @model_validator(mode="after")
    def _window_is_not_inverted(self) -> BackfillRequest:
        if self.from_ > self.until:
            raise ValueError("'from' must not be after 'until'")
        return self


class BackfillReportResponse(BaseModel):
    """The backfill report counters — the :class:`BackfillReport` vocabulary
    re-serialized for the API (identical field names, nothing added)."""

    provider: str
    repository: str
    since: datetime
    until: datetime
    dry_run: bool
    change_requests_scanned: int
    issues_scanned: int
    sessions_considered: int
    explicit_matches: int
    high_matches: int
    inferred_matches: int
    ambiguous: int
    unmatched: int
    evidence_lines: list[str] = Field(default_factory=list)


class BackfillJobResponse(BaseModel):
    """One backfill job: job metadata around the reused report vocabulary."""

    job_id: uuid.UUID
    status: str
    provider: str
    repository: str
    window_from: datetime
    window_until: datetime
    dry_run: bool
    show_evidence: bool
    requested_by: str
    retry_count: int
    failure_category: str | None = None
    failure_message: str | None = None
    evidence: list[str] | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    report: BackfillReportResponse | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BackfillJobResponse:
        """Build the response from an ``afk_backfill_jobs`` row (dict or Record)."""
        return cls(
            job_id=row["id"],
            status=row["status"],
            provider=row["provider"],
            repository=row["repository"],
            window_from=row["window_from"],
            window_until=row["window_until"],
            dry_run=row["dry_run"],
            show_evidence=row["show_evidence"],
            requested_by=row["requested_by"],
            retry_count=row["retry_count"],
            failure_category=row["failure_category"],
            failure_message=row["failure_message"],
            evidence=row["evidence"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
