"""AWX artifact schema definitions and validation.

Defines required artifact schemas for each AWX job template so the
executor can validate artifacts before constructing response models.
When artifacts are missing or malformed the validator raises
:class:`AWXArtifactError` — the executor must **not** fall back to
placeholder values (e.g. zero UUID).
"""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from app.executors.awx.exceptions import AWXArtifactError

T = TypeVar("T", bound=BaseModel)


# ── Artifact models ────────────────────────────────────────────────────────


class CreateWorkspaceArtifacts(BaseModel):
    """Expected artifacts from the ``gateway-create-workspace`` template.

    Both fields are required — a missing or malformed ``workspace_id``
    or ``workspace_path`` is a hard failure.
    """

    workspace_id: UUID = Field(description="UUID of the newly created workspace")
    workspace_path: str = Field(
        min_length=1, description="Absolute path on the Runner VM"
    )


class StartOpencodeArtifacts(BaseModel):
    """Expected artifacts from the ``gateway-opencode-lifecycle`` template
    when ``action=start``.

    Both fields are required — the Gateway must receive a valid session
    ID and port to proceed.
    """

    session_id: UUID = Field(description="UUID of the OpenCode Serve session")
    port: int = Field(gt=0, description="Port the OpenCode Serve process listens on")


class CollectStateArtifacts(BaseModel):
    """Expected artifacts from the ``gateway-workspace-teardown`` template
    when ``action=collect``.

    Only ``status`` is required.  Optional fields (``process_status``,
    ``port``) may be absent.
    """

    status: str = Field(min_length=1, description="Workspace state reported by AWX")
    process_status: str | None = Field(
        default=None, description="Systemd unit status string"
    )
    port: int | None = Field(
        default=None, description="Port the OpenCode Serve process listens on"
    )


# ── Validation helpers ─────────────────────────────────────────────────────


def _build_error(
    model_cls: type[BaseModel],
    raw: dict[str, Any],
    exc: ValidationError,
    template_name: str,
) -> AWXArtifactError:
    """Build a descriptive :class:`AWXArtifactError` from a Pydantic
    validation failure.
    """
    missing_fields: list[str] = []
    invalid_fields: list[str] = []

    for error in exc.errors():
        loc = error.get("loc", ())
        field = ".".join(str(part) for part in loc) if loc else "unknown"
        if error.get("type") == "missing":
            missing_fields.append(field)
        else:
            invalid_fields.append(field)

    return AWXArtifactError(
        f"Invalid artifacts from {template_name}: "
        f"missing={missing_fields}, invalid={invalid_fields}",
        template_name=template_name,
        missing_fields=missing_fields,
        invalid_fields=invalid_fields,
    )


def validate_artifacts(  # noqa: UP047
    model_cls: type[T],
    artifacts: dict[str, Any] | None,
    template_name: str,
    *,
    allow_empty: bool = False,
) -> T:
    """Validate AWX artifacts against a required schema.

    Args:
        model_cls: The Pydantic model class representing the expected
            artifact schema.
        artifacts: Raw artifact dictionary from the AWX job result.
            ``None`` and empty dicts are treated the same.
        template_name: Human-readable template name for error messages
            (e.g. ``"gateway-create-workspace"``).
        allow_empty: If ``True``, return the model with default values
            when ``artifacts`` is empty or ``None``.  Used for lifecycle
            methods that have no required artifacts (``stop``, ``restart``,
            ``cleanup``).

    Returns:
        An instance of *model_cls* populated from the artifacts.

    Raises:
        AWXArtifactError: If artifacts are missing or fail validation.
    """
    _raw: dict[str, Any] = artifacts or {}

    if allow_empty and not _raw:
        # Return a model populated with defaults (Pydantic will fill them).
        return model_cls()

    try:
        return model_cls(**_raw)
    except ValidationError as exc:
        raise _build_error(model_cls, _raw, exc, template_name) from exc
