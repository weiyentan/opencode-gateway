"""Concrete provider adapters for AFK Outcome Observability.

Each adapter translates one provider's raw API-shaped objects into the
neutral :mod:`afk_outcomes.models` domain.  They satisfy the
:class:`afk_outcomes.interfaces.ProviderAdapter` protocol and import
nothing from the application package (``app``).
"""

from __future__ import annotations

from afk_outcomes.providers.github import GitHubAdapter, GitHubApi

__all__ = ["GitHubAdapter", "GitHubApi"]
