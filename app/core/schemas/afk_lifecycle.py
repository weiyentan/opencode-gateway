"""Pydantic schemas for the provisional AFK run lifecycle API (issue #589).

These schemas shape the request/response contract of the provisional
lifecycle write paths — provisioning (idempotent on
``provider + host + source_event_id``), explicit change-request binding,
and recovery via ``recovered_from_afk_run_id``.  They reuse the locked
domain vocabulary (``Provider``, ``TriggerType``,
``ProviderResourceIdentity``, ``AFKRunLifecycle``) from
:mod:`afk_outcomes.models` for validation and never re-derive it.

The write requests are strict (source provenance, repository identity, and
trigger metadata are required; unknown fields are rejected); the response
is lenient on readback (legacy ``afk_runs`` rows predate the lifecycle
columns, so those fields surface as ``None``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from afk_outcomes.models import (
    AFKRunLifecycle,
    Provider,
    ProviderResourceIdentity,
    TriggerType,
)


class AFKRunProvisionRequest(BaseModel):
    """Provisional lifecycle provisioning payload (``POST /runs``).

    Carries the source provenance (``provider``, ``host``,
    ``source_event_id``), the provider-qualified repository identity
    (normalized at the API boundary), the trigger metadata, an optional
    title, an optional recovery reference, and the optional batch provenance
    (``deliveries`` — issue #595).  AWX and change-request fields are
    deliberately absent — they are populated later by the execution-binding
    and change-request-binding paths.

    ``provider + host + source_event_id`` is the idempotency key: replaying
    the same payload returns the existing lifecycle without mutation.

    ``deliveries`` is the ordered list of contributing delivery identities
    of the accepted webhook batch; the first element is the first triggering
    delivery.  It is optional for backward compatibility — provisioning
    without a batch simply carries no batch provenance (legacy behavior).
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider = Field(description="Source provider: github | gitlab")
    host: str = Field(
        min_length=1,
        description="Source host provenance (idempotency key part)",
    )
    source_event_id: str = Field(
        min_length=1,
        description="Originating source event id (idempotency key part)",
    )
    repository: str = Field(
        min_length=1,
        description="Repository URL (normalized via normalize_repository_url)",
    )
    trigger_type: TriggerType = Field(
        description=(
            "How this lifecycle was triggered: eda, manual, scheduled, "
            "backfill, or recovery"
        ),
    )
    title: str | None = Field(default=None, description="Optional lifecycle title")
    deliveries: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered delivery identities of the accepted webhook batch — the "
            "first element is the first triggering delivery.  Optional: an "
            "omitted batch carries no batch provenance on the run."
        ),
    )
    recovered_from_afk_run_id: str | None = Field(
        default=None,
        description=(
            "ULID of the predecessor lifecycle for a recovery lifecycle "
            "(never mutates the predecessor)"
        ),
    )

    @field_validator("deliveries")
    @classmethod
    def _validate_deliveries(cls, v: list[str]) -> list[str]:
        """Every delivery identity must be a non-empty string."""
        if any(not item for item in v):
            raise ValueError("deliveries entries must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _validate_recovery_reference(self) -> AFKRunProvisionRequest:
        """A recovery-triggered lifecycle must reference its predecessor."""
        if (
            self.trigger_type is TriggerType.RECOVERY
            and self.recovered_from_afk_run_id is None
        ):
            raise ValueError(
                "recovered_from_afk_run_id is required when trigger_type is 'recovery'"
            )
        return self


class ChangeRequestBindingRequest(BaseModel):
    """Explicit change-request binding payload (``POST /runs/{id}/change-request``).

    Carries the flattened stable resource identity of the change request to
    bind — GitHub pull requests and GitLab merge requests both bind here
    under the canonical ``change_request`` identity.  The binding is
    idempotent: repeating the same identity is a no-op, and binding a
    change request that already belongs to another lifecycle is a conflict
    (the 1:1 lifecycle<->change_request invariant).
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider = Field(description="Source provider: github | gitlab")
    repository: str = Field(
        min_length=1,
        description="Repository URL (normalized via normalize_repository_url)",
    )
    external_id: str = Field(
        min_length=1,
        description="Provider-scoped change-request id (PR/MR number as opaque string)",
    )


class ChangeRequestLookupResponse(BaseModel):
    """Compact change-request -> owning lifecycle lookup (``GET /runs/by-change-request``).

    Resolves a provider-qualified change-request identity (GitHub PR or
    GitLab MR, both canonical ``change_request``) to its owning
    ``afk_run_id`` via the explicit durable binding on ``afk_runs``.  The
    ``change_request`` echoes the canonical stable resource identity used
    for the lookup.
    """

    afk_run_id: str = Field(description="ULID primary key of the owning run")
    change_request: ProviderResourceIdentity = Field(
        description=(
            "The canonical change_request identity that resolved to this run"
        ),
    )


class AFKRunLifecycleResponse(BaseModel):
    """One provisional lifecycle as surfaced by the write-path responses.

    The read counterpart of the provisioning request: the same source
    provenance, repository identity, and trigger metadata, plus the
    gateway-assigned ``afk_run_id``, the current ``status``, the bound
    change request (``None`` until bound), the recovery reference, and the
    batch provenance (``first_delivery_id`` + ``delivery_ids`` — issue
    #595).  Legacy rows surface ``None`` for the lifecycle columns they
    predate and an empty batch.
    """

    afk_run_id: str = Field(description="ULID primary key of the run")
    provider: Provider = Field(description="Source provider: github | gitlab")
    status: str = Field(description="RunStatus value (provisional: 'pending')")
    host: str | None = Field(
        default=None, description="Source host provenance (None for legacy rows)"
    )
    source_event_id: str | None = Field(
        default=None,
        description="Originating source event id (None for legacy rows)",
    )
    repository: str | None = Field(
        default=None,
        description="Provider-qualified repository identity (None for legacy rows)",
    )
    trigger_type: str | None = Field(
        default=None,
        description=(
            "Trigger origin (eda, manual, scheduled, backfill, recovery); "
            "None for legacy rows"
        ),
    )
    title: str | None = Field(default=None, description="Lifecycle title")
    first_delivery_id: str | None = Field(
        default=None,
        description=(
            "First triggering delivery identity of the provisioning batch "
            "(None for legacy rows and runs provisioned without a batch)"
        ),
    )
    delivery_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Every contributing delivery identity in the provisioning batch, "
            "in stored order (empty for legacy rows)"
        ),
    )
    change_request: ProviderResourceIdentity | None = Field(
        default=None,
        description=(
            "The bound change request (canonical change_request identity); "
            "None until bound"
        ),
    )
    recovered_from_afk_run_id: str | None = Field(
        default=None,
        description="ULID of the predecessor lifecycle (recovery lifecycles)",
    )

    @classmethod
    def from_domain(
        cls,
        lifecycle: AFKRunLifecycle,
        *,
        first_delivery_id: str | None = None,
        delivery_ids: list[str] | None = None,
    ) -> AFKRunLifecycleResponse:
        """Build the response from a domain :class:`AFKRunLifecycle`.

        The bound change request is surfaced as a nested canonical
        ``ProviderResourceIdentity`` (the domain model stores the three
        flattened columns).  Batch provenance is surfaced from the stored
        batch records via the optional ``first_delivery_id`` /
        ``delivery_ids`` arguments (issue #595); ``None`` arguments render
        the legacy empty-batch shape.
        """
        return cls(
            afk_run_id=lifecycle.afk_run_id,
            provider=lifecycle.provider,
            status=lifecycle.status,
            host=lifecycle.host,
            source_event_id=lifecycle.source_event_id,
            repository=lifecycle.repository,
            trigger_type=lifecycle.trigger_type,
            title=lifecycle.title,
            first_delivery_id=first_delivery_id,
            delivery_ids=delivery_ids or [],
            change_request=lifecycle.change_request_identity(),
            recovered_from_afk_run_id=lifecycle.recovered_from_afk_run_id,
        )
