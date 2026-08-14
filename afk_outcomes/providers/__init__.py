"""Concrete provider adapters for AFK Outcome Observability.

Each adapter translates one provider's raw API-shaped objects into the
neutral :mod:`afk_outcomes.models` domain.  They satisfy the
:class:`afk_outcomes.interfaces.ProviderAdapter` protocol and import
nothing from the application package (``app``).

Each module in this subpackage implements :class:`afk_outcomes.interfaces.ProviderAdapter`
for one source provider (GitHub, GitLab, ...), translating provider-native
objects into the neutral :class:`afk_outcomes.models.EngineeringEntity` /
:class:`afk_outcomes.models.EngineeringEvent` domain model.

The adapters are pure domain: they import nothing from the application
package (``app``), a boundary enforced mechanically by a test under
``tests/``.
"""

from __future__ import annotations

from afk_outcomes.providers.github import GitHubAdapter, GitHubApi
from afk_outcomes.providers.github_http import GitHubHttpApi
from afk_outcomes.providers.gitlab import GitLabAdapter

__all__ = ["GitHubAdapter", "GitHubApi", "GitHubHttpApi", "GitLabAdapter"]
