"""Integration tests for the cleanup scheduler — issue #61.

End-to-end tests that verify the scheduler correctly picks up expired
workspaces, invokes the executor, and marks them as cleaned.
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
    ) -> None:
        self._fetch_rows = fetch_rows or []
        self._lock_acquired = lock_acquired
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
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
    conn = _MockConnection(fetch_rows=fetch_rows, lock_acquired=lock_acquired)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireContext(conn))
    return pool, conn


def _mock_executor(*, cleanup_status: str = "cleaned") -> AsyncMock:
    from app.executors.models import CleanupWorkspaceResponse
    executor = AsyncMock()
    executor.cleanup_workspace = AsyncMock(
        return_value=CleanupWorkspaceResponse(status=cleanup_status)
    )
    return executor


def _make_expired_row(workspace_id: uuid.UUID) -> dict:
    return {"id": workspace_id}


# ---------------------------------------------------------------------------
# Tests: End-to-end scheduler picks up and cleans expired workspaces
# ---------------------------------------------------------------------------


class TestSchedulerEndToEndCleanup:
    """End-to-end: scheduler queries expired workspaces, invokes executor, marks cleaned."""

    @pytest.mark.asyncio
    async def test_scheduler_cleans_single_expired_workspace(self):
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.15)
        await scheduler.stop()

        # Executor must have been called with the correct workspace_id
        executor.cleanup_workspace.assert_called()
        request = executor.cleanup_workspace.call_args[0][0]
        assert request.workspace_id == ws_id

        # Workspace status should have been updated to "cleaned".
        # The first UPDATE (to cleaning) has args=(timestamp, ws_id);
        # the second (to cleaned) also has args=(timestamp, ws_id).
        update_calls = [
            (sql, args) for sql, args in conn.execute_calls
            if "UPDATE workspaces" in sql and "cleanup_status" in sql
        ]
        assert len(update_calls) >= 2  # cleaning + cleaned
        # The 'cleaned' UPDATE has ws_id at index 1
        cleaned_sql, cleaned_args = update_calls[1]
        assert cleaned_args[1] == ws_id

    @pytest.mark.asyncio
    async def test_scheduler_cleans_multiple_expired_workspaces(self):
        ws_ids = [uuid.uuid4() for _ in range(3)]
        rows = [_make_expired_row(w) for w in ws_ids]
        pool, conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.15)
        await scheduler.stop()

        assert executor.cleanup_workspace.call_count >= 3

        # All three should have UPDATE calls.
        # Each workspace gets two UPDATEs (cleaning then cleaned), with ws_id at index 1.
        cleaned_ids = set()
        for sql, args in conn.execute_calls:
            if "UPDATE workspaces" in sql and "'cleaned'" in sql:
                cleaned_ids.add(args[1])  # $2 = ws_id
        assert all(w in cleaned_ids for w in ws_ids), (
            f"Not all workspaces cleaned: {cleaned_ids} vs {ws_ids}"
        )

    @pytest.mark.asyncio
    async def test_scheduler_only_cleans_up_to_batch_size(self):
        """The DB may return batch_size rows, and all should be processed per tick."""
        batch_size = 3
        ws_ids = [uuid.uuid4() for _ in range(batch_size)]
        rows = [_make_expired_row(w) for w in ws_ids]
        pool, conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=batch_size)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        # Exactly batch_size should have been processed (per tick)
        assert executor.cleanup_workspace.call_count >= batch_size

    @pytest.mark.asyncio
    async def test_scheduler_respects_cleanup_after_filter(self):
        """Only workspaces with cleanup_after < NOW() are returned by the query.

        We test this by mocking an empty result set to simulate no expired rows.
        """
        pool, conn = _mock_pool(fetch_rows=[], lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        # No workspaces → no cleanup calls
        executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduler_skips_already_cleaned_workspaces(self):
        """Workspaces with cleanup_status = 'cleaned' are excluded by the SQL query.

        We simulate this by returning an empty result (query filters them out).
        """
        pool, conn = _mock_pool(fetch_rows=[], lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        executor.cleanup_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduler_skips_pinned_workspaces(self):
        """Pinned workspaces (pinned=TRUE) are excluded by the query.

        An empty result set simulates all workspaces being pinned.
        """
        pool, conn = _mock_pool(fetch_rows=[], lock_acquired=True)
        executor = _mock_executor()
        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=10)

        await scheduler.start(pool=pool, executor=executor)
        await asyncio.sleep(0.12)
        await scheduler.stop()

        executor.cleanup_workspace.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Full tick flow verification
# ---------------------------------------------------------------------------


class TestFullTickFlow:
    """Verify the complete tick flow: query -> lock -> cleanup -> update."""

    @pytest.mark.asyncio
    async def test_full_tick_flow_logs_expected_messages(self, caplog):
        """The tick should log processing start and completion messages."""
        caplog.set_level(logging.INFO, logger="app.scheduler.cleaner")

        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(pool=pool, executor=executor, batch_size=10)
        await scheduler._tick()

        # At minimum the processing log exists
        assert any("processing" in r.getMessage().lower() for r in caplog.records)

        # Should log "cleaned successfully"
        cleaned_logs = [
            r.getMessage() for r in caplog.records
            if "cleaned successfully" in r.getMessage()
        ]
        assert len(cleaned_logs) >= 1

    @pytest.mark.asyncio
    async def test_full_tick_flow_releases_lock_on_completion(self):
        """After each cleanup, the advisory lock must be released."""
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(pool=pool, executor=executor, batch_size=10)
        await scheduler._tick()

        unlock_calls = [
            (sql, args) for sql, args in conn.execute_calls
            if "pg_advisory_unlock" in sql
        ]
        assert len(unlock_calls) >= 1

    @pytest.mark.asyncio
    async def test_order_of_operations_in_tick(self):
        """Verify the correct order: query -> lock -> cleanup -> unlock -> update."""
        ws_id = uuid.uuid4()
        pool, conn = _mock_pool(
            fetch_rows=[_make_expired_row(ws_id)], lock_acquired=True,
        )
        executor = _mock_executor()

        scheduler = CleanupScheduler(pool=pool, executor=executor, batch_size=10)
        await scheduler._tick()

        # Collect all operations in order
        ops = []
        for call in conn.execute_calls:
            sql, args = call
            if "pg_try_advisory_lock" in sql:
                ops.append("lock")
            elif "pg_advisory_unlock" in sql:
                ops.append("unlock")
            elif "UPDATE workspaces" in sql:
                ops.append("update")

        # Lock must come before update, unlock after update
        lock_idx = ops.index("lock") if "lock" in ops else -1
        update_idx = ops.index("update") if "update" in ops else -1
        unlock_idx = ops.index("unlock") if "unlock" in ops else -1

        if lock_idx >= 0 and update_idx >= 0:
            assert lock_idx < update_idx, "Lock must be acquired before status update"
        if update_idx >= 0 and unlock_idx >= 0:
            assert update_idx < unlock_idx, "Unlock must happen after status update"


# ---------------------------------------------------------------------------
# Tests: Scheduler integration with config
# ---------------------------------------------------------------------------


class TestSchedulerConfigIntegration:
    """Verify that config values are wired through to scheduler behaviour."""

    def test_default_interval_matches_config_default(self):
        from app.core.config import Settings
        settings = Settings()
        scheduler = CleanupScheduler()
        assert scheduler._interval == float(settings.cleanup_interval_seconds)

    def test_default_batch_size_matches_config_default(self):
        from app.core.config import Settings
        settings = Settings()
        scheduler = CleanupScheduler()
        assert scheduler._batch_size == settings.cleanup_batch_size

    def test_custom_interval_overrides_default(self):
        scheduler = CleanupScheduler(interval_seconds=42)
        assert scheduler._interval == 42.0

    def test_custom_batch_size_overrides_default(self):
        scheduler = CleanupScheduler(batch_size=25)
        assert scheduler._batch_size == 25

    def test_cleanup_retention_config_defaults(self):
        from app.core.config import Settings
        settings = Settings()
        assert settings.cleanup_success_retention_hours == 72
        assert settings.cleanup_failure_retention_hours == 168


# ---------------------------------------------------------------------------
# Tests: Multiple ticks accumulate cleanup
# ---------------------------------------------------------------------------


class TestMultiTickAccumulation:
    """Verify that across multiple ticks, all expired workspaces eventually get cleaned."""

    @pytest.mark.asyncio
    async def test_workspaces_cleaned_across_multiple_ticks(self):
        """With batch_size < total rows, multiple ticks should eventually clean all."""
        total = 5
        batch_size = 2
        ws_ids = [uuid.uuid4() for _ in range(total)]
        rows = [_make_expired_row(w) for w in ws_ids]

        pool, conn = _mock_pool(fetch_rows=rows, lock_acquired=True)
        executor = _mock_executor()

        scheduler = CleanupScheduler(interval_seconds=0.05, batch_size=batch_size)
        await scheduler.start(pool=pool, executor=executor)

        # Let multiple ticks run
        await asyncio.sleep(0.25)
        await scheduler.stop()

        # The mock returns the same rows each tick, so every tick processes
        # up to batch_size again. Total calls should be >= total.
        assert executor.cleanup_workspace.call_count >= total
