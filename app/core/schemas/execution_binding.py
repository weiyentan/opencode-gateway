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

# Maximum length for the ``failure_summary`` free-text field.  Unlike the
# short ``failure_reason`` label (which rejects over-length input),
# ``failure_summary`` is truncated text (migration 0037): a longer input is
# redacted first, then truncated to this bound by Python character count
# rather than rejected (issue #564).
MAX_FAILURE_SUMMARY_LENGTH = 1000

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
# compound keys like ``GITHUB_TOKEN`` are recognized.  The key may be quoted
# — matching JSON-style payloads like ``{"password": "value"}`` and
# ``'api_key' : 'value'``.  The value matches a quoted string (single or
# double) in full — so ``password="my secret password"`` redacts the whole
# value, never just the first word — or a non-whitespace token.
_SECRET_ASSIGNMENT_RE = re.compile(
    r'(?P<q>["\']?)(?P<key>[A-Za-z][A-Za-z0-9_\-]*)(?P=q)'
    r"(?P<sep>\s*[:=]\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|\S+)'
)


def _redact_secret_assignment(match: re.Match[str]) -> str:
    """Redact the value of a secret-key assignment, preserving the key and separator."""
    if is_secret_key(match.group("key")):
        return (
            f"{match.group('q')}{match.group('key')}{match.group('q')}"
            f"{match.group('sep')}{REDACTED}"
        )
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


def redact_and_bound_failure_summary(value: str) -> str:
    """Redact a failure summary, then truncate it to ``MAX_FAILURE_SUMMARY_LENGTH``.

    ``failure_summary`` (issue #564) is truncated text: redaction runs
    first so a secret straddling the truncation boundary is replaced as a
    whole and never survives as a truncated token, then the result is cut
    with a Python character slice (``result[:1000]``) — never a UTF-8 byte
    slice, which would split multi-byte characters mid-codepoint.

    Redaction boundary: redaction is applied at the API schema layer
    (``ExecutionBindingCreateRequest`` / ``ExecutionBindingUpdateRequest``)
    before the value reaches the domain model or repository.  The domain
    model stores the already-redacted value as-is — it does not re-redact.
    This is an intentional API-only invariant: the schema is the sole
    redaction enforcement point, and callers that bypass the API (e.g.
    direct repository access) are responsible for their own redaction.
    """
    return redact_failure_summary(value)[:MAX_FAILURE_SUMMARY_LENGTH]


def _clean_session_id_collection(v: list[str] | None) -> list[str] | None:
    """Validate and normalize an ``external_session_ids`` collection.

    Every entry must be a non-empty string; duplicates are removed while
    preserving first-occurrence order (the first entry is the primary
    session).  ``None`` passes through untouched (the field is optional).
    """
    if v is None:
        return None
    cleaned: list[str] = []
    for item in v:
        if not item:
            raise ValueError(
                "external_session_ids entries must be non-empty strings"
            )
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


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
    metadata (EDA source event id, branch, title, timestamps, a bounded
    ``failure_reason`` label, and a bounded, redacted, truncated
    ``failure_summary``).  Raw tokens, stdout, prompts, arbitrary AWX
    payloads, and unbounded ``extra_vars`` are not part of the schema and
    are rejected as unknown fields.

    The two-phase lifecycle (issue #590) extends the write semantics:

    * **Start-time provisioning** — ``outcome="running"`` records the AWX
      launch.  It must carry ``afk_run_id`` (the pre-provisioned lifecycle
      the execution attaches to); the change request and session are
      optional and may still be unknown.
    * **Terminal callback** — ``completed`` / ``failed`` / ``cancelled``
      keep the legacy terminal-only flow.  A ``completed`` callback must
      carry both a change request and a session (the execution is only
      meaningful once it resolved both); failed or cancelled executions
      persist without a change request or a session — when ``resource`` is
      omitted the caller must supply ``afk_run_id`` (the repository needs
      the run's provider to auto-provision only when no run is supplied).
    """

    model_config = ConfigDict(extra="forbid")

    awx_job: AWXJobIdentity = Field(description="AWX job run identity")
    external_session_id: str | None = Field(
        default=None,
        description=(
            "External OpenCode session id (e.g. ses_* id); optional for "
            "running provisioning and failed/cancelled executions (issue #590).  "
            "Kept for backward compatibility — normalized into "
            "external_session_ids as a single-element collection."
        ),
        min_length=1,
    )
    external_session_ids: list[str] | None = Field(
        default=None,
        description=(
            "External OpenCode session ids attributed to this execution "
            "(issue #627).  Non-empty collection; duplicates are removed "
            "preserving first-occurrence order (the first entry is the "
            "primary session).  Optional for running provisioning and "
            "failed/cancelled executions.  Mutually exclusive with the "
            "singular external_session_id."
        ),
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
    afk_run_id: str = Field(
        min_length=26,
        max_length=26,
        description=(
            "Gateway-assigned AFK run ULID (26 chars), pre-provisioned "
            "via POST /api/v1/afk/executions/runs.  Required for every "
            "new binding (issue #626): the binding attaches to that "
            "lifecycle (validated against afk_runs — an unknown run is "
            "rejected with 404) so many execution bindings can reference "
            "one lifecycle.  Legacy persisted rows without it remain "
            "readable with a null afk_run_id."
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
    def _validate_afk_run_id_required_for_new_bindings(
        self,
    ) -> ExecutionBindingCreateRequest:
        """Every new execution binding must carry ``afk_run_id`` (issue #626).

        The AWX job must join directly to its logical AFK Run — the legacy
        auto-provisioning path (POST without ``afk_run_id``, issue #595)
        is closed for new bindings.  The rule applies uniformly to
        start-time provisioning (``running``) and to direct terminal
        POSTs, covering all outcomes.  Legacy persisted rows are
        unaffected: reads return their null ``afk_run_id`` as-is and the
        ``PATCH`` terminal-update path never re-validates the create
        contract.
        """
        if self.afk_run_id is None:
            raise ValueError(
                "afk_run_id is required for new execution bindings; "
                "pre-provision the lifecycle via POST "
                "/api/v1/afk/executions/runs first"
            )
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
    def _validate_completed_requires_resource_and_session(
        self,
    ) -> ExecutionBindingCreateRequest:
        """A completed execution must carry both a change request and a session.

        There is no staged ``running`` row to draw identities from on the
        direct terminal POST — the callback arrives with everything known at
        completion.  A ``completed`` execution is only meaningful when it
        names both the change request it resolved and the OpenCode session
        it ran in.  Failed and cancelled executions may remain
        resource/session-less (issue #600 review).
        """
        if self.outcome is ExecutionOutcome.COMPLETED and (
            self.resource is None
            or (
                self.external_session_id is None
                and self.external_session_ids is None
            )
        ):
            raise ValueError(
                "resource and external_session_id (or external_session_ids) "
                "are required when outcome is 'completed'"
            )
        return self

    @field_validator("external_session_ids")
    @classmethod
    def _validate_session_collection_entries(
        cls, v: list[str] | None
    ) -> list[str] | None:
        """Every session id entry must be a non-empty string (issue #627)."""
        return _clean_session_id_collection(v)

    @model_validator(mode="after")
    def _validate_session_collection(self) -> ExecutionBindingCreateRequest:
        """Normalize the session attribution input (issue #627).

        The new plural field and the legacy singular field are mutually
        exclusive: supplying both is a contradictory payload (422).  A
        singular ``external_session_id`` is accepted for backward
        compatibility and normalizes to a one-element collection.
        The plural collection may be omitted entirely, but when supplied
        it must be non-empty (an empty collection is invalid input, not
        "no sessions" — omit the field instead).
        """
        if (
            self.external_session_id is not None
            and self.external_session_ids is not None
        ):
            raise ValueError(
                "external_session_id and external_session_ids are mutually "
                "exclusive; supply only one"
            )
        if self.external_session_ids is not None and not self.external_session_ids:
            raise ValueError(
                "external_session_ids must be a non-empty collection when "
                "supplied; omit the field to attribute no sessions"
            )
        return self

    @model_validator(mode="after")
    def _reject_failure_metadata_on_completed(self) -> ExecutionBindingCreateRequest:
        """Reject failure metadata on ``completed`` outcomes (issue #564).

        A completed execution carries no failure — non-null
        ``failure_reason`` or ``failure_summary`` alongside
        ``outcome == completed`` is a contradictory payload and is rejected
        with a 422.
        """
        if self.outcome is ExecutionOutcome.COMPLETED and (
            self.failure_reason is not None or self.failure_summary is not None
        ):
            raise ValueError(
                "failure_reason and failure_summary must be null when "
                "outcome is 'completed'"
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
    failure_summary: str | None = Field(
        default=None,
        description=(
            "Bounded, redacted failure summary text (max 1000 chars, truncated). "
            "Recognizable bearer tokens and common token/key/password/secret "
            "assignments are redacted before persistence; over-length input is "
            "truncated by Python character count rather than rejected.  Raw "
            "secrets, stdout dumps, and arbitrary AWX payloads must never be "
            "carried here."
        ),
    )

    @field_validator("failure_reason", mode="after")
    @classmethod
    def _redact_failure_reason(cls, v: str | None) -> str | None:
        """Redact secret-bearing values before the summary is persisted."""
        if v is None:
            return None
        return redact_failure_summary(v)

    @field_validator("failure_summary", mode="after")
    @classmethod
    def _redact_and_bound_failure_summary(cls, v: str | None) -> str | None:
        """Redact secret-like values and truncate ``failure_summary`` to 1000.

        ``failure_summary`` is truncated text (migration 0037): a longer
        input is redacted and then truncated to the bound rather than
        rejected, unlike the short ``failure_reason`` label which rejects
        over-length input.  Truncation is by Python character count, never
        bytes, and always happens after redaction.
        """
        if v is None:
            return None
        return redact_and_bound_failure_summary(v)

    def normalized_session_ids(self) -> list[str]:
        """Return the deduplicated, order-preserving session attribution.

        The plural collection wins when supplied (the schema validator makes
        the two forms mutually exclusive); a legacy singular
        ``external_session_id`` normalizes to a one-element collection.
        Empty when the execution carries no session attribution.
        """
        if self.external_session_ids is not None:
            cleaned = _clean_session_id_collection(self.external_session_ids)
            return cleaned or []
        if self.external_session_id is not None:
            return [self.external_session_id]
        return []


class ExecutionBindingUpdateRequest(BaseModel):
    """Terminal-update payload (``PATCH /api/v1/afk/executions/{awx_job_id}``,
    issue #590).

    Transitions the same ``execution_bindings`` row provisioned at AWX start
    from ``running`` to one of the terminal outcomes.  ``outcome`` must be
    terminal (``running`` is rejected — a start can only be created once).
    ``finished_at`` and the bounded, redacted ``failure_reason`` /
    ``failure_summary`` carry the terminal facts; ``external_session_id``
    and ``resource`` are optional fill-ins for identities that only became
    known at completion (non-erasing: an omitted field never erases a
    stored value, a supplied field that contradicts a stored value is a
    409 conflict).

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
            "(non-erasing fill-in); omitted leaves the stored value untouched.  "
            "Kept for backward compatibility — normalized into "
            "external_session_ids as a single-element collection."
        ),
    )
    external_session_ids: list[str] | None = Field(
        default=None,
        description=(
            "External OpenCode session ids attributed to this execution "
            "(issue #627), discovered at completion (non-erasing fill-in).  "
            "Duplicates are removed preserving first-occurrence order.  "
            "Mutually exclusive with the singular external_session_id; an "
            "empty collection is invalid — omit the field instead."
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
    failure_summary: str | None = Field(
        default=None,
        description=(
            "Bounded, redacted failure summary text (max 1000 chars, truncated). "
            "Recognizable bearer tokens and common token/key/password/secret "
            "assignments are redacted before persistence; over-length input is "
            "truncated by Python character count rather than rejected.  Raw "
            "secrets, stdout dumps, and arbitrary AWX payloads must never be "
            "carried here."
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

    @field_validator("external_session_ids")
    @classmethod
    def _validate_session_collection_entries(
        cls, v: list[str] | None
    ) -> list[str] | None:
        """Every session id entry must be a non-empty string (issue #627)."""
        return _clean_session_id_collection(v)

    @model_validator(mode="after")
    def _validate_session_collection(self) -> ExecutionBindingUpdateRequest:
        """Normalize the session attribution input (issue #627).

        Same contract as the POST path: the plural collection and the legacy
        singular field are mutually exclusive, and a supplied collection must
        be non-empty (omitting the field attributes no new sessions).
        """
        if (
            self.external_session_id is not None
            and self.external_session_ids is not None
        ):
            raise ValueError(
                "external_session_id and external_session_ids are mutually "
                "exclusive; supply only one"
            )
        if self.external_session_ids is not None and not self.external_session_ids:
            raise ValueError(
                "external_session_ids must be a non-empty collection when "
                "supplied; omit the field to attribute no sessions"
            )
        return self

    @model_validator(mode="after")
    def _reject_failure_metadata_on_completed(self) -> ExecutionBindingUpdateRequest:
        """Failure metadata is only valid on non-completed outcomes.

        A completed execution carries no failure — non-null
        ``failure_reason`` or ``failure_summary`` alongside
        ``outcome == completed`` is a contradictory payload and is rejected
        with a 422 (issue #564).
        """
        if self.outcome is ExecutionOutcome.COMPLETED and (
            self.failure_reason is not None or self.failure_summary is not None
        ):
            raise ValueError(
                "failure_reason and failure_summary are only valid on "
                "non-completed outcomes"
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

    @field_validator("failure_summary", mode="after")
    @classmethod
    def _redact_and_bound_failure_summary(cls, v: str | None) -> str | None:
        """Redact secret-like values and truncate ``failure_summary`` to 1000.

        Mirrors the POST-path validator: redaction runs first, then a Python
        character-count truncation to ``MAX_FAILURE_SUMMARY_LENGTH``.
        """
        if v is None:
            return None
        return redact_and_bound_failure_summary(v)

    def normalized_session_ids(self) -> list[str]:
        """Return the deduplicated, order-preserving session attribution.

        Mirrors the POST-path helper: the plural collection wins when
        supplied; a legacy singular ``external_session_id`` normalizes to a
        one-element collection.  Empty when the update attributes no
        sessions.
        """
        if self.external_session_ids is not None:
            cleaned = _clean_session_id_collection(self.external_session_ids)
            return cleaned or []
        if self.external_session_id is not None:
            return [self.external_session_id]
        return []


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
            "binding has no resolved session.  Legacy singular readback — "
            "the first entry of external_session_ids when one exists."
        ),
    )
    external_session_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Normalized session attribution (issue #627): every external "
            "OpenCode session id attributed to this execution, deduplicated "
            "with the first entry as the primary session.  Empty for "
            "historical / run-level-only bindings with no resolved session."
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
        description="Bounded failure reason (label) (max 1000 chars)",
    )
    failure_summary: str | None = Field(
        default=None,
        max_length=MAX_FAILURE_SUMMARY_LENGTH,
        description="Bounded, redacted failure summary text (max 1000 chars)",
    )
    # The read response carries the already-redacted value from the database
    # as-is — redaction is applied once at the write-path API schema
    # (ExecutionBindingCreateRequest / ExecutionBindingUpdateRequest),
    # never re-applied here.


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
