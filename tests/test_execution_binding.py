"""Tests for the Execution Binding domain model (issue #546).

Covers valid records and invalid identity/status combinations for the
pure-domain :class:`~afk_outcomes.models.ExecutionBinding` and its
associated value objects.
"""

from __future__ import annotations

import pytest

from afk_outcomes.models import (
    AWXJobIdentity,
    EntityType,
    ExecutionBinding,
    ExecutionOutcome,
    Provider,
    ProviderResourceIdentity,
)


# ---------------------------------------------------------------------------
# ExecutionOutcome enum
# ---------------------------------------------------------------------------


class TestExecutionOutcome:
    def test_has_expected_terminal_values(self) -> None:
        assert ExecutionOutcome.COMPLETED == "completed"
        assert ExecutionOutcome.FAILED == "failed"
        assert ExecutionOutcome.CANCELLED == "cancelled"

    def test_only_three_members(self) -> None:
        assert len(ExecutionOutcome) == 3


# ---------------------------------------------------------------------------
# ProviderResourceIdentity
# ---------------------------------------------------------------------------


class TestProviderResourceIdentity:
    def test_valid_github_pr(self) -> None:
        """GitHub pull requests normalize to change_request entity type."""
        identity = ProviderResourceIdentity(
            provider=Provider.GITHUB,
            repository="owner/repo",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="123",
        )
        assert identity.provider == Provider.GITHUB
        assert identity.resource_type == EntityType.CHANGE_REQUEST

    def test_valid_gitlab_mr(self) -> None:
        """GitLab merge requests normalize to change_request entity type."""
        identity = ProviderResourceIdentity(
            provider=Provider.GITLAB,
            repository="group/project",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="456",
        )
        assert identity.provider == Provider.GITLAB
        assert identity.resource_number == "456"

    def test_rejects_non_change_request_entity_type(self) -> None:
        """Only change_request entity type is allowed for execution bindings."""
        with pytest.raises(ValueError, match="resource_type"):
            ProviderResourceIdentity(
                provider=Provider.GITHUB,
                repository="owner/repo",
                resource_type=EntityType.ISSUE,
                resource_number="789",
            )


# ---------------------------------------------------------------------------
# AWXJobIdentity
# ---------------------------------------------------------------------------


class TestAWXJobIdentity:
    def test_valid_identity(self) -> None:
        identity = AWXJobIdentity(job_id="awx-job-123", job_template_id=42)
        assert identity.job_id == "awx-job-123"
        assert identity.job_template_id == 42

    def test_empty_job_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="job_id"):
            AWXJobIdentity(job_id="", job_template_id=42)


# ---------------------------------------------------------------------------
# ExecutionBinding (integration)
# ---------------------------------------------------------------------------


class TestExecutionBinding:
    def _make_binding(self, **overrides) -> ExecutionBinding:
        defaults = dict(
            binding_id="01ABCDEF0000000000000000",
            awx_job=AWXJobIdentity(job_id="awx-job-42", job_template_id=1),
            external_session_id="ses_abc123",
            resource=ProviderResourceIdentity(
                provider=Provider.GITHUB,
                repository="owner/repo",
                resource_type=EntityType.CHANGE_REQUEST,
                resource_number="100",
            ),
            outcome=ExecutionOutcome.COMPLETED,
        )
        defaults.update(overrides)
        return ExecutionBinding(**defaults)

    def test_valid_binding(self) -> None:
        binding = self._make_binding()
        assert binding.binding_id == "01ABCDEF0000000000000000"
        assert binding.outcome == ExecutionOutcome.COMPLETED

    def test_failed_binding_with_failure_reason(self) -> None:
        binding = self._make_binding(
            outcome=ExecutionOutcome.FAILED,
            failure_reason="Process exited with code 1",
        )
        assert binding.outcome == ExecutionOutcome.FAILED
        assert binding.failure_reason == "Process exited with code 1"

    def test_cancelled_binding(self) -> None:
        binding = self._make_binding(outcome=ExecutionOutcome.CANCELLED)
        assert binding.outcome == ExecutionOutcome.CANCELLED

    def test_optional_metadata_defaults(self) -> None:
        binding = self._make_binding()
        assert binding.failure_reason is None
        assert binding.title is None
        assert binding.branch is None
        assert binding.started_at is None
        assert binding.finished_at is None
        assert binding.source_event_id is None

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._make_binding(outcome="invalid_status")

    def test_empty_session_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="external_session_id"):
            self._make_binding(external_session_id="")

    def test_none_session_id_accepted(self) -> None:
        """An unresolved binding may carry no external session id (None)."""
        binding = self._make_binding(external_session_id=None)
        assert binding.external_session_id is None

    def test_failure_reason_bounded(self) -> None:
        """Failure metadata is bounded by the model contract."""
        binding = self._make_binding(
            outcome=ExecutionOutcome.FAILED,
            failure_reason="x" * 1000,
        )
        assert len(binding.failure_reason) == 1000

    def test_failure_reason_exceeds_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_reason"):
            self._make_binding(
                outcome=ExecutionOutcome.FAILED,
                failure_reason="x" * 1001,
            )
