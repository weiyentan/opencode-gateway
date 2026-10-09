"""Database session management — asyncpg connection pool and reconnect supervision.

Issue #773: the Gateway supervises its PostgreSQL pool.  A failed startup
connect is not a permanent ``pool = None`` state, and post-startup
acquisition failures (e.g. after PostgreSQL failover) trigger exactly one
supervised reconnect cycle instead of one task per request.  The
:class:`DatabasePoolSupervisor` is the focused lifecycle owner: it creates
candidate pools, verifies each with a bounded connection test plus the
required schema initialization, and publishes the verified candidate as the
active pool — so an incompletely initialized pool is never exposed, and a
request always releases its connection through the pool it was acquired
from.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg
from fastapi import HTTPException, Request, status

from app.core.config import Settings
from app.db.schema import ensure_schema

logger = logging.getLogger(__name__)


class DatabasePool:
    """Manages an asyncpg connection pool for the Gateway application.

    One instance owns exactly one underlying asyncpg pool.  The reconnect
    supervisor creates a fresh candidate instance per attempt and swaps
    the published instance on replacement — never mutating a live pool —
    so checked-out connections are always released through the pool they
    were acquired from, never through a replacement pool.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        # Single-flight wake event attached by the reconnect supervisor
        # while this pool is the active published pool.  Requests and
        # probes that observe acquisition failures call
        # ``notify_unavailable()``, which sets the event — the supervisor
        # performs exactly one reconnect cycle for the outage.
        self._wake: asyncio.Event | None = None

    @property
    def pool(self) -> asyncpg.Pool | None:
        """Return the underlying asyncpg pool, or None if not connected."""
        return self._pool

    def attach_wake(self, wake: asyncio.Event) -> None:
        """Attach the supervisor's single-flight wake event (issue #773).

        While attached, :meth:`notify_unavailable` wakes the supervisor so
        it runs one reconnect cycle for this pool.
        """
        self._wake = wake

    def detach_wake(self) -> None:
        """Detach the wake event.

        After detach, late failures on this pool are no-ops — the
        supervisor is already reconnecting, and a retired pool must never
        re-trigger a reconnect cycle.
        """
        self._wake = None

    def notify_unavailable(self) -> None:
        """Signal that this pool's backend is unreachable (single-flight).

        Sets the attached wake event if any; otherwise a no-op.  Called
        from session/probe acquisition failures — it never spawns a task.
        """
        wake = self._wake
        if wake is not None:
            wake.set()

    async def connect(self) -> None:
        """Initialize the connection pool from settings."""
        pool_kwargs = dict(
            host=self._settings.database_host,
            port=self._settings.database_port,
            database=self._settings.database_name,
            user=self._settings.database_user,
            password=self._settings.database_password,
            min_size=self._settings.database_min_connections,
            max_size=self._settings.database_max_connections,
            timeout=self._settings.database_connection_timeout,
            max_inactive_connection_lifetime=(
                self._settings.database_max_inactive_connection_lifetime
            ),
        )
        if self._settings.database_ssl:
            pool_kwargs["ssl"] = self._settings.database_ssl
        self._pool = await asyncpg.create_pool(**pool_kwargs)

    async def close(self) -> None:
        """Close the connection pool gracefully (idempotent)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def acquire(self) -> asyncpg.Connection:
        """Acquire a connection from the pool."""
        if self._pool is None:
            raise RuntimeError("Connection pool is not initialized")
        return await self._pool.acquire()

    async def release(self, conn: asyncpg.Connection) -> None:
        """Release a connection back to the pool.

        The underlying asyncpg ``release`` is shielded against task
        cancellation, so a connection is always returned to the pool it
        was acquired from even when the request is cancelled mid-flight.
        """
        if self._pool is not None:
            await self._pool.release(conn)

    async def test_connection(self) -> None:
        """Verify the pool hands out a usable connection.

        Runs a ``SELECT 1`` round-trip so a candidate pool whose backend
        is gone (or half-created) is rejected before it is published.
        The caller bounds the call with a timeout.
        """
        if self._pool is None:
            raise RuntimeError("Connection pool is not initialized")
        conn = await self._pool.acquire()
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await self._pool.release(conn)


class DatabasePoolSupervisor:
    """Focused lifecycle owner for the Gateway database pool (issue #773).

    Owns ONE supervised background reconnect loop that:

    * performs the initial connect attempt with a bounded per-attempt
      timeout, and keeps retrying across a prolonged outage using capped
      exponential backoff with jitter — never a busy loop, never one
      reconnect task per request;
    * verifies every candidate pool with a connection test AND the
      required schema initialization (``ensure_schema``) before
      publishing it as the active pool — an incomplete pool is never
      exposed, and migrations never run concurrently (the loop serialises
      all attempts);
    * wakes exactly once on the first observed acquisition failure
      (single-flight wake attached to the active pool) and replaces the
      failed pool with a verified candidate;
    * retires the replaced pool (closed only after the replacement is
      published so in-flight connections are not discarded), and cancels
      cleanly on shutdown, closing in-flight candidates, the active pool,
      and retired pools.

    The supervisor is the only reconnector: requests and probes merely
    signal unavailability (``DatabasePool.notify_unavailable``); they
    never start or manage reconnection work.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        publish: Callable[[DatabasePool | None], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._publish: Callable[[DatabasePool | None], Any] = (
            publish if publish is not None else (lambda _pool: None)
        )
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current: DatabasePool | None = None
        self._retired: list[DatabasePool] = []
        self._attempts = 0
        self._cap_warned = False
        self._first_attempt_done = asyncio.Event()

    @property
    def pool(self) -> DatabasePool | None:
        """Return the currently published DatabasePool, or None."""
        return self._current

    async def start(self) -> None:
        """Start the supervised loop and wait for the first attempt outcome.

        The first connect attempt runs inside the loop so startup observes
        the same publish-or-fail semantics as before issue #773, while
        later attempts are supervised in the background.  ``publish(None)``
        is issued immediately so ``app.state.pool`` always exists.
        """
        if self._task is not None:
            return
        self._publish(None)
        self._task = asyncio.create_task(
            self._run(), name="database-pool-supervisor"
        )
        first_task = asyncio.create_task(self._first_attempt_done.wait())
        try:
            done, _pending = await asyncio.wait(
                {self._task, first_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
        if self._task in done and self._task.exception() is not None:
            exc = self._task.exception()
            raise RuntimeError(
                "database pool supervisor failed during startup"
            ) from exc

    async def stop(self) -> None:
        """Stop the supervisor.

        Requests shutdown, cancels and awaits the reconnect loop, then
        closes the active pool and every retired pool.  Idempotent; safe
        to call once from lifespan shutdown.
        """
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — shutdown must not raise
                logger.warning(
                    "Database supervisor loop exited with an error during shutdown",
                    exc_info=exc,
                )
        current = self._current
        self._current = None
        if current is not None:
            current.detach_wake()
            await self._safe_close(current)
        retired, self._retired = self._retired, []
        for pool in retired:
            await self._safe_close(pool)
        self._wake = asyncio.Event()

    async def _run(self) -> None:
        """The supervised reconnect loop (see class docstring)."""
        candidate: DatabasePool | None = None
        try:
            while not self._stop.is_set():
                if self._attempts > 0:
                    logger.debug(
                        "Database reconnect attempt %d starting",
                        self._attempts + 1,
                    )
                candidate = DatabasePool(self._settings)
                try:
                    await asyncio.wait_for(
                        candidate.connect(),
                        timeout=self._settings.reconnect_timeout_seconds,
                    )
                except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — py39 compat
                    logger.debug(
                        "Database reconnect attempt %d timed out after %.1fs",
                        self._attempts + 1,
                        self._settings.reconnect_timeout_seconds,
                    )
                    await self._safe_close(candidate)
                    candidate = None
                    await self._after_failed_attempt()
                    continue
                except Exception as exc:  # noqa: BLE001 — an outage must never escape the loop
                    logger.debug(
                        "Database reconnect attempt %d connect failed: %r",
                        self._attempts + 1,
                        exc,
                    )
                    await self._safe_close(candidate)
                    candidate = None
                    await self._after_failed_attempt()
                    continue

                # Candidate connected — verify usability and required schema
                # initialization BEFORE publishing anything.
                inner_pool = candidate.pool
                try:
                    await asyncio.wait_for(
                        candidate.test_connection(),
                        timeout=self._settings.reconnect_timeout_seconds,
                    )
                    if inner_pool is None:  # pragma: no cover — connect() just set it
                        raise RuntimeError("candidate pool lost after connect")
                    await ensure_schema(inner_pool)
                except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041 — py39 compat
                    logger.warning("Database candidate connection test timed out")
                    await self._safe_close(candidate)
                    candidate = None
                    await self._after_failed_attempt()
                    continue
                except Exception as exc:  # noqa: BLE001 — schema failure is retried, not fatal
                    logger.warning(
                        "Database candidate initialization failed: %r", exc
                    )
                    await self._safe_close(candidate)
                    candidate = None
                    await self._after_failed_attempt()
                    continue

                # Verified — publish as the active pool.
                previous = self._current
                failed_before = self._attempts
                self._attempts = 0
                self._cap_warned = False
                self._current = candidate
                candidate.attach_wake(self._wake)
                self._wake.clear()
                self._publish(candidate)
                candidate = None  # ownership transferred to self._current
                self._first_attempt_done.set()

                if previous is not None:
                    # Retire the replaced pool AFTER the new one is live, so
                    # in-flight connections keep their owning pool until they
                    # are released.
                    previous.detach_wake()
                    self._retired.append(previous)
                    await self._safe_close(previous)

                self._log_published(failed_before, previous)

                if await self._wait_failure_or_stop():
                    break

                self._mark_unavailable(self._current)
        finally:
            if candidate is not None:
                await self._safe_close(candidate)

    async def _wait_failure_or_stop(self) -> bool:
        """Wait until the active pool reports unavailability or shutdown.

        Returns True when shutdown was requested (the loop exits and
        :meth:`stop` closes the pools).
        """
        wake_task = asyncio.create_task(self._wake.wait())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, _pending = await asyncio.wait(
                {wake_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            return stop_task in done
        finally:
            for task in (wake_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, stop_task, return_exceptions=True)

    async def _after_failed_attempt(self) -> None:
        """Apply capped exponential backoff with jitter after a failed attempt."""
        self._attempts += 1
        delay = self._next_delay()
        if self._attempts == 1:
            logger.warning(
                "Database unavailable — connect attempt %d failed; Gateway "
                "not ready; next attempt in %.2fs",
                self._attempts,
                delay,
            )
        else:
            logger.debug(
                "Database reconnect attempt %d failed; next attempt in %.2fs",
                self._attempts,
                delay,
            )
        if (
            delay >= self._settings.reconnect_max_backoff_seconds
            and not self._cap_warned
        ):
            self._cap_warned = True
            logger.warning(
                "Database reconnect backoff reached the configured cap of "
                "%.2fs — sustained outage; supervised retries continue at a "
                "bounded rate",
                self._settings.reconnect_max_backoff_seconds,
            )
        self._first_attempt_done.set()
        await self._sleep(delay)

    def _next_delay(self) -> float:
        """Capped exponential backoff with jitter (never exceeds the cap)."""
        base = self._settings.reconnect_initial_backoff_seconds * (
            2.0 ** (self._attempts - 1)
        )
        base = min(base, self._settings.reconnect_max_backoff_seconds)
        jitter = self._settings.reconnect_jitter_ratio
        factor: float = (
            random.uniform(1 - jitter, 1 + jitter) if jitter > 0 else 1.0
        )
        return min(base * factor, self._settings.reconnect_max_backoff_seconds)

    async def _sleep(self, delay: float) -> None:
        """Sleep for *delay* — cancellable so shutdown never waits it out."""
        await asyncio.sleep(delay)

    def _mark_unavailable(self, pool: DatabasePool) -> None:
        """Transition to unavailable: detach the wake and log once.

        The failed pool stays published — /ready and request paths return
        503 independently while the loop reconnects, and the pool is
        closed only when a verified replacement is published.
        """
        pool.detach_wake()
        logger.warning(
            "Database connection lost — Gateway marked not ready; supervised "
            "reconnect started (capped backoff %.2fs)",
            self._settings.reconnect_max_backoff_seconds,
        )

    def _log_published(
        self, failed_before: int, previous: DatabasePool | None
    ) -> None:
        """Log the publish outcome — first connect vs outage recovery."""
        if previous is None and failed_before == 0:
            logger.info("Database connected — Gateway ready")
        elif previous is None:
            logger.warning(
                "Database connectivity recovered after %d failed attempt(s) "
                "— Gateway ready",
                failed_before,
            )
        else:
            logger.warning(
                "Database connectivity recovered — pool replaced; Gateway ready"
            )

    async def _safe_close(self, pool: DatabasePool) -> None:
        """Close a pool best-effort under a bounded timeout.

        Cancellation propagates (asyncpg terminates the pool when its
        ``close`` is cancelled), so shutdown always proceeds.
        """
        try:
            await asyncio.wait_for(
                pool.close(),
                timeout=self._settings.reconnect_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — closing must never break the loop
            logger.debug("Database pool close failed (best-effort)", exc_info=True)


async def get_session(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency that yields a database connection from the pool.

    Fails safely when PostgreSQL is unavailable (issue #772): a missing,
    absent, or uninitialized pool — and a failed connection acquisition —
    raise a controlled 503 Service Unavailable through the standard API
    error envelope, instead of the former ``AttributeError``/``RuntimeError``
    that surfaced as an unhandled 500.  The healthy path is unchanged.

    An acquisition failure also notifies the reconnect supervisor
    (issue #773) — the single-flight wake collapses concurrent failures
    into exactly one supervised reconnect cycle.
    """
    db_pool: DatabasePool | None = getattr(request.app.state, "pool", None)
    if db_pool is None or db_pool.pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection pool is not initialized",
        )

    try:
        conn = await db_pool.acquire()
    except Exception as exc:  # noqa: BLE001 — DB outage must map to 503
        logger.warning("Database connection acquisition failed", exc_info=exc)
        db_pool.notify_unavailable()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from exc

    try:
        yield conn
    finally:
        try:
            await db_pool.release(conn)
        except Exception as exc:  # noqa: BLE001 — teardown must stay graceful
            logger.warning("Database connection release failed", exc_info=exc)