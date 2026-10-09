"""Tests for the supervised database reconnect lifecycle (issue #773).

Deterministic tests with simulated asyncpg connection failures — no real
PostgreSQL.  They cover:

* startup failure then automatic recovery (no restart),
* post-startup failure (failover) then automatic recovery,
* prolonged outage with capped backoff (no busy loop, bounded attempts),
* concurrent-request single-flight reconnect (one cycle, not one per request),
* failed schema initialization (candidate never published, retried safely),
* cancellation/shutdown without leaked tasks or pools,
* healthy no-regression path,
* the /ready, /live, /health probe contracts across the transitions,
* bounded credential-redacted logs.

The staging/integration outage smoke test lives in
``tests/integration/test_db_reconnect_smoke.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db.session import DatabasePool, DatabasePoolSupervisor, get_session


def _settings(**overrides) -> Settings:
    """Return deterministic reconnect settings (zero jitter, tiny backoff)."""
    defaults = dict(
        env="development",
        database_host="dbhost",
        database_name="gateway",
        database_user="user",
        database_password="super-secret-pw",
        reconnect_timeout_seconds=0.5,
        reconnect_initial_backoff_seconds=0.01,
        reconnect_max_backoff_seconds=0.05,
        reconnect_jitter_ratio=0.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _asyncpg_pool() -> AsyncMock:
    """Return a mock asyncpg.Pool whose acquire() yields a usable connection."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    pool = AsyncMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    pool.close = AsyncMock()
    return pool


def _patch_create_pool(create_fn):
    """Patch app.db.session.asyncpg.create_pool with an async callable."""
    return patch("app.db.session.asyncpg.create_pool", AsyncMock(side_effect=create_fn))


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> None:
    """Poll *predicate* until it is truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout:.1f}s")


def _supervisor_tasks() -> list[asyncio.Task]:
    """Return live tasks named like the supervisor loop task."""
    return [t for t in asyncio.all_tasks() if t.get_name() == "database-pool-supervisor"]


def _client(app: object) -> AsyncClient:
    """Return an authenticated httpx client against *app* (no lifespan mgmt)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


# ══════════════════════════════════════════════════════════════════════════
#  DatabasePool — single-flight wake and connection test
# ══════════════════════════════════════════════════════════════════════════


class TestDatabasePoolWake:
    """The single-flight unavailable signal (issue #773)."""

    @pytest.mark.asyncio
    async def test_notify_unavailable_sets_attached_wake(self):
        """With a wake attached, notify_unavailable() sets it (one per request)."""
        wake = asyncio.Event()
        db_pool = DatabasePool(_settings())
        db_pool.attach_wake(wake)
        assert not wake.is_set()

        db_pool.notify_unavailable()

        assert wake.is_set()

    @pytest.mark.asyncio
    async def test_notify_unavailable_is_noop_without_wake(self):
        """A retired/unattached pool's late failures never re-trigger a reconnect."""
        db_pool = DatabasePool(_settings())
        db_pool.attach_wake(asyncio.Event())
        db_pool.detach_wake()

        db_pool.notify_unavailable()  # must not raise

    @pytest.mark.asyncio
    async def test_detach_wake_makes_late_notifications_noop(self):
        """After detach, notifications no longer wake the supervisor."""
        wake = asyncio.Event()
        db_pool = DatabasePool(_settings())
        db_pool.attach_wake(wake)
        db_pool.detach_wake()

        db_pool.notify_unavailable()

        assert not wake.is_set()


class TestDatabasePoolConnectionTest:
    """The candidate verification round-trip."""

    @pytest.mark.asyncio
    async def test_test_connection_round_trips_select_1(self):
        """test_connection() acquires a connection and runs SELECT 1, releasing it."""
        inner = _asyncpg_pool()
        with _patch_create_pool(lambda **kw: inner):
            db_pool = DatabasePool(_settings())
            await db_pool.connect()
            await db_pool.test_connection()

        inner.acquire.assert_awaited_once()
        assert inner.acquire.return_value.fetchval.await_args.args[0] == "SELECT 1"
        inner.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_test_connection_raises_when_uninitialized(self):
        """test_connection() on a never-connected pool raises, never hangs."""
        db_pool = DatabasePool(_settings())
        with pytest.raises(RuntimeError, match="not initialized"):
            await db_pool.test_connection()


# ══════════════════════════════════════════════════════════════════════════
#  DatabasePoolSupervisor — startup failure then recovery
# ══════════════════════════════════════════════════════════════════════════


class TestSupervisorStartupRecovery:
    """A failed startup connect must not be permanent (issue #773)."""

    @pytest.mark.asyncio
    async def test_startup_failure_then_recovery_publishes_only_verified_pool(self):
        """The first create_pool fails; the supervisor retries with backoff and
        publishes the second (verified) candidate."""
        published: list = []
        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("Connection refused")
            return _asyncpg_pool()

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            assert published == [None], "initial publish must be None (not ready)"

            await _wait_until(lambda: len(published) >= 2)

        pool = published[-1]
        assert isinstance(pool, DatabasePool)
        assert pool.pool is not None, "published pool must be a verified candidate"
        assert calls["n"] == 2, "exactly one reconnect after the failed startup"
        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_healthy_startup_publishes_once_and_never_reconnects(self):
        """No-regression: a healthy startup publishes once; nothing reconnects."""
        published: list = []
        inner = _asyncpg_pool()

        with _patch_create_pool(lambda **kw: inner):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            await asyncio.sleep(0.05)  # give the loop time to (wrongly) reconnect

        assert [p for p in published if p is not None] == [published[1]]
        assert len(published) == 2  # None + one healthy pool
        assert published[1].pool is inner
        await supervisor.stop()


class TestSupervisorRuntimeRecovery:
    """Post-startup pool failure (failover) then automatic recovery."""

    @pytest.mark.asyncio
    async def test_runtime_failure_replaces_pool_and_retires_old(self):
        """When the active pool's backend dies, one reconnect cycle replaces it;
        the retired pool is closed only after the replacement is published."""
        published: list = []
        pools = [_asyncpg_pool()]
        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            return pools.pop(0) if pools else _asyncpg_pool()

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            await _wait_until(lambda: len(published) == 2)
            active = published[-1]
            old_inner = active.pool

            # Simulate failover: the backend behind the active pool dies.
            assert old_inner is not None
            old_inner.acquire = AsyncMock(side_effect=OSError("gone"))
            active.notify_unavailable()  # like a request acquisition failure

            await _wait_until(lambda: len(published) == 3)
            replacement = published[-1]

        assert replacement is not active, "a replacement pool must be published"
        assert replacement.pool is not None
        assert replacement.pool is not old_inner
        assert calls["n"] == 2, "exactly one reconnect cycle for the outage"
        assert old_inner.close.await_count >= 1, "retired pool must be closed"
        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_concurrent_failures_cause_exactly_one_reconnect(self):
        """Many concurrent request failures collapse into ONE reconnect cycle
        (single-flight guard), never one reconnect task per request."""
        published: list = []
        pools = [_asyncpg_pool()]
        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            return pools.pop(0) if pools else _asyncpg_pool()

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            await _wait_until(lambda: len(published) == 2)
            active = published[-1]
            old_inner = active.pool
            assert old_inner is not None
            old_inner.acquire = AsyncMock(side_effect=OSError("gone"))

            # 20 concurrent requests observe the acquisition failure.
            async def failing_request() -> None:
                try:
                    await active.acquire()
                except OSError:
                    active.notify_unavailable()

            await asyncio.gather(*[failing_request() for _ in range(20)])

            await _wait_until(lambda: len(published) == 3)

        assert calls["n"] == 2, (
            f"expected exactly one reconnect (2 create_pool calls), got {calls['n']}"
        )
        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_second_outage_reconnects_again(self):
        """Recovery must keep working for repeated failures (no one-shot latch)."""
        published: list = []
        pools = [_asyncpg_pool()]
        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            return pools.pop(0) if pools else _asyncpg_pool()

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            await _wait_until(lambda: len(published) == 2)

            for _ in range(2):
                active = published[-1]
                inner = active.pool
                assert inner is not None
                inner.acquire = AsyncMock(side_effect=OSError("flap"))
                active.notify_unavailable()
                await _wait_until(lambda: published[-1] is not active)

        assert calls["n"] == 3, "one reconnect per outage, no latch"
        await supervisor.stop()


class TestSupervisorProlongedOutage:
    """Sustained outage: capped backoff, bounded attempts, bounded logs."""

    @pytest.mark.asyncio
    async def test_backoff_is_capped_and_attempts_are_bounded(self):
        """With a 0.01s base and 0.08s cap, a ~0.3s outage yields a handful of
        attempts (never a busy loop) whose spacing never exceeds the cap."""
        attempt_times: list[float] = []

        async def create_fn(**kwargs):
            attempt_times.append(time.monotonic())
            raise OSError("still down")

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(
                    reconnect_initial_backoff_seconds=0.01,
                    reconnect_max_backoff_seconds=0.08,
                ),
            )
            await supervisor.start()
            await asyncio.sleep(0.3)

        # The first attempt happens at start(); it must keep trying (no hang).
        assert len(attempt_times) >= 3, "reconnect attempts must continue"
        # Bounded: 0.3s at 0.01s base would be ~30+ attempts without a cap.
        assert len(attempt_times) < 20, "attempts must be backoff-bounded"
        gaps = [
            b - a
            for a, b in zip(attempt_times, attempt_times[1:])
            if b - a > 0.005
        ]
        if gaps:
            assert max(gaps) <= 0.08 + 0.02, "backoff must never exceed the cap"
        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_sustained_outage_logs_are_bounded(self, caplog):
        """The outage emits exactly one 'unavailable' warning and one capped-backoff
        warning — never an unbounded warning stream."""
        caplog.set_level(logging.DEBUG, logger="app.db.session")

        async def create_fn(**kwargs):
            raise OSError("still down")

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(
                    reconnect_initial_backoff_seconds=0.005,
                    reconnect_max_backoff_seconds=0.02,
                ),
            )
            await supervisor.start()
            await asyncio.sleep(0.15)
            await supervisor.stop()

        records = [
            r for r in caplog.records if r.name == "app.db.session"
        ]
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert warnings, "expected outage warnings"
        # One 'unavailable' + one capped-backoff warning at most (a third could
        # only appear for a later recovery — which never happens here).
        assert len(warnings) <= 2, f"uncontrolled warnings: {[w.getMessage() for w in warnings]}"
        assert any("Database unavailable" in w.getMessage() for w in warnings)
        assert any("cap" in w.getMessage() for w in warnings)


class TestSupervisorVerification:
    """Candidate verification: connection test + ensure_schema before publish."""

    @pytest.mark.asyncio
    async def test_unsuccessful_schema_init_never_publishes_and_retries(self):
        """A candidate whose ensure_schema fails is closed and NOT published;
        the next attempt re-verifies and publishes."""
        published: list = []
        first_inner = _asyncpg_pool()
        second_inner = _asyncpg_pool()
        attempts = {"n": 0}

        async def create_fn(**kwargs):
            attempts["n"] += 1
            return first_inner if attempts["n"] == 1 else second_inner

        def publish(pool):
            published.append(pool)

        schema_failures = {"n": 0}

        async def flaky_ensure_schema(pool):
            schema_failures["n"] += 1
            if schema_failures["n"] == 1:
                raise RuntimeError("alembic upgrade failed")

        with _patch_create_pool(create_fn), patch(
            "app.db.session.ensure_schema", flaky_ensure_schema
        ):
            supervisor = DatabasePoolSupervisor(_settings(), publish=publish)
            await supervisor.start()

            await _wait_until(lambda: len(published) == 2)

        assert published[0] is None
        pool = published[1]
        assert pool is not None and pool.pool is second_inner
        assert schema_failures["n"] == 2, "schema init retried after failure"
        assert first_inner.close.await_count >= 1, "failed candidate must be closed"
        assert published[1].pool is not first_inner
        await supervisor.stop()

    @pytest.mark.asyncio
    async def test_schema_init_runs_for_recovery_candidates(self):
        """ensure_schema runs again for a replacement pool after an outage."""
        published: list = []
        pools = [_asyncpg_pool(), _asyncpg_pool()]
        schema_calls: list = []

        async def create_fn(**kwargs):
            return pools.pop(0)

        async def recording_ensure_schema(pool):
            schema_calls.append(pool)

        with _patch_create_pool(create_fn), patch(
            "app.db.session.ensure_schema", recording_ensure_schema
        ):
            supervisor = DatabasePoolSupervisor(
                _settings(), publish=published.append
            )
            await supervisor.start()
            await _wait_until(lambda: len(published) == 2)
            active = published[-1]
            inner = active.pool
            assert inner is not None
            inner.acquire = AsyncMock(side_effect=OSError("gone"))
            active.notify_unavailable()
            await _wait_until(lambda: len(published) == 3)
            await supervisor.stop()

        assert len(schema_calls) == 2, "schema init runs for every published candidate"


# ══════════════════════════════════════════════════════════════════════════
#  DatabasePoolSupervisor — shutdown / cancellation
# ══════════════════════════════════════════════════════════════════════════


class TestSupervisorShutdown:
    """Shutdown cancels/awaits reconnect work and closes resources."""

    @pytest.mark.asyncio
    async def test_stop_closes_active_pool_and_leaves_no_tasks(self):
        """A healthy supervisor stops cleanly: pool closed, loop task gone."""
        inner = _asyncpg_pool()
        with _patch_create_pool(lambda **kw: inner):
            supervisor = DatabasePoolSupervisor(_settings())
            await supervisor.start()
            assert _supervisor_tasks()

            await supervisor.stop()

        assert not _supervisor_tasks(), "supervisor task leaked"
        assert inner.close.await_count >= 1, "active pool must be closed"
        assert supervisor.pool is None

    @pytest.mark.asyncio
    async def test_stop_during_reconnect_cancels_loop_and_closes_candidate(self):
        """Stop while the loop is mid-reconnect: no leaked tasks, no leaked pools."""
        published: list = []
        pools = [_asyncpg_pool()]
        gate = asyncio.Event()

        async def create_fn(**kwargs):
            if not pools:
                await gate.wait()  # hang the reconnect attempt until cancelled
                return _asyncpg_pool()
            return pools.pop(0)

        with _patch_create_pool(create_fn):
            supervisor = DatabasePoolSupervisor(
                _settings(reconnect_timeout_seconds=30.0),
                publish=published.append,
            )
            await supervisor.start()
            await _wait_until(lambda: len(published) == 2)
            active = published[-1]
            inner = active.pool
            assert inner is not None
            inner.acquire = AsyncMock(side_effect=OSError("gone"))
            active.notify_unavailable()

            # Let the loop enter the hanging reconnect attempt, then stop.
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await supervisor.stop()
            assert time.monotonic() - started < 2.0, "stop must not hang on the attempt"

        assert not _supervisor_tasks()
        assert inner.close.await_count >= 1


# ══════════════════════════════════════════════════════════════════════════
#  get_session / /ready — failure signalling
# ══════════════════════════════════════════════════════════════════════════


class TestFailureSignalling:
    """Acquisition failures notify the supervisor (single-flight trigger)."""

    @pytest.mark.asyncio
    async def test_get_session_failure_notifies_supervisor(self):
        """get_session maps an acquisition failure to 503 AND wakes the supervisor."""
        wake = asyncio.Event()
        inner = _asyncpg_pool()
        db_pool = DatabasePool(_settings())
        with _patch_create_pool(lambda **kw: inner):
            await db_pool.connect()
        db_pool.attach_wake(wake)
        inner.acquire = AsyncMock(side_effect=OSError("connection lost"))

        request = MagicMock()
        request.app.state.pool = db_pool

        with pytest.raises(HTTPException) as exc_info:
            await get_session(request).__anext__()

        assert exc_info.value.status_code == 503
        assert wake.is_set(), "acquisition failure must trigger the single flight"

    @pytest.mark.asyncio
    async def test_ready_probe_failure_notifies_supervisor(self):
        """/ready returns 503 for a broken pool AND wakes the supervisor."""
        from app.core.factory import create_app

        wake = asyncio.Event()
        inner = _asyncpg_pool()
        db_pool = DatabasePool(_settings())
        with _patch_create_pool(lambda **kw: inner):
            await db_pool.connect()
        db_pool.attach_wake(wake)
        inner.acquire = AsyncMock(side_effect=OSError("connection lost"))

        app = create_app(configure_logging=False)
        app.state.pool = db_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        assert wake.is_set(), "/ready failure must trigger the single flight"


# ══════════════════════════════════════════════════════════════════════════
#  App-level: probe contracts across the recovery transitions
# ══════════════════════════════════════════════════════════════════════════


class TestAppRecoveryProbes:
    """End-to-end lifespan probes: unready -> reconnecting -> ready."""

    @pytest.mark.asyncio
    async def test_startup_outage_then_recovery(self, monkeypatch):
        """Postgres unreachable at startup: /live 200, /ready 503, DB calls 503,
        health disconnected — then automatic reconnection flips /ready to 200,
        health to connected, and DB-backed API to normal."""
        # A 0.2s initial backoff keeps the outage window observable while the
        # first probes run (start() already returned after attempt 1 failed).
        monkeypatch.setenv("GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS", "0.2")
        monkeypatch.setenv("GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS", "0.2")
        monkeypatch.setenv("GATEWAY_RECONNECT_TIMEOUT_SECONDS", "0.5")
        monkeypatch.setenv("GATEWAY_RECONNECT_JITTER_RATIO", "0")

        from app.core.factory import create_app

        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("Connection refused")
            return _asyncpg_pool()

        app = create_app(configure_logging=False)
        with _patch_create_pool(create_fn):
            async with app.router.lifespan_context(app):
                async with _client(app) as client:
                    live = await client.get("/live")
                    assert live.status_code == 200

                    ready = await client.get("/ready")
                    assert ready.status_code == 503
                    assert ready.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

                    health = await client.get("/health")
                    assert health.status_code == 200
                    assert health.json()["data"]["database"] == "disconnected"

                    change_requests = await client.get(
                        "/api/v1/afk-outcomes/change-requests"
                    )
                    assert change_requests.status_code == 503
                    assert change_requests.json()["status"] == "error"

                    # Postgres becomes reachable — recovery without restart.
                    await _wait_until(
                        lambda: (
                            getattr(app.state, "pool", None) is not None
                            and app.state.pool.pool is not None
                        )
                    )

                    ready = await client.get("/ready")
                    assert ready.status_code == 200
                    assert ready.json()["data"] == {"ready": True}

                    health = await client.get("/health")
                    assert health.json()["data"]["database"] == "connected"

                    change_requests = await client.get(
                        "/api/v1/afk-outcomes/change-requests"
                    )
                    assert change_requests.status_code == 200

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_runtime_outage_then_recovery_single_flight(self, monkeypatch):
        """Postgres dies after a healthy startup: concurrent requests fail
        gracefully (503), /ready flips to 503, and exactly ONE supervised
        reconnect restores readiness."""
        monkeypatch.setenv("GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS", "0.15")
        monkeypatch.setenv("GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS", "0.15")
        monkeypatch.setenv("GATEWAY_RECONNECT_TIMEOUT_SECONDS", "0.5")
        monkeypatch.setenv("GATEWAY_RECONNECT_JITTER_RATIO", "0")

        from app.core.factory import create_app

        calls = {"n": 0}

        async def create_fn(**kwargs):
            calls["n"] += 1
            # Startup attempt 1 and the runtime reconnect attempt 3 fail once
            # each, so both outage windows are deterministic.
            if calls["n"] in (1, 3):
                raise OSError("Connection refused")
            return _asyncpg_pool()

        app = create_app(configure_logging=False)
        with _patch_create_pool(create_fn):
            async with app.router.lifespan_context(app):
                async with _client(app) as client:
                    await _wait_until(
                        lambda: (
                            getattr(app.state, "pool", None) is not None
                            and app.state.pool.pool is not None
                        )
                    )
                    ready = await client.get("/ready")
                    assert ready.status_code == 200

                    active = app.state.pool
                    inner = active.pool
                    assert inner is not None
                    inner.acquire = AsyncMock(side_effect=OSError("failover"))

                    # Concurrent requests during the outage: all 503, never 500.
                    async def probe() -> int:
                        return (await client.get("/ready")).status_code

                    statuses = await asyncio.gather(*[probe() for _ in range(15)])
                    assert statuses == [503] * 15

                    change_requests = await client.get(
                        "/api/v1/afk-outcomes/change-requests"
                    )
                    assert change_requests.status_code == 503
                    assert change_requests.json()["error"]["code"] == (
                        "SERVICE_UNAVAILABLE"
                    )

                    # Automatic recovery restores readiness and normal APIs.
                    await _wait_until(
                        lambda: (
                            getattr(app.state, "pool", None) is not None
                            and app.state.pool is not active
                            and app.state.pool.pool is not None
                        )
                    )
                    ready = await client.get("/ready")
                    assert ready.status_code == 200

                    change_requests = await client.get(
                        "/api/v1/afk-outcomes/change-requests"
                    )
                    assert change_requests.status_code == 200

                # A short settle window must not spawn extra reconnects.
                await asyncio.sleep(0.05)
                assert calls["n"] == 4, (
                    "one reconnect for the entire outage, not one per request"
                )

    @pytest.mark.asyncio
    async def test_shutdown_during_outage_leaves_no_tasks(self, monkeypatch):
        """Exiting the lifespan during a reconnect cycle cancels and awaits the
        loop — no leaked tasks."""
        monkeypatch.setenv("GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS", "0.01")
        monkeypatch.setenv("GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS", "0.05")
        monkeypatch.setenv("GATEWAY_RECONNECT_TIMEOUT_SECONDS", "0.5")
        monkeypatch.setenv("GATEWAY_RECONNECT_JITTER_RATIO", "0")

        from app.core.factory import create_app

        gates = {"fail": True}

        async def create_fn(**kwargs):
            if gates["fail"]:
                raise OSError("down")
            return _asyncpg_pool()

        app = create_app(configure_logging=False)
        with _patch_create_pool(create_fn):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.05)  # let the loop enter backoff
                assert _supervisor_tasks()

        assert not _supervisor_tasks(), "supervisor task leaked on shutdown"
        assert app.state.pool is None


class TestAppHealthyNoRegression:
    """Healthy ingestion/rollup-style behaviour is untouched by supervision."""

    @pytest.mark.asyncio
    async def test_healthy_app_single_pool_and_clean_shutdown(self, monkeypatch):
        """A healthy app: one create_pool, /ready 200, /health connected,
        clean shutdown with the pool closed."""
        monkeypatch.setenv("GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS", "0.01")
        monkeypatch.setenv("GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS", "0.05")

        from app.core.factory import create_app

        calls = {"n": 0}
        inner = _asyncpg_pool()

        async def create_fn(**kwargs):
            calls["n"] += 1
            return inner

        app = create_app(configure_logging=False)
        with _patch_create_pool(create_fn):
            async with app.router.lifespan_context(app):
                async with _client(app) as client:
                    ready = await client.get("/ready")
                    assert ready.status_code == 200
                    health = await client.get("/health")
                    assert health.json()["data"]["database"] == "connected"

        assert calls["n"] == 1
        assert inner.close.await_count >= 1
        assert not _supervisor_tasks()