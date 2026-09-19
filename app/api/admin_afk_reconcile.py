"""Admin trigger for AFK AWX execution reconciliation (issue #637).

``POST /admin/afk-executions/reconcile``

Runs one bounded reconciliation pass over AFK AWX executions whose
bindings are stuck in the provisional ``running`` outcome because their
AWX job terminated without ever delivering a terminal callback — the job
failed, was cancelled, or died before the playbook could report.  For
each discovered binding the endpoint queries AWX for the job's terminal
status and persists the terminal outcome
(``completed`` / ``failed`` / ``cancelled``) through the **existing**
terminal-update path, which is serialized, history-preserving, and
idempotent.

Invariants:

* **No lifecycle authority** — reconciliation never changes the AFK Run
  lifecycle status and never marks a change request merged/closed; it
  only ever transitions individual execution outcomes.
* **Idempotent** — repeated runs never overwrite terminal execution
  history and never create duplicate records (terminal rows are not
  re-discovered; conflicting re-observations are reported as conflicts).
* **Graceful degradation** — a missing AWX job or a per-binding lookup
  failure is reported in the summary and never crashes the pass.  With
  the AWX integration unconfigured (``GATEWAY_AWX_RECONCILIATION_BASE_URL`` empty) the
  endpoint responds ``configured=false`` without touching the database.
* **No credential exposure** — the AWX token lives only in the outbound
  request ``Authorization`` header; lookup-failure detail records the
  exception class name only, and no credential ever appears in logs,
  error messages, or the response body.

Requires the Admin API Key (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).  The pass runs to completion
within the request — it never blocks or touches the AFK outcome consumer
ingestion path, and each terminal update is its own short DB transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from afk_outcomes.models import ExecutionOutcome
from afk_outcomes.repository import AsyncpgOutcomeRepository
from afk_outcomes.service.execution_reconciliation import (
    AWXHttpApi,
    AWXJobLookup,
    BindingReconciliationResult,
    ExecutionReconciler,
    ReconciliationSummary,
)
from app.core.config import get_settings
from app.db.session import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Response models ──────────────────────────────────────────────────────────


class AFKExecutionReconcileResultItem(BaseModel):
    """Per-binding reconciliation result (credential-free)."""

    awx_job_id: int | None = Field(default=None, description="AWX job id of the examined binding (None when malformed)")
    result: str = Field(
        description=(
            "Per-binding result kind: updated | already_terminal | "
            "missing_job | binding_missing | not_yet_terminal | conflict "
            "| lookup_error | persist_error"
        )
    )
    outcome: str | None = Field(
        default=None,
        description="Terminal outcome persisted (when one was persisted)",
    )
    detail: str | None = Field(
        default=None,
        description=(
            "Bounded, credential-free diagnostic (exception class name for "
            "lookup errors) — never an error message that could carry "
            "AWX credentials"
        ),
    )


class AFKExecutionReconcileResponse(BaseModel):
    """Summary of one reconciliation pass."""

    configured: bool = Field(
        default=True,
        description=(
            "False when the AWX integration is unconfigured "
            "(GATEWAY_AWX_RECONCILIATION_BASE_URL empty) — no discovery or writes occurred"
        ),
    )
    examined: int = Field(default=0, description="Running bindings examined")
    updated: int = Field(default=0, description="Bindings transitioned to a terminal outcome")
    already_terminal: int = Field(default=0, description="Idempotent terminal replays (no mutation)")
    missing_job: int = Field(default=0, description="AWX jobs that do not exist")
    binding_missing: int = Field(default=0, description="Bindings that disappeared before update")
    not_yet_terminal: int = Field(default=0, description="AWX jobs still active — binding stays running")
    conflicts: int = Field(default=0, description="Conflicting re-observations (history preserved)")
    lookup_errors: int = Field(default=0, description="Per-binding AWX lookup failures (pass continued)")
    persist_errors: int = Field(default=0, description="Per-binding persistence failures (pass continued)")
    results: list[AFKExecutionReconcileResultItem] = Field(default_factory=list)


# ── Seam providers (overridable in tests) ────────────────────────────────────


def _provide_repository(
    conn: asyncpg.Connection = Depends(get_session),
) -> AsyncpgOutcomeRepository:
    """Provide the outcome repository over the request's DB connection."""
    return AsyncpgOutcomeRepository(conn)


async def _provide_awx_lookup() -> AsyncIterator[AWXJobLookup | None]:
    """Provide the AWX job lookup, or ``None`` when unconfigured.

    The token is read from settings and passed only to the HTTP client —
    it is never logged or echoed.  An empty
    ``GATEWAY_AWX_RECONCILIATION_BASE_URL`` or an empty
    ``GATEWAY_AWX_RECONCILIATION_API_TOKEN`` disables reconciliation: the
    endpoint then reports ``configured=false`` and touches nothing.

    This is an async generator so that FastAPI runs the ``finally`` block
    (closing the underlying ``httpx.AsyncClient``) after the request.
    """
    settings = get_settings()
    if not settings.awx_reconciliation_base_url or not settings.awx_reconciliation_api_token:
        yield None
        return
    api = AWXHttpApi(
        base_url=settings.awx_reconciliation_base_url,
        token=settings.awx_reconciliation_api_token,
    )
    try:
        yield api
    finally:
        await api.aclose()


def _summary_to_response(
    summary: ReconciliationSummary, *, configured: bool = True
) -> AFKExecutionReconcileResponse:
    def _item(r: BindingReconciliationResult) -> AFKExecutionReconcileResultItem:
        outcome: str | None = None
        if isinstance(r.outcome, ExecutionOutcome):
            outcome = r.outcome.value
        return AFKExecutionReconcileResultItem(
            awx_job_id=r.awx_job_id,
            result=r.kind.value,
            outcome=outcome,
            detail=r.detail,
        )

    return AFKExecutionReconcileResponse(
        configured=configured,
        examined=summary.examined,
        updated=summary.updated_count,
        already_terminal=summary.already_terminal_count,
        missing_job=summary.missing_job_count,
        binding_missing=summary.binding_missing_count,
        not_yet_terminal=summary.not_yet_terminal_count,
        conflicts=summary.conflict_count,
        lookup_errors=summary.lookup_error_count,
        persist_errors=summary.persist_error_count,
        results=[_item(r) for r in summary.results],
    )


# ── Route ────────────────────────────────────────────────────────────────────


@router.post(
    "/afk-executions/reconcile",
    response_model=AFKExecutionReconcileResponse,
)
async def reconcile_afk_executions(
    repository: AsyncpgOutcomeRepository = Depends(_provide_repository),
    awx_lookup: AWXJobLookup | None = Depends(_provide_awx_lookup),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of running bindings examined in this pass",
    ),
    max_age_seconds: int | None = Query(
        default=None,
        ge=1,
        description="Optional maximum age in seconds — only examine bindings older than this",
    ),
) -> AFKExecutionReconcileResponse:
    """Run one bounded AFK AWX execution reconciliation pass.

    Discovers execution bindings stuck in ``running``, queries AWX for
    each job's terminal status, and persists terminal outcomes through the
    existing terminal-update path.  Missing AWX jobs, still-active jobs,
    and lookup failures are reported per binding without crashing the
    pass.  See the module docstring for the full invariants.
    """
    if awx_lookup is None:
        # Fail closed, gracefully: without AWX access nothing can be
        # reconciled — no discovery, no writes.
        return AFKExecutionReconcileResponse(configured=False)

    reconciler = ExecutionReconciler(
        repository=repository, awx_lookup=awx_lookup, limit=limit
    )
    summary = await reconciler.reconcile(max_age_seconds=max_age_seconds)
    return _summary_to_response(summary)
