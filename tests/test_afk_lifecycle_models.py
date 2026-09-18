"""Domain model tests for the provisional AFK run lifecycle (issue #589).

Covers the :class:`~afk_outcomes.models.AFKRunLifecycle` read model:

* the change-request all-or-none validator (a lifecycle owns at most one
  change request, and a half-written tuple is never a valid bound state),
* ``change_request_identity()`` — the bound change request surfaces as the
  canonical ``change_request`` :class:`ProviderResourceIdentity`,
* lenient legacy-row readback (lifecycle columns predate migration 0039
  rows and read back as ``None``).

Issue #649 retired the lifecycle ``status`` field (ADR 0028 makes the bound
change request the lifecycle authority), so the former provisional-status
tests are removed with it.
"""

from __future__ import annotations

import pytest

from afk_outcomes.models import (
    AFKRunLifecycle,
    EntityType,
    Provider,
)


def _lifecycle(**overrides) -> dict:
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
    return kwargs


# ── Change-request all-or-none validator ────────────────────────────────────


def test_unbound_lifecycle_is_valid() -> None:
    """A lifecycle with no change-request fields validates."""
    lifecycle = AFKRunLifecycle(**_lifecycle())
    assert lifecycle.change_request_provider is None
    assert lifecycle.change_request_repository is None
    assert lifecycle.change_request_external_id is None


def test_partial_change_request_tuple_is_rejected() -> None:
    """A half-written change-request tuple is rejected — all or none."""
    with pytest.raises(ValueError, match="all set or all None"):
        AFKRunLifecycle(
            **_lifecycle(
                change_request_provider=Provider.GITLAB,
                change_request_repository="gitlab.com/cnp/cnp",
            )
        )
    with pytest.raises(ValueError, match="all set or all None"):
        AFKRunLifecycle(
            **_lifecycle(
                change_request_repository="gitlab.com/cnp/cnp",
                change_request_external_id="6",
            )
        )


def test_full_change_request_tuple_is_valid() -> None:
    """A fully-bound change-request tuple validates."""
    lifecycle = AFKRunLifecycle(
        **_lifecycle(
            change_request_provider=Provider.GITLAB,
            change_request_repository="gitlab.com/cnp/cnp",
            change_request_external_id="6",
        )
    )
    assert lifecycle.change_request_identity() is not None


# ── change_request_identity() ───────────────────────────────────────────────


def test_change_request_identity_unbound_returns_none() -> None:
    """change_request_identity() is None while the lifecycle is unbound."""
    lifecycle = AFKRunLifecycle(**_lifecycle())
    assert lifecycle.change_request_identity() is None


def test_change_request_identity_normalizes_to_canonical_entity_type() -> None:
    """The bound change request surfaces under the canonical change_request type."""
    lifecycle = AFKRunLifecycle(
        **_lifecycle(
            change_request_provider=Provider.GITLAB,
            change_request_repository="gitlab.com/cnp/cnp",
            change_request_external_id="6",
        )
    )
    identity = lifecycle.change_request_identity()
    assert identity is not None
    assert identity.provider == Provider.GITLAB
    assert identity.repository == "gitlab.com/cnp/cnp"
    assert identity.resource_type == EntityType.CHANGE_REQUEST
    assert identity.resource_number == "6"


# ── Lenient legacy-row readback ─────────────────────────────────────────────


def test_legacy_row_readback_allows_null_lifecycle_fields() -> None:
    """Legacy afk_runs rows (migration 0026) read back with None lifecycle fields."""
    lifecycle = AFKRunLifecycle(
        afk_run_id="01HLEGACY0000000000000001",
        provider=Provider.GITHUB,
        status="completed",
    )
    assert lifecycle.host is None
    assert lifecycle.source_event_id is None
    assert lifecycle.repository is None
    assert lifecycle.trigger_type is None
    assert lifecycle.change_request_identity() is None
