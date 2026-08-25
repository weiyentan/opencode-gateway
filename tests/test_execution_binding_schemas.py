"""Tests for the execution-binding API schemas (issue #548).

The execution-binding write path (``POST /api/v1/afk/executions``, ADR 0024)
accepts an AWX job identity, an OpenCode external session id, a provider
resource identity, and a terminal outcome.  GitHub pull requests and GitLab
merge requests both normalize to the canonical ``change_request`` identity.
The public schemas expose only approved execution metadata — raw tokens,
stdout, prompts, arbitrary AWX payloads, and unbounded ``extra_vars`` are
structurally absent and rejected as unknown fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.schemas.execution_binding import (
    ExecutionBindingCreateRequest,
    ExecutionBindingHistoryResponse,
    ExecutionBindingReadResponse,
    ExecutionBindingResourceIn,
    ExecutionBindingUpdateRequest,
    redact_failure_summary,
)
from afk_outcomes.models import (
    EntityType,
    ExecutionOutcome,
    Provider,
    TriggerType,
)


def _valid_request(**overrides) -> dict:
    """A minimal valid write request (GitHub PR → change_request)."""
    payload = {
        "awx_job": {"job_id": "awx-job-42", "job_template_id": 7},
        "external_session_id": "ses_abc123",
        "resource": {
            "provider": "github",
            "repository": "owner/repo",
            "resource_type": "pull_request",
            "resource_number": "101",
        },
        "outcome": "completed",
        "trigger_type": "manual",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Provider resource identity normalization
# ---------------------------------------------------------------------------


class TestProviderNormalization:
    def test_github_pull_request_normalizes_to_change_request(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(_valid_request())
        identity = request.resource.to_provider_resource_identity()
        assert identity.provider is Provider.GITHUB
        assert identity.resource_type is EntityType.CHANGE_REQUEST
        assert identity.repository == "owner/repo"
        assert identity.resource_number == "101"

    def test_gitlab_merge_request_normalizes_to_change_request(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                resource={
                    "provider": "gitlab",
                    "repository": "group/project",
                    "resource_type": "merge_request",
                    "resource_number": "25",
                }
            )
        )
        identity = request.resource.to_provider_resource_identity()
        assert identity.provider is Provider.GITLAB
        assert identity.resource_type is EntityType.CHANGE_REQUEST
        assert identity.resource_number == "25"

    def test_canonical_change_request_passes_through(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                resource={
                    "provider": "github",
                    "repository": "owner/repo",
                    "resource_type": "change_request",
                    "resource_number": "7",
                }
            )
        )
        identity = request.resource.to_provider_resource_identity()
        assert identity.resource_type is EntityType.CHANGE_REQUEST


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_minimal_request_validates(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(_valid_request())
        assert request.awx_job.job_id == "awx-job-42"
        assert request.external_session_id == "ses_abc123"
        assert request.outcome is ExecutionOutcome.COMPLETED

    def test_missing_awx_job_rejected(self) -> None:
        payload = _valid_request()
        del payload["awx_job"]
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(payload)

    def test_missing_session_id_now_optional(self) -> None:
        """Issue #590: the session is optional on the two-phase write path."""
        payload = _valid_request()
        del payload["external_session_id"]
        request = ExecutionBindingCreateRequest.model_validate(payload)
        assert request.external_session_id is None

    def test_empty_session_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="external_session_id"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(external_session_id="")
            )

    def test_missing_resource_rejected(self) -> None:
        payload = _valid_request()
        del payload["resource"]
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(payload)

    def test_missing_outcome_rejected(self) -> None:
        payload = _valid_request()
        del payload["outcome"]
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Optional-field handling
# ---------------------------------------------------------------------------


class TestOptionalFields:
    def test_optional_fields_default_to_none(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(_valid_request())
        assert request.source_event_id is None
        assert request.branch is None
        assert request.title is None
        assert request.started_at is None
        assert request.finished_at is None
        assert request.failure_reason is None

    def test_optional_metadata_preserved(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                outcome="failed",
                source_event_id="eda-event-9",
                branch="feature/execution-binding",
                title="Implement execution binding",
                started_at="2026-08-21T01:02:03Z",
                finished_at="2026-08-21T01:05:00Z",
                failure_reason="Process exited with code 1",
            )
        )
        assert request.source_event_id == "eda-event-9"
        assert request.branch == "feature/execution-binding"
        assert request.title == "Implement execution binding"
        assert request.finished_at is not None
        assert request.failure_reason == "Process exited with code 1"

    def test_resource_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionBindingResourceIn.model_validate(
                {
                    "provider": "github",
                    "repository": "owner/repo",
                    "resource_type": "pull_request",
                    "resource_number": "1",
                    "extra_vars": {"password": "secret"},
                }
            )


# ---------------------------------------------------------------------------
# Invalid payload rejection
# ---------------------------------------------------------------------------


class TestInvalidPayloads:
    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="provider"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(
                    resource={
                        "provider": "bitbucket",
                        "repository": "owner/repo",
                        "resource_type": "pull_request",
                        "resource_number": "1",
                    }
                )
            )

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(ValidationError, match="outcome"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(outcome="in_progress")
            )

    def test_outcome_must_be_terminal(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(_valid_request(outcome="in_progress"))

    def test_non_change_request_resource_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="resource_type"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(
                    resource={
                        "provider": "github",
                        "repository": "owner/repo",
                        "resource_type": "issue",
                        "resource_number": "1",
                    }
                )
            )

    def test_failure_reason_bounded_at_max_length(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(outcome="failed", failure_reason="x" * 1000)
        )
        assert len(request.failure_reason) == 1000

    def test_unbounded_failure_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="failure_reason"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(outcome="failed", failure_reason="x" * 1001)
            )

    def test_raw_tokens_stdout_prompts_rejected(self) -> None:
        """Raw tokens/stdout/prompts/extra_vars are not part of the schema."""
        for forbidden in (
            "input_tokens",
            "output_tokens",
            "stdout",
            "prompts",
            "extra_vars",
        ):
            payload = _valid_request()
            payload[forbidden] = {"raw": "sensitive payload"}
            with pytest.raises(ValidationError):
                ExecutionBindingCreateRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


def _valid_read_binding(**overrides) -> dict:
    binding = {
        "binding_id": "01HZX7S0KQ00000000000000",
        "awx_job": {"job_id": "awx-job-1", "job_template_id": 3},
        "external_session_id": "ses_retry123",
        "resource": {
            "provider": "gitlab",
            "repository": "group/project",
            "resource_type": "change_request",
            "resource_number": "25",
        },
        "outcome": "completed",
    }
    binding.update(overrides)
    return binding


class TestResponseSchemas:
    def test_single_binding_read_response(self) -> None:
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding(
                outcome="failed",
                failure_reason="Transient AWX failure",
                source_event_id="eda-event-2",
            )
        )
        assert response.binding_id == "01HZX7S0KQ00000000000000"
        assert response.outcome is ExecutionOutcome.FAILED
        assert response.failure_reason == "Transient AWX failure"
        assert response.resource.resource_type is EntityType.CHANGE_REQUEST

    def test_read_response_nullable_session_id(self) -> None:
        """A binding without a resolved session reads back with None."""
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding(external_session_id=None)
        )
        assert response.external_session_id is None

    def test_resource_history_preserves_failed_then_successful(self) -> None:
        history = ExecutionBindingHistoryResponse.model_validate(
            {
                "resource": {
                    "provider": "github",
                    "repository": "owner/repo",
                    "resource_type": "change_request",
                    "resource_number": "101",
                },
                "bindings": [
                    _valid_read_binding(outcome="failed"),
                    _valid_read_binding(binding_id="01HZX7S0KQ00000000000001"),
                ],
            }
        )
        assert len(history.bindings) == 2
        assert history.bindings[0].outcome is ExecutionOutcome.FAILED
        assert history.bindings[1].outcome is ExecutionOutcome.COMPLETED
        assert history.resource.resource_number == "101"

    def test_response_schemas_expose_no_forbidden_fields(self) -> None:
        """Approved metadata only — no raw tokens/stdout/prompts/extra_vars."""
        for field_name in ("tokens", "stdout", "prompts", "extra_vars", "payload"):
            assert field_name not in ExecutionBindingReadResponse.model_fields
            assert field_name not in ExecutionBindingHistoryResponse.model_fields
            assert field_name not in ExecutionBindingCreateRequest.model_fields


# ---------------------------------------------------------------------------
# TriggerType enum validation (issue #583)
# ---------------------------------------------------------------------------


class TestTriggerTypeValidation:
    def test_valid_trigger_type_accepted(self) -> None:
        for tt in ("eda", "manual", "scheduled", "backfill", "recovery"):
            overrides: dict[str, object] = {"trigger_type": tt}
            if tt == "eda":
                overrides["source_event_id"] = "evt-001"
            request = ExecutionBindingCreateRequest.model_validate(
                _valid_request(**overrides)
            )
            assert request.trigger_type is TriggerType(tt)

    def test_trigger_type_is_required(self) -> None:
        payload = _valid_request()
        del payload["trigger_type"]
        with pytest.raises(ValidationError, match="trigger_type"):
            ExecutionBindingCreateRequest.model_validate(payload)

    def test_unknown_trigger_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="trigger_type"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(trigger_type="unknown_type")
            )

    def test_empty_trigger_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="trigger_type"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(trigger_type="")
            )


# ---------------------------------------------------------------------------
# source_event_id conditional validation (issue #583)
# ---------------------------------------------------------------------------


class TestSourceEventIdConditionalValidation:
    def test_eda_requires_source_event_id(self) -> None:
        with pytest.raises(ValidationError, match="source_event_id"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(trigger_type="eda", source_event_id=None)
            )

    def test_eda_with_source_event_id_accepted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="eda", source_event_id="evt-123")
        )
        assert request.trigger_type is TriggerType.EDA
        assert request.source_event_id == "evt-123"

    def test_manual_allows_source_event_id_none(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="manual", source_event_id=None)
        )
        assert request.source_event_id is None

    def test_scheduled_allows_source_event_id_none(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="scheduled", source_event_id=None)
        )
        assert request.source_event_id is None

    def test_backfill_allows_source_event_id_none(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="backfill", source_event_id=None)
        )
        assert request.source_event_id is None

    def test_recovery_allows_source_event_id_none(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="recovery", source_event_id=None)
        )
        assert request.source_event_id is None

    def test_non_eda_with_source_event_id_accepted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(trigger_type="manual", source_event_id="evt-456")
        )
        assert request.source_event_id == "evt-456"


# ---------------------------------------------------------------------------
# Read response nullable AFK run fields (issue #583)
# ---------------------------------------------------------------------------


class TestReadResponseAfkRunFields:
    def test_read_response_has_afk_run_id_nullable(self) -> None:
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding()
        )
        assert response.afk_run_id is None

    def test_read_response_has_trigger_type_nullable(self) -> None:
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding()
        )
        assert response.trigger_type is None

    def test_read_response_accepts_populated_afk_run_id(self) -> None:
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding(afk_run_id="01J_ARUN_ID_12345")
        )
        assert response.afk_run_id == "01J_ARUN_ID_12345"

    def test_read_response_accepts_populated_trigger_type(self) -> None:
        response = ExecutionBindingReadResponse.model_validate(
            _valid_read_binding(trigger_type="eda")
        )
        assert response.trigger_type == "eda"

    def test_history_response_items_have_nullable_afk_fields(self) -> None:
        history = ExecutionBindingHistoryResponse.model_validate(
            {
                "resource": {
                    "provider": "github",
                    "repository": "owner/repo",
                    "resource_type": "change_request",
                    "resource_number": "101",
                },
                "bindings": [
                    _valid_read_binding(),
                    _valid_read_binding(
                        binding_id="01HZX7S0KQ00000000000001",
                        afk_run_id="01J_ARUN_ID_99999",
                        trigger_type="scheduled",
                    ),
                ],
            }
        )
        assert history.bindings[0].afk_run_id is None
        assert history.bindings[0].trigger_type is None
        assert history.bindings[1].afk_run_id == "01J_ARUN_ID_99999"
        assert history.bindings[1].trigger_type == "scheduled"


# ---------------------------------------------------------------------------
# extra="forbid" preservation (issue #583)
# ---------------------------------------------------------------------------


class TestExtraForbidPreserved:
    def test_create_request_rejects_unknown_top_level_fields(self) -> None:
        payload = _valid_request()
        payload["unknown_field"] = "should_fail"
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(payload)

    def test_create_request_rejects_unknown_nested_resource_fields(self) -> None:
        payload = _valid_request()
        payload["resource"]["bogus"] = "nope"
        with pytest.raises(ValidationError):
            ExecutionBindingCreateRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Two-phase lifecycle create validation (issue #590)
# ---------------------------------------------------------------------------


class TestTwoPhaseCreateValidation:
    def test_running_with_afk_run_id_validates(self) -> None:
        """Start-time provisioning attaches to a pre-provisioned lifecycle."""
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                outcome="running",
                afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
                external_session_id=None,
                resource=None,
                started_at="2026-08-21T01:02:03Z",
            )
        )
        assert request.outcome is ExecutionOutcome.RUNNING
        assert request.afk_run_id == "01JZABCDEFGHJKLMNPQRSTVWXY"
        assert request.external_session_id is None
        assert request.resource is None

    def test_running_requires_afk_run_id(self) -> None:
        with pytest.raises(ValidationError, match="afk_run_id"):
            ExecutionBindingCreateRequest.model_validate(_valid_request(outcome="running"))

    def test_terminal_without_resource_requires_afk_run_id(self) -> None:
        with pytest.raises(ValidationError, match="resource or afk_run_id"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(outcome="failed", resource=None, external_session_id=None)
            )

    def test_failed_without_resource_or_session_with_run_validates(self) -> None:
        """Failed executions persist without a change request or session."""
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                outcome="failed",
                afk_run_id="01JZABCDEFGHJKLMNPQRSTVWXY",
                resource=None,
                external_session_id=None,
                failure_reason="AWX job crashed before launch",
            )
        )
        assert request.resource is None
        assert request.external_session_id is None
        assert request.failure_reason == "AWX job crashed before launch"

    def test_failed_without_session_validates(self) -> None:
        """A terminal callback may omit the session (still unresolved)."""
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(outcome="failed", external_session_id=None)
        )
        assert request.external_session_id is None
        assert request.resource is not None

    def test_completed_with_failure_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="failure_reason"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(outcome="completed", failure_reason="boom")
            )

    def test_failed_with_failure_reason_accepted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(outcome="failed", failure_reason="boom")
        )
        assert request.failure_reason == "boom"


# ---------------------------------------------------------------------------
# Failure-summary redaction (issue #590)
# ---------------------------------------------------------------------------


class TestFailureReasonRedaction:
    def test_bearer_token_redacted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                outcome="failed",
                failure_reason="auth failed: Bearer abc123secret for job",
            )
        )
        assert request.failure_reason == "auth failed: Bearer *** for job"

    def test_provider_token_prefixes_redacted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(outcome="failed", failure_reason="token ghp_abc123 leaked")
        )
        assert "ghp_abc123" not in request.failure_reason
        assert "***" in request.failure_reason

    def test_secret_assignment_redacted(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(
                outcome="failed",
                failure_reason="env GITHUB_TOKEN=ghp_secret and password: hunter2",
            )
        )
        assert "ghp_secret" not in request.failure_reason
        assert "hunter2" not in request.failure_reason

    def test_ordinary_text_untouched(self) -> None:
        request = ExecutionBindingCreateRequest.model_validate(
            _valid_request(outcome="failed", failure_reason="Process exited with code 1")
        )
        assert request.failure_reason == "Process exited with code 1"

    def test_redact_failure_summary_helper_none_handling(self) -> None:
        assert redact_failure_summary("plain text") == "plain text"

    def test_bounded_length_still_enforced_after_redaction(self) -> None:
        with pytest.raises(ValidationError, match="failure_reason"):
            ExecutionBindingCreateRequest.model_validate(
                _valid_request(outcome="failed", failure_reason="x" * 1001)
            )


# ---------------------------------------------------------------------------
# Terminal-update schema (issue #590)
# ---------------------------------------------------------------------------


def _valid_update(**overrides) -> dict:
    payload = {"outcome": "completed", "finished_at": "2026-08-21T01:05:00Z"}
    payload.update(overrides)
    return payload


class TestTerminalUpdateSchema:
    def test_minimal_update_validates(self) -> None:
        request = ExecutionBindingUpdateRequest.model_validate(_valid_update())
        assert request.outcome is ExecutionOutcome.COMPLETED
        assert request.failure_reason is None
        assert request.resource is None
        assert request.external_session_id is None

    def test_running_outcome_rejected(self) -> None:
        with pytest.raises(ValidationError, match="terminal"):
            ExecutionBindingUpdateRequest.model_validate(_valid_update(outcome="running"))

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(ValidationError, match="outcome"):
            ExecutionBindingUpdateRequest.model_validate(
                _valid_update(outcome="in_progress")
            )

    def test_failed_without_resource_or_session_validates(self) -> None:
        """Failed/cancelled terminal updates need no change request or session."""
        request = ExecutionBindingUpdateRequest.model_validate(
            _valid_update(outcome="failed", failure_reason="Timeout after 300s")
        )
        assert request.outcome is ExecutionOutcome.FAILED
        assert request.failure_reason == "Timeout after 300s"

    def test_completed_with_failure_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="failure_reason"):
            ExecutionBindingUpdateRequest.model_validate(
                _valid_update(outcome="completed", failure_reason="boom")
            )

    def test_resource_fill_in_accepted(self) -> None:
        request = ExecutionBindingUpdateRequest.model_validate(
            _valid_update(
                outcome="completed",
                resource={
                    "provider": "github",
                    "repository": "owner/repo",
                    "resource_type": "pull_request",
                    "resource_number": "101",
                },
                external_session_id="ses_abc123",
            )
        )
        assert request.resource is not None
        assert request.resource.to_provider_resource_identity().resource_number == "101"

    def test_failure_reason_redacted_on_update(self) -> None:
        request = ExecutionBindingUpdateRequest.model_validate(
            _valid_update(outcome="failed", failure_reason="Bearer glpat-xyz leaked")
        )
        assert "glpat-xyz" not in request.failure_reason

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionBindingUpdateRequest.model_validate(
                _valid_update(extra_vars={"secret": "x"})
            )
