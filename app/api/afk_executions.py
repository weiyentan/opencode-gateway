"""Execution-binding REST API (issue #549).

Three endpoints under the versioned namespace expose the execution-binding
write and read paths:

- ``POST /executions``   — persist one final execution binding from AWX.
  Idempotent by AWX job identity; conflicting data returns 409.
- ``GET /executions/{awx_job_id}`` — return one binding by AWX job ID, or 404.
- ``GET /executions``    — list bindings filtered by provider, repository URL,
  entity type, and entity number.  Full history including failed attempts and
  later successful retries in deterministic order.

All responses use the ``{status, data, error}`` envelope and are protected
by the global :class:`~app.core.auth.ApiKeyMiddleware`.  The write path
additionally requires a collector credential via
:func:`~app.core.auth.require_collector_token` — and the credential must be
attributable to the dedicated AWX execution-binding integration client
(``AWX_EXECUTION_BINDING_CLIENT_NAME``), never the usage collector
(``opencode-collector``) or any other client (issue #550).

Write semantics (ADR 0024):

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
from app.core.auth import require_collector_token
from app.core.config import get_settings
from app.core.schemas.execution_binding import (
    ExecutionBindingCreateRequest,
    ExecutionBindingHistoryResponse,
    ExecutionBindingReadResponse,
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


def _binding_conflicts_with(
    existing: ExecutionBinding,
    body: ExecutionBindingCreateRequest,
) -> bool:
    """Return True when ANY field of ``body`` differs from ``existing``.

    Every persisted field participates in the comparison — external session
    id, AWX job template id, the full resource identity (including
    repository), outcome, and optional traceability metadata.  A replay that
    changes any value is a 409 conflict, never a silent accept of stale
    data (issue #549 review).
    """
    resource = body.resource
    return (
        existing.external_session_id != body.external_session_id
        or existing.awx_job.job_template_id != body.awx_job.job_template_id
        or existing.resource.provider != resource.provider
        or existing.resource.repository != resource.repository
        or existing.resource.resource_type.value != resource.resource_type
        or existing.resource.resource_number != resource.resource_number
        or existing.outcome != body.outcome
        or existing.source_event_id != body.source_event_id
        or existing.branch != body.branch
        or existing.title != body.title
        or existing.failure_reason != body.failure_reason
        or existing.started_at != body.started_at
        or existing.finished_at != body.finished_at
    )


def _binding_to_read_response(binding: object) -> ExecutionBindingReadResponse:
    """Convert a domain :class:`ExecutionBinding` to the API read response.

    The ``binding_id`` is the UUID primary key from the ``execution_bindings``
    table, used as the gateway-assigned identifier.  ``external_session_id``
    passes through as ``None`` for unresolved bindings (the read schema and
    domain model both accept ``None``).
    """
    # Import here to avoid circular imports at module level.
    from afk_outcomes.models import ExecutionBinding as ExecutionBindingModel

    assert isinstance(binding, ExecutionBindingModel)
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
        ),
        outcome=binding.outcome,
        source_event_id=binding.source_event_id,
        branch=binding.branch,
        title=binding.title,
        started_at=binding.started_at,
        finished_at=binding.finished_at,
        failure_reason=binding.failure_reason,
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
    """Persist one final execution binding from AWX.

    **Idempotency**: repeating an identical POST (same ``awx_job_id`` and same
    data) returns 200 with the existing binding without inserting a duplicate
    row.

    **Conflict**: different data for the same ``awx_job_id`` returns 409
    Conflict without mutating stored data.

    **Multiple jobs per resource**: different AWX jobs targeting the same
    GitHub pull request or GitLab merge request are both persisted.
    """
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        awx_job_id_str = _validate_awx_job_id(body.awx_job.job_id)

        # Check if a binding already exists for this AWX job ID.
        existing = await repo.get_execution_binding_by_awx_job_id(awx_job_id_str)

        if existing is not None:
            # Conflict check: ANY differing field — session, resource identity,
            # outcome, or traceability metadata — rejects the replay.
            if _binding_conflicts_with(existing, body):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Conflicting data for existing AWX job binding"
                )
            # Idempotent replay: same data → 200 with existing binding.
            response.status_code = status.HTTP_200_OK
            return _binding_to_read_response(existing)

        # New binding — persist it.
        domain_binding = ExecutionBinding(
            binding_id="",
            awx_job=body.awx_job,
            external_session_id=body.external_session_id,
            resource=body.resource.to_provider_resource_identity(),
            outcome=body.outcome,
            source_event_id=body.source_event_id,
            branch=body.branch,
            title=body.title,
            started_at=body.started_at,
            finished_at=body.finished_at,
            failure_reason=body.failure_reason,
        )

        async with timed_operation("db.insert.execution_binding", "db"):
            async with _db_timeout(
                "db.insert.execution_binding", settings.database_timeout_seconds
            ):
                await repo.save_execution_binding(domain_binding)

        # Re-read to get the gateway-assigned binding_id.
        saved = await repo.get_execution_binding_by_awx_job_id(awx_job_id_str)
        if saved is None:
            # Should not happen — save succeeded — but handle gracefully.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve saved execution binding",
            )
        response.status_code = status.HTTP_201_CREATED
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
                    repository=repository_url,
                    resource_type=EntityType(entity_type),
                    resource_number=entity_number,
                )

    resource = ProviderResourceIdentity(
        provider=Provider(provider),
        repository=repository_url,
        resource_type=EntityType(entity_type),
        resource_number=entity_number,
    )
    return ExecutionBindingHistoryResponse(
        resource=resource,
        bindings=[_binding_to_read_response(b) for b in bindings],
    )
