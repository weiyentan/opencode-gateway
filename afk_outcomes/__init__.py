"""AFK Outcome Observability — pure domain package.

Provider-independent, versionable domain models and interfaces for
representing the engineering outcome of an AFK (away-from-keyboard) run,
plus stable canonical sorted-key JSON serialization.

This package deliberately imports nothing from the application package
(``app``) — it is pure domain, a boundary enforced mechanically by a test
under ``tests/``.
"""

from __future__ import annotations

from afk_outcomes.interfaces import CorrelationRule, OutcomeRepository, ProviderAdapter
from afk_outcomes.models import (
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    Provider,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
)
from afk_outcomes.repository import (
    RESOLVER_VERSION,
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
    "AsyncpgOutcomeRepository",
    "CANONICAL_SCHEMA_VERSION",
    "Correlation",
    "CorrelationEvidence",
    "CorrelationRule",
    "EngineeringEntity",
    "EngineeringEvent",
    "EngineeringOutcome",
    "EngineeringOutcomeStatus",
    "EntityType",
    "MonotonicULID",
    "OutcomeRepository",
    "Provider",
    "ProviderAdapter",
    "RESOLVER_VERSION",
    "RunEntityLink",
    "RunSessionLink",
    "RunStatus",
    "SequenceULID",
    "ULIDSource",
    "dumps_canonical",
    "loads_canonical",
    "make_ulid",
]
