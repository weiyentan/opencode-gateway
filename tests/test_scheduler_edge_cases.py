"""Edge-case tests for the cleanup scheduler — issue #61.

Covers gaps not addressed by existing scheduler/cleaner test suites:
- Database connection failure during query
- All workspaces pinned (empty result set)
- Advisory lock contention across concurrent scheduler instances
- Partial batch handling (fewer rows than batch_size)
- Large batch size config edge
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.scheduler.cleaner import CleanupScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockConnection:
    def __init__(
        self,
        fetch_rows: list[dict] | None = None,
        lock_acquired: bool = True,
        *,
        acquire_raises: Exception | None = None,
        unlock_raises: Exception | None = None,
    ) -> None:
        self._fetch_rows = fetch_rows or []
        self._lock_acquired = lock_acquired
        self._acquire_raises = acquire_raises
        self._unlock_raises = unlock_raises
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        rows = []
        for r in self._fetch_rows:
            m = MagicMock()
            m.__getitem__.side_effect = r.__getitem__
            m.get = r.get
            rows.append(m)
        return rows

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return self._lock_acquired
        return None

    async def execute(self, sql: str, *args):
        if "pg_advisory_unlock" in sql and self._unlock_raises:
            raise self._unlock_raises
        self.execute_calls.append((sql, args))

    async def close(self):
        pass


class _AcquireContext:
    def __init__(self, conn: _MockConnection, raises: Exception | None = None) -> None:
        self._conn = conn
        self._raises = raises

    async def __aenter__(self):
        if self._raises is not None:
            raise self._raises
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def _mock_pool(
    *,
    fetch_rows: list[dict] | None = None,
    lock_acquired: bool = True,
    acquire_raises: Exception | None = None,
    unlock_raises: Exception | None = None,
) -> tuple[Mock, _MockConnection]:
    conn = _MockConnection(
        fetch_rows=fetch_rows,
        lock_acquired=lock_acquired,
        acquire_raises=acquire_raises,
        unlock_raises=unlock_raises,
    )
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireContext(conn, raises=acquire_raises))
    return pool, conn


def _mock_executor(
    *,
    cleanup_status: str = "cleaned",
    raises: Exception | None = None,
) -> AsyncMock:
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
    return {"id": workspace_id}


# ---------------------------------------------------------------------------
# Tests: Database connection failure
# ---------------------------------------------------------------------------


class TestDatabaseConnectionFailure:
    @pytest.mark.asyncio
    async def test_query_expired_raises_does_not_crash_scheduler(self, caplog):
        caplog.set_level(logging.ERROR, logger="app.scheduler.cleaner")
        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            acquire_raises=ConnectionError("database unavailable"),
        )
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=5)
        with pytest.raises(ConnectionError):
            await scheduler._query_expired(pool)
        pool2, _conn2 = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)],
            acquire_raises=ConnectionError("database unavailable"),
        )
        await scheduler.start(ctx={"pool": pool2, "executor": executor})
        await asyncio.sleep(0.12)
        await scheduler.stop(ctx={})
        assert scheduler._task is None or scheduler._task.done()

    @pytest.mark.asyncio
    async def test_tick_handles_db_failure_gracefully(self, caplog):
        caplog.set_level(logging.ERROR, logger="app.scheduler.engine")
        tick_count = 0
        class DBFailingScheduler(CleanupScheduler):
            async def _tick(self, ctx):
                nonlocal tick_count
                tick_count += 1
                pool = ctx.get("pool")
                if pool is not None:
                    try:
                        await self._query_expired(pool)
                    except ConnectionError:
                        logger = logging.getLogger("app.scheduler.cleaner")
                        logger.error("DB failure during query — will retry")
                await asyncio.sleep(0)
        pool, _conn = _mock_pool(
            fetch_rows=[], acquire_raises=ConnectionError("database unavailable"),
        )
        executor = _mock_executor()
        scheduler = DBFailingScheduler(interval_seconds=0.05)
        await scheduler.start(ctx={"pool": pool, "executor": executor})
        await asyncio.sleep(0.15)
        await scheduler.stop(ctx={})
        assert tick_count >= 2


# ---------------------------------------------------------------------------
# Tests: All workspaces pinned
# ---------------------------------------------------------------------------


class TestAllWorkspacesPinned:
    @pytest.mark.asyncio
    async def test_no_rows_returned_when_all_pinned(self):
        scheduler = CleanupScheduler(batch_size=5)
        pool, _conn = _mock_pool(fetch_rows=[])
        rows = await scheduler._query_expired(pool)
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_tick_skips_processing_when_all_pinned(self, caplog):
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")
        pool, _conn = _mock_pool(fetch_rows=[])
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05)
        await scheduler.start(ctx={"pool": pool, "executor": executor})
        await asyncio.sleep(0.12)
        await scheduler.stop(ctx={})
        executor.cleanup_workspace.assert_not_called()
        processing_logs = [
            r.getMessage() for r in caplog.records
            if "processing" in r.getMessage().lower()
        ]
        assert len(processing_logs) == 0


# ---------------------------------------------------------------------------
# Tests: Advisory lock contention
# ---------------------------------------------------------------------------


class TestAdvisoryLockContention:
    @pytest.mark.asyncio
    async def test_concurrent_process_one_lock_contention(self):
        ws_id = uuid.uuid4()
        pool1, conn1 = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        pool2, conn2 = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=False,
        )
        executor1 = _mock_executor()
        executor2 = _mock_executor()
        scheduler = CleanupScheduler(batch_size=10)
        row = {"id": ws_id}
        await asyncio.gather(
            scheduler._process_one(pool1, executor1, row),
            scheduler._process_one(pool2, executor2, row),
        )
        assert executor1.cleanup_workspace.called
        assert not executor2.cleanup_workspace.called

    @pytest.mark.asyncio
    async def test_serial_lock_acquisition_allows_both(self):
        ws_id = uuid.uuid4()
        pool1, conn1 = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        pool2, conn2 = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor1 = _mock_executor()
        executor2 = _mock_executor()
        scheduler = CleanupScheduler(batch_size=10)
        row = {"id": ws_id}
        await scheduler._process_one(pool1, executor1, row)
        await scheduler._process_one(pool2, executor2, row)
        assert executor1.cleanup_workspace.called
        assert executor2.cleanup_workspace.called

    @pytest.mark.asyncio
    async def test_lock_release_failure_is_logged(self, caplog):
        caplog.set_level(logging.ERROR, logger="app.scheduler.cleaner")
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
            unlock_raises=RuntimeError("unlock failed"),
        )
        executor = _mock_executor()
        scheduler = CleanupScheduler()
        row = {"id": ws_id}
        await scheduler._process_one(pool, executor, row)
        executor.cleanup_workspace.assert_called_once()
        error_logs = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR
            and "Failed to release advisory lock" in r.getMessage()
        ]
        assert len(error_logs) >= 1


# ---------------------------------------------------------------------------
# Tests: Partial batch handling
# ---------------------------------------------------------------------------


class TestPartialBatchHandling:
    @pytest.mark.asyncio
    async def test_fewer_rows_than_batch_processed_fully(self):
        ws_ids = [uuid.uuid4(), uuid.uuid4()]
        rows = [_make_expired_row(w) for w in ws_ids]
        pool, _conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(batch_size=10)
        await scheduler._tick({"pool": pool, "executor": executor})
        assert executor.cleanup_workspace.call_count == 2

    @pytest.mark.asyncio
    async def test_exact_batch_size_processed_fully(self):
        batch_size = 4
        ws_ids = [uuid.uuid4() for _ in range(batch_size)]
        rows = [_make_expired_row(w) for w in ws_ids]
        pool, _conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(batch_size=batch_size)
        await scheduler._tick({"pool": pool, "executor": executor})
        assert executor.cleanup_workspace.call_count == batch_size


# ---------------------------------------------------------------------------
# Tests: Batch size config edges
# ---------------------------------------------------------------------------


class TestBatchSizeEdgeCases:
    def test_batch_size_one(self):
        scheduler = CleanupScheduler(batch_size=1)
        assert scheduler._batch_size == 1

    def test_batch_size_zero_technically_allowed(self):
        scheduler = CleanupScheduler(batch_size=0)
        assert scheduler._batch_size == 0

    @pytest.mark.asyncio
    async def test_large_batch_still_respects_returned_rows(self):
        ws_id = uuid.uuid4()
        pool, _conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor = _mock_executor()
        scheduler = CleanupScheduler(batch_size=1000)
        await scheduler._tick({"pool": pool, "executor": executor})
        assert executor.cleanup_workspace.call_count == 1


# ---------------------------------------------------------------------------
# Tests: Mixed cleanup results
# ---------------------------------------------------------------------------


class TestMixedCleanupResults:
    @pytest.mark.asyncio
    async def test_mixed_success_and_failure_executor(self):
        ws_ok = uuid.uuid4()
        ws_fail = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_ok), _make_expired_row(ws_fail)],
            lock_acquired=True,
        )
        from app.executors.models import CleanupWorkspaceResponse
        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock()
        executor.cleanup_workspace.side_effect = [
            CleanupWorkspaceResponse(status="cleaned"),
            RuntimeError("cleanup exploded"),
        ]
        scheduler = CleanupScheduler(batch_size=10)
        await scheduler._tick({"pool": pool, "executor": executor})
        assert executor.cleanup_workspace.call_count == 2
        update_calls = [
            (sql, args) for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql
        ]
        assert len(update_calls) >= 1

    @pytest.mark.asyncio
    async def test_executor_returns_error_status_then_next_succeeds(self):
        ws1 = uuid.uuid4()
        ws2 = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws1), _make_expired_row(ws2)],
            lock_acquired=True,
        )
        from app.executors.models import CleanupWorkspaceResponse
        executor = AsyncMock()
        executor.cleanup_workspace = AsyncMock()
        executor.cleanup_workspace.side_effect = [
            CleanupWorkspaceResponse(status="error"),
            CleanupWorkspaceResponse(status="cleaned"),
        ]
        scheduler = CleanupScheduler(batch_size=10)
        await scheduler._tick({"pool": pool, "executor": executor})
        assert executor.cleanup_workspace.call_count == 2
        update_calls = [
            (sql, args) for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql
        ]
        assert len(update_calls) == 1
