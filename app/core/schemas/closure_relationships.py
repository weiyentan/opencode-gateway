"""Pydantic response schemas for the closure-relationships read-only API
(issue #525).

These view models map the Slice 3 projection tables (``closure_episodes``,
``closure_links``, ``closure_unresolved``, migration 0036) 1:1 onto the
consumer-facing response.  They reuse the locked domain vocabulary
(``ClosureEpisodeStatus`` / ``ClosureLinkKind`` / ``ClosureLinkState``)
from :mod:`afk_outcomes.models` for validation and never re-derive it.

Every projection view carries ``derived_at`` (the last successful recompute
of that row — the stored freshness marker, PRD Implementation Decision 16)
and ``resolver_version``, so staleness is visible in every response and
never silent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ClosureEpisodeView(BaseModel):
    """One immutable ``closure_episodes`` row (an open→close interval).

    Carries the issue endpoint identity (its flattened stable resource
    identity), the interval bounds, the fixed episode status, and the
    attributed change-request tuple (set only for ``inferred`` episodes).
    ``superseded_at`` marks an episode overtaken by a later reopen/reclose
    cycle — superseded episodes are surfaced, never hidden.
    """

    issue_provider: str
    issue_repository: str
    issue_external_id: str
    opened_at: datetime | None = Field(
        default=None, description="Interval start — the issue's own open/reopen observation"
    )
    closed_at: datetime | None = Field(
        default=None,
        description="Interval end — the issue.closed occurrence; NULL while open",
    )
    status: str = Field(
        description=(
            "ClosureEpisodeStatus value: pending | awaiting_closure | unmatched | "
            "ambiguous | inferred | superseded"
        )
    )
    change_request_provider: str | None = Field(
        default=None, description="Attributed change request (inferred only)"
    )
    change_request_repository: str | None = None
    change_request_external_id: str | None = None
    resolver_version: str | None = Field(
        default=None, description="Version of the closure-episode projector"
    )
    derived_at: datetime | None = Field(
        default=None, description="Last successful recompute of this row"
    )
    superseded_at: datetime | None = Field(
        default=None,
        description="Set when a later reopen/reclose episode overtook this one",
    )


class ClosureLinkView(BaseModel):
    """One ``closure_links`` row — the derived current state of one
    change-request→issue link.

    Carries both endpoint identities (change-request and issue, flattened
    stable resource identities), the relationship ``kind`` (``references``
    vs ``declares_closure``), and the derived ``state`` (``active`` /
    ``revoked`` / ``parked``) — the declaration/revocation snapshot surface.
    """

    change_request_provider: str
    change_request_repository: str
    change_request_external_id: str
    issue_provider: str
    issue_repository: str
    issue_external_id: str
    kind: str = Field(description="references | declares_closure")
    state: str = Field(description="active | revoked | parked")
    revoked_at: datetime | None = Field(
        default=None, description="Revocation stamp (cleared on re-activation)"
    )
    resolver_version: str | None = None
    derived_at: datetime | None = Field(
        default=None, description="Last successful recompute of this row"
    )


class ClosureCandidateView(BaseModel):
    """One competing change-request candidate in an ambiguous episode."""

    provider: str
    repository: str
    external_id: str


class ClosureUnresolvedView(BaseModel):
    """One ``closure_unresolved`` row — a versioned unresolved record for a
    closed episode (``unmatched`` or ``ambiguous``).

    Candidates are the competing change-request endpoint identities (empty
    for ``unmatched``).  Never tie-broken, never scored.
    """

    issue_provider: str
    issue_repository: str
    issue_external_id: str
    closed_at: datetime
    reason: str = Field(description="unmatched | ambiguous")
    candidates: list[ClosureCandidateView] = Field(default_factory=list)
    resolver_version: str | None = None
    derived_at: datetime | None = Field(
        default=None, description="Last successful recompute of this row"
    )


class IssueClosureAnswer(BaseModel):
    """The current issue→change-request answer (PRD #521 Read API surface 1).

    The current episode (the one with ``superseded_at IS NULL``) with its
    status, the unresolved-record candidates when the episode is
    ``ambiguous``/``unmatched`` (empty otherwise), and the evidence links
    for the issue.  ``derived_at`` is the latest last-successful-recompute
    timestamp across the returned projection rows; ``resolver_version`` the
    projector version that produced them.
    """

    issue_provider: str
    issue_repository: str
    issue_external_id: str
    episode: ClosureEpisodeView
    candidates: list[ClosureCandidateView] = Field(default_factory=list)
    evidence: list[ClosureLinkView] = Field(default_factory=list)
    derived_at: datetime | None = Field(
        default=None, description="Latest last-successful-recompute timestamp"
    )
    resolver_version: str | None = Field(
        default=None, description="Closure-episode projector version"
    )


class IssueEpisodeHistory(BaseModel):
    """The auditable episode/evidence history for one issue (PRD #521 Read
    API surface 2).

    Every immutable episode (including ``superseded`` — never hidden), the
    declaration/revocation link states (evidence), and the versioned
    unresolved records.  ``derived_at`` / ``resolver_version`` mirror the
    freshness of the returned projection rows.
    """

    issue_provider: str
    issue_repository: str
    issue_external_id: str
    episodes: list[ClosureEpisodeView] = Field(default_factory=list)
    evidence: list[ClosureLinkView] = Field(default_factory=list)
    unresolved: list[ClosureUnresolvedView] = Field(default_factory=list)
    derived_at: datetime | None = None
    resolver_version: str | None = None
