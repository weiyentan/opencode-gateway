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

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from afk_outcomes.models import (
    AWXJobIdentity,
    EntityType,
    ExecutionOutcome,
    Provider,
    ProviderResourceIdentity,
    TriggerType,
)
from app.core.secrets import REDACTED, is_secret_key

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

# Secret-bearing patterns redacted from failure summaries before persistence
# (PRD "redact recognizable bearer tokens and common token/key/password/
# secret assignments").  Only the value — never the key — is replaced, so
# the redacted text remains a bounded, diagnostic summary.
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)\S+")
_TOKEN_PREFIX_RE = re.compile(r"\b(ghp_|gho_|ghu_|ghs_|github_pat_|glpat-)\S+")
# ``key=value`` / ``key: value`` assignments; the key is classified with the
# same ``is_secret_key`` vocabulary used elsewhere in the Gateway, so
# compound keys like ``GITHUB_TOKEN`` are recognized.  The value matches a
# quoted string (single or double) in full — so ``password="my secret
# password"`` redacts the whole value, never just the first word — or a
# non-whitespace token.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9_\-]*)\s*([:=])\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)"
)


def _redact_secret_assignment(match: re.Match) -> str:
    """Redact the value of a secret-key assignment, preserving the key and separator."""
    if is_secret_key(match.group(1)):
        return f"{match.group(1)}{match.group(2)}{REDACTED}"
    return match.group(0)


def redact_failure_summary(value: str) -> str:
    """Redact recognizable secret-bearing values from a failure summary.

    Bearer tokens, common provider token prefixes (``ghp_…``, ``glpat-…``),
    and ``key=value``/``key: value`` assignments whose key is
    secret-like are replaced with ``***``.  The result is what the API
    persists — the raw value is never stored.  Length bounding remains the
    schema's ``max_length`` contract (over-length values are rejected, not
    silently truncated).
    """
    value = _BEARER_TOKEN_RE.sub(r"\1***", value)
    value = _SECRET_ASSIGNMENT_RE.sub(_redact_secret_assignment, value)
    value = _TOKEN_PREFIX_RE.sub(r"\1***", value)
    return value


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
    """Execution-binding write payload (``POST /api/v1/afk/executions``).

    Carries the AWX job identity, the OpenCode external session id, the
    provider resource identity (normalized to the canonical
    ``change_request``), the lifecycle outcome, and optional traceability
    metadata (EDA source event id, branch, title, timestamps, and a
    bounded failure summary).  Raw tokens, stdout, prompts, arbitrary AWX
    payloads, and unbounded ``extra_vars`` are not part of the schema and
    are rejected as unknown fields.

    The two-phase lifecycle (issue #590) extends the write semantics:

    * **Start-time provisioning** — ``outcome="running"`` records the AWX
      launch.  It must carry ``afk_run_id`` (the pre-provisioned lifecycle
      the execution attaches to); the change request and session are
      optional and may still be unknown.
    * **Terminal callback** — ``completed`` / ``failed`` / ``cancelled``
      keep the legacy terminal-only flow.  Failed or cancelled executions
      persist without a change request or a session: when ``resource`` is
      omitted the caller must supply ``afk_run_id`` (the repository needs
      the run's provider to auto-provision only when no run is supplied).
    """

    model_config = ConfigDict(extra="forbid")

    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str | None = Field(
        default=None,
        description=(
            "External OpenCode session id (e.g. ses_* id); optional for "
            "running provisioning and failed/cancelled executions (issue #590)"
        ),
        min_length=1,
    )
    resource: ExecutionBindingResourceIn | None = Field(
        default=None,
        description=(
            "Provider resource identity (normalized to change_request); "
            "optional — failed/cancelled executions may persist without "
            "a change request (issue #590)"
        ),
    )
    outcome: ExecutionOutcome = Field(
        description="Lifecycle execution outcome (running or a terminal value)"
    )
    trigger_type: TriggerType = Field(
        description=(
            "How this execution binding was triggered: eda, manual, scheduled, "
            "backfill, or recovery"
        ),
    )
    afk_run_id: str | None = Field(
        default=None,
        min_length=26,
        max_length=26,
        description=(
            "Optional gateway-assigned AFK run ULID (26 chars), pre-provisioned "
            "via POST /api/v1/afk/executions/runs.  When supplied, the binding "
            "attaches to that lifecycle (validated against afk_runs — an "
            "unknown run is rejected with 404) so many execution bindings can "
            "reference one lifecycle; when omitted, the gateway provisions a "
            "run for the binding (legacy behavior preserved).  Required when "
            "outcome is 'running' or when no resource is supplied (issue #590)."
        ),
    )

    # Optional traceability metadata — bounded and redacted by the contract.
    source_event_id: str | None = Field(
        default=None,
        description=(
            "Originating EDA source event id (for traceability).  Required "
            "when trigger_type is 'eda', optional otherwise."
        ),
    )

    @model_validator(mode="after")
    def _validate_source_event_id_for_eda(self) -> ExecutionBindingCreateRequest:
        """source_event_id is required when trigger_type is EDA."""
        if self.trigger_type is TriggerType.EDA and self.source_event_id is None:
            raise ValueError(
                "source_event_id is required when trigger_type is 'eda'"
            )
        return self

    @model_validator(mode="after")
    def _validate_running_requires_afk_run_id(self) -> ExecutionBindingCreateRequest:
        """Start-time provisioning must attach to a pre-provisioned lifecycle."""
        if self.outcome is ExecutionOutcome.RUNNING and self.afk_run_id is None:
            raise ValueError("afk_run_id is required when outcome is 'running'")
        return self

    @model_validator(mode="after")
    def _validate_terminal_requires_resource_or_run(self) -> ExecutionBindingCreateRequest:
        """A terminal callback without a change request must carry afk_run_id.

        The repository auto-provisions an ``afk_runs`` row (with the
        resource's provider) only when no run is supplied; a resource-less
        terminal callback therefore needs the explicit run reference.
        """
        if (
            self.outcome.is_terminal
            and self.resource is None
            and self.afk_run_id is None
        ):
            raise ValueError(
                "resource or afk_run_id is required for a terminal execution binding"
            )
        return self

    @model_validator(mode="after")
    def _validate_failure_reason_not_on_completed(self) -> ExecutionBindingCreateRequest:
        """Failure metadata is only valid on non-completed outcomes."""
        if self.outcome is ExecutionOutcome.COMPLETED and self.failure_reason is not None:
            raise ValueError(
                "failure_reason is only valid on non-completed outcomes"
            )
        return self

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

    @field_validator("failure_reason", mode="after")
    @classmethod
    def _redact_failure_reason(cls, v: str | None) -> str | None:
        """Redact secret-bearing values before the summary is persisted."""
        if v is None:
            return None
        return redact_failure_summary(v)


class ExecutionBindingUpdateRequest(BaseModel):
    """Terminal-update payload (``PATCH /api/v1/afk/executions/{awx_job_id}``,
    issue #590).

    Transitions the same ``execution_bindings`` row provisioned at AWX start
    from ``running`` to one of the terminal outcomes.  ``outcome`` must be
    terminal (``running`` is rejected — a start can only be created once).
    ``finished_at`` and the bounded, redacted ``failure_reason`` carry the
    terminal facts; ``external_session_id`` and ``resource`` are optional
    fill-ins for identities that only became known at completion (non-erasing:
    an omitted field never erases a stored value, a supplied field that
    contradicts a stored value is a 409 conflict).

    Failed or cancelled executions persist without a change request or a
    session — every field other than ``outcome`` is optional for those
    outcomes.  A ``completed`` update may omit ``resource`` and
    ``external_session_id``: the stored ``running`` row can already hold
    both identities from phase-one provisioning, and the repository rejects
    a completed transition that ends without them (issue #600 review).
    Raw tokens, stdout, prompts, arbitrary AWX payloads, and unbounded
    ``extra_vars`` are not part of the schema and are rejected as unknown
    fields.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: ExecutionOutcome = Field(
        description="Terminal execution outcome: completed | failed | cancelled"
    )
    finished_at: datetime | None = Field(
        default=None, description="Execution finish timestamp"
    )
    external_session_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "External OpenCode session id discovered at completion "
            "(non-erasing fill-in); omitted leaves the stored value untouched"
        ),
    )
    resource: ExecutionBindingResourceIn | None = Field(
        default=None,
        description=(
            "Change-request identity discovered at completion (non-erasing "
            "fill-in); omitted leaves the stored value untouched"
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        max_length=MAX_FAILURE_REASON_LENGTH,
        description=(
            "Bounded failure summary (max 1000 chars).  Raw secrets, stdout "
            "dumps, and arbitrary AWX payloads must never be carried here."
        ),
    )

    @model_validator(mode="after")
    def _validate_outcome_is_terminal(self) -> ExecutionBindingUpdateRequest:
        """A terminal update must target a terminal outcome — never running."""
        if not self.outcome.is_terminal:
            raise ValueError(
                "outcome must be a terminal outcome: completed, failed, or cancelled"
            )
        return self

    @model_validator(mode="after")
    def _validate_failure_reason_not_on_completed(self) -> ExecutionBindingUpdateRequest:
        """Failure metadata is only valid on non-completed outcomes."""
        if self.outcome is ExecutionOutcome.COMPLETED and self.failure_reason is not None:
            raise ValueError(
                "failure_reason is only valid on non-completed outcomes"
            )
        return self

    @model_validator(mode="after")
    def _validate_completed_requires_resource_and_session(
        self,
    ) -> ExecutionBindingUpdateRequest:
        """A completed execution must carry both a change request and a session.

        The stored ``running`` row may already hold these identities from
        phase-one provisioning, so the terminal callback is not required to
        repeat them in the body.  The repository enforces the invariant
        after merge: a completed transition without both a change-request
        identity and a resolved session (whether stored or supplied) is
        rejected as a conflict (issue #600 review).
        """
        return self

    @field_validator("failure_reason", mode="after")
    @classmethod
    def _redact_failure_reason(cls, v: str | None) -> str | None:
        """Redact secret-bearing values before the summary is persisted."""
        if v is None:
            return None
        return redact_failure_summary(v)


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
    resource: ProviderResourceIdentity | None = Field(
        default=None,
        description=(
            "Normalized provider resource identity (change_request); None "
            "when the execution carries no change-request identity "
            "(failed/cancelled executions may persist without one)"
        ),
    )
    outcome: ExecutionOutcome = Field(
        description="Lifecycle execution outcome (running or a terminal value)"
    )

    afk_run_id: str | None = Field(
        default=None,
        description=(
            "Gateway-assigned AFK run ULID when this binding produced "
            "an AFK run; None for legacy bindings or when the run has "
            "not yet been created"
        ),
    )
    trigger_type: str | None = Field(
        default=None,
        description=(
            "How this execution binding was triggered (eda, manual, "
            "scheduled, backfill, recovery); None for legacy rows"
        ),
    )
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
