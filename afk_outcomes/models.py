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

from pydantic import BaseModel, ConfigDict, Field

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
