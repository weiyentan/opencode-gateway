"""Protocol interfaces for AFK Outcome Observability.

The provider-abstraction, correlation, and persistence seams of the
domain.  Each interface is a :class:`typing.Protocol` — a structural
contract that any implementation can satisfy without inheriting from this
package.  No application modules are imported here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from afk_outcomes.models import (
    AFKRun,
    Correlation,
    EngineeringEntity,
    EngineeringEvent,
    Provider,
)


class ProviderAdapter(Protocol):
    """Translate one provider's raw objects into the neutral domain model.

    Concrete adapters (e.g. a GitHub adapter or a GitLab adapter) satisfy
    this protocol by returning provider-independent
    :class:`EngineeringEntity` and :class:`EngineeringEvent` objects.
    """

    provider: Provider

    async def fetch_entities(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEntity]: ...

    async def fetch_events(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEvent]: ...


class CorrelationRule(Protocol):
    """Derive :class:`Correlation` links tying a run to the entities it touched."""

    async def correlate(
        self,
        run: AFKRun,
        *,
        entities: list[EngineeringEntity],
        events: list[EngineeringEvent],
    ) -> list[Correlation]: ...


class OutcomeRepository(Protocol):
    """Persist and retrieve AFK runs keyed by ``afk_run_id``."""

    async def save(self, run: AFKRun) -> None: ...

    async def get(self, afk_run_id: str) -> AFKRun | None: ...
