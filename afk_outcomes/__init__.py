"""AFK Outcome Observability — pure domain package.

Provider-independent, versionable domain models and interfaces for
representing the engineering outcome of an AFK (away-from-keyboard) run,
plus stable canonical sorted-key JSON serialization.

This package deliberately imports nothing from the application package
(``app``) — it is pure domain, a boundary enforced mechanically by a test
under ``tests/``.
"""

from __future__ import annotations

from afk_outcomes.associations import derive_exact_associations
from afk_outcomes.correlation import (
    BranchIssueReferenceRule,
    CommitIssueReferenceRule,
    CorrelationEngine,
    ExplicitRunIdRule,
    IssueReferenceRule,
    SessionDescriptor,
    TemporalInferenceRule,
)
from afk_outcomes.interfaces import CorrelationRule, OutcomeRepository, ProviderAdapter
from afk_outcomes.models import (
    ASSOCIATION_RESOLVER_VERSION,
    RESOLVER_VERSION,
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    Provider,
    ReferenceSource,
    ResolutionResult,
    ResourceSessionAssociation,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
    SessionResourceReference,
    UnresolvedCorrelation,
    UnresolvedReason,
)
from afk_outcomes.repository import (
    AsyncpgOutcomeRepository,
)
from afk_outcomes.serialization import (
    CANONICAL_SCHEMA_VERSION,
    MonotonicULID,
    SequenceULID,
    ULIDSource,
    dumps_canonical,
    loads_canonical,
    make_ulid,
)

__all__ = [
    "AFKRun",
    "ASSOCIATION_RESOLVER_VERSION",
    "AsyncpgOutcomeRepository",
    "BranchIssueReferenceRule",
    "CANONICAL_SCHEMA_VERSION",
    "CommitIssueReferenceRule",
    "Correlation",
    "CorrelationEngine",
    "CorrelationEvidence",
    "CorrelationRule",
    "EngineeringEntity",
    "EngineeringEvent",
    "EngineeringOutcome",
    "EngineeringOutcomeStatus",
    "EntityType",
    "ExplicitRunIdRule",
    "IssueReferenceRule",
    "MonotonicULID",
    "OutcomeRepository",
    "Provider",
    "ProviderAdapter",
    "RESOLVER_VERSION",
    "ReferenceSource",
    "ResolutionResult",
    "ResourceSessionAssociation",
    "RunEntityLink",
    "RunSessionLink",
    "RunStatus",
    "SequenceULID",
    "SessionDescriptor",
    "SessionResourceReference",
    "TemporalInferenceRule",
    "ULIDSource",
    "UnresolvedCorrelation",
    "UnresolvedReason",
    "derive_exact_associations",
    "dumps_canonical",
    "loads_canonical",
    "make_ulid",
]
