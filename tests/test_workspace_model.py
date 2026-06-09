"""Tests for the Workspace Pydantic model."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError


class TestWorkspaceModelConstruction:
    """Tests that a Workspace can be constructed with required and optional fields."""

    def test_workspace_can_be_constructed_with_all_required_fields(self):
        """Workspace should accept all required fields and provide defaults for optionals."""
        from app.core.models.workspace import Workspace

        workspace_id = uuid4()
        now = datetime.now(timezone.utc)

        workspace = Workspace(
            id=workspace_id,
            workspace_name="ws-abc123",
            path="/data/workspaces/ws-abc123",
            repo_url="https://github.com/example/repo.git",
            created_at=now,
            updated_at=now,
        )

        assert workspace.id == workspace_id
        assert workspace.workspace_name == "ws-abc123"
        assert workspace.path == "/data/workspaces/ws-abc123"
        assert workspace.repo_url == "https://github.com/example/repo.git"
        assert workspace.runner_id is None
        assert workspace.branch is None
        assert workspace.port is None
        assert workspace.service_name is None
        assert workspace.pinned is False
        assert workspace.cleanup_after is None
        assert workspace.cleanup_status == "active"
        assert workspace.created_at == now
        assert workspace.updated_at == now

    def test_workspace_accepts_all_optional_fields(self):
        """Workspace should accept all optional fields when provided."""
        from app.core.models.workspace import Workspace

        workspace_id = uuid4()
        runner_id = uuid4()
        now = datetime.now(timezone.utc)
        cleanup_after = datetime.now(timezone.utc)

        workspace = Workspace(
            id=workspace_id,
            runner_id=runner_id,
            workspace_name="ws-xyz789",
            path="/data/workspaces/ws-xyz789",
            repo_url="https://github.com/example/other.git",
            branch="feature/new",
            port=8080,
            service_name="opencode-serve-xyz789",
            pinned=True,
            cleanup_after=cleanup_after,
            cleanup_status="cleaning",
            created_at=now,
            updated_at=now,
        )

        assert workspace.runner_id == runner_id
        assert workspace.branch == "feature/new"
        assert workspace.port == 8080
        assert workspace.service_name == "opencode-serve-xyz789"
        assert workspace.pinned is True
        assert workspace.cleanup_after == cleanup_after
        assert workspace.cleanup_status == "cleaning"


class TestWorkspaceModelValidation:
    """Tests that Workspace model rejects invalid data."""

    def test_workspace_requires_id(self):
        """Workspace should require the id field."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Workspace(
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("id",) for e in errors)

    def test_workspace_requires_workspace_name(self):
        """Workspace should require the workspace_name field."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Workspace(
                id=uuid4(),
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("workspace_name",) for e in errors)

    def test_workspace_requires_path(self):
        """Workspace should require the path field."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Workspace(
                id=uuid4(),
                workspace_name="ws-abc123",
                repo_url="https://github.com/example/repo.git",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("path",) for e in errors)

    def test_workspace_requires_repo_url(self):
        """Workspace should require the repo_url field."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError) as exc_info:
            Workspace(
                id=uuid4(),
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                created_at=now,
                updated_at=now,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("repo_url",) for e in errors)

    def test_workspace_id_must_be_valid_uuid(self):
        """Workspace id must be a valid UUID."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Workspace(
                id="not-a-uuid",
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                created_at=now,
                updated_at=now,
            )

    def test_workspace_created_at_must_be_datetime(self):
        """Workspace created_at must be a datetime."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Workspace(
                id=uuid4(),
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                created_at="not-a-datetime",
                updated_at=now,
            )


class TestWorkspaceModelFields:
    """Tests that Workspace model has all the expected field names matching the DB schema."""

    def test_workspace_has_all_expected_field_names(self):
        """Workspace model fields should match the workspaces table columns."""
        from app.core.models.workspace import Workspace

        expected_fields = {
            "id",
            "runner_id",
            "workspace_name",
            "path",
            "repo_url",
            "branch",
            "port",
            "service_name",
            "pinned",
            "cleanup_after",
            "cleanup_status",
            "created_at",
            "updated_at",
        }

        actual_fields = set(Workspace.model_fields.keys())
        assert actual_fields == expected_fields


class TestWorkspaceStatusEnum:
    """Tests for the WorkspaceStatus enum used on the Workspace model."""

    def test_workspace_status_enum_has_expected_values(self):
        """WorkspaceStatus enum should define active, cleaning, pinned."""
        from app.core.models.workspace import WorkspaceStatus

        assert WorkspaceStatus.ACTIVE.value == "active"
        assert WorkspaceStatus.CLEANING.value == "cleaning"
        assert WorkspaceStatus.PINNED.value == "pinned"

    def test_workspace_rejects_invalid_status(self):
        """Workspace should reject a status not in the WorkspaceStatus enum."""
        from app.core.models.workspace import Workspace

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            Workspace(
                id=uuid4(),
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                cleanup_status="expired",
                created_at=now,
                updated_at=now,
            )

    def test_workspace_accepts_valid_statuses(self):
        """Workspace should accept all valid WorkspaceStatus values."""
        from app.core.models.workspace import Workspace, WorkspaceStatus

        now = datetime.now(timezone.utc)
        for status in WorkspaceStatus:
            workspace = Workspace(
                id=uuid4(),
                workspace_name="ws-abc123",
                path="/data/workspaces/ws-abc123",
                repo_url="https://github.com/example/repo.git",
                cleanup_status=status,
                created_at=now,
                updated_at=now,
            )
            assert workspace.cleanup_status == status

    def test_workspace_status_is_string_enum(self):
        """WorkspaceStatus should be a string enum."""
        from app.core.models.workspace import WorkspaceStatus
        from enum import Enum

        assert issubclass(WorkspaceStatus, str)
        assert issubclass(WorkspaceStatus, Enum)


class TestWorkspaceModelSerialization:
    """Tests that Workspace can be serialized and deserialized."""

    def test_workspace_model_dump_roundtrip(self):
        """Workspace.model_dump() should produce dict that can be used to construct a new Workspace."""
        from app.core.models.workspace import Workspace, WorkspaceStatus

        now = datetime.now(timezone.utc)
        workspace = Workspace(
            id=uuid4(),
            workspace_name="ws-abc123",
            path="/data/workspaces/ws-abc123",
            repo_url="https://github.com/example/repo.git",
            pinned=True,
            cleanup_status=WorkspaceStatus.PINNED,
            created_at=now,
            updated_at=now,
        )

        data = workspace.model_dump()
        assert isinstance(data, dict)
        assert isinstance(data["id"], UUID)
        assert isinstance(data["created_at"], datetime)
        assert data["cleanup_status"] == WorkspaceStatus.PINNED

        # Round-trip: reconstruct from dumped data
        workspace2 = Workspace(**data)
        assert workspace2 == workspace

    def test_workspace_model_dump_json_produces_valid_json(self):
        """Workspace.model_dump_json() should produce valid JSON with UUID/datetime serialized."""
        from app.core.models.workspace import Workspace, WorkspaceStatus

        now = datetime.now(timezone.utc)
        workspace = Workspace(
            id=uuid4(),
            workspace_name="ws-abc123",
            path="/data/workspaces/ws-abc123",
            repo_url="https://github.com/example/repo.git",
            cleanup_status=WorkspaceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )

        json_str = workspace.model_dump_json()
        assert isinstance(json_str, str)

        import json

        parsed = json.loads(json_str)
        assert parsed["cleanup_status"] == "active"
        assert parsed["workspace_name"] == "ws-abc123"
        assert parsed["path"] == "/data/workspaces/ws-abc123"
        assert parsed["repo_url"] == "https://github.com/example/repo.git"


class TestWorkspacePydanticModel:
    """Tests for the WorkspacePydantic API serialization model."""

    def test_workspace_pydantic_mirrors_workspace_fields(self):
        """WorkspacePydantic should have the same fields as Workspace."""
        from app.core.models.workspace import Workspace, WorkspacePydantic

        assert set(WorkspacePydantic.model_fields.keys()) == set(
            Workspace.model_fields.keys()
        )

    def test_workspace_pydantic_can_be_constructed_like_workspace(self):
        """WorkspacePydantic should accept the same construction pattern as Workspace."""
        from app.core.models.workspace import WorkspacePydantic

        workspace_id = uuid4()
        now = datetime.now(timezone.utc)

        w = WorkspacePydantic(
            id=workspace_id,
            workspace_name="ws-abc123",
            path="/data/workspaces/ws-abc123",
            repo_url="https://github.com/example/repo.git",
            created_at=now,
            updated_at=now,
        )

        assert w.id == workspace_id
        assert w.workspace_name == "ws-abc123"
        assert w.path == "/data/workspaces/ws-abc123"
        assert w.repo_url == "https://github.com/example/repo.git"
        assert w.cleanup_status == "active"
        assert w.pinned is False
