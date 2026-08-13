"""Pure domain models for AFK Outcome Observability.

These models are provider-independent: they express engineering artifacts
(issues, change requests, commits, reviews, merge events) in neutral terms
shared by GitHub, GitLab, and any future provider.  They carry no database,
network, or application concerns — and, deliberately, no imports from the
application package (``app``).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Provider(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """The source provider that produced the observed engineering data."""

    GITHUB = "github"
    GITLAB = "gitlab"


class EntityType(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """The kind of engineering artifact an :class:`EngineeringEntity` refers to."""

    ISSUE = "issue"
    CHANGE_REQUEST = "change_request"
    COMMIT = "commit"
    REVIEW = "review"
    MERGE_EVENT = "merge_event"


class RunStatus(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Lifecycle status of an AFK run (mirrors the CONTEXT.md Run Status vocabulary)."""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    STALE = "stale"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EngineeringOutcomeStatus(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Terminal status of the change request(s) produced by a run."""

    MERGED = "merged"
    CLOSED = "closed"
    ABANDONED = "abandoned"
    OPEN = "open"


class EngineeringEntity(BaseModel):
    """A stable reference to one engineering artifact at the provider.

    ``entity_id`` is the provider-scoped stable identifier (e.g.
    ``"issue:437"``, ``"change_request:442"``); the remaining fields are
    descriptive metadata that never serves as identity.
    """

    entity_id: str = Field(
        description="Provider-scoped stable identifier (e.g. 'issue:437')"
    )
    entity_type: EntityType
    provider: Provider
    repository: str = Field(description="Full owner/repo (or group/project) name")
    number: int | None = Field(
        default=None, description="Provider issue/change-request number, when applicable"
    )
    head_ref: str | None = Field(
        default=None,
        description="Source branch for a change_request (GitHub head.ref / GitLab source_branch)",
    )
    title: str | None = None
    state: str | None = None
    author: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EngineeringEvent(BaseModel):
    """A timestamped observation about an :class:`EngineeringEntity`."""

    event_id: str
    event_type: str = Field(
        description="e.g. opened, committed, review_submitted, merged, closed"
    )
    provider: Provider
    entity_id: str = Field(description="Reference to an EngineeringEntity.entity_id")
    occurred_at: datetime
    actor: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class CorrelationEvidence(BaseModel):
    """One piece of evidence supporting a :class:`Correlation`."""

    kind: str = Field(
        description="e.g. commit_message_reference, issue_mention, branch_name"
    )
    source_entity_id: str
    detail: str | None = None
    weight: float = Field(default=1.0, ge=0.0)


class Correlation(BaseModel):
    """A link between an AFK run and one engineering entity, with confidence."""

    correlation_id: str
    afk_run_id: str
    entity_id: str
    correlation_confidence: float = Field(ge=0.0, le=1.0)
    method: str = Field(description="How the correlation was established")
    evidence: list[CorrelationEvidence] = Field(default_factory=list)


class EngineeringOutcome(BaseModel):
    """The resolved engineering result of an AFK run."""

    status: EngineeringOutcomeStatus
    change_request_ids: list[str] = Field(default_factory=list)
    resolved_issue_ids: list[str] = Field(default_factory=list)
    merge_event_id: str | None = None
    merged_at: datetime | None = None


class RunEntityLink(BaseModel):
    """An association between an AFK run and one engineering entity."""

    afk_run_id: str
    entity_id: str
    role: str = Field(description="resolved | referenced | noise")
    correlation_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RunSessionLink(BaseModel):
    """An association between an AFK run and an OpenCode session."""

    afk_run_id: str
    session_id: str | None = Field(
        default=None, description="Internal Gateway session UUID"
    )
    external_session_id: str | None = Field(
        default=None, description="External OpenCode session ID (e.g. ses_* ID)"
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AFKRun(BaseModel):
    """The aggregate root: one AFK run and its engineering outcome.

    Carries the run's :class:`RunStatus` and :class:`EngineeringOutcome`,
    the observed entities/events, and the correlations and links that tie
    the run back to the engineering artifacts it touched.
    """

    model_config = ConfigDict(extra="ignore")

    afk_run_id: str = Field(description="ULID primary key of the run")
    provider: Provider
    status: RunStatus
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    entities: list[EngineeringEntity] = Field(default_factory=list)
    events: list[EngineeringEvent] = Field(default_factory=list)
    correlations: list[Correlation] = Field(default_factory=list)
    outcome: EngineeringOutcome | None = None
    entity_links: list[RunEntityLink] = Field(default_factory=list)
    session_links: list[RunSessionLink] = Field(default_factory=list)
