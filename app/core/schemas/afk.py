"""Pydantic response schemas for the AFK outcomes read-only API (issue #452).

These view models map the stored ``afk_*`` tables 1:1 onto the consumer-facing
response.  They reuse the locked domain vocabulary (``EngineeringOutcome``,
``CorrelationEvidence``) verbatim from :mod:`afk_outcomes.models` and never
re-derive it.  The ``provisional`` / ``inferred`` markers make low-confidence
derived links visible so consumers never mistake an inferred link for an
explicit one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from afk_outcomes.models import CorrelationEvidence, EngineeringOutcome


class RunSummary(BaseModel):
    """A single run row from the ``afk_runs`` aggregate table."""

    afk_run_id: str = Field(description="ULID primary key of the run")
    provider: str = Field(description="Source provider: github | gitlab")
    status: str = Field(description="RunStatus value (e.g. running, completed)")
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome_status: str | None = Field(
        default=None,
        description="Derived EngineeringOutcomeStatus (e.g. merged, closed)",
    )
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class EntityLink(BaseModel):
    """One derived link between a run and an engineering entity.

    Carries the full correlation provenance stored on ``afk_run_entities``
    plus a computed ``provisional`` marker that is ``True`` for any link whose
    ``role`` is not ``resolved`` (i.e. ``referenced`` or ``noise``) — the
    low-confidence, inferred links produced by the temporal-inference rule or
    a mere mention.  Provisional links are never silently indistinguishable
    from explicit ``resolved`` links.
    """

    entity_id: str = Field(description="Provider-scoped stable id (e.g. 'issue:37')")
    entity_type: str
    external_id: str
    provider: str
    repository: str
    role: str = Field(description="resolved | referenced | noise")
    correlation_method: str | None = None
    correlation_confidence: float = 0.0
    evidence: list[CorrelationEvidence] = Field(default_factory=list)
    resolver_version: str | None = None
    owning_change_request_id: str | None = Field(
        default=None,
        description="External id of the owning change request (lineage links)",
    )
    correlation_source: str = Field(
        default="direct",
        description="direct | owning_change_request",
    )
    provisional: bool = Field(
        default=False,
        description="True when the link is inferred (role != 'resolved')",
    )


class EntityRow(BaseModel):
    """One ``afk_run_entities`` row returned by the list-entities endpoint.

    Includes the owning ``afk_run_id`` and the ``superseded_at`` timestamp so
    superseded state is surfaced (never hidden), alongside the same provenance
    and provisional marker as :class:`EntityLink`.
    """

    entity_id: str
    entity_type: str
    external_id: str
    provider: str
    repository: str
    afk_run_id: str
    role: str
    correlation_method: str | None = None
    correlation_confidence: float = 0.0
    evidence: list[CorrelationEvidence] = Field(default_factory=list)
    resolver_version: str | None = None
    owning_change_request_id: str | None = Field(
        default=None,
        description="External id of the owning change request (lineage links)",
    )
    correlation_source: str = Field(
        default="direct",
        description="direct | owning_change_request",
    )
    superseded_at: datetime | None = Field(
        default=None,
        description="Set when a higher-confidence link superseded this one",
    )
    provisional: bool = Field(
        default=False,
        description="True when the link is inferred (role != 'resolved')",
    )


class SessionLink(BaseModel):
    """An association between a run and an OpenCode session.

    Session attachments are always heuristic (temporal overlap, agent/client
    identity, project match) — the correlation engine marks every session link
    ``inferred=True``.  ``inferred`` is therefore always ``True`` here and is
    surfaced so provisional attachments are never indistinguishable from
    explicit links.  Usage/cost aggregates are joined from the ``sessions``
    table when the link resolved to an internal session.
    """

    session_id: str | None = Field(
        default=None, description="Internal Gateway session UUID"
    )
    external_session_id: str | None = Field(
        default=None, description="External OpenCode session ID (e.g. ses_* ID)"
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    inferred: bool = Field(
        default=True,
        description="True — session attachments are always heuristic/inferred",
    )
    agent: str | None = None
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cost_usd: Decimal | None = None


class UsageAggregate(BaseModel):
    """Per-run usage and cost aggregates (CONTEXT.md token vocabulary).

    ``active_tokens`` is input + output (Active Tokens); cache read/write are
    siblings, never folded into active tokens.
    """

    active_tokens: int = Field(
        default=0, description="Active Tokens = input + output"
    )
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Decimal | None = None
    message_count: int = 0
    session_count: int = 0


class RunDetail(BaseModel):
    """The full chain for one ``afk_run_id``.

    Surfaces the run aggregate, its outcome, the engineering entities grouped
    by type (each carrying correlation provenance and a provisional marker),
    the linked sessions (with usage/cost aggregates), the distinct agents, and
    the run-level usage aggregate.
    """

    run: RunSummary
    outcome: EngineeringOutcome | None = None
    issues: list[EntityLink] = Field(default_factory=list)
    change_requests: list[EntityLink] = Field(default_factory=list)
    reviews: list[EntityLink] = Field(default_factory=list)
    commits: list[EntityLink] = Field(default_factory=list)
    merge_events: list[EntityLink] = Field(default_factory=list)
    sessions: list[SessionLink] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    usage: UsageAggregate = Field(default_factory=UsageAggregate)


class UnresolvedCorrelationRow(BaseModel):
    """One ``unresolved_correlations`` row returned by the list-correlations
    endpoint.

    Carries the correlation method/confidence/evidence/resolver_version and a
    ``provisional`` marker that is always ``True`` (an unresolved correlation
    is, by definition, not confidently resolved to an entity link).

    Two kinds of row are surfaced:

    * **Low-confidence links** — ``reason`` is ``None`` and ``candidates`` is
      empty; ``entity_id``/``entity_type``/``external_id`` are the real
      entity identity and ``method`` the correlation method.
    * **Engine ambiguous/unmatched outcomes** — ``reason`` is
      ``ambiguous``/``unmatched`` and ``candidates`` holds the competing
      entity ids (empty for ``unmatched``); the row is run-level, so
      ``entity_type`` is the ``afk_run`` sentinel and ``entity_id`` resolves
      to ``afk_run:<afk_run_id>``.
    """

    entity_id: str
    entity_type: str
    external_id: str
    provider: str
    repository: str
    afk_run_id: str | None = Field(
        default=None,
        description="Attributed run, or None when not yet attributed (unresolved)",
    )
    method: str
    reason: str | None = Field(
        default=None,
        description="ambiguous | unmatched for engine unresolved outcomes; None for low-confidence links",
    )
    correlation_confidence: float = 0.0
    candidates: list[str] = Field(
        default_factory=list,
        description="Competing candidate entity ids (ambiguous); empty for unmatched and low-confidence links",
    )
    evidence: list[CorrelationEvidence] = Field(default_factory=list)
    resolver_version: str | None = None
    created_at: datetime | None = None
    provisional: bool = Field(
        default=True,
        description="True — unresolved correlations are always provisional",
    )
