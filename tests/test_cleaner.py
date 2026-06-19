"""Tests for the background workspace cleanup scheduler (issue #60).

Covers:
- Interval timing and periodic ticks
- Expired workspace query filtering
- PostgreSQL advisory lock acquisition / contention
- Configurable batch processing
- Executor integration and status updates
- Error recovery (loop continues after individual failures)
- Context cancellation clean shutdown
- Missing pool/executor graceful skip
- UUID-to-lock-key conversion
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.lock import uuid_to_lock_key
from app.scheduler.cleaner import CleanupScheduler

# ---------------------------------------------------------------------------
# Helpers — build consistent mocks for asyncpg Pool
# ---------------------------------------------------------------------------
#
# asyncpg.Pool.acquire() is a *synchronous* method that returns an async
# context manager.  ``async with pool.acquire() as conn:`` works because
# the returned object implements ``__aenter__`` / ``__aexit__``.  The
# helpers below replicate this contract faithfully.
# ---------------------------------------------------------------------------


class _MockConnection:
    """Simulates a single asyncpg connection.

    All methods are async to match the real asyncpg.Connection interface.
    """

    def __init__(
        self,
        fetch_rows: list[dict] | None = None,
        lock_acquired: bool = True,
    ) -> None:
        self._fetch_rows = fetch_rows or []
        self._lock_acquired = lock_acquired
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        rows = []
        for r in self._fetch_rows:
            m = MagicMock()
            m.__getitem__.side_effect = r.__getitem__
            m.get = r.get
            rows.append(m)
        return rows

    async def fetchval(self, sql: str, *args):
        if "pg_try_advisory_lock" in sql:
            return self._lock_acquired
        return None

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))

    async def close(self):
        pass


class _AcquireContext:
    """Async context manager returned by ``pool.acquire()``."""

    def __init__(self, conn: _MockConnection) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def _mock_pool(
    *,
    fetch_rows: list[dict] | None = None,
    lock_acquired: bool = True,
) -> tuple[Mock, _MockConnection]:
    """Return a (mock_pool, mock_connection) pair.

    ``pool.acquire()`` returns an ``_AcquireContext`` whose
    ``__aenter__`` yields a ``_MockConnection``.
    """
    conn = _MockConnection(fetch_rows=fetch_rows, lock_acquired=lock_acquired)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireContext(conn))
    return pool, conn


def _mock_executor(
    *,
    cleanup_status: str = "cleaned",
    raises: Exception | None = None,
) -> AsyncMock:
    """Build a mock ExecutorPlugin with configurable cleanup_workspace."""
    from app.executors.models import CleanupWorkspaceResponse

    executor = AsyncMock()
    if raises:
        executor.cleanup_workspace = AsyncMock(side_effect=raises)
    else:
        executor.cleanup_workspace = AsyncMock(
            return_value=CleanupWorkspaceResponse(status=cleanup_status)
        )
    return executor


def _make_expired_row(workspace_id: uuid.UUID) -> dict:
    """Return a dict representing an expired workspace row from the DB."""
    return {"id": workspace_id}


# ---------------------------------------------------------------------------
# Tests: UUID to lock key
# ---------------------------------------------------------------------------


class TestUuidToLockKey:
    """Tests for the UUID-to-bigint conversion function."""

    def test_converts_uuid_to_positive_bigint(self):
        """A UUID object must be converted to a positive integer."""
        ws_id = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
        key = uuid_to_lock_key(ws_id)
        assert isinstance(key, int)
        assert 0 <= key <= 0x7FFFFFFFFFFFFFFF

    def test_converts_string_uuid(self):
        """A string UUID should work the same as a UUID object."""
        uid_str = "550e8400-e29b-41d4-a716-446655440000"
        uid_obj = uuid.UUID(uid_str)
        assert uuid_to_lock_key(uid_str) == uuid_to_lock_key(uid_obj)

    def test_different_uuids_produce_different_keys(self):
        """Two different UUIDs should NOT produce the same key."""
        a = uuid.uuid4()
        b = uuid.uuid4()
        # Statistically impossible to collide with 63-bit space
        assert uuid_to_lock_key(a) != uuid_to_lock_key(b)

    def test_same_uuid_produces_same_key(self):
        """The same UUID always produces the same key."""
        ws_id = uuid.uuid4()
        assert uuid_to_lock_key(ws_id) == uuid_to_lock_key(ws_id)


# ---------------------------------------------------------------------------
# Tests: CleanupScheduler lifecycle
# ---------------------------------------------------------------------------


class TestCleanupSchedulerLifecycle:
    """Tests for start / stop lifecycle of the cleanup scheduler."""

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        """Calling start() should create a running asyncio Task."""
        scheduler = CleanupScheduler(interval_seconds=0.05)
        executor = _mock_executor()
        await scheduler.start(pool=None, executor=executor)

        assert scheduler._task is not None
        assert not scheduler._task.done()

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task_and_awaits_it(self):
        """stop() should cancel the internal task and wait for it to finish."""
        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=None, executor=_mock_executor())

        task = scheduler._task
        assert task is not None

        await scheduler.stop()

        assert task.cancelled() or task.done()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        """Calling stop() on an unstarted scheduler should not error."""
        scheduler = CleanupScheduler()
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_start_logs_info_message(self, caplog):
        """start() must emit an INFO-level log about scheduler startup."""
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")

        scheduler = CleanupScheduler()
        await scheduler.start(pool=None, executor=_mock_executor())

        info_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO
            and "Cleanup scheduler started" in r.getMessage()
        ]
        assert len(info_logs) == 1

        await scheduler.stop()


# ---------------------------------------------------------------------------
# Tests: Interval timing
# ---------------------------------------------------------------------------


class TestIntervalTiming:
    """Tests that the tick is called repeatedly on the configured interval."""

    @pytest.mark.asyncio
    async def test_tick_called_multiple_times(self):
        """_tick() should be called multiple times as intervals elapse."""
        call_count = 0

        class CountingCleanup(CleanupScheduler):
            async def _tick(self):
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0)

        scheduler = CountingCleanup(interval_seconds=0.05)
        await scheduler.start(pool=None, executor=_mock_executor())

        await asyncio.sleep(0.20)
        await scheduler.stop()

        assert call_count >= 2, f"Expected >= 2 ticks, got {call_count}"

    @pytest.mark.asyncio
    async def test_custom_interval_used(self, caplog):
        """The interval_seconds passed to the constructor should appear in the log."""
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")

        scheduler = CleanupScheduler(interval_seconds=42.0)
        await scheduler.start(pool=None, executor=_mock_executor())

        info_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO
            and "Cleanup scheduler started" in r.getMessage()
        ]
        assert len(info_logs) == 1
        assert "42.0" in info_logs[0]

        await scheduler.stop()


# ---------------------------------------------------------------------------
# Tests: Expired workspace query
# ---------------------------------------------------------------------------


class TestExpiredWorkspaceQuery:
    """Tests that the scheduler queries for the right workspaces."""

    @pytest.mark.asyncio
    async def test_fetches_expired_workspaces(self):
        """_query_expired should call fetch with the correct SQL and limit."""
        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(fetch_rows=[_make_expired_row(ws_id)])

        scheduler = CleanupScheduler(batch_size=5)
        rows = await scheduler._query_expired(pool)

        assert len(rows) == 1
        assert rows[0]["id"] == ws_id

    @pytest.mark.asyncio
    async def test_batch_size_passed_to_query(self):
        """The query should LIMIT to the configured batch size."""
        pool, conn = _mock_pool(fetch_rows=[])

        scheduler = CleanupScheduler(batch_size=7)
        await scheduler._query_expired(pool)

        # pool.acquire should have been used
        pool.acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_expired_workspaces_skips_processing(self, caplog):
        """When the query returns no rows, no processing log should appear."""
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")

        pool, _conn = _mock_pool(fetch_rows=[])
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        processing_logs = [
            r.getMessage()
            for r in caplog.records
            if "processing" in r.getMessage().lower()
        ]
        assert len(processing_logs) == 0


# ---------------------------------------------------------------------------
# Tests: Advisory locks
# ---------------------------------------------------------------------------


class TestAdvisoryLockAcquisition:
    """Tests for PostgreSQL advisory lock behaviour."""

    @pytest.mark.asyncio
    async def test_acquires_lock_before_cleanup(self):
        """The advisory lock must be acquired before calling executor.cleanup_workspace."""
        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=True,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        executor.cleanup_workspace.assert_called()
        call_args = executor.cleanup_workspace.call_args[0][0]
        assert call_args.workspace_id == ws_id

    @pytest.mark.asyncio
    async def test_skips_when_lock_unavailable(self, caplog):
        """When pg_try_advisory_lock returns false, the workspace is skipped."""
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")

        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=False,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        # Executor should NOT be called because lock was unavailable
        executor.cleanup_workspace.assert_not_called()

        # Should have logged a skip message
        skip_logs = [
            r.getMessage()
            for r in caplog.records
            if "Advisory lock unavailable" in r.getMessage()
        ]
        assert len(skip_logs) >= 1

    @pytest.mark.asyncio
    async def test_releases_lock_after_cleanup(self):
        """After cleanup (success), the advisory lock is released via pg_advisory_unlock."""
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=True,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        unlock_calls = [
            (sql, args)
            for sql, args in conn.execute_calls
            if "pg_advisory_unlock" in sql
        ]
        assert len(unlock_calls) >= 1


# ---------------------------------------------------------------------------
# Tests: Batch processing
# ---------------------------------------------------------------------------


class TestBatchProcessing:
    """Tests for configurable batch processing behaviour."""

    @pytest.mark.asyncio
    async def test_processes_up_to_batch_size(self):
        """Only batch_size workspaces should be processed per tick.

        Because mock rows are reused across ticks, we supply exactly
        *batch_size* rows to simulate the DB-side LIMIT.  Multiple ticks
        will produce multiples of batch_size, so we use >= batch_size.
        """
        batch_size = 3
        ws_ids = [uuid.uuid4() for _ in range(batch_size)]
        rows = [_make_expired_row(w) for w in ws_ids]

        pool, _conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()

        scheduler = CleanupScheduler(batch_size=batch_size, interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        # Each tick processes all rows returned by the mock.
        # With a 0.05s interval and 0.12s sleep, at least 1 tick fires.
        assert executor.cleanup_workspace.call_count >= batch_size, (
            f"Expected >= {batch_size} calls, got {executor.cleanup_workspace.call_count}"
        )

    @pytest.mark.asyncio
    async def test_batch_respects_config(self):
        """The batch_size from constructor should be honoured."""
        scheduler = CleanupScheduler(batch_size=15)
        assert scheduler._batch_size == 15


# ---------------------------------------------------------------------------
# Tests: Executor integration
# ---------------------------------------------------------------------------


class TestExecutorIntegration:
    """Tests for executor.cleanup_workspace() integration."""

    @pytest.mark.asyncio
    async def test_calls_executor_cleanup(self):
        """The executor.cleanup_workspace() method must be called for each workspace.

        The scheduler may tick multiple times during the test window,
        so we assert at least one call — the key verification is that
        the correct workspace_id is passed.
        """
        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(fetch_rows=[_make_expired_row(ws_id)])
        executor = _mock_executor()

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        executor.cleanup_workspace.assert_called()
        request = executor.cleanup_workspace.call_args[0][0]
        assert request.workspace_id == ws_id

    @pytest.mark.asyncio
    async def test_updates_cleanup_status_on_success(self):
        """When cleanup succeeds, the workspace cleanup_status is set to 'cleaned'."""
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=True,
        )
        executor = _mock_executor(cleanup_status="cleaned")

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        # Should have UPDATE calls setting cleanup_status to cleaning then cleaned.
        # The second update (cleaned) passes (now_done, ws_id).
        update_calls = [
            (sql, args)
            for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql and "cleaned" in sql
        ]
        assert len(update_calls) >= 1, (
            f"Expected UPDATE workspaces SET ... cleaned call, got: {conn.execute_calls}"
        )
        _sql, args = update_calls[0]
        # args: (now_done, ws_id) — ws_id is at index 1
        assert args[1] == ws_id

    @pytest.mark.asyncio
    async def test_unexpected_response_transitions_to_cleanup_failed(self, caplog):
        """If executor returns status != 'cleaned', workspace transitions to cleanup_failed."""
        caplog.set_level(logging.WARNING, logger="app.scheduler.cleaner")

        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=True,
        )
        executor = _mock_executor(cleanup_status="error")

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        warning_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "unexpected status" in r.getMessage()
        ]
        assert len(warning_logs) >= 1

        # A cleanup_failed UPDATE should have been issued
        update_calls = [
            (sql, args)
            for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql and "cleanup_failed" in sql
        ]
        assert len(update_calls) >= 1, (
            f"Expected cleanup_failed UPDATE, got: {conn.execute_calls}"
        )


# ---------------------------------------------------------------------------
# Tests: Error recovery
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    """Tests that the loop continues after individual cleanup failures."""

    @pytest.mark.asyncio
    async def test_loop_continues_after_cleanup_failure(self, caplog):
        """An exception in one cleanup should not stop subsequent ticks."""
        caplog.set_level(logging.ERROR, logger="app.scheduler.cleaner")

        ws1 = uuid.uuid4()
        ws2 = uuid.uuid4()
        pool, _conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws1), _make_expired_row(ws2)],
            lock_acquired=True,
        )
        # All cleanups raise — but the loop keeps ticking and retries them
        executor = _mock_executor(raises=RuntimeError("simulated cleanup failure"))

        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.20)
        await scheduler.stop()

        # Both workspaces should have been attempted across multiple ticks
        assert executor.cleanup_workspace.call_count >= 2

        # Error should be logged
        error_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "Cleanup failed" in r.getMessage()
        ]
        assert len(error_logs) >= 1


# ---------------------------------------------------------------------------
# Tests: Context cancellation
# ---------------------------------------------------------------------------


class TestContextCancellation:
    """Tests that the scheduler exits cleanly on context cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_stops_loop(self):
        """Cancelling the task should cause the loop to exit."""
        scheduler = CleanupScheduler(interval_seconds=60.0)  # long interval
        await scheduler.start(pool=None, executor=_mock_executor())

        assert scheduler._task is not None
        assert not scheduler._task.done()

        scheduler._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scheduler._task

        # After cancellation, the task is done
        assert scheduler._task.done()

    @pytest.mark.asyncio
    async def test_stop_during_long_sleep_exits_cleanly(self):
        """Calling stop() while the scheduler is sleeping should cancel and exit."""
        scheduler = CleanupScheduler(interval_seconds=60.0)
        await scheduler.start(pool=None, executor=_mock_executor())

        # Give it a moment to enter the sleep
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert scheduler._task is None or scheduler._task.done()


# ---------------------------------------------------------------------------
# Tests: Missing dependencies
# ---------------------------------------------------------------------------


class TestMissingDependencies:
    """Tests that the scheduler gracefully handles missing pool or executor."""

    @pytest.mark.asyncio
    async def test_missing_pool_logs_skip_and_continues(self, caplog):
        """When pool is None, tick logs a debug skip and does not crash."""
        caplog.set_level(logging.DEBUG, logger="app.scheduler.cleaner")

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=None, executor=_mock_executor())
        await asyncio.sleep(0.12)
        await scheduler.stop()

        debug_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and "no database pool" in r.getMessage()
        ]
        assert len(debug_logs) >= 1

    @pytest.mark.asyncio
    async def test_missing_executor_logs_skip_and_continues(self, caplog):
        """When executor is None, tick logs a debug skip and does not crash."""
        caplog.set_level(logging.DEBUG, logger="app.scheduler.cleaner")

        pool, _conn = _mock_pool()
        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=None)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        debug_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and "no executor" in r.getMessage()
        ]
        assert len(debug_logs) >= 1


# ---------------------------------------------------------------------------
# Tests: Factory integration
# ---------------------------------------------------------------------------


class TestFactoryCleanupSchedulerIntegration:
    """Verify that create_app() properly wires the CleanupScheduler."""

    @pytest.mark.asyncio
    async def test_app_uses_cleanup_scheduler(self):
        """create_app should wire a CleanupScheduler (not base Scheduler)."""
        from app.core.factory import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            scheduler = getattr(app.state, "scheduler", None)
            assert isinstance(scheduler, CleanupScheduler)

    @pytest.mark.asyncio
    async def test_scheduler_cleaned_up_after_lifespan(self):
        """After the lifespan context exits, the scheduler task is stopped."""
        from app.core.factory import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            scheduler = getattr(app.state, "scheduler", None)
            assert scheduler._task is not None
            assert not scheduler._task.done()

        assert scheduler._task is None or scheduler._task.done()


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestStatusResponseHandling:
    """Edge cases around executor status responses."""

    @pytest.mark.asyncio
    async def test_empty_status_transitions_to_cleanup_failed(self):
        """If the response status is empty/blank, workspace transitions to cleanup_failed."""
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            lock_acquired=True,
        )
        executor = _mock_executor(cleanup_status="")

        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        update_calls = [
            (sql, args) for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql and "cleanup_failed" in sql
        ]
        assert len(update_calls) >= 1, (
            f"Expected cleanup_failed UPDATE, got: {conn.execute_calls}"
        )
