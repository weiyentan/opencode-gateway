"""Provider adapters for AFK Outcome Observability.

Each module in this subpackage implements :class:`afk_outcomes.interfaces.ProviderAdapter`
for one source provider (GitHub, GitLab, ...), translating provider-native
objects into the neutral :class:`afk_outcomes.models.EngineeringEntity` /
:class:`afk_outcomes.models.EngineeringEvent` domain model.

The adapters are pure domain: they import nothing from the application
package (``app``), a boundary enforced mechanically by a test under
``tests/``.
"""

from __future__ import annotations

from afk_outcomes.providers.gitlab import GitLabAdapter

__all__ = ["GitLabAdapter"]
