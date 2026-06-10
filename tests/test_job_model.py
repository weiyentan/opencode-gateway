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
        """JobStatus enum should define pending, running, completed,
        failed, needs_approval, rejected.
        """
        from app.core.models.job import JobStatus

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

    def test_job_status_is_string_enum(self):
        """JobStatus should be a string enum."""
        from app.core.models.job import JobStatus
        from enum import Enum

        assert issubclass(JobStatus, str)
        assert issubclass(JobStatus, Enum)


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
