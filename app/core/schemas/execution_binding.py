"""Pydantic schemas for the execution-binding API (issue #548).

These schemas shape the request/response contract of the execution-binding
write path (``POST /api/v1/afk/executions``) and its read paths by AWX job
identity or provider resource identity (ADR 0024).  They reuse the locked
domain vocabulary (``ExecutionOutcome``, ``AWXJobIdentity``,
``ProviderResourceIdentity``) from :mod:`afk_outcomes.models` for validation
and never re-derive it.

GitHub pull requests and GitLab merge requests both normalize to the
canonical ``change_request`` identity at the API boundary.  The public
schemas expose only approved execution metadata — raw tokens, stdout,
prompts, arbitrary AWX payloads, and unbounded ``extra_vars`` are
structurally absent and rejected as unknown fields on the write path.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from afk_outcomes.models import (
    AWXJobIdentity,
    EntityType,
    ExecutionOutcome,
    Provider,
    ProviderResourceIdentity,
)

# Maximum length for the bounded failure metadata string on the public
# schema.  Mirrors the domain model's bound (issue #546): a bounded,
# redacted failure summary — raw secrets, stdout dumps, and arbitrary AWX
# payloads must never be carried here.
MAX_FAILURE_REASON_LENGTH = 1000

# Provider-native resource-type vocabulary accepted on the write path.
# GitHub ``pull_request`` and GitLab ``merge_request`` both normalize to the
# canonical ``change_request`` identity (the mapping-bridge rule of ADR 0020);
# the already-canonical form passes through unchanged.
_PROVIDER_NATIVE_RESOURCE_TYPES = frozenset(
    {"pull_request", "merge_request", "change_request"}
)


class ExecutionBindingResourceIn(BaseModel):
    """Provider resource identity as supplied by the AWX integration.

    Accepts the provider-native vocabulary — GitHub ``pull_request`` and
    GitLab ``merge_request`` — and normalizes both to the canonical
    ``change_request`` entity type.  Other entity types are rejected: an
    execution binding always targets a change request.

    Provider/type compatibility (issue #565):

    * GitHub accepts ``pull_request`` and ``change_request``.
    * GitLab accepts ``merge_request`` and ``change_request``.
    * GitHub ``merge_request`` and GitLab ``pull_request`` are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider = Field(description="Source provider: github | gitlab")
    repository: str = Field(description="Repository URL (normalized via normalize_repository_url)")
    resource_type: str = Field(
        description=(
            "pull_request | merge_request | change_request — all normalize "
            "to the canonical change_request identity"
        )
    )
    resource_number: str = Field(
        description="Provider-scoped external id (PR/MR number as opaque string)"
    )

    @field_validator("resource_type", mode="after")
    @classmethod
    def _validate_resource_type_vocab(cls, v: str) -> str:
        """Validate provider-native PR/MR vocabulary (normalization deferred)."""
        if v not in _PROVIDER_NATIVE_RESOURCE_TYPES:
            raise ValueError(
                "resource_type must be 'pull_request', 'merge_request', or "
                f"'change_request'; got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def _validate_provider_type_and_normalize(self) -> ExecutionBindingResourceIn:
        """Enforce provider-specific resource-type compatibility and canonicalize.

        GitHub: pull_request | change_request
        GitLab: merge_request | change_request
        Cross-provider combos are rejected with a clear ValueError (surfaces as 422).
        The resource_type is then normalized to the canonical ``change_request``.
        """
        if self.provider == Provider.GITHUB and self.resource_type == "merge_request":
            raise ValueError(
                "resource_type 'merge_request' is not valid for provider 'github'; "
                "use 'pull_request' or 'change_request'"
            )
        if self.provider == Provider.GITLAB and self.resource_type == "pull_request":
            raise ValueError(
                "resource_type 'pull_request' is not valid for provider 'gitlab'; "
                "use 'merge_request' or 'change_request'"
            )
        # Canonical persistence: normalize to change_request.
        object.__setattr__(self, "resource_type", EntityType.CHANGE_REQUEST.value)
        return self

    def to_provider_resource_identity(self) -> ProviderResourceIdentity:
        """Return the canonical domain identity for this resource.

        GitHub pull requests and GitLab merge requests both resolve to a
        ``change_request`` :class:`~afk_outcomes.models.ProviderResourceIdentity`.
        """
        return ProviderResourceIdentity(
            provider=self.provider,
            repository=self.repository,
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number=self.resource_number,
        )


class ExecutionBindingCreateRequest(BaseModel):
    """Final execution-binding write payload (``POST /api/v1/afk/executions``).

    Carries the AWX job identity, the OpenCode external session id, the
    provider resource identity (normalized to the canonical
    ``change_request``), the terminal outcome, and optional traceability
    metadata (EDA source event id, branch, title, terminal timestamps, and
    a bounded failure summary).  Raw tokens, stdout, prompts, arbitrary AWX
    payloads, and unbounded ``extra_vars`` are not part of the schema and
    are rejected as unknown fields.
    """

    model_config = ConfigDict(extra="forbid")

    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str = Field(
        description="External OpenCode session id (e.g. ses_* id)",
        min_length=1,
    )
    resource: ExecutionBindingResourceIn = Field(
        description="Provider resource identity (normalized to change_request)"
    )
    outcome: ExecutionOutcome = Field(description="Terminal execution outcome")

    # Optional traceability metadata — bounded and redacted by the contract.
    source_event_id: str | None = Field(
        default=None,
        description="Originating EDA source event id (for traceability)",
    )
    branch: str | None = Field(default=None, description="Branch or ref")
    title: str | None = Field(default=None, description="Execution title")
    started_at: datetime | None = Field(
        default=None, description="Execution start timestamp"
    )
    finished_at: datetime | None = Field(
        default=None, description="Execution finish timestamp"
    )
    failure_reason: str | None = Field(
        default=None,
        max_length=MAX_FAILURE_REASON_LENGTH,
        description=(
            "Bounded failure summary (max 1000 chars).  Raw secrets, stdout "
            "dumps, and arbitrary AWX payloads must never be carried here."
        ),
    )


class ExecutionBindingReadResponse(BaseModel):
    """One execution binding (single-binding read by AWX job id).

    The read counterpart of the write request: the same AWX job identity,
    external session id, canonical resource identity, terminal outcome, and
    optional traceability metadata, plus the gateway-assigned
    ``binding_id``.  Exposes only approved execution metadata — no raw
    tokens, stdout, prompts, or AWX payload fields exist on this shape.
    """

    binding_id: str = Field(description="Gateway-assigned binding id")
    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str | None = Field(
        default=None,
        description=(
            "External OpenCode session id (e.g. ses_* id); None when the "
            "binding has no resolved session"
        ),
    )
    resource: ProviderResourceIdentity = Field(
        description="Normalized provider resource identity (change_request)"
    )
    outcome: ExecutionOutcome = Field(description="Terminal execution outcome")

    source_event_id: str | None = Field(
        default=None, description="Originating EDA source event id"
    )
    branch: str | None = Field(default=None, description="Branch or ref")
    title: str | None = Field(default=None, description="Execution title")
    started_at: datetime | None = Field(default=None, description="Start timestamp")
    finished_at: datetime | None = Field(default=None, description="Finish timestamp")
    failure_reason: str | None = Field(
        default=None,
        max_length=MAX_FAILURE_REASON_LENGTH,
        description="Bounded failure summary (max 1000 chars)",
    )


class ExecutionBindingHistoryResponse(BaseModel):
    """Full failed-to-successful execution history for one resource.

    The resource-history read (by provider resource identity) returns the
    normalized resource identity plus every binding observed for it — a
    resource may have many bindings, including failed and later successful
    executions (ADR 0024).  ``bindings`` preserves that full history; the
    Gateway never collapses it to a single terminal answer.
    """

    resource: ProviderResourceIdentity = Field(
        description="Normalized provider resource identity (change_request)"
    )
    bindings: list[ExecutionBindingReadResponse] = Field(
        default_factory=list,
        description="All execution bindings for the resource (full history)",
    )
