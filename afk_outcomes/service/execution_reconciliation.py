"""AFK AWX execution reconciliation (issue #637).

Reconciles **AWX Execution Bindings** that are stuck in the provisional
``running`` **Execution Outcome** because their AWX job terminated without
ever delivering a terminal callback — the job failed, was cancelled, or
died before the playbook could report.  Without this pass those bindings
would stay ``running`` forever, misrepresenting individual execution
history.

The pass is deliberately narrow:

* **Discover** — read ``running`` bindings through the repository seam
  (``list_running_execution_bindings``).
* **Lookup** — query AWX for each binding's job status through the
  :class:`AWXJobLookup` seam; the production implementation
  (:class:`AWXHttpApi`) is a thin httpx client.
* **Persist** — map the AWX status onto a terminal
  :class:`~afk_outcomes.models.ExecutionOutcome` and transition the
  binding through the **existing** terminal-update path
  (``update_execution_binding_terminal``), which is already serialized,
  history-preserving, and idempotent.

The pass runs in **two phases** so it is safe on a single shared asyncpg
connection:

* **Phase 1 (parallel)** — AWX status lookups.  These are HTTP calls that
  never touch the database, so they run concurrently but are bounded by
  an ``asyncio.Semaphore`` to avoid overwhelming the AWX API during
  partial outages or latency spikes.
* **Phase 2 (serialized)** — terminal persistence.  Every task shares one
  connection, so writes are issued one at a time.

Invariants (issue #637 acceptance criteria):

* Reconciliation **never** changes the AFK Run lifecycle status and never
  marks a change request merged/closed — it only ever touches the two
  reconciliation seams (discovery + terminal update).  ``afk_runs.status``
  is not projected from child execution outcomes (ADR 0028), and the
  terminal-update path used here issues no run-status convergence.
* Repeated runs never overwrite terminal execution history and never
  create duplicate records — terminal rows are not re-discovered, and the
  update path rejects or no-ops any conflicting re-observation.
* A missing AWX job (the lookup returns ``None``) is handled gracefully —
  the binding is left untouched and the result is reported.
* AWX credentials are never logged, returned, or embedded in error
  detail — lookup failures record only the exception class name.
* Per-binding lookup failures are isolated: one unreachable job never
  aborts the pass.

This module is pure domain: it imports nothing from the application
package (``app``) — only stdlib + ``afk_outcomes`` + ``httpx``.
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from afk_outcomes.models import ExecutionOutcome

logger = logging.getLogger(__name__)


# ── AWX job-status seam ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AWXJobState:
    """The terminal-relevant slice of one AWX job's status.

    ``status`` is the native AWX job status string (e.g. ``successful``,
    ``failed``, ``canceled``, ``running``); ``finished_at`` is the parsed
    AWX ``finished`` timestamp when available.
    """

    status: str
    finished_at: datetime | None = None


class AWXJobLookup(Protocol):
    """Read seam for AWX job status.

    Returns the job's :class:`AWXJobState`, or ``None`` when AWX knows no
    such job (the job was deleted or the id never existed).  Implementations
    must never embed credentials in raised error messages beyond what the
    HTTP client itself controls; the reconciler records only the exception
    class name, so no credential can reach result detail or logs.
    """

    async def get_job(self, job_id: int) -> AWXJobState | None:
        """Return the AWX job state, or ``None`` when the job does not exist."""
        ...


def map_awx_status_to_outcome(status: str) -> ExecutionOutcome | None:
    """Map a native AWX job status onto a terminal Execution Outcome.

    ``successful`` → ``completed``; ``failed`` / ``error`` → ``failed``;
    ``canceled`` (AWX's single-l spelling) → ``cancelled``.  Any
    non-terminal status (``new``, ``pending``, ``waiting``, ``running``,
    or an unknown future value) maps to ``None`` — the binding stays
    ``running`` and the pass simply skips it.
    """
    normalized = (status or "").strip().lower()
    if normalized == "successful":
        return ExecutionOutcome.COMPLETED
    if normalized in {"failed", "error"}:
        return ExecutionOutcome.FAILED
    if normalized == "canceled":
        return ExecutionOutcome.CANCELLED
    return None


# ── Result vocabulary ────────────────────────────────────────────────────────


class BindingResultKind(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; keep importable on 3.9
    """Per-binding result of one reconciliation pass."""

    UPDATED = "updated"
    ALREADY_TERMINAL = "already_terminal"
    MISSING_JOB = "missing_job"
    BINDING_MISSING = "binding_missing"
    NOT_YET_TERMINAL = "not_yet_terminal"
    CONFLICT = "conflict"
    LOOKUP_ERROR = "lookup_error"
    PERSIST_ERROR = "persist_error"


@dataclass(frozen=True)
class BindingReconciliationResult:
    """Outcome of reconciling one execution binding.

    ``detail`` is a bounded, credential-free diagnostic (the exception
    class name for lookup errors) — never an error message that could
    carry AWX credentials.
    """

    awx_job_id: int | None
    kind: BindingResultKind
    outcome: ExecutionOutcome | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ReconciliationSummary:
    """Aggregate result of one reconciliation pass."""

    examined: int = 0
    results: list[BindingReconciliationResult] = field(default_factory=list)

    def _count(self, kind: BindingResultKind) -> int:
        return sum(1 for r in self.results if r.kind is kind)

    @property
    def updated_count(self) -> int:
        return self._count(BindingResultKind.UPDATED)

    @property
    def already_terminal_count(self) -> int:
        return self._count(BindingResultKind.ALREADY_TERMINAL)

    @property
    def missing_job_count(self) -> int:
        return self._count(BindingResultKind.MISSING_JOB)

    @property
    def binding_missing_count(self) -> int:
        return self._count(BindingResultKind.BINDING_MISSING)

    @property
    def not_yet_terminal_count(self) -> int:
        return self._count(BindingResultKind.NOT_YET_TERMINAL)

    @property
    def conflict_count(self) -> int:
        return self._count(BindingResultKind.CONFLICT)

    @property
    def lookup_error_count(self) -> int:
        return self._count(BindingResultKind.LOOKUP_ERROR)

    @property
    def persist_error_count(self) -> int:
        return self._count(BindingResultKind.PERSIST_ERROR)


# ── Reconciler ───────────────────────────────────────────────────────────────


class ExecutionReconciler:
    """One bounded reconciliation pass over ``running`` execution bindings.

    Composes the two repository seams —
    ``list_running_execution_bindings`` (discovery) and
    ``update_execution_binding_terminal`` (persistence) — with an
    :class:`AWXJobLookup`.  No other repository surface is touched, so the
    AFK Run lifecycle status and change-request merge/close semantics are
    structurally out of reach.
    """

    def __init__(
        self,
        *,
        repository: object,
        awx_lookup: AWXJobLookup,
        limit: int = 100,
        max_concurrency: int = 10,
    ) -> None:
        self._repository = repository
        self._awx_lookup = awx_lookup
        self._limit = max(1, limit)
        self._max_concurrency = max(1, max_concurrency)

    async def reconcile(
        self, *, max_age_seconds: int | None = None
    ) -> ReconciliationSummary:
        """Run one pass and return the per-binding results.

        Two-phase design for safe concurrent operation on a single asyncpg Connection:
        Phase 1 runs AWX lookups concurrently but bounded by a semaphore (HTTP only, no DB).
        Phase 2 serializes DB writes over the shared connection.
        """
        bindings = await self._repository.list_running_execution_bindings(  # type: ignore[attr-defined]
            limit=self._limit,
            max_age_seconds=max_age_seconds,
        )

        # Phase 1: Parallel AWX lookups (no DB needed — concurrent, but
        # bounded by the semaphore to avoid overwhelming the AWX API).
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _semaphored_lookup(binding: object) -> tuple[int | None, AWXJobState | None, str | None]:
            async with semaphore:
                return await self._lookup_job(binding)

        lookup_results = await asyncio.gather(
            *[_semaphored_lookup(b) for b in bindings]
        )

        # Phase 2: Serialized DB writes over the single shared connection.
        results: list[BindingReconciliationResult] = []
        for binding, (job_id, state, error_detail) in zip(bindings, lookup_results):
            if error_detail is not None and job_id is None:
                # Malformed binding — couldn't extract job_id at all.
                results.append(BindingReconciliationResult(
                    awx_job_id=None,
                    kind=BindingResultKind.LOOKUP_ERROR,
                    detail=error_detail,
                ))
            elif error_detail is not None:
                # AWX lookup failed — job_id known, but state unknown.
                results.append(BindingReconciliationResult(
                    awx_job_id=job_id,
                    kind=BindingResultKind.LOOKUP_ERROR,
                    detail=error_detail,
                ))
            elif state is None:
                # AWX knows no such job.
                results.append(BindingReconciliationResult(
                    awx_job_id=job_id,
                    kind=BindingResultKind.MISSING_JOB,
                ))
            else:
                # Got a valid state — persist (serialized on single connection).
                if job_id is None:
                    # Unreachable: a None job_id is only produced with
                    # error_detail set. Defensive for the type-checker.
                    results.append(BindingReconciliationResult(
                        awx_job_id=None,
                        kind=BindingResultKind.LOOKUP_ERROR,
                        detail="missing_job_id",
                    ))
                else:
                    result = await self._persist_result(binding, job_id, state)
                    results.append(result)

        summary = ReconciliationSummary(examined=len(bindings), results=results)
        logger.info(
            "AFK execution reconciliation pass: examined=%d updated=%d "
            "already_terminal=%d missing_job=%d binding_missing=%d "
            "not_yet_terminal=%d conflict=%d lookup_error=%d persist_error=%d",
            summary.examined,
            summary.updated_count,
            summary.already_terminal_count,
            summary.missing_job_count,
            summary.binding_missing_count,
            summary.not_yet_terminal_count,
            summary.conflict_count,
            summary.lookup_error_count,
            summary.persist_error_count,
        )
        return summary

    async def _lookup_job(self, binding: object) -> tuple[int | None, AWXJobState | None, str | None]:
        """Look up the AWX job status for one binding. Returns (job_id, state, error_detail).

        Does NOT touch the database — pure HTTP lookup.
        """
        try:
            awx_job = getattr(binding, "awx_job")
            job_id = int(awx_job.job_id)
        except (AttributeError, TypeError, ValueError) as exc:
            return None, None, type(exc).__name__

        try:
            state = await self._awx_lookup.get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            return job_id, None, type(exc).__name__

        return job_id, state, None

    async def _persist_result(
        self, binding: object, job_id: int, state: AWXJobState
    ) -> BindingReconciliationResult:
        """Persist the AWX lookup result for one binding. DB write only."""
        outcome = map_awx_status_to_outcome(state.status)
        if outcome is None:
            return BindingReconciliationResult(
                awx_job_id=job_id,
                kind=BindingResultKind.NOT_YET_TERMINAL,
            )

        try:
            update = await self._repository.update_execution_binding_terminal(  # type: ignore[attr-defined]
                awx_job_id=str(job_id),
                outcome=outcome,
                finished_at=state.finished_at,
            )
        except Exception as exc:  # noqa: BLE001
            return BindingReconciliationResult(
                awx_job_id=job_id,
                kind=BindingResultKind.PERSIST_ERROR,
                detail=type(exc).__name__,
            )

        if update.is_updated:
            kind = BindingResultKind.UPDATED
        elif update.is_conflict:
            kind = BindingResultKind.CONFLICT
        elif update.not_found:
            kind = BindingResultKind.BINDING_MISSING
        else:
            kind = BindingResultKind.ALREADY_TERMINAL
        return BindingReconciliationResult(awx_job_id=job_id, kind=kind, outcome=outcome)


# ── Production AWX HTTP client ───────────────────────────────────────────────


class AWXHttpApi:
    """:class:`AWXJobLookup` over httpx against the AWX REST API.

    Reads ``GET /api/v2/jobs/{id}/`` and maps the response onto
    :class:`AWXJobState`.  The bearer token is sent only in the
    ``Authorization`` header — it is never logged, persisted, or included
    in any error raised to the reconciler (httpx exceptions carry the URL
    and status, not the request headers).
    """

    def __init__(self, *, base_url: str, token: str, timeout: float = 10.0) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def get_job(self, job_id: int) -> AWXJobState | None:
        response = await self._client.get(f"/api/v2/jobs/{job_id}/")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        finished_raw = body.get("finished")
        finished_at: datetime | None = None
        if finished_raw:
            try:
                finished_at = datetime.fromisoformat(
                    str(finished_raw).replace("Z", "+00:00")
                )
            except ValueError:
                finished_at = None
        return AWXJobState(
            status=str(body.get("status") or ""),
            finished_at=finished_at,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
