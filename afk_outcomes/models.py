"""Pure domain models for AFK Outcome Observability.

These models are provider-independent: they express engineering artifacts
(issues, change requests, commits, reviews, merge events) in neutral terms
shared by GitHub, GitLab, and any future provider.  They carry no database,
network, or application concerns — and, deliberately, no imports from the
application package (``app``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Version of the correlation engine that produced a derived link.  Recorded on
# every Correlation, RunEntityLink, RunSessionLink, and UnresolvedCorrelation so
# a change in rule semantics can be detected downstream.  Bump when any rule's
# matching logic changes.
RESOLVER_VERSION = "2"

# Version of the exact resource<->session association resolver.  Independent of
# ``RESOLVER_VERSION`` (the correlation engine): the association path derives
# links only from explicit stable resource references and shares no rule
# semantics with the correlation engine (it never runs temporal/heuristic
# inference).  Recorded on every :class:`ResourceSessionAssociation`.  Bump when
# the reference-extraction or dedup logic changes.
ASSOCIATION_RESOLVER_VERSION = "1"

# Version of the closure-episode projector (issue #524).  Independent of
# ``RESOLVER_VERSION`` (the correlation engine) and
# ``ASSOCIATION_RESOLVER_VERSION`` (the exact-association resolver): the
# closure-episode path derives the change-request->issue closure relationship
# from immutable engineering facts and ``issue_links`` snapshot diffs and
# shares no rule semantics with either.  Recorded on every
# :class:`ClosureEpisode`, :class:`ClosureLink`, and :class:`ClosureUnresolved`.
# Bump when any projection/ordering-policy rule changes.
CLOSURE_RESOLVER_VERSION = "1"


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
    description: str | None = Field(
        default=None,
        description="Body/description text (e.g. a change-request description)",
    )
    branch: str | None = Field(
        default=None,
        description="Source branch / head ref (change requests only)",
    )
    owning_change_request_id: str | None = Field(
        default=None,
        description=(
            "External id of the change request that owns this entity's branch "
            "(set at fetch time for commits and reviews; None otherwise)"
        ),
    )


def build_observation_key(
    *,
    provider: Provider | str,
    repository: str,
    entity_type: EntityType | str,
    external_id: str,
    event_type: str,
    occurred_at: datetime,
) -> str:
    """Derive the deterministic observation key for one engineering event fact.

    Every ``engineering_events`` fact carries a deterministic, NOT NULL,
    UNIQUE ``observation_key`` (never random).  The key is a SHA-256 over the
    canonical form of the fact's six identity fields — ``(provider,
    repository, entity_type, external_id, event_type, occurred_at)``, the
    same fields as the table's 6-column identity UNIQUE — so re-deriving the
    same fact always yields the identical key (content-stable, replay-safe)
    and distinct facts yield distinct keys.  The key never depends on
    volatile delivery identifiers.

    ``occurred_at`` is normalised to UTC before hashing so the same instant
    expressed with different UTC offsets derives the identical key.  A naive
    ``occurred_at`` (no ``tzinfo``) is interpreted as UTC.
    """
    provider_value = provider.value if isinstance(provider, Provider) else str(provider)
    entity_type_value = (
        entity_type.value if isinstance(entity_type, EntityType) else str(entity_type)
    )
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    occurred_at_value = occurred_at.astimezone(timezone.utc).isoformat()  # noqa: UP017 - datetime.UTC is 3.11+
    canonical = json.dumps(
        {
            "provider": provider_value,
            "repository": repository,
            "entity_type": entity_type_value,
            "external_id": external_id,
            "event_type": event_type,
            "occurred_at": occurred_at_value,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    observation_key: str = Field(
        default="",
        description=(
            "Deterministic fact identity — see build_observation_key.  Empty "
            "for backfill/engine-built events; the repository derives the key "
            "from the fact's identity fields at persist time."
        ),
        exclude=True,
    )
    observed_via: str = Field(
        default="webhook",
        description="Provenance of the observation: 'webhook' or 'backfill'",
        exclude=True,
    )
    snapshot_at: datetime | None = Field(
        default=None,
        description="Observation time (distinct from the occurred_at occurrence time)",
        exclude=True,
    )


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
    resolver_version: str = Field(
        default=RESOLVER_VERSION,
        description="Version of the correlation engine that produced this link",
    )


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
    correlation_source: str = Field(
        default="direct",
        description=(
            "How the link was established: 'direct' (a correlation rule) or "
            "'owning_change_request' (inherited via the owning change "
            "request's branch lineage)"
        ),
    )
    resolver_version: str = Field(
        default=RESOLVER_VERSION,
        description="Version of the correlation engine that produced this link",
    )


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
    inferred: bool = Field(
        default=False,
        description="True when the session attachment is provisional/inferred",
    )
    method: str | None = Field(
        default=None,
        description="Heuristic that attached the session (e.g. temporal_overlap)",
    )
    resolver_version: str = Field(
        default=RESOLVER_VERSION,
        description="Version of the correlation engine that produced this link",
    )


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


class UnresolvedReason(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Why a correlation could not be deterministically resolved."""

    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class UnresolvedCorrelation(BaseModel):
    """A correlation the resolver could not deterministically establish.

    Ambiguous outcomes (multiple competing candidate sources with no
    higher-confidence rule to break the tie) and unmatched outcomes (no rule
    produced a link) are surfaced here for persistence in
    ``unresolved_correlations`` (#448).  They are never forced into a
    :class:`Correlation` or :class:`RunEntityLink`, and never random-tiebroken.
    """

    unresolved_id: str
    afk_run_id: str
    entity_id: str
    reason: UnresolvedReason
    candidates: list[str] = Field(
        default_factory=list,
        description="Competing source entity IDs (ambiguous) — empty for unmatched",
    )
    evidence: list[CorrelationEvidence] = Field(default_factory=list)
    resolver_version: str = Field(
        default=RESOLVER_VERSION,
        description="Version of the correlation engine that produced this result",
    )


class ResolutionResult(BaseModel):
    """Batch resolver output: the reconstructed run plus unresolved items."""

    run: AFKRun
    unresolved: list[UnresolvedCorrelation] = Field(default_factory=list)
    resolver_version: str = Field(
        default=RESOLVER_VERSION,
        description="Version of the correlation engine that produced this result",
    )


class ReferenceSource(BaseModel):
    """Provenance of one source reference that produced an association.

    ``field`` names the session metadata field that carried the stable
    resource reference (e.g. ``"title"``, ``"project"``, ``"resource_refs"``);
    ``detail`` records the value found there.  Together they make a link
    provable and reproducible: re-reading that field yields the same resource.
    """

    field: str = Field(
        description="Name of the session metadata field that carried the reference"
    )
    detail: str | None = Field(
        default=None,
        description="The resource value found in that field (e.g. the resource number)",
    )


class SessionResourceReference(BaseModel):
    """An explicit stable resource reference carried by one session's metadata.

    This is the ONLY input the association resolver accepts.  It carries the
    full stable resource identity (``provider``, ``repository``,
    ``resource_type``, ``resource_number``) plus the session identity and the
    ``source_field`` that carried it.  It carries no timestamps, no windows,
    and no scores — the resolver structurally cannot temporally or
    heuristically infer a link from it.
    """

    session_id: str | None = Field(
        default=None, description="Internal Gateway session UUID"
    )
    external_session_id: str = Field(
        description=(
            "External OpenCode session ID (e.g. ses_* id) — the deterministic "
            "session anchor"
        )
    )
    source_field: str = Field(
        description="Name of the session metadata field that carried the reference"
    )
    provider: Provider
    repository: str = Field(description="Full owner/repo (or group/project) name")
    resource_type: EntityType
    resource_number: str = Field(
        description=(
            "Provider-scoped external resource id (issue/MR number as string, "
            "commit SHA, review id, ...) — the 'resource_number' half of the "
            "stable resource identity"
        )
    )


class ResourceSessionAssociation(BaseModel):
    """A deterministic many-to-many association between a resource and a session.

    One resource may link to many sessions and one session may link to many
    resources.  Each association records ``source_reference`` — the set of
    session fields that carried the explicit stable reference producing the
    link — so every link is provable and reproducible.  Associations are never
    derived from temporal or heuristic inference, and deliberately carry no
    completion/finished claim (PRD Implementation Decision 13).
    """

    session_id: str | None = Field(
        default=None, description="Internal Gateway session UUID"
    )
    external_session_id: str = Field(
        description="External OpenCode session ID — the deterministic session anchor"
    )
    provider: Provider
    repository: str
    resource_type: EntityType
    resource_number: str
    source_reference: list[ReferenceSource] = Field(
        default_factory=list,
        description="Which session fields carried the link (source-reference provenance)",
    )
    resolver_version: str = Field(
        default=ASSOCIATION_RESOLVER_VERSION,
        description="Version of the association resolver that produced this link",
    )


class ClosureEpisodeStatus(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Lifecycle status of one closure episode (issue #524).

    The fixed vocabulary from PRD #521 — unknowns are never collapsed into one
    opaque ``unresolved``:

    * ``pending`` — an active declaration whose change request is not merged.
    * ``awaiting_closure`` — a merged change request with an active
      declaration, no ``issue.closed`` observed yet.
    * ``unmatched`` — issue closed, zero eligible candidates.
    * ``ambiguous`` — issue closed, multiple candidates (or an eligible
      parked declaration) — never an arbitrary winner.
    * ``inferred`` — exactly one eligible candidate.
    * ``superseded`` — overtaken by a reopen/reclose cycle; the current
      projection points at the latest episode.
    """

    PENDING = "pending"
    AWAITING_CLOSURE = "awaiting_closure"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    INFERRED = "inferred"
    SUPERSEDED = "superseded"


class ClosureLinkKind(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """The two distinct change-request->issue relationship kinds.

    ``references`` — a plain mention (any ``#N`` / ``group/project#N``).
    ``declares_closure`` — closing-syntax declaration (provider-documented
    syntax).  Stored as separate kinds and never conflated.
    """

    REFERENCES = "references"
    DECLARES_CLOSURE = "declares_closure"


class ClosureLinkState(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """The derived current state of one closure link.

    ``active`` — present in the latest unambiguous snapshot.
    ``revoked`` — present in an earlier snapshot, absent in the latest
    (an explicit snapshot-diff revocation).
    ``parked`` — conflicting same-timestamp snapshots left the link
    indeterminate; never arbitrarily won.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    PARKED = "parked"


class IssueLinkTarget(BaseModel):
    """One issue endpoint referenced by an ``issue_links`` snapshot.

    Carries the issue repository (a normalized identity) and its
    provider-scoped number as an opaque string.  Cross-repository references
    carry the issue's own repository, which may differ from the
    change-request repository.
    """

    repository: str = Field(description="Normalized issue repository identity")
    number: str = Field(description="Provider-scoped issue number as an opaque string")


class IssueLinksSnapshot(BaseModel):
    """A full ``issue_links`` snapshot on one change-request observation.

    Two distinct relationship kinds: ``references`` (plain mentions) and
    ``declares_closure`` (closing-syntax declarations).  Both are full
    snapshot sets on every open/update observation; revocations are derived
    from snapshot diffs by the closure-episode projector.
    """

    references: list[IssueLinkTarget] = Field(default_factory=list)
    declares_closure: list[IssueLinkTarget] = Field(default_factory=list)


class ClosureLink(BaseModel):
    """The derived current state of one change-request->issue link.

    Keyed by the change-request endpoint identity (its stable resource
    identity), the issue endpoint identity, and the relationship kind.  The
    projector derives the state from snapshot diffs in provider
    ``occurred_at`` order (ordering policy D) and corrects it toward the
    latest observation on every recompute — the projection is rebuildable
    from facts, never a separate source of truth.
    """

    change_request_provider: Provider
    change_request_repository: str
    change_request_external_id: str
    issue_provider: Provider
    issue_repository: str
    issue_external_id: str
    kind: ClosureLinkKind
    state: ClosureLinkState
    resolver_version: str = Field(
        default=CLOSURE_RESOLVER_VERSION,
        description="Version of the closure-episode projector that derived this link",
    )


class ClosureEpisode(BaseModel):
    """One immutable closure episode: an open->close interval for one issue.

    Keyed by the issue endpoint identity (its stable resource identity) plus
    the close observation time.  The episode identity and historical
    attribution never change; the ``superseded`` marker is the one designed
    transition applied when a later reopen/reclose episode overtakes it.
    The attributed change request is set only when the episode is
    ``inferred`` — ambiguous/unmatched episodes attribute nothing.
    """

    issue_provider: Provider
    issue_repository: str
    issue_external_id: str
    opened_at: datetime | None = Field(
        default=None,
        description="Interval start — the issue's own open/reopen observation",
    )
    closed_at: datetime | None = Field(
        default=None,
        description=(
            "Interval end — the issue.closed occurrence; NULL while the "
            "episode is still open"
        ),
    )
    status: ClosureEpisodeStatus
    change_request_provider: Provider | None = Field(
        default=None,
        description="Provider of the attributed change request (inferred only)",
    )
    change_request_repository: str | None = None
    change_request_external_id: str | None = None
    resolver_version: str = Field(
        default=CLOSURE_RESOLVER_VERSION,
        description="Version of the closure-episode projector that derived this episode",
    )


class ClosureCandidate(BaseModel):
    """One competing change-request candidate in an ambiguous episode."""

    provider: Provider
    repository: str
    external_id: str


class ClosureUnresolved(BaseModel):
    """A versioned unresolved record for one closed closure episode.

    ``reason`` is ``unmatched`` (zero candidates) or ``ambiguous`` (multiple
    candidates, or an eligible parked declaration).  Candidates are the
    competing change-request endpoint identities — empty for unmatched.
    Never tie-broken, never scored; the projection keeps these provisional
    until a later fact resolves them.
    """

    issue_provider: Provider
    issue_repository: str
    issue_external_id: str
    closed_at: datetime
    reason: str = Field(description="unmatched | ambiguous")
    candidates: list[ClosureCandidate] = Field(default_factory=list)
    resolver_version: str = Field(
        default=CLOSURE_RESOLVER_VERSION,
        description="Version of the closure-episode projector that derived this record",
    )


class ClosureProjection(BaseModel):
    """The full closure-episode projector output for a set of facts.

    ``links`` carries the derived per-kind link states for every change
    request present in the facts; ``episodes`` the immutable closure
    episodes (the last per issue is current, all earlier are superseded);
    ``unresolved`` the versioned unmatched/ambiguous records.
    """

    links: list[ClosureLink] = Field(default_factory=list)
    episodes: list[ClosureEpisode] = Field(default_factory=list)
    unresolved: list[ClosureUnresolved] = Field(default_factory=list)
    resolver_version: str = Field(
        default=CLOSURE_RESOLVER_VERSION,
        description="Version of the closure-episode projector that produced this output",
    )


# ---------------------------------------------------------------------------
# Execution Binding domain model (issue #546)
# ---------------------------------------------------------------------------

# Maximum length for the bounded failure metadata string.  The model contract
# requires optional failure metadata to be bounded and redacted — raw secrets,
# stdout dumps, and arbitrary AWX payloads must never be stored here.
_EXECUTION_BINDING_MAX_FAILURE_REASON_LENGTH = 1000


class ExecutionOutcome(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Terminal result of an :class:`ExecutionBinding`.

    Constrained to the three valid terminal outcomes: ``completed``,
    ``failed``, and ``cancelled``.  Invalid values are rejected by Pydantic
    validation on the :class:`ExecutionBinding.outcome` field.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerType(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """How the execution binding was triggered.

    Carried on the write path (``ExecutionBindingCreateRequest``) and
    stored on the domain model.  The read path surfaces it as a nullable
    string for backward-compatible readback of legacy rows.
    """

    EDA = "eda"
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    BACKFILL = "backfill"
    RECOVERY = "recovery"


class AWXJobIdentity(BaseModel):
    """The externally identified AWX job run that invokes an OpenCode execution.

    ``job_id`` is the AWX-assigned identifier for the job run;
    ``job_template_id`` is the numeric template that launched it.
    """

    job_id: str = Field(
        description="AWX-assigned job run identifier",
        min_length=1,
    )
    job_template_id: int = Field(description="Numeric AWX job template id")


class ProviderResourceIdentity(BaseModel):
    """Normalized provider resource identity for an execution binding.

    GitHub pull requests and GitLab merge requests both normalize to the
    canonical ``change_request`` entity type.  The ``resource_number`` is
    the provider-scoped external id stored as an opaque string (issue/MR
    number).
    """

    provider: Provider
    repository: str = Field(description="Full owner/repo (or group/project) name")
    resource_type: EntityType = Field(
        description=(
            "Entity type — must be ``change_request`` (GitHub PRs and "
            "GitLab MRs both normalize here)"
        ),
    )
    resource_number: str = Field(
        description="Provider-scoped external id (issue/MR number as opaque string)"
    )

    @model_validator(mode="after")
    def _validate_resource_type_is_change_request(self) -> ProviderResourceIdentity:
        """Ensure resource_type is change_request (the only allowed type for
        execution bindings — GitHub PRs and GitLab MRs both normalize here)."""
        if self.resource_type is not EntityType.CHANGE_REQUEST:
            raise ValueError(
                f"resource_type must be 'change_request', got '{self.resource_type.value}'"
            )
        return self


class ExecutionBinding(BaseModel):
    """A durable Gateway record linking one AWX execution to one OpenCode
    external session and one provider resource identity.

    Carries the AWX job identity, the external session id, the normalized
    provider resource identity, the terminal :class:`ExecutionOutcome`,
    and optional traceability metadata (branch, title, timestamps, source
    event id).  The optional ``failure_reason`` is bounded and redacted by
    the model contract — raw secrets, stdout dumps, and arbitrary AWX
    payloads must never be stored here.

    GitHub pull requests and GitLab merge requests both use the canonical
    ``change_request`` entity type in :attr:`resource`.
    """

    binding_id: str = Field(description="ULID primary key of the binding")
    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str | None = Field(
        default=None,
        description=(
            "External OpenCode session id (e.g. ses_* id); None when the "
            "binding has no resolved session (the DB column is nullable)"
        ),
        min_length=1,
    )
    resource: ProviderResourceIdentity = Field(
        description="Normalized provider resource identity"
    )
    outcome: ExecutionOutcome = Field(description="Terminal execution outcome")

    # Optional traceability metadata — bounded and redacted by model contract.
    failure_reason: str | None = Field(
        default=None,
        max_length=_EXECUTION_BINDING_MAX_FAILURE_REASON_LENGTH,
        description=(
            "Bounded failure summary (max 1000 chars).  Raw secrets, stdout "
            "dumps, and arbitrary AWX payloads must never be stored here."
        ),
    )
    title: str | None = Field(default=None, description="Execution title")
    branch: str | None = Field(default=None, description="Branch or ref")
    started_at: datetime | None = Field(
        default=None, description="Execution start timestamp"
    )
    finished_at: datetime | None = Field(
        default=None, description="Execution finish timestamp"
    )
    source_event_id: str | None = Field(
        default=None,
        description="Originating EDA source event id (for traceability)",
    )
