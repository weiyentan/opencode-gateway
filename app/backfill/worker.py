"""Dedicated AFK backfill worker (API-triggered AFK backfill).

Claims queued jobs from the durable ``afk_backfill_jobs`` table and executes
the existing ``scripts.afk_backfill.run_backfill`` orchestration — the worker
never re-implements correlation or persistence semantics.

Operational model (mirrors :mod:`app.consumer.afk_consumer`):

* **Per-repository serialization** — before claiming, the worker acquires a
  session-level advisory lock keyed on (provider, repository)
  (``pg_try_advisory_lock`` held on a dedicated connection for the whole
  job).  One job per repository runs at a time across ALL worker instances;
  unrelated repositories proceed concurrently (each running job is an asyncio
  task, bounded by ``GATEWAY_BACKFILL_MAX_CONCURRENT_JOBS``).
* **Bounded retries** — transient provider/database failures are retried at
  most ``GATEWAY_BACKFILL_MAX_RETRIES`` times with exponential backoff while
  the job stays ``running``.  Retries are safe by construction: existing
  writes are idempotent (enrich-only upserts, ``ON CONFLICT DO NOTHING``), so
  a failed job keeps every successful write.  Non-transient failures (e.g.
  provider 4xx) fail fast.
* **Crash recovery** — ``running`` jobs whose claim is older than
  ``GATEWAY_BACKFILL_STALE_RUNNING_HOURS`` are reclaimed as ``failed`` with
  category ``interrupted`` on the next sweep (a crashed worker never wedges
  the queue; advisory locks die with the session).
* **Retention** — completed/failed/cancelled rows are pruned after
  ``GATEWAY_BACKFILL_RETENTION_DAYS`` (default 90).

Entry point: ``python -m app.backfill.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
from asyncpg.exceptions import PostgresConnectionError

from app.backfill.jobs import (
    BackfillJobStore,
    bounded_evidence,
)
from app.core.telemetry import timed_operation
from app.db.lock import BACKFILL_LOCK_CLASS
from scripts.afk_backfill import BackfillReport, _build_adapter, run_backfill

logger = logging.getLogger(__name__)

# ── Defaults (overridable via Settings / constructor) ────────────────────────

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETENTION_DAYS = 90
DEFAULT_STALE_RUNNING_HOURS = 24.0
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
MAX_FAILURE_MESSAGE_CHARS = 500

# Run the retention/stale sweep roughly once per minute (not every poll).
SWEEP_EVERY_CYCLES = 12

# ── Failure classification ───────────────────────────────────────────────────


def is_transient_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` is worth retrying with backoff.

    Transient: provider network/5xx failures, connection-level Postgres
    failures, and timeouts.  Non-transient: provider 4xx responses (a bad
    token or a missing repository will never succeed by retrying), data-level
    database errors, and anything unexpected — those fail fast.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    if isinstance(exc, PostgresConnectionError):
        return True
    return False


def _failure_category(exc: BaseException) -> str:
    """A safe, stable failure category for the job audit fields."""
    if isinstance(exc, PostgresConnectionError):
        return "database_error"
    if isinstance(exc, httpx.HTTPError):
        return "provider_error"
    return "internal_error"


def repo_lock_key(provider: str, repository: str) -> int:
    """Stable int4 advisory-lock key for a (provider, repository) pair.

    The low 32 bits of the identity hash are interpreted as *signed* so the
    value always binds to ``pg_try_advisory_lock``'s int4 argument — the same
    mapping the reconciliation lock applies (issue #395).
    """
    digest = hashlib.sha256(f"{provider}:{repository}".encode()).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


# ── Worker ───────────────────────────────────────────────────────────────────


class BackfillWorker:
    """Claims and executes durable backfill jobs until shut down."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        stale_running_hours: float = DEFAULT_STALE_RUNNING_HOURS,
        max_evidence_lines: int = 200,
        initial_backoff: float = INITIAL_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._pool = pool
        self._poll_seconds = poll_seconds
        self._max_concurrent_jobs = max_concurrent_jobs
        self._max_retries = max_retries
        self._retention_days = retention_days
        self._stale_running_hours = stale_running_hours
        self._max_evidence_lines = max_evidence_lines
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._held_repos: set[tuple[str, str]] = set()
        self._cycles = 0
        self._owns_pool = False

    # ── Factory ────────────────────────────────────────────────────────

    @classmethod
    async def from_env(cls) -> BackfillWorker:
        """Build a worker from application settings (mirrors the consumer factory)."""
        from app.core.config import get_settings

        settings = get_settings()
        pool = await asyncpg.create_pool(
            host=settings.database_host,
            port=settings.database_port,
            database=settings.database_name,
            user=settings.database_user,
            password=settings.database_password,
            min_size=1,
            max_size=max(4, settings.backfill_max_concurrent_jobs * 2 + 2),
        )
        worker = cls(
            pool=pool,
            poll_seconds=settings.backfill_worker_poll_seconds,
            max_concurrent_jobs=settings.backfill_max_concurrent_jobs,
            max_retries=settings.backfill_max_retries,
            retention_days=settings.backfill_retention_days,
            stale_running_hours=settings.backfill_stale_running_hours,
            max_evidence_lines=settings.backfill_max_evidence_lines,
        )
        worker._owns_pool = True
        return worker

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin polling (registers shutdown signal handlers)."""
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._signal_handler)
            except NotImplementedError:
                # Signal handlers not available on this platform
                pass
        logger.info(
            "Backfill worker started: poll=%.1fs concurrency=%d retries=%d retention=%dd",
            self._poll_seconds,
            self._max_concurrent_jobs,
            self._max_retries,
            self._retention_days,
        )

    async def run(self) -> None:
        """Main loop: schedule claims, sweep periodically, sleep."""
        while self._running:
            try:
                await self._schedule_claims()
                self._cycles += 1
                if self._cycles % SWEEP_EVERY_CYCLES == 0:
                    await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Backfill worker cycle failed")
            try:
                await asyncio.sleep(self._poll_seconds)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        """Graceful shutdown: stop claiming, drain in-flight jobs."""
        logger.info("Stopping backfill worker …")
        self._running = False
        self._shutdown_event.set()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._held_repos.clear()
        if self._owns_pool:
            await self._pool.close()
        logger.info("Backfill worker stopped")

    def _signal_handler(self) -> None:
        """Handle SIGTERM / SIGINT — trigger graceful shutdown."""
        logger.info("Shutdown signal received — draining …")
        self._running = False
        self._shutdown_event.set()

    # ── Scheduling ─────────────────────────────────────────────────────

    async def _schedule_claims(self) -> None:
        """Claim queued jobs for repos not currently held, up to the concurrency cap."""
        self._prune_done_tasks()
        if len(self._tasks) >= self._max_concurrent_jobs:
            return
        async with self._pool.acquire() as conn:
            repos = await BackfillJobStore(conn).queued_repositories()
        for provider, repository in repos:
            if (provider, repository) in self._held_repos:
                continue
            self._held_repos.add((provider, repository))
            task = asyncio.create_task(
                self._run_next_job(provider, repository),
                name=f"backfill:{provider}:{repository}",
            )
            self._tasks.add(task)
            if len(self._tasks) >= self._max_concurrent_jobs:
                break

    def _prune_done_tasks(self) -> None:
        """Drop finished tasks so their concurrency slots free up."""
        self._tasks = {task for task in self._tasks if not task.done()}

    async def _sweep(self) -> None:
        """Prune expired terminal rows and reclaim stale running jobs."""
        now = datetime.now(timezone.utc)  # noqa: UP017
        retention_cutoff = now - timedelta(days=self._retention_days)
        stale_cutoff = now - timedelta(hours=self._stale_running_hours)
        try:
            async with self._pool.acquire() as conn:
                store = BackfillJobStore(conn)
                pruned = await store.prune(retention_cutoff)
                reclaimed = await store.reclaim_stale(stale_cutoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Backfill retention sweep failed")
            return
        if pruned:
            logger.info(
                "backfill.job.pruned", extra={"pruned_count": pruned}
            )
        for job in reclaimed:
            logger.warning(
                "backfill.job.stale_reclaimed",
                extra={
                    "job_id": str(job["id"]),
                    "provider": job["provider"],
                    "repository": job["repository"],
                },
            )

    # ── One repository's next job ──────────────────────────────────────

    async def _run_next_job(self, provider: str, repository: str) -> None:
        """Hold the per-repo advisory lock, claim and run one job, release."""
        key = repo_lock_key(provider, repository)
        lock_conn: asyncpg.Connection | None = None
        try:
            lock_conn = await self._pool.acquire()
            got = await lock_conn.fetchval(
                "SELECT pg_try_advisory_lock($1, $2)", BACKFILL_LOCK_CLASS, key
            )
            if not got:
                # Another worker instance is already running this repository.
                return
            job = await BackfillJobStore(lock_conn).claim(provider, repository)
            if job is None:
                return
            logger.info(
                "backfill.job.claimed",
                extra={
                    "job_id": str(job["id"]),
                    "provider": provider,
                    "repository": repository,
                    "queued_since": str(job["created_at"]),
                },
            )
            async with self._pool.acquire() as run_conn:
                await self._run_job(run_conn, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "backfill.job.task.error",
                extra={"provider": provider, "repository": repository},
            )
        finally:
            if lock_conn is not None:
                with contextlib.suppress(Exception):
                    await lock_conn.execute(
                        "SELECT pg_advisory_unlock($1, $2)",
                        BACKFILL_LOCK_CLASS,
                        key,
                    )
                await self._pool.release(lock_conn)
            self._held_repos.discard((provider, repository))

    # ── One job ────────────────────────────────────────────────────────

    async def _run_job(self, conn: asyncpg.Connection, job: dict[str, Any]) -> None:
        """Execute one claimed job through the backfill engine, with bounded retries."""
        job_id = job["id"]
        started = datetime.now(timezone.utc)  # noqa: UP017
        store = BackfillJobStore(conn)
        adapter, client = _build_adapter(job["provider"])
        retries = 0
        try:
            async with timed_operation("backfill.job.run", "external", correlation_id=str(job_id)):
                while True:
                    try:
                        report: BackfillReport = await run_backfill(
                            conn,
                            adapter=adapter,
                            repository=job["repository"],
                            since=job["window_from"],
                            until=job["window_until"],
                            dry_run=job["dry_run"],
                            show_evidence=job["show_evidence"],
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if retries >= self._max_retries or not is_transient_error(exc):
                            raise
                        retries += 1
                        await store.increment_retry(job_id)
                        delay = min(
                            self._initial_backoff * (2 ** (retries - 1)),
                            self._max_backoff,
                        )
                        logger.warning(
                            "backfill.job.retry",
                            extra={
                                "job_id": str(job_id),
                                "attempt": retries,
                                "delay_seconds": round(delay, 2),
                                "error_type": type(exc).__name__,
                            },
                        )
                        await asyncio.sleep(delay)

            evidence = (
                bounded_evidence(
                    report.evidence_lines, max_lines=self._max_evidence_lines
                )
                if job["show_evidence"]
                else None
            )
            await store.complete(
                job_id, report=report, evidence=evidence, retry_count=retries
            )
            duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000  # noqa: UP017
            logger.info(
                "backfill.job.completed",
                extra={
                    "job_id": str(job_id),
                    "provider": job["provider"],
                    "repository": job["repository"],
                    "duration_ms": round(duration_ms, 3),
                    "retries": retries,
                    "change_requests_scanned": report.change_requests_scanned,
                    "issues_scanned": report.issues_scanned,
                    "sessions_considered": report.sessions_considered,
                    "explicit_matches": report.explicit_matches,
                    "high_matches": report.high_matches,
                    "inferred_matches": report.inferred_matches,
                    "ambiguous": report.ambiguous,
                    "unmatched": report.unmatched,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = _failure_category(exc)
            message = str(exc)[:MAX_FAILURE_MESSAGE_CHARS] or type(exc).__name__
            await store.fail(
                job_id,
                category=category,
                message=message,
                retry_count=retries,
            )
            duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000  # noqa: UP017
            logger.error(
                "backfill.job.failed",
                extra={
                    "job_id": str(job_id),
                    "provider": job["provider"],
                    "repository": job["repository"],
                    "failure_category": category,
                    "retries": retries,
                    "duration_ms": round(duration_ms, 3),
                    "error_type": type(exc).__name__,
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()


# ── Entry point ────────────────────────────────────────────────────────────


async def _main() -> None:
    """Entry point when run as ``python -m app.backfill.worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    worker = await BackfillWorker.from_env()
    await worker.start()
    try:
        await worker.run()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
