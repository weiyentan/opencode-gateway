"""Tests for the Job Pydantic model."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError


class TestJobModelConstruction:
    """Tests that a Job can be constructed with required and optional fields."""

    def test_job_can_be_constructed_with_all_required_fields(self):
        """Job should accept all required fields and provide defaults for optionals."""
        from app.core.models.job import Job

        job_id = uuid4()
        now = datetime.now(timezone.utc)

        job = Job(
            id=job_id,
            status="pending",
            repo_url="https://github.com/example/repo.git",
            task_summary="Fix the login bug",
            executor_type="awx",
            created_at=now,
            updated_at=now,
        )

        assert job.id == job_id
        assert job.status == "pending"
        assert job.repo_url == "https://github.com/example/repo.git"
        assert job.task_summary == "Fix the login bug"
        assert job.executor_type == "awx"
        assert job.runner_id is None
        assert job.workspace_name is None
        assert job.opencode_url is None
        assert job.opencode_session_id is None
        assert job.executor_job_id is None
        assert job.completed_at is None
        assert job.diff is None
        assert job.created_at == now
        assert job.updated_at == now

    def test_job_accepts_all_optional_fields(self):
        """Job should accept all optional fields when provided."""
        from app.core.models.job import Job

        job_id = uuid4()
        runner_id = uuid4()
        now = datetime.now(timezone.utc)

        job = Job(
            id=job_id,
            status="running",
            repo_url="https://github.com/example/repo.git",
            task_summary="Add unit tests",
            runner_id=runner_id,
            workspace_name="ws-abc123",
            opencode_url="http://opencode:8080",
            opencode_session_id="sess-xyz789",
            executor_type="awx",
            executor_job_id="awx-job-42",
            created_at=now,
            updated_at=now,
            completed_at=now,
            diff="+ added unit tests for core module",
        )

        assert job.runner_id == runner_id
        assert job.workspace_name == "ws-abc123"
        assert job.opencode_url == "http://opencode:8080"
        assert job.opencode_session_id == "sess-xyz789"
        assert job.executor_job_id == "awx-job-42"
        assert job.completed_at == now
        assert job.diff == "+ added unit tests for core module"


class TestJobModelValidation:
    """Tests that Job model rejects invalid data."""

    def test_job_requires_id(self):
        """Job should require the id field."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Job(
                status="pending",
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("id",) for e in errors)

    def test_job_requires_status(self):
        """Job should require the status field."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Job(
                id=uuid4(),
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("status",) for e in errors)

    def test_job_requires_executor_type(self):
        """Job should require the executor_type field."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Job(
                id=uuid4(),
                status="pending",
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("executor_type",) for e in errors)

    def test_job_id_must_be_valid_uuid(self):
        """Job id must be a valid UUID."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Job(
                id="not-a-uuid",
                status="pending",
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )

    def test_job_created_at_must_be_datetime(self):
        """Job created_at must be a datetime."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Job(
                id=uuid4(),
                status="pending",
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at="not-a-datetime",
                updated_at=now,
            )


class TestJobModelFields:
    """Tests that Job model has all the expected field names matching the DB schema."""

    def test_job_has_all_expected_field_names(self):
        """Job model fields should match the gateway_jobs table columns."""
        from app.core.models.job import Job

        expected_fields = {
            "id",
            "status",
            "repo_url",
            "task_summary",
            "runner_id",
            "workspace_name",
            "opencode_url",
            "opencode_session_id",
            "executor_type",
            "executor_job_id",
            "created_at",
            "updated_at",
            "completed_at",
            "diff",
        }

        actual_fields = set(Job.model_fields.keys())
        assert actual_fields == expected_fields


class TestJobStatusEnum:
    """Tests for the JobStatus enum used on the Job model."""

    def test_job_status_enum_has_expected_values(self):
        """JobStatus enum should define all 8 status values including aborting and aborted."""
        from app.core.models.job import JobStatus

        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.NEEDS_APPROVAL.value == "needs_approval"
        assert JobStatus.REJECTED.value == "rejected"
        assert JobStatus.ABORTING.value == "aborting"
        assert JobStatus.ABORTED.value == "aborted"

    def test_original_six_values_are_unchanged(self):
        """The original 6 enum values must be unchanged."""
        from app.core.models.job import JobStatus

        original_values = {
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.NEEDS_APPROVAL,
            JobStatus.REJECTED,
        }
        assert len(original_values) == 6
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.NEEDS_APPROVAL.value == "needs_approval"
        assert JobStatus.REJECTED.value == "rejected"

    def test_job_rejects_invalid_status(self):
        """Job should reject a status not in the JobStatus enum."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Job(
                id=uuid4(),
                status="done",
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )

    def test_job_accepts_valid_statuses(self):
        """Job should accept all valid JobStatus values."""
        from app.core.models.job import Job, JobStatus

        now = datetime.now(timezone.utc)
        for status in JobStatus:
            job = Job(
                id=uuid4(),
                status=status,
                repo_url="https://github.com/example/repo.git",
                task_summary="Fix bug",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )
            assert job.status == status

    def test_job_accepts_aborting_and_aborted_strings(self):
        """Job should accept 'aborting' and 'aborted' as valid status strings."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        job = Job(
            id=uuid4(),
            status="aborting",
            repo_url="https://github.com/example/repo.git",
            task_summary="Test aborting",
            executor_type="awx",
            created_at=now,
            updated_at=now,
        )
        assert job.status.value == "aborting"

        job2 = Job(
            id=uuid4(),
            status="aborted",
            repo_url="https://github.com/example/repo.git",
            task_summary="Test aborted",
            executor_type="awx",
            created_at=now,
            updated_at=now,
        )
        assert job2.status.value == "aborted"

    def test_job_status_is_string_enum(self):
        """JobStatus should be a string enum."""
        from enum import Enum

        from app.core.models.job import JobStatus

        assert issubclass(JobStatus, str)
        assert issubclass(JobStatus, Enum)

    def test_job_status_enum_has_eight_members(self):
        """JobStatus should have exactly 8 members."""
        from app.core.models.job import JobStatus

        assert len(JobStatus) == 8


class TestJobStatusTransition:
    """Tests for the JobStatus.validate_transition method."""

    def test_pending_to_aborting_is_valid(self):
        """pending → aborting is allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.ABORTING) is True

    def test_running_to_aborting_is_valid(self):
        """running → aborting is allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.RUNNING, JobStatus.ABORTING) is True

    def test_aborting_to_aborted_is_valid(self):
        """aborting → aborted is allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.ABORTING, JobStatus.ABORTED) is True

    def test_pending_to_aborted_is_invalid(self):
        """pending → aborted is not allowed (must go through aborting)."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.ABORTED) is False

    def test_running_to_aborted_is_invalid(self):
        """running → aborted is not allowed (must go through aborting)."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.RUNNING, JobStatus.ABORTED) is False

    def test_completed_to_aborting_is_invalid(self):
        """completed → aborting is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.COMPLETED, JobStatus.ABORTING) is False

    def test_failed_to_aborting_is_invalid(self):
        """failed → aborting is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.FAILED, JobStatus.ABORTING) is False

    def test_aborted_to_running_is_invalid(self):
        """aborted → running is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.ABORTED, JobStatus.RUNNING) is False

    def test_aborting_to_pending_is_invalid(self):
        """aborting → pending is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.ABORTING, JobStatus.PENDING) is False

    def test_aborted_to_completed_is_invalid(self):
        """aborted → completed is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.ABORTED, JobStatus.COMPLETED) is False

    def test_needs_approval_to_aborting_is_invalid(self):
        """needs_approval → aborting is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.NEEDS_APPROVAL, JobStatus.ABORTING) is False

    def test_rejected_to_aborting_is_invalid(self):
        """rejected → aborting is not allowed."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.REJECTED, JobStatus.ABORTING) is False

    def test_same_status_self_transition_is_invalid(self):
        """Same-status transitions should be rejected."""
        from app.core.models.job import JobStatus

        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.PENDING) is False
        assert JobStatus.validate_transition(JobStatus.RUNNING, JobStatus.RUNNING) is False
        assert JobStatus.validate_transition(JobStatus.ABORTING, JobStatus.ABORTING) is False
        assert JobStatus.validate_transition(JobStatus.ABORTED, JobStatus.ABORTED) is False

    def test_invalid_status_string_rejected_by_pydantic(self):
        """Invalid status strings are still rejected by Pydantic."""
        from app.core.models.job import Job

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Job(
                id=uuid4(),
                status="invalid_status",
                repo_url="https://github.com/example/repo.git",
                task_summary="Test",
                executor_type="awx",
                created_at=now,
                updated_at=now,
            )


class TestLifecycleCanTransition:
    """Tests for the centralised can_transition function in app.core.lifecycle."""

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _can_transition(current: str, target: str) -> bool:
        """Thin wrapper so tests can use plain strings."""
        from app.core.lifecycle import can_transition
        from app.core.models.job import JobStatus

        return can_transition(JobStatus(current), JobStatus(target))

    # -- valid transitions ------------------------------------------------

    def test_pending_to_running_is_valid(self):
        assert self._can_transition("pending", "running") is True

    def test_pending_to_needs_approval_is_valid(self):
        assert self._can_transition("pending", "needs_approval") is True

    def test_pending_to_aborting_is_valid(self):
        assert self._can_transition("pending", "aborting") is True

    def test_running_to_completed_is_valid(self):
        assert self._can_transition("running", "completed") is True

    def test_running_to_failed_is_valid(self):
        assert self._can_transition("running", "failed") is True

    def test_running_to_aborting_is_valid(self):
        assert self._can_transition("running", "aborting") is True

    def test_needs_approval_to_running_is_valid(self):
        assert self._can_transition("needs_approval", "running") is True

    def test_needs_approval_to_rejected_is_valid(self):
        assert self._can_transition("needs_approval", "rejected") is True

    def test_aborting_to_aborted_is_valid(self):
        assert self._can_transition("aborting", "aborted") is True

    # -- self-transitions are always rejected -----------------------------

    def test_self_transition_pending_is_invalid(self):
        assert self._can_transition("pending", "pending") is False

    def test_self_transition_running_is_invalid(self):
        assert self._can_transition("running", "running") is False

    def test_self_transition_completed_is_invalid(self):
        assert self._can_transition("completed", "completed") is False

    def test_self_transition_failed_is_invalid(self):
        assert self._can_transition("failed", "failed") is False

    def test_self_transition_aborted_is_invalid(self):
        assert self._can_transition("aborted", "aborted") is False

    # -- terminal states (completed / failed / rejected / aborted) --------

    def test_completed_to_any_is_invalid(self):
        for target in ("pending", "running", "needs_approval", "failed",
                       "rejected", "aborting", "aborted", "completed"):
            assert self._can_transition("completed", target) is False

    def test_failed_to_any_is_invalid(self):
        for target in ("pending", "running", "needs_approval", "completed",
                       "rejected", "aborting", "aborted", "failed"):
            assert self._can_transition("failed", target) is False

    def test_rejected_to_any_is_invalid(self):
        for target in ("pending", "running", "needs_approval", "completed",
                       "failed", "aborting", "aborted", "rejected"):
            assert self._can_transition("rejected", target) is False

    def test_aborted_to_any_is_invalid(self):
        for target in ("pending", "running", "needs_approval", "completed",
                       "failed", "aborting", "rejected", "aborted"):
            assert self._can_transition("aborted", target) is False

    # -- backward edges that are NOT allowed ------------------------------

    def test_running_to_pending_is_invalid(self):
        assert self._can_transition("running", "pending") is False

    def test_completed_to_running_is_invalid(self):
        assert self._can_transition("completed", "running") is False

    def test_failed_to_running_is_invalid(self):
        assert self._can_transition("failed", "running") is False

    def test_aborting_to_running_is_invalid(self):
        assert self._can_transition("aborting", "running") is False

    def test_aborted_to_aborting_is_invalid(self):
        assert self._can_transition("aborted", "aborting") is False

    # -- direct aborted (skip aborting) -----------------------------------

    def test_pending_to_aborted_is_invalid(self):
        assert self._can_transition("pending", "aborted") is False

    def test_running_to_aborted_is_invalid(self):
        assert self._can_transition("running", "aborted") is False

    # -- approve / reject from non-needs_approval -------------------------

    def test_running_to_needs_approval_is_invalid(self):
        assert self._can_transition("running", "needs_approval") is False

    def test_pending_to_rejected_is_invalid(self):
        assert self._can_transition("pending", "rejected") is False

    # -- the transition table has exactly 9 entries -----------------------

    def test_valid_transitions_count_is_nine(self):
        from app.core.lifecycle import VALID_TRANSITIONS

        assert len(VALID_TRANSITIONS) == 9

    # -- every transition in the table references known enum members ------

    def test_all_transitions_use_valid_enum_members(self):
        from app.core.lifecycle import VALID_TRANSITIONS
        from app.core.models.job import JobStatus

        all_members = set(JobStatus)
        for src, dst in VALID_TRANSITIONS:
            assert src in all_members
            assert dst in all_members


class TestJobStatusValidateTransitionBackwardCompat:
    """Verify that JobStatus.validate_transition still works as before."""

    def test_delegates_to_lifecycle_can_transition(self):
        """validate_transition delegates to the centralised lifecycle module."""
        from app.core.models.job import JobStatus

        # These should still be valid (matching the old behaviour)
        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.ABORTING) is True
        assert JobStatus.validate_transition(JobStatus.RUNNING, JobStatus.ABORTING) is True
        assert JobStatus.validate_transition(JobStatus.ABORTING, JobStatus.ABORTED) is True

        # These should still be invalid (matching the old behaviour)
        assert JobStatus.validate_transition(JobStatus.COMPLETED, JobStatus.ABORTING) is False
        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.ABORTED) is False
        assert JobStatus.validate_transition(JobStatus.ABORTED, JobStatus.RUNNING) is False

        # New transitions available via the expanded table
        assert JobStatus.validate_transition(JobStatus.PENDING, JobStatus.RUNNING) is True
        assert JobStatus.validate_transition(JobStatus.RUNNING, JobStatus.COMPLETED) is True


class TestJobModelSerialization:
    """Tests that Job can be serialized and deserialized."""

    def test_job_model_dump_roundtrip(self):
        """Job.model_dump() should produce dict that can be used to construct a new Job."""
        from app.core.models.job import Job, JobStatus

        now = datetime.now(timezone.utc)
        job = Job(
            id=uuid4(),
            status=JobStatus.COMPLETED,
            repo_url="https://github.com/example/repo.git",
            task_summary="Complete refactor",
            executor_type="awx",
            created_at=now,
            updated_at=now,
        )

        data = job.model_dump()
        assert isinstance(data, dict)
        assert isinstance(data["id"], UUID)
        assert isinstance(data["created_at"], datetime)

        # Round-trip: reconstruct from dumped data
        job2 = Job(**data)
        assert job2 == job

    def test_job_model_dump_json_produces_valid_json(self):
        """Job.model_dump_json() should produce valid JSON with UUID/datetime serialized."""
        from app.core.models.job import Job, JobStatus

        now = datetime.now(timezone.utc)
        job = Job(
            id=uuid4(),
            status=JobStatus.PENDING,
            repo_url="https://github.com/example/repo.git",
            task_summary="Test task",
            executor_type="awx",
            created_at=now,
            updated_at=now,
        )

        json_str = job.model_dump_json()
        assert isinstance(json_str, str)

        import json

        parsed = json.loads(json_str)
        assert parsed["status"] == "pending"
        assert parsed["repo_url"] == "https://github.com/example/repo.git"
