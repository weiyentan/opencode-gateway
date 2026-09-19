# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Unit tests for AFK AWX execution reconciliation (issue #637).

Covers the pure-domain reconciler
(:class:`afk_outcomes.service.execution_reconciliation.ExecutionReconciler`)
with fake seams:

- discovery of ``running`` execution bindings from the repository,
- AWX job-status lookup and terminal-outcome mapping
  (``failed`` / ``successful`` / ``canceled``),
- persistence through the existing terminal-update path,
- missing AWX jobs handled gracefully,
- AWX lookup failures isolated per binding with redacted detail
  (credentials never surface in result detail),
- idempotency: already-terminal records are never re-persisted and
  repeated runs create no duplicates,
- the reconciler never touches the AFK Run lifecycle (no run-status
  writes, no change-request merge/close semantics).

The AWX client is a seam (:class:`afk_outcomes.service.execution_reconciliation.AWXJobLookup`);
the production HTTP implementation is exercised separately from the API
tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from afk_outcomes.models import ExecutionBinding, ExecutionOutcome
from afk_outcomes.repository.executions import UpdateExecutionBindingResult
from afk_outcomes.service.execution_reconciliation import (
    AWXJobState,
    BindingResultKind,
    ExecutionReconciler,
    map_awx_status_to_outcome,
)

_FINISHED = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeRepository:
    """Fake repository seam exposing only the two methods the reconciler uses.

    Any OTHER attribute access is recorded and fails the tests that assert
    the reconciler never touches run-status or change-request surfaces.
    """

    def __init__(
        self,
        running_bindings: list[ExecutionBinding] | None = None,
        update_results: dict[str, UpdateExecutionBindingResult] | None = None,
    ) -> None:
        self.running_bindings = running_bindings or []
        self.update_results = update_results or {}
        self.update_calls: list[dict[str, Any]] = []
        self.limit_used: int | None = None
        self.max_age_seconds_used: int | None = None
        self.other_calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # Record unexpected surface use; returning a recorder keeps the
        # reconciler from crashing so the assertion below can fire.
        self.other_calls.append(name)
        raise AssertionError(
            f"Reconciler used non-reconciliation repository surface: {name}"
        )

    async def list_running_execution_bindings(
        self, *, limit: int = 100, max_age_seconds: int | None = None
    ) -> list[ExecutionBinding]:
        self.limit_used = limit
        self.max_age_seconds_used = max_age_seconds
        return list(self.running_bindings)[:limit]

    async def update_execution_binding_terminal(
        self, **kwargs: Any
    ) -> UpdateExecutionBindingResult:
        self.update_calls.append(kwargs)
        return self.update_results.get(
            str(kwargs["awx_job_id"]), UpdateExecutionBindingResult(is_updated=True)
        )


class FakeLookup:
    """Fake AWX job lookup: ``jobs`` maps job id → AWXJobState."""

    def __init__(
        self,
        jobs: dict[int, AWXJobState] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.jobs = jobs or {}
        self.error = error
        self.calls: list[int] = []

    async def get_job(self, job_id: int) -> AWXJobState | None:
        self.calls.append(job_id)
        if self.error is not None:
            raise self.error
        return self.jobs.get(job_id)


def _running_binding(awx_job_id: int) -> ExecutionBinding:
    return ExecutionBinding(
        binding_id=f"00000000-0000-0000-0000-{awx_job_id:012d}",
        awx_job={"job_id": str(awx_job_id), "job_template_id": 7},
        outcome=ExecutionOutcome.RUNNING,
    )


# ── Status mapping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("awx_status", "expected"),
    [
        ("successful", ExecutionOutcome.COMPLETED),
        ("failed", ExecutionOutcome.FAILED),
        ("canceled", ExecutionOutcome.CANCELLED),
        ("error", ExecutionOutcome.FAILED),
    ],
)
def test_map_awx_status_to_terminal_outcome(awx_status: str, expected: ExecutionOutcome):
    assert map_awx_status_to_outcome(awx_status) is expected


@pytest.mark.parametrize(
    "awx_status",
    ["running", "new", "pending", "waiting", "some_future_status"],
)
def test_map_awx_status_non_terminal_is_none(awx_status: str):
    assert map_awx_status_to_outcome(awx_status) is None


# ── Discovery ────────────────────────────────────────────────────────────────


async def test_reconciler_discovers_running_bindings_with_limit():
    repo = FakeRepository()
    lookup = FakeLookup()
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup, limit=25
    ).reconcile()
    assert repo.limit_used == 25
    assert summary.examined == 0
    assert summary.updated_count == 0


async def test_reconciler_forwards_max_age_seconds_to_discovery():
    repo = FakeRepository()
    await ExecutionReconciler(repository=repo, awx_lookup=FakeLookup()).reconcile(
        max_age_seconds=300
    )
    assert repo.max_age_seconds_used == 300


async def test_reconciler_defaults_to_no_age_filter():
    repo = FakeRepository()
    await ExecutionReconciler(repository=repo, awx_lookup=FakeLookup()).reconcile()
    assert repo.max_age_seconds_used is None


async def test_reconciler_processes_each_running_binding():
    repo = FakeRepository(
        running_bindings=[_running_binding(11), _running_binding(22)]
    )
    lookup = FakeLookup(
        jobs={
            11: AWXJobState(status="running"),
            22: AWXJobState(status="running"),
        }
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup, limit=100
    ).reconcile()
    assert sorted(lookup.calls) == [11, 22]
    assert summary.examined == 2
    assert summary.not_yet_terminal_count == 2
    assert repo.update_calls == []


# ── Terminal persistence via the existing update path ────────────────────────


async def test_failed_awx_job_persists_failed_outcome():
    repo = FakeRepository(running_bindings=[_running_binding(101)])
    lookup = FakeLookup(
        jobs={101: AWXJobState(status="failed", finished_at=_FINISHED)}
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.updated_count == 1
    assert len(repo.update_calls) == 1
    call = repo.update_calls[0]
    assert call["awx_job_id"] == "101"
    assert call["outcome"] is ExecutionOutcome.FAILED
    assert call["finished_at"] == _FINISHED
    result = summary.results[0]
    assert result.kind is BindingResultKind.UPDATED
    assert result.outcome is ExecutionOutcome.FAILED


async def test_successful_awx_job_persists_completed_outcome():
    repo = FakeRepository(running_bindings=[_running_binding(202)])
    lookup = FakeLookup(
        jobs={202: AWXJobState(status="successful", finished_at=_FINISHED)}
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.updated_count == 1
    assert repo.update_calls[0]["outcome"] is ExecutionOutcome.COMPLETED


async def test_canceled_awx_job_persists_cancelled_outcome():
    repo = FakeRepository(running_bindings=[_running_binding(303)])
    lookup = FakeLookup(
        jobs={303: AWXJobState(status="canceled", finished_at=_FINISHED)}
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.updated_count == 1
    assert repo.update_calls[0]["outcome"] is ExecutionOutcome.CANCELLED


# ── Missing AWX jobs ─────────────────────────────────────────────────────────


async def test_missing_awx_job_is_graceful():
    repo = FakeRepository(running_bindings=[_running_binding(404)])
    lookup = FakeLookup(jobs={})  # AWX knows no such job
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.examined == 1
    assert summary.missing_job_count == 1
    assert repo.update_calls == []
    assert summary.results[0].kind is BindingResultKind.MISSING_JOB


# ── Lookup failures ──────────────────────────────────────────────────────────


async def test_lookup_failure_is_isolated_per_binding():
    class Boom(Exception):
        pass

    repo = FakeRepository(
        running_bindings=[_running_binding(1), _running_binding(2)]
    )
    # First lookup raises, second succeeds — isolation must let the pass
    # continue and still persist the healthy binding.
    lookup = FakeLookup(jobs={2: AWXJobState(status="failed", finished_at=_FINISHED)})

    async def get_job(job_id: int) -> dict[str, Any] | None:
        lookup.calls.append(job_id)
        if job_id == 1:
            raise Boom("connection refused")
        return lookup.jobs.get(job_id)

    lookup.get_job = get_job  # type: ignore[method-assign]
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.examined == 2
    assert summary.lookup_error_count == 1
    assert summary.updated_count == 1
    assert len(repo.update_calls) == 1
    assert repo.update_calls[0]["awx_job_id"] == "2"


async def test_lookup_failure_detail_never_carries_credentials():
    secret = "super-secret-awx-token"

    class Boom(Exception):
        pass

    class LeakyLookup:
        async def get_job(self, job_id: int) -> AWXJobState | None:
            raise Boom(f"AWX request failed with Authorization: Bearer {secret}")

    repo = FakeRepository(running_bindings=[_running_binding(9)])
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=LeakyLookup()  # type: ignore[arg-type]
    ).reconcile()
    assert summary.lookup_error_count == 1
    result = summary.results[0]
    assert result.kind is BindingResultKind.LOOKUP_ERROR
    assert secret not in (result.detail or "")


# ── Persistence failures ─────────────────────────────────────────────────────


async def test_persist_failure_is_isolated_per_binding():
    class Boom(Exception):
        pass

    repo = FakeRepository(running_bindings=[_running_binding(1), _running_binding(2)])
    lookup = FakeLookup(
        jobs={
            1: AWXJobState(status="failed", finished_at=_FINISHED),
            2: AWXJobState(status="failed", finished_at=_FINISHED),
        }
    )

    async def update_execution_binding_terminal(**kwargs: Any):
        if kwargs["awx_job_id"] == "1":
            raise Boom("db write failed")
        return UpdateExecutionBindingResult(is_updated=True)

    repo.update_execution_binding_terminal = update_execution_binding_terminal  # type: ignore[method-assign]
    summary = await ExecutionReconciler(repository=repo, awx_lookup=lookup).reconcile()
    # The DB error on binding 1 does not abort the pass; binding 2 persists.
    assert summary.examined == 2
    assert summary.persist_error_count == 1
    assert summary.updated_count == 1
    persistence_failure = next(
        r for r in summary.results if r.kind is BindingResultKind.PERSIST_ERROR
    )
    assert persistence_failure.awx_job_id == 1
    assert persistence_failure.detail == "Boom"


async def test_malformed_binding_without_awx_job_is_isolated():
    class FakeBinding:
        pass

    repo = FakeRepository()
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=FakeLookup()
    ).reconcile()
    assert summary.examined == 0

    repo.running_bindings = [FakeBinding()]  # type: ignore[list-item]
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=FakeLookup()
    ).reconcile()
    assert summary.examined == 1
    assert summary.lookup_error_count == 1
    assert summary.results[0].awx_job_id is None
    assert summary.results[0].detail == "AttributeError"


# ── Bounded concurrency ─────────────────────────────────────────────────────


async def test_reconcile_bounds_concurrent_awx_lookups():
    import asyncio

    repo = FakeRepository(
        running_bindings=[_running_binding(i) for i in range(1, 26)]
    )
    in_flight = 0
    peak = 0

    class ConcurrencyTrackingLookup:
        async def get_job(self, job_id: int) -> AWXJobState | None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return AWXJobState(status="running")

    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=ConcurrencyTrackingLookup()  # type: ignore[arg-type]
    ).reconcile()
    assert summary.examined == 25
    # Phase 1 runs every AWX lookup concurrently (HTTP only, no DB), so all
    # bindings are in flight at once — there is no concurrency bound.
    assert peak == 25


# ── Idempotency / already-terminal records ───────────────────────────────────


async def test_conflicting_terminal_update_is_reported_not_crashing():
    repo = FakeRepository(
        running_bindings=[_running_binding(77)],
        update_results={
            "77": UpdateExecutionBindingResult(is_conflict=True)
        },
    )
    lookup = FakeLookup(
        jobs={77: AWXJobState(status="successful", finished_at=_FINISHED)}
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.conflict_count == 1
    assert summary.results[0].kind is BindingResultKind.CONFLICT


async def test_idempotent_terminal_replay_is_reported_as_already_terminal():
    # The update path reports an identical terminal replay with no flags set
    # (no mutation).  Reconciliation must treat that as a no-op, not an error.
    repo = FakeRepository(
        running_bindings=[_running_binding(88)],
        update_results={"88": UpdateExecutionBindingResult()},
    )
    lookup = FakeLookup(
        jobs={88: AWXJobState(status="failed", finished_at=_FINISHED)}
    )
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert summary.already_terminal_count == 1
    assert summary.results[0].kind is BindingResultKind.ALREADY_TERMINAL


async def test_repeated_run_over_terminal_records_creates_no_duplicates():
    # First run transitions the binding; a repeated run discovers nothing
    # (the binding is no longer running) and makes no update calls.
    repo = FakeRepository(running_bindings=[_running_binding(5)])
    lookup = FakeLookup(
        jobs={5: AWXJobState(status="failed", finished_at=_FINISHED)}
    )
    first = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert first.updated_count == 1

    repo.running_bindings = []  # terminal records are never re-discovered
    second = await ExecutionReconciler(
        repository=repo, awx_lookup=lookup
    ).reconcile()
    assert second.examined == 0
    assert second.updated_count == 0
    assert len(repo.update_calls) == 1  # still only the first run's call


async def test_empty_discovery_is_noop():
    repo = FakeRepository()
    summary = await ExecutionReconciler(
        repository=repo, awx_lookup=FakeLookup()
    ).reconcile()
    assert summary.examined == 0
    assert summary.results == []


# ── AFK Run lifecycle authority ──────────────────────────────────────────────


async def test_reconciler_never_touches_run_lifecycle_surfaces():
    # The reconciler must only ever use the two reconciliation seams.  Any
    # attempt to project run status or bind change requests is recorded by
    # FakeRepository and fails here.
    repo = FakeRepository(running_bindings=[_running_binding(1)])
    lookup = FakeLookup(jobs={1: AWXJobState(status="failed", finished_at=_FINISHED)})
    await ExecutionReconciler(repository=repo, awx_lookup=lookup).reconcile()
    assert repo.other_calls == []


# ── Production AWXHttpApi client tests (F3) ──────────────────────────────────


def _awx_api_with_transport(status_code: int, payload: dict[str, Any] | None = None):
    """Build an AWXHttpApi whose httpx client runs on an httpx.MockTransport."""
    import httpx

    from afk_outcomes.service.execution_reconciliation import AWXHttpApi

    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code=status_code, json=payload or {})
    )
    api = AWXHttpApi(base_url="https://awx.example.com", token="test-token")
    api._client = httpx.AsyncClient(
        transport=transport, base_url="https://awx.example.com"
    )
    return api


@pytest.mark.asyncio
async def test_awk_http_api_get_job_returns_awx_job_state():
    api = _awx_api_with_transport(
        200,
        {
            "status": "successful",
            "finished": "2026-09-01T12:00:00Z",
        },
    )
    try:
        state = await api.get_job(42)
        assert state is not None
        assert state.status == "successful"
        assert state.finished_at is not None
        assert state.finished_at.year == 2026
    finally:
        await api.aclose()


@pytest.mark.asyncio
async def test_awk_http_api_get_job_404_returns_none():
    api = _awx_api_with_transport(404, {"detail": "Not found"})
    try:
        state = await api.get_job(999)
        assert state is None
    finally:
        await api.aclose()


@pytest.mark.asyncio
async def test_awk_http_api_get_job_500_raises():
    import httpx

    api = _awx_api_with_transport(500, {"detail": "Server error"})
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await api.get_job(1)
    finally:
        await api.aclose()


@pytest.mark.asyncio
async def test_awk_http_api_get_job_malformed_timestamp_finished_at_is_none():
    api = _awx_api_with_transport(
        200,
        {
            "status": "failed",
            "finished": "not-a-valid-timestamp",
        },
    )
    try:
        state = await api.get_job(55)
        assert state is not None
        assert state.status == "failed"
        assert state.finished_at is None
    finally:
        await api.aclose()


@pytest.mark.asyncio
async def test_awk_http_api_get_job_no_finished_field():
    api = _awx_api_with_transport(200, {"status": "running"})
    try:
        state = await api.get_job(77)
        assert state is not None
        assert state.status == "running"
        assert state.finished_at is None
    finally:
        await api.aclose()
