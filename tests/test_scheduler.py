"""Tests for the cleanup scheduler lifecycle skeleton."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.scheduler import Scheduler


class TestSchedulerLifecycle:
    """Tests for start / stop lifecycle of the cleanup scheduler."""

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        """Calling start() should create a running asyncio Task."""
        scheduler = Scheduler(interval_seconds=0.05)
        await scheduler.start(ctx={})

        assert scheduler._task is not None
        assert not scheduler._task.done()

        await scheduler.stop(ctx={})

    @pytest.mark.asyncio
    async def test_stop_cancels_task_and_awaits_it(self):
        """stop() should cancel the internal task and wait for it to finish."""
        scheduler = Scheduler(interval_seconds=0.05)
        await scheduler.start(ctx={})

        task = scheduler._task
        assert task is not None

        await scheduler.stop(ctx={})

        assert task.cancelled() or task.done()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        """Calling stop() on an unstarted scheduler should not error."""
        scheduler = Scheduler()
        await scheduler.stop(ctx={})  # no-op, should not raise

    @pytest.mark.asyncio
    async def test_stop_awaits_task_completion(self):
        """stop() awaits the internal task before returning, clearing _task."""
        scheduler = Scheduler(interval_seconds=0.05)
        await scheduler.start(ctx={})
        task = scheduler._task

        # stop() must clear _task after the background loop exits
        assert task is not None
        await scheduler.stop(ctx={})
        assert task.done()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_errors_inside_tick_are_logged_not_raised(self, caplog):
        """An exception in _tick must not crash the scheduler or propagate."""
        scheduler = Scheduler(interval_seconds=0.05)

        async def broken_tick(ctx):
            raise RuntimeError("simulated tick failure")

        scheduler._tick = broken_tick  # type: ignore[method-assign]
        await scheduler.start(ctx={})

        # Let a few ticks run so the error path is exercised
        await asyncio.sleep(0.25)

        # The scheduler should still be running
        assert not scheduler._task.done()

        await scheduler.stop(ctx={})

        # The error should have been logged
        error_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and "Scheduler loop encountered an error" in r.getMessage()
        ]
        assert len(error_logs) >= 1, (
            f"Expected at least one error log, got records: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_start_logs_info_message(self, caplog):
        """start() must emit an INFO-level log about scheduler startup."""
        caplog.set_level(logging.INFO, logger="app.scheduler.engine")

        scheduler = Scheduler()
        await scheduler.start(ctx={})

        info_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO
            and "Cleanup scheduler started" in r.getMessage()
        ]
        assert len(info_logs) == 1

        await scheduler.stop(ctx={})

    @pytest.mark.asyncio
    async def test_stop_logs_stopped_message(self, caplog):
        """stop() should log that the scheduler was stopped."""
        caplog.set_level(logging.INFO, logger="app.scheduler.engine")

        scheduler = Scheduler()
        await scheduler.start(ctx={})
        await scheduler.stop(ctx={})

        stop_logs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO
            and "Cleanup scheduler stopped" in r.getMessage()
        ]
        assert len(stop_logs) >= 1


class TestSchedulerTick:
    """Tests for the internal tick mechanism."""

    @pytest.mark.asyncio
    async def test_tick_is_called_periodically(self):
        """_tick() should be invoked multiple times as intervals elapse."""
        call_count = 0

        class CountingScheduler(Scheduler):
            async def _tick(self, ctx):
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0)

        scheduler = CountingScheduler(interval_seconds=0.05)
        await scheduler.start(ctx={})

        # Let enough time pass for several ticks
        await asyncio.sleep(0.20)

        await scheduler.stop(ctx={})
        assert call_count >= 2, f"Expected >= 2 ticks, got {call_count}"

    @pytest.mark.asyncio
    async def test_tick_receives_context(self):
        """The ctx dict passed to start() should be forwarded to _tick()."""
        received_ctx = None

        class RecordingScheduler(Scheduler):
            async def _tick(self, ctx):
                nonlocal received_ctx
                received_ctx = ctx
                await asyncio.sleep(0)

        scheduler = RecordingScheduler(interval_seconds=0.05)
        test_ctx = {"pool": "fake-pool", "executor": "fake-executor"}
        await scheduler.start(ctx=test_ctx)
        await asyncio.sleep(0.10)
        await scheduler.stop(ctx={})

        assert received_ctx is test_ctx

    @pytest.mark.asyncio
    async def test_errors_in_tick_do_not_stop_loop(self):
        """An exception in one tick should not prevent subsequent ticks."""
        tick_count = 0
        error_raised = False

        class IntermittentErrorScheduler(Scheduler):
            async def _tick(self, ctx):
                nonlocal tick_count, error_raised
                tick_count += 1
                if not error_raised:
                    error_raised = True
                    raise RuntimeError("simulated intermittent error")
                await asyncio.sleep(0)

        scheduler = IntermittentErrorScheduler(interval_seconds=0.05)
        await scheduler.start(ctx={})
        await asyncio.sleep(0.20)
        await scheduler.stop(ctx={})

        assert tick_count >= 2, f"Expected >= 2 ticks even after error, got {tick_count}"


class TestSchedulerFactoryIntegration:
    """Verify that create_app() properly wires the scheduler."""

    @pytest.mark.asyncio
    async def test_scheduler_attached_to_app_state(self):
        """After startup, app.state.scheduler must be a Scheduler instance."""
        from app.core.factory import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            scheduler = getattr(app.state, "scheduler", None)
            assert isinstance(scheduler, Scheduler)

    @pytest.mark.asyncio
    async def test_scheduler_cleaned_up_after_lifespan(self):
        """After the lifespan context exits, the scheduler task is stopped."""
        from app.core.factory import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            scheduler = getattr(app.state, "scheduler", None)
            assert scheduler._task is not None
            assert not scheduler._task.done()

        # After the context exits, the task should be cancelled
        assert scheduler._task is None or scheduler._task.done()

    @pytest.mark.asyncio
    async def test_startup_and_shutdown_hooks_still_fire(self):
        """User-provided hooks fire in order alongside the scheduler."""
        from app.core.factory import create_app

        events: list[str] = []

        app = create_app(
            on_startup=[lambda: events.append("startup")],
            on_shutdown=[lambda: events.append("shutdown")],
        )
        async with app.router.lifespan_context(app):
            assert events == ["startup"]

        assert events == ["startup", "shutdown"]
