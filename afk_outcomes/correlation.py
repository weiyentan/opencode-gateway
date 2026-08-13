"""Deterministic AFK correlation engine (pure domain).

Reconstructs an :class:`AFKRun` from a window of canonical engineering
entities and events by running five correlation rules in descending
confidence order:

1. ``explicit_run_id``        — an explicit run identifier in an event payload
2. ``issue_reference``        — the owning change request's body references
3. ``branch_issue_reference`` — the change request's branch name references
4. ``commit_issue_reference`` — commit messages reference
5. ``temporal_inference``     — activity temporally overlaps the run window

The first deterministic lock wins: once a rule binds an entity to the run, a
lower-confidence rule never overrides it.  A change request that closes
several issues binds to *all* of them (multi-issue is not ambiguity).
Ambiguity is never random-tiebroken: competing candidates and unmatched
outcomes are surfaced as :class:`UnresolvedCorrelation` results rather than
forced into links.

Every derived link records ``method``, ``correlation_confidence``, evidence
carrying source identifiers, and ``resolver_version``.  Session-to-run
attachment uses provisional inferred links (temporal overlap, agent identity,
client identity, project match) marked ``inferred=True``.

Determinism: identical input produces byte-identical canonical output —
stable iteration order everywhere, no randomness, no clock dependence beyond
the injectable ULID source.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from afk_outcomes.models import (
    RESOLVER_VERSION,
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    ResolutionResult,
    RunEntityLink,
    RunSessionLink,
    UnresolvedCorrelation,
    UnresolvedReason,
)
from afk_outcomes.serialization import MonotonicULID, ULIDSource

# ── Confidence levels (descending rule order) ────────────────────────────────

CONFIDENCE_EXPLICIT_RUN_ID = 1.0
CONFIDENCE_ISSUE_RESOLVED = 1.0
CONFIDENCE_ISSUE_MENTION = 0.1
CONFIDENCE_BRANCH_ISSUE = 0.8
CONFIDENCE_COMMIT_ISSUE = 0.6
CONFIDENCE_TEMPORAL = 0.4

# Confidence threshold separating "resolved" from "referenced" entity roles.
RESOLVED_ROLE_THRESHOLD = 0.5

# Priority order for choosing the dominant session-attachment heuristic.
_SESSION_HEURISTIC_PRIORITY = (
    "agent_identity",
    "client_identity",
    "project_match",
    "temporal_overlap",
)

_ISSUE_REF_RE = re.compile(r"#(\d+)")
_RESOLVE_RE = re.compile(
    r"\b(?:resolves?|closes?|fixes?)\s+#?\d+(?:\s*(?:,|and|&)?\s*#?\d+)*",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+")


@dataclass
class Proposal:
    """An un-materialised correlation candidate emitted by a rule."""

    entity_id: str
    confidence: float
    method: str
    evidence: list[CorrelationEvidence] = field(default_factory=list)


@dataclass
class Ambiguity:
    """Competing candidates a rule could not deterministically resolve."""

    entity_id: str
    candidates: list[str] = field(default_factory=list)
    evidence: list[CorrelationEvidence] = field(default_factory=list)


@dataclass
class RuleResult:
    """The output of one correlation rule pass."""

    proposals: list[Proposal] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)


@dataclass
class SessionDescriptor:
    """A candidate OpenCode session the resolver may attach to the run."""

    session_id: str | None = None
    external_session_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    agent: str | None = None
    client: str | None = None
    project: str | None = None


# ── Shared parsing helpers ───────────────────────────────────────────────────


def _issue_numbers(text: str | None) -> set[int]:
    """Return every ``#N`` reference in ``text`` (provider-agnostic)."""
    if not text:
        return set()
    return {int(n) for n in _ISSUE_REF_RE.findall(text)}


def _resolved_issue_numbers(body: str | None) -> set[int]:
    """Return issue numbers explicitly resolved/closed/fixed in ``body``."""
    if not body:
        return set()
    numbers: set[int] = set()
    for match in _RESOLVE_RE.finditer(body):
        numbers.update(int(n) for n in _NUMBER_RE.findall(match.group(0)))
    return numbers


def _issues(entities: Sequence[EngineeringEntity]) -> dict[int, EngineeringEntity]:
    return {
        e.number: e
        for e in entities
        if e.entity_type is EntityType.ISSUE and e.number is not None
    }


def _change_requests(entities: Sequence[EngineeringEntity]) -> list[EngineeringEntity]:
    return [e for e in entities if e.entity_type is EntityType.CHANGE_REQUEST]


def _commits(entities: Sequence[EngineeringEntity]) -> list[EngineeringEntity]:
    return [e for e in entities if e.entity_type is EntityType.COMMIT]


def _merge_events(entities: Sequence[EngineeringEntity]) -> list[EngineeringEntity]:
    return [e for e in entities if e.entity_type is EntityType.MERGE_EVENT]


def _overlaps(
    a_start: datetime | None,
    a_end: datetime | None,
    b_start: datetime | None,
    b_end: datetime | None,
) -> bool:
    """True when the two half-open time windows overlap."""
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    return a_start <= b_end and b_start <= a_end


# ── Correlation rules ────────────────────────────────────────────────────────


class ExplicitRunIdRule:
    """Bind entities whose event payloads carry an explicit run identifier."""

    name = "explicit_run_id"
    method = "explicit_run_id"
    confidence = CONFIDENCE_EXPLICIT_RUN_ID

    async def evaluate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> RuleResult:
        proposals: list[Proposal] = []
        for event in sorted(events, key=lambda e: e.event_id):
            payload = event.payload or {}
            matched_key = next(
                (
                    key
                    for key in ("run_id", "afk_run_id")
                    if payload.get(key) == run.afk_run_id
                ),
                None,
            )
            if matched_key is not None:
                proposals.append(
                    Proposal(
                        entity_id=event.entity_id,
                        confidence=self.confidence,
                        method=self.method,
                        evidence=[
                            CorrelationEvidence(
                                kind="explicit_run_id",
                                source_entity_id=event.entity_id,
                                detail=f"{matched_key}={run.afk_run_id}",
                                weight=self.confidence,
                            )
                        ],
                    )
                )
        return RuleResult(proposals=proposals)

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> list[Correlation]:
        result = await self.evaluate(run, entities=entities, events=events)
        return [
            Correlation(
                correlation_id="",
                afk_run_id=run.afk_run_id,
                entity_id=p.entity_id,
                correlation_confidence=p.confidence,
                method=p.method,
                evidence=p.evidence,
            )
            for p in result.proposals
        ]


class IssueReferenceRule:
    """Anchor the run's owning change request and extract its issue references."""

    name = "issue_reference"
    method = "issue_reference"

    async def evaluate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> RuleResult:
        change_requests = _change_requests(entities)
        issues = _issues(entities)

        matches = [cr for cr in change_requests if cr.title == run.title]
        if len(matches) > 1:
            # Multiple change requests share the run title — cannot resolve.
            ambiguity = Ambiguity(
                entity_id=run.afk_run_id,
                candidates=sorted(cr.entity_id for cr in matches),
                evidence=[
                    CorrelationEvidence(
                        kind="title_match",
                        source_entity_id=cr.entity_id,
                        detail=f"title={run.title}",
                        weight=1.0,
                    )
                    for cr in sorted(matches, key=lambda c: c.entity_id)
                ],
            )
            return RuleResult(ambiguities=[ambiguity])
        if not matches:
            return RuleResult()

        owning = matches[0]
        proposals: list[Proposal] = []

        # The owning change request itself is bound to the run.
        proposals.append(
            Proposal(
                entity_id=owning.entity_id,
                confidence=CONFIDENCE_ISSUE_RESOLVED,
                method=self.method,
                evidence=[
                    CorrelationEvidence(
                        kind="title_match",
                        source_entity_id=owning.entity_id,
                        detail=f"title={run.title}",
                        weight=1.0,
                    )
                ],
            )
        )

        # Issues explicitly resolved by the owning change request's body.
        resolved = _resolved_issue_numbers(owning.description)
        for number in sorted(resolved):
            if number not in issues:
                continue
            proposals.append(
                Proposal(
                    entity_id=issues[number].entity_id,
                    confidence=CONFIDENCE_ISSUE_RESOLVED,
                    method=self.method,
                    evidence=[
                        CorrelationEvidence(
                            kind="issue_reference",
                            source_entity_id=owning.entity_id,
                            detail=f"resolves #{number}",
                            weight=1.0,
                        )
                    ],
                )
            )

        # Issues merely mentioned (not resolved) — low confidence.
        mentioned = _issue_numbers(owning.description) - resolved
        for number in sorted(mentioned):
            if number not in issues:
                continue
            proposals.append(
                Proposal(
                    entity_id=issues[number].entity_id,
                    confidence=CONFIDENCE_ISSUE_MENTION,
                    method=self.method,
                    evidence=[
                        CorrelationEvidence(
                            kind="issue_reference",
                            source_entity_id=owning.entity_id,
                            detail=f"mentioned #{number}",
                            weight=CONFIDENCE_ISSUE_MENTION,
                        )
                    ],
                )
            )

        return RuleResult(proposals=proposals)

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> list[Correlation]:
        result = await self.evaluate(run, entities=entities, events=events)
        return [
            Correlation(
                correlation_id="",
                afk_run_id=run.afk_run_id,
                entity_id=p.entity_id,
                correlation_confidence=p.confidence,
                method=p.method,
                evidence=p.evidence,
            )
            for p in result.proposals
        ]


class BranchIssueReferenceRule:
    """Bind issues referenced by a change request's branch name."""

    name = "branch_issue_reference"
    method = "branch_issue_reference"
    confidence = CONFIDENCE_BRANCH_ISSUE

    async def evaluate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> RuleResult:
        issues = _issues(entities)
        proposals: list[Proposal] = []
        for cr in sorted(_change_requests(entities), key=lambda c: c.entity_id):
            if not cr.branch:
                continue
            numbers = {
                int(n) for n in _NUMBER_RE.findall(cr.branch)
            } & set(issues)
            for number in sorted(numbers):
                proposals.append(
                    Proposal(
                        entity_id=issues[number].entity_id,
                        confidence=self.confidence,
                        method=self.method,
                        evidence=[
                            CorrelationEvidence(
                                kind="branch_reference",
                                source_entity_id=cr.entity_id,
                                detail=f"branch={cr.branch}",
                                weight=self.confidence,
                            )
                        ],
                    )
                )
        return RuleResult(proposals=proposals)

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> list[Correlation]:
        result = await self.evaluate(run, entities=entities, events=events)
        return [
            Correlation(
                correlation_id="",
                afk_run_id=run.afk_run_id,
                entity_id=p.entity_id,
                correlation_confidence=p.confidence,
                method=p.method,
                evidence=p.evidence,
            )
            for p in result.proposals
        ]


class CommitIssueReferenceRule:
    """Bind issues referenced by commit messages."""

    name = "commit_issue_reference"
    method = "commit_issue_reference"
    confidence = CONFIDENCE_COMMIT_ISSUE

    async def evaluate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> RuleResult:
        issues = _issues(entities)
        proposals: list[Proposal] = []
        for commit in sorted(_commits(entities), key=lambda c: c.entity_id):
            numbers = _issue_numbers(commit.title) & set(issues)
            for number in sorted(numbers):
                proposals.append(
                    Proposal(
                        entity_id=issues[number].entity_id,
                        confidence=self.confidence,
                        method=self.method,
                        evidence=[
                            CorrelationEvidence(
                                kind="commit_reference",
                                source_entity_id=commit.entity_id,
                                detail=f"commit={commit.entity_id}",
                                weight=self.confidence,
                            )
                        ],
                    )
                )
        return RuleResult(proposals=proposals)

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> list[Correlation]:
        result = await self.evaluate(run, entities=entities, events=events)
        return [
            Correlation(
                correlation_id="",
                afk_run_id=run.afk_run_id,
                entity_id=p.entity_id,
                correlation_confidence=p.confidence,
                method=p.method,
                evidence=p.evidence,
            )
            for p in result.proposals
        ]


class TemporalInferenceRule:
    """Bind entities whose activity window overlaps the run window."""

    name = "temporal_inference"
    method = "temporal_inference"
    confidence = CONFIDENCE_TEMPORAL

    async def evaluate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> RuleResult:
        proposals: list[Proposal] = []
        for entity in sorted(entities, key=lambda e: e.entity_id):
            if entity.entity_type is EntityType.MERGE_EVENT:
                continue
            if _overlaps(
                entity.created_at,
                entity.updated_at,
                run.started_at,
                run.finished_at,
            ):
                proposals.append(
                    Proposal(
                        entity_id=entity.entity_id,
                        confidence=self.confidence,
                        method=self.method,
                        evidence=[
                            CorrelationEvidence(
                                kind="temporal_overlap",
                                source_entity_id=entity.entity_id,
                                detail="activity overlaps run window",
                                weight=self.confidence,
                            )
                        ],
                    )
                )
        return RuleResult(proposals=proposals)

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
    ) -> list[Correlation]:
        result = await self.evaluate(run, entities=entities, events=events)
        return [
            Correlation(
                correlation_id="",
                afk_run_id=run.afk_run_id,
                entity_id=p.entity_id,
                correlation_confidence=p.confidence,
                method=p.method,
                evidence=p.evidence,
            )
            for p in result.proposals
        ]


DEFAULT_RULES = (
    ExplicitRunIdRule(),
    IssueReferenceRule(),
    BranchIssueReferenceRule(),
    CommitIssueReferenceRule(),
    TemporalInferenceRule(),
)


# ── Session attachment ───────────────────────────────────────────────────────


def _session_heuristic(
    run: AFKRun,
    *,
    run_agent: str | None,
    run_client: str | None,
    run_project: str | None,
    session: SessionDescriptor,
) -> str | None:
    """Return the dominant matched heuristic for ``session`` vs ``run``, or None."""
    matched: set[str] = set()
    if (
        session.agent is not None
        and run_agent is not None
        and session.agent == run_agent
    ):
        matched.add("agent_identity")
    if (
        session.client is not None
        and run_client is not None
        and session.client == run_client
    ):
        matched.add("client_identity")
    if session.project is not None and run_project is not None and session.project == run_project:
        matched.add("project_match")
    if _overlaps(
        session.started_at,
        session.finished_at,
        run.started_at,
        run.finished_at,
    ):
        matched.add("temporal_overlap")
    for heuristic in _SESSION_HEURISTIC_PRIORITY:
        if heuristic in matched:
            return heuristic
    return None


# ── The batch resolver ───────────────────────────────────────────────────────


class CorrelationEngine:
    """Orchestrate the five rules into a deterministic, lock-wins resolution."""

    def __init__(
        self,
        *,
        rules: Sequence[Any] | None = None,
        ulid_source: ULIDSource | None = None,
        resolver_version: str = RESOLVER_VERSION,
    ) -> None:
        self._rules: Sequence[Any] = rules if rules is not None else DEFAULT_RULES
        self._ulid: ULIDSource = (
            ulid_source if ulid_source is not None else MonotonicULID()
        )
        self._resolver_version = resolver_version

    async def resolve(
        self,
        run: AFKRun,
        *,
        entities: Sequence[EngineeringEntity],
        events: Sequence[EngineeringEvent],
        sessions: Sequence[SessionDescriptor] | None = None,
        client: str | None = None,
    ) -> ResolutionResult:
        afk_run_id = self._ulid.next_ulid()
        seed = run.model_copy(update={"afk_run_id": afk_run_id})

        locked: dict[str, Correlation] = {}
        correlations: list[Correlation] = []
        ambiguities: dict[str, Ambiguity] = {}

        for rule in self._rules:
            result = await rule.evaluate(run=seed, entities=entities, events=events)
            for ambiguity in result.ambiguities:
                if ambiguity.entity_id in locked or ambiguity.entity_id in ambiguities:
                    continue
                ambiguities[ambiguity.entity_id] = ambiguity
            for proposal in result.proposals:
                if proposal.entity_id in locked or proposal.entity_id in ambiguities:
                    continue
                correlation = Correlation(
                    correlation_id=self._ulid.next_ulid(),
                    afk_run_id=afk_run_id,
                    entity_id=proposal.entity_id,
                    correlation_confidence=proposal.confidence,
                    method=proposal.method,
                    evidence=proposal.evidence,
                    resolver_version=self._resolver_version,
                )
                locked[proposal.entity_id] = correlation
                correlations.append(correlation)

        owning = self._owning_change_request(seed, entities)
        run_agent = owning.author if owning is not None else None
        run_project = (
            owning.repository
            if owning is not None
            else (entities[0].repository if entities else None)
        )

        entity_links = self._entity_links(
            afk_run_id, correlations, entities, owning=owning
        )
        session_links = self._session_links(
            afk_run_id,
            seed,
            run_agent=run_agent,
            run_project=run_project,
            client=client,
            sessions=sessions,
        )
        unresolved = self._unresolved(
            afk_run_id, seed, entities, ambiguities=ambiguities, correlations=correlations
        )

        reconstructed = AFKRun(
            afk_run_id=afk_run_id,
            provider=seed.provider,
            status=seed.status,
            title=seed.title,
            started_at=seed.started_at,
            finished_at=seed.finished_at,
            entities=sorted(entities, key=lambda e: e.entity_id),
            events=sorted(events, key=lambda e: (e.occurred_at, e.event_id)),
            correlations=correlations,
            outcome=self._outcome(seed, entities, correlations),
            entity_links=entity_links,
            session_links=session_links,
        )

        return ResolutionResult(
            run=reconstructed,
            unresolved=unresolved,
            resolver_version=self._resolver_version,
        )

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _owning_change_request(
        run: AFKRun, entities: Sequence[EngineeringEntity]
    ) -> EngineeringEntity | None:
        matches = [cr for cr in _change_requests(entities) if cr.title == run.title]
        return matches[0] if len(matches) == 1 else None

    def _entity_links(
        self,
        afk_run_id: str,
        correlations: list[Correlation],
        entities: Sequence[EngineeringEntity],
        *,
        owning: EngineeringEntity | None,
    ) -> list[RunEntityLink]:
        links: list[RunEntityLink] = []
        # Bound entities (in correlation order).
        for correlation in correlations:
            role = (
                "resolved"
                if correlation.correlation_confidence >= RESOLVED_ROLE_THRESHOLD
                else "referenced"
            )
            links.append(
                RunEntityLink(
                    afk_run_id=afk_run_id,
                    entity_id=correlation.entity_id,
                    role=role,
                    correlation_confidence=correlation.correlation_confidence,
                    resolver_version=self._resolver_version,
                )
            )

        bound_ids = {c.entity_id for c in correlations}

        # Noise: unrelated change requests.
        for cr in sorted(_change_requests(entities), key=lambda e: e.entity_id):
            if cr.entity_id not in bound_ids:
                links.append(
                    RunEntityLink(
                        afk_run_id=afk_run_id,
                        entity_id=cr.entity_id,
                        role="noise",
                        correlation_confidence=0.0,
                        resolver_version=self._resolver_version,
                    )
                )

        # Noise: commits referencing numbers outside the run's known set
        # (resolved issues plus the owning change request's own number).
        known_numbers: set[int] = set()
        if owning is not None:
            known_numbers = _resolved_issue_numbers(owning.description)
            if owning.number is not None:
                known_numbers.add(owning.number)
        for commit in sorted(_commits(entities), key=lambda e: e.entity_id):
            if commit.entity_id in bound_ids:
                continue
            referenced = _issue_numbers(commit.title)
            if referenced and not referenced.issubset(known_numbers):
                links.append(
                    RunEntityLink(
                        afk_run_id=afk_run_id,
                        entity_id=commit.entity_id,
                        role="noise",
                        correlation_confidence=0.0,
                        resolver_version=self._resolver_version,
                    )
                )

        return links

    def _session_links(
        self,
        afk_run_id: str,
        run: AFKRun,
        *,
        run_agent: str | None,
        run_project: str | None,
        client: str | None,
        sessions: Sequence[SessionDescriptor] | None,
    ) -> list[RunSessionLink]:
        if not sessions:
            return []
        links: list[RunSessionLink] = []
        for session in sorted(
            sessions,
            key=lambda s: (s.external_session_id or "", s.session_id or ""),
        ):
            method = _session_heuristic(
                run,
                run_agent=run_agent,
                run_client=client,
                run_project=run_project,
                session=session,
            )
            if method is None:
                continue
            links.append(
                RunSessionLink(
                    afk_run_id=afk_run_id,
                    session_id=session.session_id,
                    external_session_id=session.external_session_id,
                    started_at=session.started_at,
                    finished_at=session.finished_at,
                    inferred=True,
                    method=method,
                    resolver_version=self._resolver_version,
                )
            )
        return links

    def _unresolved(
        self,
        afk_run_id: str,
        run: AFKRun,
        entities: Sequence[EngineeringEntity],
        *,
        ambiguities: dict[str, Ambiguity],
        correlations: list[Correlation],
    ) -> list[UnresolvedCorrelation]:
        unresolved: list[UnresolvedCorrelation] = []

        # Ambiguous outcomes surfaced by a rule (competing candidate sources).
        for entity_id in sorted(ambiguities):
            ambiguity = ambiguities[entity_id]
            unresolved.append(
                UnresolvedCorrelation(
                    unresolved_id=self._ulid.next_ulid(),
                    afk_run_id=afk_run_id,
                    entity_id=entity_id,
                    reason=UnresolvedReason.AMBIGUOUS,
                    candidates=ambiguity.candidates,
                    evidence=ambiguity.evidence,
                    resolver_version=self._resolver_version,
                )
            )

        # Unmatched: no owning change request could be anchored to the run and
        # no rule produced any link — the run's engineering outcome is unknown.
        if (
            self._owning_change_request(run, entities) is None
            and not ambiguities
            and not correlations
        ):
            unresolved.append(
                UnresolvedCorrelation(
                    unresolved_id=self._ulid.next_ulid(),
                    afk_run_id=afk_run_id,
                    entity_id=afk_run_id,
                    reason=UnresolvedReason.UNMATCHED,
                    candidates=[],
                    evidence=[],
                    resolver_version=self._resolver_version,
                )
            )

        return unresolved

    @staticmethod
    def _outcome(
        run: AFKRun,
        entities: Sequence[EngineeringEntity],
        correlations: list[Correlation],
    ) -> EngineeringOutcome | None:
        owning = CorrelationEngine._owning_change_request(run, entities)
        if owning is None:
            return None
        change_request_ids = [owning.entity_id]
        resolved_issue_ids = sorted(
            c.entity_id
            for c in correlations
            if c.entity_id.startswith("issue:")
            and c.correlation_confidence >= RESOLVED_ROLE_THRESHOLD
        )
        status = EngineeringOutcomeStatus.MERGED
        merge = next(
            (
                m
                for m in sorted(_merge_events(entities), key=lambda e: e.entity_id)
                if m.number == owning.number
            ),
            None,
        )
        return EngineeringOutcome(
            status=status,
            change_request_ids=change_request_ids,
            resolved_issue_ids=resolved_issue_ids,
            merge_event_id=merge.entity_id if merge is not None else None,
            merged_at=merge.created_at if merge is not None else None,
        )
