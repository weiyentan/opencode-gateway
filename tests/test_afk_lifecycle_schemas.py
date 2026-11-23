"""Schema tests for the provisional AFK run lifecycle response (issue #589/#856).

Covers :meth:`AFKRunLifecycleResponse.from_domain` serialization of the
immutable logical execution target: the explicit argument stays authoritative
(backwards compatible with every existing caller), and the domain
``AFKRunLifecycle.logical_target`` field is the fallback when the argument is
omitted — so a lifecycle readback carries the target without a separate
round-trip.
"""

from __future__ import annotations

from afk_outcomes.models import AFKRunLifecycle, Provider
from app.core.schemas.afk_lifecycle import AFKRunLifecycleResponse


def _lifecycle(**overrides) -> AFKRunLifecycle:
    """Minimal fully-populated lifecycle kwargs."""
    kwargs = {
        "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWX",
        "provider": Provider.GITHUB,
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "github.com/acme/proj",
        "trigger_type": "eda",
    }
    kwargs.update(overrides)
    return AFKRunLifecycle(**kwargs)


def test_from_domain_explicit_argument_is_authoritative() -> None:
    """An explicitly supplied logical_target wins (existing callers unchanged)."""
    response = AFKRunLifecycleResponse.from_domain(
        _lifecycle(logical_target="runner-vm-domain"),
        logical_target="runner-vm-explicit",
    )
    assert response.logical_target == "runner-vm-explicit"


def test_from_domain_falls_back_to_domain_field() -> None:
    """Omitting the argument surfaces the domain lifecycle's stored target."""
    response = AFKRunLifecycleResponse.from_domain(
        _lifecycle(logical_target="runner-vm-01"),
    )
    assert response.logical_target == "runner-vm-01"


def test_from_domain_renders_none_for_targetless_runs() -> None:
    """Runs provisioned without a target (and legacy rows) render null."""
    response = AFKRunLifecycleResponse.from_domain(_lifecycle())
    assert response.logical_target is None


# ── Original trigger actor (issue #859) ─────────────────────────────────────


def test_from_domain_explicit_actor_argument_is_authoritative() -> None:
    """An explicitly supplied actor wins (existing callers unchanged)."""
    response = AFKRunLifecycleResponse.from_domain(
        _lifecycle(actor="github:domain-actor"),
        actor="gitlab:explicit-actor",
    )
    assert response.actor == "gitlab:explicit-actor"


def test_from_domain_falls_back_to_domain_actor() -> None:
    """Omitting the argument surfaces the domain lifecycle's stored actor."""
    response = AFKRunLifecycleResponse.from_domain(
        _lifecycle(actor="github:octocat"),
    )
    assert response.actor == "github:octocat"


def test_from_domain_renders_none_for_actorless_runs() -> None:
    """Runs provisioned without an actor (and legacy rows) render null."""
    response = AFKRunLifecycleResponse.from_domain(_lifecycle())
    assert response.actor is None
