"""Tests for the dedicated AFK backfill worker.

Covers the claim protocol (per-repo advisory lock, skip when busy), job
execution through the reused ``run_backfill`` orchestration, bounded retries
on transient failures, fail-fast on non-transient failures, crash recovery
(stale ``running`` reclaim), the 90-day retention sweep, the transient-error
classifier, the stable int4 repo lock key, and evidence bounding/redaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from asyncpg.exceptions import PostgresConnectionError

from afk_outcomes.models import Provider
from app.backfill.jobs import (
    CLAIM_JOB_SQL,
    COMPLETE_JOB_SQL,
    FAIL_JOB_SQL,
    INCREMENT_RETRY_SQL,
    PRUNE_JOBS_SQL,
    RECLAIM_STALE_SQL,
    bounded_evidence,
)
from app.backfill.worker import (
    BackfillWorker,
    _failure_category,
    is_transient_error,
    repo_lock_key,
)
from scripts.afk_backfill import BackfillReport

_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)  # noqa: UP017
_UNTIL = datetime(2026, 8, 8, tzinfo=timezone.utc)  # noqa: UP017
_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017


def _job_row(**overrides: object) -> dict:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "status": "running",
        "provider": "github",
        "repository": "acme/proj",
        "window_from": _FROM,
        "window_until": _UNTIL,
        "dry_run": False,
        "show_evidence": False,
        "requested_by": "test-label",
        "retry_count": 0,
        "failure_category": None,
        "failure_message": None,
        "evidence": None,
        "change_requests_scanned": None,
        "issues_scanned": None,
        "sessions_considered": None,
        "explicit_matches": None,
        "high_matches": None,
        "inferred_matches": None,
        "ambiguous": None,
        "unmatched": None,
        "created_at": _NOW,
        "started_at": _NOW,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _report() -> BackfillReport:
    return BackfillReport(
        provider=Provider.GITHUB,
        repository="acme/proj",
        since=_FROM,
        until=_UNTIL,
        dry_run=False,
        change_requests_scanned=5,
        issues_scanned=4,
        sessions_considered=3,
        explicit_matches=1,
        high_matches=2,
        inferred_matches=1,
        ambiguous=1,
        unmatched=0,
    )


class _Acquired:
    """Both awaitable and async-context-manager — mirrors asyncpg's acquire."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def __await__(self):
        async def _inner():
            return self._conn

        return _inner().__await__()

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    """Pool whose acquire/release hand out one shared mock connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquired:
        return _Acquired(self._conn)

    async def release(self, conn) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        return None


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _make_worker(conn) -> BackfillWorker:
    return BackfillWorker(pool=_FakePool(conn), max_retries=3)


def _patch_adapter():
    return patch(
        "app.backfill.worker._build_adapter",
        return_value=(AsyncMock(), AsyncMock()),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Failure classification
# ═══════════════════════════════════════════════════════════════════════════


def test_transient_errors_are_retryable() -> None:
    assert is_transient_error(httpx.TransportError("network"))
    assert is_transient_error(httpx.TimeoutException("timeout", request=None))
    assert is_transient_error(_http_error(503))
    assert is_transient_error(PostgresConnectionError("connection reset"))
    assert is_transient_error(TimeoutError())


def test_non_transient_errors_fail_fast() -> None:
    assert not is_transient_error(_http_error(404))
    assert not is_transient_error(ValueError("bad window"))
    assert not is_transient_error(RuntimeError("unexpected"))


def test_failure_category_is_stable() -> None:
    assert _failure_category(PostgresConnectionError("x")) == "database_error"
    assert _failure_category(_http_error(404)) == "provider_error"
    assert _failure_category(RuntimeError("x")) == "internal_error"


# ═══════════════════════════════════════════════════════════════════════════
#  Lock key
# ═══════════════════════════════════════════════════════════════════════════


def test_repo_lock_key_is_stable_signed_int4() -> None:
    key = repo_lock_key("github", "acme/proj")
    assert key == repo_lock_key("github", "acme/proj")
    assert -(2**31) <= key <= 2**31 - 1
    assert key != repo_lock_key("gitlab", "acme/proj")


# ═══════════════════════════════════════════════════════════════════════════
#  Evidence bounding / redaction
# ═══════════════════════════════════════════════════════════════════════════


def test_bounded_evidence_caps_lines_and_redacts() -> None:
    long_line = "x" * 2000
    token_line = "match issue:1 detail=Authorization: Bearer " + "a" * 80
    lines = [token_line, long_line, "third"]
    bounded = bounded_evidence(lines, max_lines=2)
    assert len(bounded) == 2
    assert len(bounded[1]) == 1000
    assert "***" in bounded[0]
    assert "a" * 80 not in bounded[0]


# ═══════════════════════════════════════════════════════════════════════════
#  Claim + execute
# ═══════════════════════════════════════════════════════════════════════════


async def test_worker_claims_and_completes_job() -> None:
    conn = AsyncMock()
    claim_row = _job_row()
    completed_row = _job_row(status="completed", completed_at=_NOW)
    conn.fetchval.return_value = True
    conn.fetchrow.side_effect = [claim_row, completed_row]
    worker = _make_worker(conn)

    with patch("app.backfill.worker.run_backfill", new=AsyncMock(return_value=_report())), (
        _patch_adapter()
    ):
        await worker._run_next_job("github", "acme/proj")

    assert conn.fetchrow.await_args_list[0].args[0] == CLAIM_JOB_SQL
    assert conn.fetchrow.await_args_list[1].args[0] == COMPLETE_JOB_SQL
    # advisory lock released after the run
    unlock_calls = [
        call for call in conn.execute.await_args_list if "advisory_unlock" in call.args[0]
    ]
    assert unlock_calls


async def test_worker_skips_repo_held_by_another_instance() -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = False
    worker = _make_worker(conn)
    await worker._run_next_job("github", "acme/proj")
    assert not conn.fetchrow.called


async def test_worker_retries_transient_failures_then_succeeds() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = _job_row()
    worker = _make_worker(conn)
    run_backfill_mock = AsyncMock(
        side_effect=[httpx.TransportError("x"), httpx.TransportError("y"), _report()]
    )
    with (
        patch("app.backfill.worker.run_backfill", new=run_backfill_mock),
        patch("app.backfill.worker.asyncio.sleep", new=AsyncMock()),
        _patch_adapter(),
    ):
        await worker._run_job(conn, _job_row())

    assert run_backfill_mock.await_count == 3
    retry_updates = [
        call for call in conn.execute.await_args_list
        if call.args[0] == INCREMENT_RETRY_SQL
    ]
    assert len(retry_updates) == 2
    complete_calls = [
        call for call in conn.fetchrow.await_args_list
        if call.args[0] == COMPLETE_JOB_SQL
    ]
    assert complete_calls
    assert complete_calls[0].args[2] == 2  # retry_count persisted


async def test_worker_fails_after_max_retries() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = _job_row()
    worker = _make_worker(conn)
    run_backfill_mock = AsyncMock(
        side_effect=httpx.TransportError("still down")
    )
    with (
        patch("app.backfill.worker.run_backfill", new=run_backfill_mock),
        patch("app.backfill.worker.asyncio.sleep", new=AsyncMock()),
        _patch_adapter(),
    ):
        await worker._run_job(conn, _job_row())

    assert run_backfill_mock.await_count == 4  # initial + 3 retries
    fail_calls = [
        call for call in conn.fetchrow.await_args_list
        if call.args[0] == FAIL_JOB_SQL
    ]
    assert fail_calls
    assert fail_calls[0].args[3] == "provider_error"
    assert fail_calls[0].args[2] == 3  # retry_count


async def test_worker_non_transient_failure_is_not_retried() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = _job_row()
    worker = _make_worker(conn)
    run_backfill_mock = AsyncMock(side_effect=_http_error(404))
    with (
        patch("app.backfill.worker.run_backfill", new=run_backfill_mock),
        patch("app.backfill.worker.asyncio.sleep", new=AsyncMock()),
        _patch_adapter(),
    ):
        await worker._run_job(conn, _job_row())

    assert run_backfill_mock.await_count == 1
    fail_calls = [
        call for call in conn.fetchrow.await_args_list
        if call.args[0] == FAIL_JOB_SQL
    ]
    assert fail_calls
    assert fail_calls[0].args[2] == 0  # no retries recorded


async def test_worker_never_forcibly_interrupts_running_jobs() -> None:
    """Cancellation is queued-only: the worker has no cancel path for running jobs."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _job_row()
    worker = _make_worker(conn)
    with patch("app.backfill.worker.run_backfill", new=AsyncMock(return_value=_report())), (
        _patch_adapter()
    ):
        await worker._run_job(conn, _job_row())
    for call in conn.fetchrow.await_args_list:
        assert "cancelled" not in call.args[0]


# ═══════════════════════════════════════════════════════════════════════════
#  Sweep: retention + stale-running reclaim
# ═══════════════════════════════════════════════════════════════════════════


async def test_sweep_prunes_expired_terminal_rows() -> None:
    conn = AsyncMock()
    stale_job = {"id": uuid.uuid4()}
    conn.fetch.side_effect = [[stale_job], []]
    worker = _make_worker(conn)
    await worker._sweep()
    prune_calls = [
        call for call in conn.fetch.await_args_list if call.args[0] == PRUNE_JOBS_SQL
    ]
    assert prune_calls
    assert sorted(prune_calls[0].args[1]) == ["cancelled", "completed", "failed"]
    cutoff = prune_calls[0].args[2]
    assert cutoff < datetime.now(timezone.utc) - timedelta(days=89)  # noqa: UP017


async def test_sweep_reclaims_stale_running_jobs() -> None:
    conn = AsyncMock()
    reclaimed = _job_row()
    conn.fetch.side_effect = [[], [reclaimed]]
    worker = _make_worker(conn)
    await worker._sweep()
    reclaim_calls = [
        call for call in conn.fetch.await_args_list if call.args[0] == RECLAIM_STALE_SQL
    ]
    assert reclaim_calls
    assert reclaim_calls[0].args[2] == "Worker interrupted; re-submit to retry."
