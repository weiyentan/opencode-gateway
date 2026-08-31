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

from afk_outcomes.models import AWXJobIdentity, CorrelationEvidence, EngineeringOutcome


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
    parent_session_id: str | None = Field(
        default=None,
        description="External session ID of the parent session, if any (issue #575)",
    )


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


class ChangeRequestExecutionCounts(BaseModel):
    """Aggregated AWX execution counts for one change request (issue #610).

    Counts use the locked :class:`~afk_outcomes.models.ExecutionOutcome`
    vocabulary (``running`` / ``completed`` / ``failed`` / ``cancelled``) and
    are aggregated per change-request identity — implementation, review, and
    retry executions all converge on one row.  Executions without a durable
    change-request identity (any NULL resource identity column) are excluded
    at the query layer and never contribute counts.
    """

    total: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class ChangeRequestSummaryRow(BaseModel):
    """One change-request summary row returned by ``GET /change-requests``.

    Identity is the flattened stable resource identity
    ``(provider, repository, external_id)`` with ``resource_type`` fixed to
    ``change_request`` (GitHub PRs and GitLab MRs both normalize there).

    * ``provider_state`` is derived from observed ``engineering_events``
      facts — ``merged`` / ``closed`` / ``open`` — never a provider API
      claim; ``None`` when no lifecycle fact is observed.
    * ``automation_state`` is the owning lifecycle's ``afk_runs.status``
      (``pending`` / ``running`` / ``completed`` / ``failed`` /
      ``cancelled``); ``None`` when no AFK run is linked.
    * ``total_estimated_cost_usd`` sums linked session cost and is ``None``
      when no linked session carries cost telemetry — unavailable, never
      zero.
    * ``latest_linked_activity`` is the most recent timestamp across linked
      runs, observed facts, and executions.
    * ``provider_state_observed_at`` is the ``occurred_at`` of the most
      recent observed ``change_request`` lifecycle fact — the freshness of
      the derived ``provider_state`` (``None`` when no fact is observed).
    """

    provider: str = Field(description="Source provider: github | gitlab")
    repository: str = Field(description="Full owner/repo (or group/project) name")
    external_id: str = Field(
        description="Provider-scoped change-request number (opaque string)"
    )
    resource_type: str = Field(
        default="change_request",
        description="Fixed resource type — GitHub PRs and GitLab MRs both normalize to change_request",
    )
    provider_state: str | None = Field(
        default=None,
        description="Derived from observed facts: merged | closed | open",
    )
    automation_state: str | None = Field(
        default=None,
        description=(
            "AFK run lifecycle status: pending | running | completed | failed | cancelled"
        ),
    )
    total_estimated_cost_usd: Decimal | None = Field(
        default=None,
        description="Sum of linked session costs; null when no cost telemetry is available",
    )
    latest_linked_activity: datetime | None = Field(
        default=None,
        description="Most recent activity among linked runs, facts, and executions",
    )
    provider_state_observed_at: datetime | None = Field(
        default=None,
        description=(
            "occurred_at of the most recent observed change_request lifecycle "
            "fact — freshness of the derived provider state"
        ),
    )
    executions: ChangeRequestExecutionCounts = Field(
        default_factory=ChangeRequestExecutionCounts
    )


class ChangeRequestDetailSummary(ChangeRequestSummaryRow):
    """The summary block of the change-request detail (issue #611).

    Extends the list summary row with detail-only enrichment:

    * ``title`` — the most recent linked execution/run title, ``None`` when
      no linked title is stored;
    * ``merged_at`` — the provider occurrence time of the observed
      ``change_request.merged`` fact, ``None`` when never observed merged.

    ``provider_state_observed_at`` (the ``occurred_at`` of the most recent
    observed ``change_request`` lifecycle fact — the freshness of the derived
    ``provider_state``) is inherited from :class:`ChangeRequestSummaryRow`.
    """

    title: str | None = None
    merged_at: datetime | None = None


class ChangeRequestLinkedRun(BaseModel):
    """One AFK run linked to the change request (issue #611).

    A run links through any of three durable paths — the explicit
    change-request binding on ``afk_runs``, a ``change_request`` entity link
    on ``afk_run_entities``, or an AWX execution binding — and
    ``link_sources`` records every path that linked it (deduplicated,
    deterministic order).
    """

    afk_run_id: str
    provider: str
    status: str
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome_status: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    link_sources: list[str] = Field(
        default_factory=list,
        description=(
            "change_request_binding | entity_link | execution — every durable "
            "path that linked this run, deduplicated and sorted"
        ),
    )


class ChangeRequestExecutionItem(BaseModel):
    """One linked AWX execution binding in the change-request detail.

    Carries the same approved execution metadata as the execution-binding
    read path (AWX job identity, outcome, timestamps, failure metadata)
    plus the change-request-detail enrichment:

    * ``purpose`` — the execution purpose vocabulary (``implementation`` /
      ``review`` / ``retry``) when an explicit stored signal carries it;
      ``None`` (unavailable) otherwise — the Gateway never invents a purpose
      it cannot derive from stored data;
    * ``duration_seconds`` — computed ``finished_at − started_at`` (``None``
      when either timestamp is missing);
    * per-execution token usage and ``estimated_cost_usd`` — joined from the
      OpenCode session the binding resolved (``None`` when the binding has no
      resolved session or the session carries no telemetry — unavailable,
      never zero).
    """

    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str | None = Field(
        default=None,
        description="External OpenCode session id (None when unresolved)",
    )
    session_id: str | None = Field(
        default=None,
        description="Internal Gateway session UUID (None when unresolved)",
    )
    afk_run_id: str | None = None
    outcome: str | None = Field(
        default=None,
        description="ExecutionOutcome: running | completed | failed | cancelled",
    )
    purpose: str | None = Field(
        default=None,
        description=(
            "implementation | review | retry when an explicit stored signal "
            "carries it; None when unavailable (never invented)"
        ),
    )
    trigger_type: str | None = None
    source_event_id: str | None = None
    branch: str | None = None
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    failure_reason: str | None = None
    failure_summary: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_cache_read_tokens: int | None = None
    total_cache_write_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None


class ChangeRequestMergeState(BaseModel):
    """The provider merge state of the change request (issue #611).

    Derived from observed ``engineering_events`` facts — never a provider
    API claim.  ``state`` is ``merged`` when a ``change_request.merged``
    fact is observed and ``not_merged`` when lifecycle facts are observed
    but none is a merge.  ``merged_at`` is the provider occurrence time of
    the merged fact (``None`` when never observed merged).
    """

    state: str = Field(description="merged | not_merged")
    merged_at: datetime | None = None


class ChangeRequestTimelineEvent(BaseModel):
    """One observed ``change_request`` fact in the provenance timeline."""

    event_type: str
    occurred_at: datetime
    observed_via: str | None = Field(
        default=None, description="webhook | backfill (None for legacy rows)"
    )
    snapshot_at: datetime | None = None
    actor: str | None = None


class ChangeRequestTimeline(BaseModel):
    """The optional provenance timeline (issue #611).

    Chronologically ordered (earliest first) observed ``change_request``
    facts.
    """

    events: list[ChangeRequestTimelineEvent] = Field(default_factory=list)


class ChangeRequestDetail(BaseModel):
    """The provider-scoped change-request detail (issue #611).

    One composite read model per ``(provider, repository, external_id)``:
    the summary block, the linked AFK runs (with link provenance), the
    ordered AWX execution bindings (with purpose, per-execution session
    telemetry, cost, and duration), the deduplicated linked sessions, the
    aggregate usage/cost, the provider merge state, and the optional
    provenance timeline.  Missing optional identity/cost telemetry is
    ``None`` — never invented.
    """

    change_request: ChangeRequestDetailSummary
    afk_runs: list[ChangeRequestLinkedRun] = Field(default_factory=list)
    executions: list[ChangeRequestExecutionItem] = Field(default_factory=list)
    sessions: list[SessionLink] = Field(default_factory=list)
    usage: UsageAggregate = Field(default_factory=UsageAggregate)
    total_estimated_cost_usd: Decimal | None = Field(
        default=None,
        description="Aggregate cost across linked sessions; null when unavailable",
    )
    merge_state: ChangeRequestMergeState | None = None
    timeline: ChangeRequestTimeline | None = None


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
