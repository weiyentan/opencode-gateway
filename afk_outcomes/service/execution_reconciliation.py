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

    awx_job_id: int
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
    ) -> None:
        self._repository = repository
        self._awx_lookup = awx_lookup
        self._limit = max(1, limit)

    async def reconcile(
        self, *, max_age_seconds: int | None = None
    ) -> ReconciliationSummary:
        """Run one pass and return the per-binding results.

        Discovery is bounded by ``limit`` (oldest bindings first), so a
        large backlog is drained across repeated invocations rather than
        in one unbounded sweep.  Each binding is reconciled independently:
        an AWX lookup failure is recorded for that binding and the pass
        continues.

        ``max_age_seconds`` optionally filters discovery to bindings whose
        ``created_at`` is older than the given number of seconds.  When
        ``None`` (the default), all ``running`` bindings are considered.
        """
        bindings = await self._repository.list_running_execution_bindings(  # type: ignore[attr-defined]
            limit=self._limit,
            max_age_seconds=max_age_seconds,
        )
        # Bound concurrency to avoid holding a worker for a long sequential
        # sweep of AWX HTTP calls.  The semaphore limits simultaneous
        # in-flight AWX lookups + persistence writes.
        semaphore = asyncio.Semaphore(min(10, len(bindings) or 1))

        async def _bounded_reconcile_one(b: object) -> BindingReconciliationResult:
            async with semaphore:
                return await self._reconcile_one(b)

        results = await asyncio.gather(
            *[_bounded_reconcile_one(b) for b in bindings]
        )
        summary = ReconciliationSummary(examined=len(bindings), results=list(results))
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

    async def _reconcile_one(self, binding: object) -> BindingReconciliationResult:
        """Reconcile one ``running`` binding against its AWX job status."""
        try:
            awx_job = getattr(binding, "awx_job")
            job_id = int(awx_job.job_id)
        except (AttributeError, TypeError, ValueError) as exc:
            return BindingReconciliationResult(
                awx_job_id=-1,
                kind=BindingResultKind.LOOKUP_ERROR,
                detail=type(exc).__name__,
            )

        try:
            state = await self._awx_lookup.get_job(job_id)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            # Credential redaction: only the exception class name is
            # recorded — never str(exc), which could carry auth material
            # from the HTTP layer.
            return BindingReconciliationResult(
                awx_job_id=job_id,
                kind=BindingResultKind.LOOKUP_ERROR,
                detail=type(exc).__name__,
            )

        if state is None:
            # AWX knows no such job — graceful skip, no crash, no mutation.
            return BindingReconciliationResult(
                awx_job_id=job_id,
                kind=BindingResultKind.MISSING_JOB,
            )

        outcome = map_awx_status_to_outcome(state.status)
        if outcome is None:
            # The job is still active (or an unknown future status) — the
            # binding legitimately stays running.
            return BindingReconciliationResult(
                awx_job_id=job_id,
                kind=BindingResultKind.NOT_YET_TERMINAL,
            )

        # Persist through the existing terminal-update path: serialized,
        # history-preserving, idempotent — terminal rows are never mutated
        # and conflicting re-observations are rejected as conflicts.
        try:
            update = await self._repository.update_execution_binding_terminal(  # type: ignore[attr-defined]
                awx_job_id=str(job_id),
                outcome=outcome,
                finished_at=state.finished_at,
            )
        except Exception as exc:  # noqa: BLE001 - persistence isolation
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
            # The binding disappeared between discovery and update —
            # nothing to persist.
            kind = BindingResultKind.BINDING_MISSING
        else:
            # Idempotent replay of an identical terminal record (no flags
            # set) — a no-op, not an error.
            kind = BindingResultKind.ALREADY_TERMINAL
        return BindingReconciliationResult(
            awx_job_id=job_id, kind=kind, outcome=outcome
        )


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
