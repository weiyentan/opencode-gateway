"""Execution-binding and provisional AFK lifecycle REST API (issues #549, #589, #590).

Execution-binding endpoints (``/api/v1/afk/executions``):

- ``POST /executions``   — persist one execution binding from AWX.
  Two-phase lifecycle (issue #590): ``outcome="running"`` provisions the
  execution at AWX start (attached to a pre-provisioned ``afk_run_id``,
  optional change request/session); terminal outcomes keep the legacy
  final-callback flow.  Idempotent by AWX job identity; conflicting data
  returns 409.
- ``PATCH /executions/{awx_job_id}`` — transition the same row from
  ``running`` to a terminal outcome (``completed``/``failed``/``cancelled``).
  Idempotent identical replay returns 200; conflicting payloads return 409
  without overwriting history; failed/cancelled persist without a change
  request or session.
- ``GET /executions/{awx_job_id}`` — return one binding by AWX job ID, or 404.
- ``GET /executions``    — list bindings filtered by provider, repository URL,
  entity type, and entity number.  Full history including failed attempts and
  later successful retries in deterministic order.

Provisional AFK run lifecycle endpoints (mounted on this router as
``/runs`` — i.e. ``/api/v1/afk/executions/runs``, issue #589):

- ``POST /runs`` — provision one provisional lifecycle.  Idempotent on
  ``provider + host + source_event_id``; a conflicting replay returns 409.
  ``recovered_from_afk_run_id`` provisions a recovery lifecycle without
  mutating its predecessor.
- ``POST /runs/{afk_run_id}/change-request`` — bind one change request to a
  lifecycle.  Idempotent per lifecycle (the 1:1 lifecycle<->change_request
  invariant); conflicts return 409.
- ``GET /runs/by-change-request`` — resolve a provider-qualified
  change-request identity (GitHub PR / GitLab MR) to its owning
  ``afk_run_id`` via the explicit durable binding on ``afk_runs``.
  Read-only (API-key auth only, no collector token); 400 for invalid
  identity, 404 for unknown/unbound, 409 for an impossible ownership
  conflict (issue #597).

All responses use the ``{status, data, error}`` envelope and are protected
by the global :class:`~app.core.auth.ApiKeyMiddleware`.  The write paths
additionally require a collector credential via
:func:`~app.core.auth.require_collector_token` — and the credential must be
attributable to the dedicated AWX execution-binding integration client
(``AWX_EXECUTION_BINDING_CLIENT_NAME``), never the usage collector
(``opencode-collector``) or any other client (issue #550).

Write semantics (ADR 0024; issue #589):

* **Idempotent by AWX job identity** — repeating an identical POST is a no-op
  (200 with the existing binding, no duplicate row).
* **Conflict rejection** — different data for the same AWX job ID returns
  409 Conflict without mutating stored data.
* **Multiple jobs per resource** — different AWX jobs targeting the same
  GitHub pull request or GitLab merge request are both persisted.
* **No sensitive data** — raw tokens, stdout, prompts, and arbitrary AWX
  payloads are never persisted or returned.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from afk_outcomes.models import (
    EntityType,
    ExecutionBinding,
    Provider,
    ProviderResourceIdentity,
)
from afk_outcomes.repository import AsyncpgOutcomeRepository
from afk_outcomes.serialization import MonotonicULID
from app.core.auth import require_collector_token
from app.core.config import get_settings
from app.core.repository import normalize_repository_url
from app.core.schemas.afk_lifecycle import (
    AFKRunLifecycleResponse,
    AFKRunProvisionRequest,
    ChangeRequestBindingRequest,
    ChangeRequestLookupResponse,
)
from app.core.schemas.execution_binding import (
    ExecutionBindingCreateRequest,
    ExecutionBindingHistoryResponse,
    ExecutionBindingReadResponse,
    ExecutionBindingUpdateRequest,
)
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["afk-executions"])

# ── Dedicated AWX integration credential contract (issue #550) ──────────────

# The write path accepts ONLY collector credentials attributable to this
# client.  Provision it through the existing admin clients API
# (``POST /admin/clients``) with this exact name, then register the AWX
# integration's bearer-token hash as one of its collector credentials —
# never reuse the usage collector's (``opencode-collector``) credential or
# treat the Admin API Key alone as sufficient.
AWX_EXECUTION_BINDING_CLIENT_NAME = "awx-execution-bindings"

# ── Valid enum filter values (locked domain vocabulary) ──────────────────────

_VALID_PROVIDERS = frozenset(m.value for m in Provider)
_VALID_ENTITY_TYPES = frozenset(m.value for m in EntityType)


# ── Helpers ──────────────────────────────────────────────────────────────────


async def require_awx_execution_binding_credential(
    auth: dict[str, str] = Depends(require_collector_token),
) -> dict[str, str]:
    """Collector-token gate for the execution-binding write path (issue #550).

    Composes the existing ``require_collector_token`` dependency — missing,
    malformed, empty, invalid, revoked, and inactive credentials are
    rejected with the same 401 behavior and error codes as ``/ingest`` —
    with a client-attribution check: the credential must belong to the
    dedicated AWX execution-binding integration client.  A valid
    credential owned by any other client (e.g. the usage collector
    ``opencode-collector``) is rejected with 403.

    Only the SHA-256 token hash is ever inspected; the raw bearer token is
    never persisted, returned, or logged.
    """
    if auth["client_name"] != AWX_EXECUTION_BINDING_CLIENT_NAME:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Credential is not attributable to the dedicated AWX "
                "execution-binding integration client"
            ),
        )
    return auth


def _validate_awx_job_id(awx_job_id: str) -> str:
    """Validate an AWX job id is a numeric string, else raise 400.

    The request/path schemas accept arbitrary strings while the repository
    coerces the id to ``int``.  Validating here turns a malformed id into a
    deterministic 400 instead of an unhandled ``ValueError`` surfacing as a
    500 (issue #549 review).
    """
    try:
        int(awx_job_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid AWX job id: {awx_job_id!r} must be a numeric string",
        )
    return awx_job_id


def _normalize_repository_or_400(repository: str) -> str:
    """Normalize a repository URL at the API boundary (issue #565).

    Uses the canonical :func:`app.core.repository.normalize_repository_url`
    helper — the same normalizer used by the AFK consumer, reporting
    aggregates, and closure projection.  Returns the normalized identity
    (host lowercased, scheme dropped, trailing slash/.git stripped) or
    raises a clear 400 when the identity is invalid (not an absolute
    HTTP(S) URL, missing hostname/path, etc.).
    """
    normalized = normalize_repository_url(repository)
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid repository identity: {repository!r} — "
                "must be an absolute HTTP(S) URL with a valid hostname and path"
            ),
        )
    return normalized


def _binding_conflicts_with(
    existing: ExecutionBinding,
    body: ExecutionBindingCreateRequest,
) -> bool:
    """Return True when ANY supplied field of ``body`` differs from ``existing``.

    Every explicitly-supplied persisted field participates in the
    comparison — AWX job template id, outcome, traceability metadata, and
    trigger type.  A replay that changes any value is a 409 conflict, never
    a silent accept of stale data (issue #549 review).

    Optional identity fields participate only when the caller supplied them
    (non-erasing, issue #590): an omitted resource, session, or
    ``afk_run_id`` never conflicts on the stored value, so a legacy replay
    that omits them never conflicts on the stored auto-created run
    (issue #595).
    """
    conflict = (
        existing.awx_job.job_template_id != body.awx_job.job_template_id
        or existing.outcome != body.outcome
        or existing.source_event_id != body.source_event_id
        or existing.branch != body.branch
        or existing.title != body.title
        or existing.failure_reason != body.failure_reason
        or existing.failure_summary != body.failure_summary
        or existing.started_at != body.started_at
        or existing.finished_at != body.finished_at
        or existing.trigger_type != body.trigger_type.value
    )
    if conflict:
        return True
    if (
        body.external_session_id is not None
        and existing.external_session_id != body.external_session_id
    ):
        return True
    if body.afk_run_id is not None and existing.afk_run_id != body.afk_run_id:
        return True
    resource = body.resource
    if resource is not None:
        if existing.resource is None:
            return True
        if (
            existing.resource.provider != resource.provider
            or existing.resource.repository != resource.repository
            or existing.resource.resource_type.value != resource.resource_type
            or existing.resource.resource_number != resource.resource_number
        ):
            return True
    return False


def _binding_to_read_response(binding: ExecutionBinding) -> ExecutionBindingReadResponse:
    """Convert a domain :class:`ExecutionBinding` to the API read response.

    The ``binding_id`` is the UUID primary key from the ``execution_bindings``
    table, used as the gateway-assigned identifier.  ``external_session_id``
    passes through as ``None`` for unresolved bindings (the read schema and
    domain model both accept ``None``).  ``afk_run_id`` and ``trigger_type``
    pass through as ``None`` for legacy rows without the columns.  ``resource``
    passes through as ``None`` when the binding carries no change-request
    identity (failed/cancelled executions, issue #590).
    """
    resource = binding.resource
    return ExecutionBindingReadResponse(
        binding_id=binding.binding_id,
        awx_job=binding.awx_job,
        external_session_id=binding.external_session_id,
        resource=ProviderResourceIdentity(
            provider=resource.provider,
            repository=resource.repository,
            resource_type=resource.resource_type,
            resource_number=resource.resource_number,
        )
        if resource is not None
        else None,
        outcome=binding.outcome,
        source_event_id=binding.source_event_id,
        afk_run_id=binding.afk_run_id,
        trigger_type=binding.trigger_type,
        branch=binding.branch,
        title=binding.title,
        started_at=binding.started_at,
        finished_at=binding.finished_at,
        failure_reason=binding.failure_reason,
        failure_summary=binding.failure_summary,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions — write path
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "",
    response_model=ExecutionBindingReadResponse,
    responses={
        status.HTTP_200_OK: {
            "description": "Idempotent replay — existing binding returned unchanged"
        },
        status.HTTP_201_CREATED: {"description": "New execution binding persisted"},
    },
)
async def create_execution_binding(
    body: ExecutionBindingCreateRequest,
    request: Request,
    response: Response,
    auth: dict = Depends(require_awx_execution_binding_credential),
    conn: asyncpg.Connection = Depends(get_session),
) -> ExecutionBindingReadResponse:
    """Persist one execution binding from AWX.

    **Two-phase lifecycle (issue #590)**: ``outcome="running"`` provisions
    the execution at AWX start, attached to the pre-provisioned lifecycle
    named by ``afk_run_id`` (required for ``running``); the change request
    and session are optional and may still be unknown.  Terminal outcomes
    (``completed`` / ``failed`` / ``cancelled``) keep the legacy
    final-callback flow — failed/cancelled executions persist without a
    change request or a session (the schema then requires ``afk_run_id``
    when no resource is supplied).

    **Atomic idempotency**: the INSERT is the linearisation point.
    ``UNIQUE (awx_job_id)`` is enforced by the database and
    ``ON CONFLICT DO NOTHING RETURNING id`` lets us distinguish a
    genuinely-new insert from a conflict-skip in a single round-trip.

    * Insert succeeded → ``201 Created`` with the new binding.
    * Insert conflict → fetch existing, compare fields:
      - Identical data → ``200 OK`` (idempotent replay, no mutation).
      - Different data → ``409 Conflict`` (no mutation).

    **Multiple jobs per resource**: different AWX jobs targeting the same
    GitHub pull request or GitLab merge request are both persisted.

    **Lifecycle multiplicity (issue #595)**: an optional ``afk_run_id``
    attaches the binding to a pre-provisioned lifecycle — many execution
    bindings (a failed attempt and a later retry with a new ``awx_job_id``)
    can reference one ``afk_run_id``.  A supplied ``afk_run_id`` that
    references no provisioned lifecycle is rejected with ``404``; omitting
    it auto-provisions a run with the binding — and when the canonical PR/MR
    already owns a lifecycle, that existing lifecycle is reused and the new
    binding attaches to it with ``201`` (PR #600 blocker, no second
    lifecycle).
    """
    resource = body.resource
    # Normalize repository URL at the API boundary before any persistence
    # or conflict comparison (issue #565).  Invalid identities surface as
    # a clear 400 without touching the database.  A resource-less payload
    # (two-phase provisioning / failed execution, issue #590) skips this.
    normalized_repo = None
    if resource is not None:
        normalized_repo = _normalize_repository_or_400(resource.repository)
        # Mutate the validated body so conflict comparison and persistence use
        # the canonical identity; provider-native types are already normalized
        # to change_request by the schema's model_validator.
        resource.repository = normalized_repo

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        awx_job_id_str = _validate_awx_job_id(body.awx_job.job_id)

        # Parse trigger_type from the request body for persistence.
        trigger_type_value: str | None = body.trigger_type.value

        # Transactional creation — attaches to the pre-provisioned lifecycle
        # when afk_run_id is supplied, else inserts afk_runs +
        # execution_bindings atomically (reusing the existing canonical
        # lifecycle when one already owns the PR/MR — PR #600 blocker).
        # Returns is_created/is_reused (201), is_conflict (409),
        # run_missing (404), or idempotent replay (200).
        async with timed_operation("db.insert.execution_binding", "db"):
            async with _db_timeout(
                "db.insert.execution_binding", settings.database_timeout_seconds
            ):
                result = await repo.create_or_replay_afk_execution_binding(
                    awx_job_id=awx_job_id_str,
                    job_template_id=body.awx_job.job_template_id,
                    provider=resource.provider if resource is not None else None,
                    repository=normalized_repo,
                    resource_number=(
                        resource.resource_number if resource is not None else None
                    ),
                    external_session_id=body.external_session_id,
                    outcome=body.outcome,
                    source_event_id=body.source_event_id,
                    branch=body.branch,
                    title=body.title,
                    failure_reason=body.failure_reason,
                    failure_summary=body.failure_summary,
                    started_at=body.started_at,
                    finished_at=body.finished_at,
                    trigger_type=trigger_type_value,
                    afk_run_id=body.afk_run_id,
                    ulid_source=MonotonicULID(),
                )

        if result.run_missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AFK run not found: {body.afk_run_id}",
            )

        if result.is_created or result.is_reused:
            # New binding was inserted — either attached to a fresh
            # auto-provisioned lifecycle (is_created) or to an existing
            # lifecycle the canonical PR/MR already owned (is_reused,
            # PR #600 blocker).  Both surface as 201.
            saved = await repo.get_execution_binding_by_awx_job_id(awx_job_id_str)
            if saved is None:
                # Should not happen — save succeeded — but handle gracefully.
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to retrieve saved execution binding",
                )
            response.status_code = status.HTTP_201_CREATED
            return _binding_to_read_response(saved)

        if result.is_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflicting data for existing AWX job binding",
            )

        # Idempotent replay — the repository compared a subset of fields
        # (outcome, title, branch, failure_reason, failure_summary,
        # source_event_id, external_session_id).  Additional fields (resource
        # identity, trigger_type, job_template_id) may still differ; check
        # with the full comparison helper to catch those as 409 conflicts.
        existing = await repo.get_execution_binding_by_awx_job_id(awx_job_id_str)
        if existing is None:
            # Should not happen — the operation returned a result, so a row
            # must exist.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Execution binding disappeared after idempotent replay",
            )

        if _binding_conflicts_with(existing, body):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflicting data for existing AWX job binding",
            )

        response.status_code = status.HTTP_200_OK
        return _binding_to_read_response(existing)


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH /api/v1/afk/executions/{awx_job_id} — terminal update
# ═══════════════════════════════════════════════════════════════════════════


@router.patch(
    "/{awx_job_id}",
    response_model=ExecutionBindingReadResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Binding updated to the terminal outcome, or idempotent "
                "replay of an identical terminal update"
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "No binding for this AWX job"},
        status.HTTP_409_CONFLICT: {
            "description": "Conflicting data for the existing binding"
        },
    },
)
async def update_execution_binding(
    awx_job_id: str,
    body: ExecutionBindingUpdateRequest,
    request: Request,
    auth: dict = Depends(require_awx_execution_binding_credential),
    conn: asyncpg.Connection = Depends(get_session),
) -> ExecutionBindingReadResponse:
    """Transition one binding from ``running`` to a terminal outcome (issue #590).

    The terminal update is the second phase of the two-phase lifecycle: it
    updates the same ``execution_bindings`` row provisioned at AWX start.

    **Serialized, history-preserving** — the row is locked with
    ``SELECT ... FOR UPDATE`` inside the repository transaction, so
    concurrent updates are serialized.  An already-terminal row is never
    mutated:

    * Identical terminal replay → ``200 OK`` (idempotent, no mutation).
    * Conflicting payload → ``409 Conflict`` (history is never overwritten).
    * Unknown AWX job → ``404``.

    **Failed/cancelled without a change request or session** — every field
    except ``outcome`` is optional.  ``external_session_id`` and ``resource``
    are non-erasing fill-ins: omitted fields leave stored values untouched,
    supplied fields fill stored NULLs, and supplied fields contradicting a
    stored value are a 409 conflict.
    """
    awx_job_id = _validate_awx_job_id(awx_job_id)

    resource = body.resource
    normalized_repo = None
    if resource is not None:
        normalized_repo = _normalize_repository_or_400(resource.repository)
        resource.repository = normalized_repo

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.update.execution_binding.terminal", "db"):
            async with _db_timeout(
                "db.update.execution_binding.terminal",
                settings.database_timeout_seconds,
            ):
                result = await repo.update_execution_binding_terminal(
                    awx_job_id=awx_job_id,
                    outcome=body.outcome,
                    finished_at=body.finished_at,
                    failure_reason=body.failure_reason,
                    failure_summary=body.failure_summary,
                    external_session_id=body.external_session_id,
                    provider=resource.provider if resource is not None else None,
                    repository=normalized_repo,
                    resource_number=(
                        resource.resource_number if resource is not None else None
                    ),
                )

    if result.not_found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution binding not found for AWX job: {awx_job_id}",
        )
    if result.is_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflicting data for existing AWX job binding",
        )

    saved = await repo.get_execution_binding_by_awx_job_id(awx_job_id)
    if saved is None:
        # Should not happen — the update returned a result, so a row exists.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Execution binding disappeared after terminal update",
        )
    return _binding_to_read_response(saved)


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions/{awx_job_id} — single-binding read
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/{awx_job_id}", response_model=ExecutionBindingReadResponse)
async def get_execution_binding(
    request: Request,
    awx_job_id: str,
    conn: asyncpg.Connection = Depends(get_session),
) -> ExecutionBindingReadResponse:
    """Return one execution binding by AWX job ID, or 404 when not found.

    A non-numeric ``awx_job_id`` is rejected with 400 before any database
    access (the path schema accepts arbitrary strings).
    """
    awx_job_id = _validate_awx_job_id(awx_job_id)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.query.execution_binding.by_awx_job_id", "db"):
            async with _db_timeout(
                "db.query.execution_binding.by_awx_job_id",
                settings.database_timeout_seconds,
            ):
                binding = await repo.get_execution_binding_by_awx_job_id(awx_job_id)

    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution binding not found for AWX job: {awx_job_id}",
        )
    return _binding_to_read_response(binding)


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions — resource history (filtered)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("", response_model=ExecutionBindingHistoryResponse)
async def list_execution_bindings(
    request: Request,
    provider: str = Query(..., description="Source provider: github | gitlab"),
    repository_url: str = Query(..., description="Full owner/repo name"),
    entity_type: str = Query(
        ...,
        description=(
            "Canonical entity type: issue, change_request, commit, review, "
            "merge_event"
        ),
    ),
    entity_number: str = Query(..., description="Provider-scoped external id"),
    conn: asyncpg.Connection = Depends(get_session),
) -> ExecutionBindingHistoryResponse:
    """List all execution bindings for a provider resource.

    Returns the full failed-to-successful execution history in deterministic
    order (earliest first).  Different AWX jobs targeting the same GitHub pull
    request or GitLab merge request are both returned.
    """
    # Validate enum values.
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid provider: {provider!r}. "
                f"Valid values: {', '.join(sorted(_VALID_PROVIDERS))}"
            ),
        )
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid entity_type: {entity_type!r}. "
                f"Valid values: {', '.join(sorted(_VALID_ENTITY_TYPES))}"
            ),
        )
    # Execution bindings only target change requests (GitHub PRs / GitLab MRs).
    if entity_type != EntityType.CHANGE_REQUEST.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid entity_type: {entity_type!r}. "
                f"Execution bindings only accept 'change_request'"
            ),
        )

    # Normalize repository URL at the API boundary before querying (issue #565).
    normalized_repository_url = _normalize_repository_or_400(repository_url)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.query.execution_bindings.for_resource", "db"):
            async with _db_timeout(
                "db.query.execution_bindings.for_resource",
                settings.database_timeout_seconds,
            ):
                bindings = await repo.list_execution_bindings_for_resource(
                    provider=Provider(provider),
                    repository=normalized_repository_url,
                    resource_type=EntityType(entity_type),
                    resource_number=entity_number,
                )

    resource = ProviderResourceIdentity(
        provider=Provider(provider),
        repository=normalized_repository_url,
        resource_type=EntityType(entity_type),
        resource_number=entity_number,
    )
    return ExecutionBindingHistoryResponse(
        resource=resource,
        bindings=[_binding_to_read_response(b) for b in bindings],
    )


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions/runs — provisional lifecycle provisioning
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/runs",
    response_model=AFKRunLifecycleResponse,
    responses={
        status.HTTP_200_OK: {
            "description": "Idempotent replay — existing lifecycle returned unchanged"
        },
        status.HTTP_201_CREATED: {"description": "New provisional lifecycle provisioned"},
    },
)
async def provision_afk_run_lifecycle(
    body: AFKRunProvisionRequest,
    request: Request,
    response: Response,
    auth: dict = Depends(require_awx_execution_binding_credential),
    conn: asyncpg.Connection = Depends(get_session),
) -> AFKRunLifecycleResponse:
    """Provision one provisional AFK run lifecycle (issues #589, #595).

    **Idempotent provisioning** — keyed on ``provider + host +
    source_event_id``, enforced by the partial unique index
    ``uq_afk_runs_provisioning_key`` (migration 0039):

    * New key → ``201 Created`` with the new lifecycle (status ``pending``).
    * Identical replay → ``200 OK`` with the existing lifecycle unchanged.
    * Conflicting replay → ``409 Conflict`` (no mutation).

    **Batch provenance (issue #595)** — ``deliveries`` is the ordered list
    of contributing delivery identities of the accepted webhook batch; the
    first element is the first triggering delivery, stored on the run as
    ``first_delivery_id``, and every identity is stored as a batch record.
    Batch provenance is non-erasing: a replay with a different or omitted
    batch is a conflict, never an erasure.  Provisioning without a batch
    (legacy behavior) simply carries no batch provenance.

    **Recovery** — ``recovered_from_afk_run_id`` provisions a recovery
    lifecycle that references its predecessor without mutating it; a
    missing predecessor returns ``404``.
    """
    # Normalize the repository identity at the API boundary before any
    # persistence or conflict comparison (same helper as execution bindings).
    normalized_repo = _normalize_repository_or_400(body.repository)
    body.repository = normalized_repo

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.insert.provisional_afk_run", "db"):
            async with _db_timeout(
                "db.insert.provisional_afk_run", settings.database_timeout_seconds
            ):
                result = await repo.provision_afk_run(
                    provider=body.provider,
                    host=body.host,
                    source_event_id=body.source_event_id,
                    repository=normalized_repo,
                    trigger_type=body.trigger_type,
                    title=body.title,
                    recovered_from_afk_run_id=body.recovered_from_afk_run_id,
                    deliveries=body.deliveries,
                    ulid_source=MonotonicULID(),
                )

    if result.predecessor_missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Recovered-from AFK run not found: "
                f"{body.recovered_from_afk_run_id}"
            ),
        )
    if result.is_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflicting data for existing provisional lifecycle",
        )

    lifecycle = await repo.get_afk_run_lifecycle(result.afk_run_id)
    if lifecycle is None:
        # Should not happen — provisioning returned a result, so a row exists.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve provisioned lifecycle",
        )
    first_delivery_id, delivery_ids = await repo.get_afk_run_batch_provenance(
        result.afk_run_id
    )

    if result.is_created:
        response.status_code = status.HTTP_201_CREATED
    return AFKRunLifecycleResponse.from_domain(
        lifecycle,
        first_delivery_id=first_delivery_id,
        delivery_ids=delivery_ids,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/afk/executions/runs/{afk_run_id}/change-request — binding
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/runs/{afk_run_id}/change-request",
    response_model=AFKRunLifecycleResponse,
)
async def bind_lifecycle_change_request(
    afk_run_id: str,
    body: ChangeRequestBindingRequest,
    request: Request,
    auth: dict = Depends(require_awx_execution_binding_credential),
    conn: asyncpg.Connection = Depends(get_session),
) -> AFKRunLifecycleResponse:
    """Bind one change request to a provisional lifecycle (issue #589).

    **Idempotent and available before review processing** — the binding
    never depends on the correlation engine:

    * Unbound lifecycle → the change request is bound (``200 OK``).
    * Identical replay → ``200 OK`` unchanged (no mutation).
    * Different change request already bound to this lifecycle, or the
      requested change request already belongs to another lifecycle (the
      1:1 invariant) → ``409 Conflict`` (no mutation).
    * Unknown lifecycle → ``404``.
    """
    # Normalize the repository identity at the API boundary (same helper as
    # execution bindings); GitHub PRs and GitLab MRs both bind under the
    # canonical change_request identity.
    normalized_repo = _normalize_repository_or_400(body.repository)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.update.afk_run_change_request_binding", "db"):
            async with _db_timeout(
                "db.update.afk_run_change_request_binding",
                settings.database_timeout_seconds,
            ):
                result = await repo.bind_change_request(
                    afk_run_id=afk_run_id,
                    provider=body.provider,
                    repository=normalized_repo,
                    external_id=body.external_id,
                )

    if result.run_missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provisional lifecycle not found: {afk_run_id}",
        )
    if result.is_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Change-request binding conflict: the lifecycle already "
                "carries a different change request, or the change request "
                "belongs to another lifecycle"
            ),
        )

    lifecycle = await repo.get_afk_run_lifecycle(afk_run_id)
    if lifecycle is None:
        # Should not happen — the bind returned a result, so a row exists.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve lifecycle after change-request binding",
        )
    return AFKRunLifecycleResponse.from_domain(lifecycle)


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/v1/afk/executions/runs/by-change-request — change-request lookup
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/runs/by-change-request",
    response_model=ChangeRequestLookupResponse,
)
async def get_run_by_change_request(
    request: Request,
    provider: str = Query(..., description="Source provider: github | gitlab"),
    repository: str = Query(..., description="Repository URL (normalized)"),
    external_id: str = Query(
        ...,
        description="Provider-scoped change-request id (PR/MR number as opaque string)",
    ),
    conn: asyncpg.Connection = Depends(get_session),
) -> ChangeRequestLookupResponse:
    """Resolve a provider-qualified change-request identity to its owning run.

    Follow-up GitHub PR / GitLab MR webhooks use this to continue the same
    durable AFK lifecycle that originally created and bound the change
    request (issue #597).  Read-only: queries only the explicit durable
    change-request binding on ``afk_runs`` and issues no writes.

    * ``200`` — the owning ``afk_run_id`` plus the canonical
      ``change_request`` identity.
    * ``400`` — invalid provider, repository, or change-request identity.
    * ``404`` — unknown or unbound PR/MR (no lifecycle owns it).
    * ``409`` — impossible ownership conflict (more than one lifecycle
      claims it).
    """
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid provider: {provider!r}. "
                f"Valid values: {', '.join(sorted(_VALID_PROVIDERS))}"
            ),
        )
    if not external_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid change-request identity: external_id must be a "
                "non-empty string"
            ),
        )
    # Normalize the repository identity at the API boundary (same helper as
    # the write paths); GitHub PRs and GitLab MRs both resolve under the
    # canonical change_request identity.
    normalized_repo = _normalize_repository_or_400(repository)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.query.afk_run.by_change_request", "db"):
            async with _db_timeout(
                "db.query.afk_run.by_change_request",
                settings.database_timeout_seconds,
            ):
                result = await repo.get_afk_run_by_change_request(
                    provider=Provider(provider),
                    repository=normalized_repo,
                    external_id=external_id,
                )

    if result.is_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Impossible ownership conflict: more than one lifecycle "
                "claims this change request"
            ),
        )
    if result.afk_run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Change request not found / unbound: "
                f"{provider} {normalized_repo} #{external_id}"
            ),
        )

    return ChangeRequestLookupResponse(
        afk_run_id=result.afk_run_id,
        change_request=ProviderResourceIdentity(
            provider=Provider(provider),
            repository=normalized_repo,
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number=external_id,
        ),
    )
