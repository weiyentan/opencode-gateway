"""Integration tests for the AWX execution reconciliation path (issue #637, F2).

Runs against the docker-compose Postgres (port 5433) and exercises the
reconciler's two repository seams against a **real** database — the piece the
unit tests (``tests/test_afk_execution_reconciliation.py``) deliberately fake:

* ``AsyncpgOutcomeRepository.list_running_execution_bindings`` — the
  discovery seam actually queries ``execution_bindings`` for ``running``
  rows, applies the ``max_age_seconds`` window and the ``limit`` bound, and
  never returns terminal rows.
* ``AsyncpgOutcomeRepository.update_execution_binding_terminal`` — the
  persistence seam transitions a real ``running`` row in place (no duplicate
  insert) to a terminal outcome, and the transitioned binding is no longer
  discovered by a subsequent reconciliation pass.
* ``ExecutionReconciler`` composed with the real repository and a fake AWX
  lookup — the full discovery → lookup → persist path inserts no second row
  and ends with a terminal outcome in the database.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_execution_reconciliation_integration.py -v -m integration
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from afk_outcomes.models import ExecutionOutcome
from afk_outcomes.repository import AsyncpgOutcomeRepository
from afk_outcomes.service.execution_reconciliation import (
    AWXJobState,
    BindingResultKind,
    ExecutionReconciler,
)

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

_FINISHED = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _dsn() -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


async def _can_connect() -> bool:
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=_dsn(), timeout=5), timeout=10.0
        )
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _integration_db_available() -> bool:
    if not await _can_connect():
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


def _migration_script_dir() -> str:
    """Return a Python-3.9-import-safe copy of the alembic script directory.

    The pre-existing 0024/0025 migrations evaluate ``str | None`` module-level
    annotations, which cannot import on Python 3.9.  Copy ``env.py`` and the
    version modules to a temp dir with ``from __future__ import annotations``
    injected so the revision map builds on 3.9 without touching the shipped
    migrations — the migration bodies are byte-for-byte identical.
    """
    import shutil
    import tempfile

    src = _PROJ_ROOT / "alembic"
    dst = Path(tempfile.mkdtemp(prefix="gateway-alembic-"))
    shutil.copy(src / "env.py", dst / "env.py")
    (dst / "versions").mkdir()
    for version in sorted((src / "versions").glob("*.py")):
        text = version.read_text()
        if not text.startswith("from __future__ import annotations"):
            text = "from __future__ import annotations\n" + text
        (dst / "versions" / version.name).write_text(text)
    return str(dst)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config
    import shutil

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    migration_dir = _migration_script_dir()
    alembic_cfg.set_main_option("script_location", migration_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        alembic.command.upgrade(alembic_cfg, "head")

    # alembic/env.py's online path drives asyncpg via ``asyncio.run``, which
    # cannot execute inside this fixture's running event loop — run the
    # upgrade in a worker thread where no loop is running.
    try:
        await asyncio.to_thread(_upgrade)
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        await asyncio.to_thread(_upgrade)

    yield pool

    shutil.rmtree(migration_dir, ignore_errors=True)

    async with pool.acquire() as conn:
        await conn.execute(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        )
    await pool.close()


# ── Builders ─────────────────────────────────────────────────────────────────


def _new_job_id() -> int:
    """A unique BigInteger AWX job id (fits ``execution_bindings.awx_job_id``)."""
    return int(uuid.uuid4().int >> 96)


async def _insert_binding(
    conn: asyncpg.Connection,
    *,
    awx_job_id: int,
    outcome: str = "running",
    created_at: datetime | None = None,
) -> None:
    """Insert one ``execution_bindings`` row directly at the SQL level.

    The two-phase lifecycle (migration 0041) makes the change-request
    identity columns nullable, so a resource-less ``running`` row is a valid
    provisioning row exactly as the API writes it.  ``created_at`` may be
    pinned to exercise the discovery age window.
    """
    await conn.execute(
        """
        INSERT INTO execution_bindings
            (awx_job_id, job_template_id, outcome, created_at, updated_at)
        VALUES ($1, $2, $3, COALESCE($4, now()), COALESCE($4, now()))
        """,
        awx_job_id,
        7,
        outcome,
        created_at,
    )


def _discovered_job_ids(bindings: list) -> set[int]:
    """The AWX job ids present in a discovery result."""
    return {int(binding.awx_job.job_id) for binding in bindings}


class _FakeAWXLookup:
    """Deterministic AWX job-status seam for the end-to-end reconciler test."""

    def __init__(self, jobs: dict[int, AWXJobState]) -> None:
        self._jobs = jobs
        self.calls: list[int] = []

    async def get_job(self, job_id: int) -> AWXJobState | None:
        self.calls.append(job_id)
        return self._jobs.get(job_id)


# ── Discovery against the real database ──────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_list_running_execution_bindings_discovers_running_row(
    db_pool: asyncpg.Pool,
) -> None:
    """A real ``running`` row is discovered through the repository seam."""
    awx_job_id = _new_job_id()
    async with db_pool.acquire() as conn:
        await _insert_binding(conn, awx_job_id=awx_job_id, outcome="running")
        repo = AsyncpgOutcomeRepository(conn)

        discovered = await repo.list_running_execution_bindings()

    discovered_ids = _discovered_job_ids(discovered)
    assert awx_job_id in discovered_ids, (
        f"running binding {awx_job_id} was not discovered; got {discovered_ids}"
    )
    binding = next(b for b in discovered if int(b.awx_job.job_id) == awx_job_id)
    assert binding.outcome is ExecutionOutcome.RUNNING
    assert binding.awx_job.job_template_id == 7


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_list_running_execution_bindings_excludes_terminal_rows(
    db_pool: asyncpg.Pool,
) -> None:
    """A terminal row is never returned by the discovery seam."""
    awx_job_id = _new_job_id()
    async with db_pool.acquire() as conn:
        await _insert_binding(conn, awx_job_id=awx_job_id, outcome="completed")
        repo = AsyncpgOutcomeRepository(conn)

        discovered = await repo.list_running_execution_bindings()

    assert awx_job_id not in _discovered_job_ids(discovered)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_list_running_execution_bindings_applies_max_age_window(
    db_pool: asyncpg.Pool,
) -> None:
    """``max_age_seconds`` excludes recently-created bindings and keeps old ones.

    A binding created moments ago may still be legitimately running and must
    not be reconciled prematurely; one created beyond the age window is a
    genuine stuck-candidate and is discovered.
    """
    stale_job_id = _new_job_id()
    fresh_job_id = _new_job_id()
    now = datetime.now(tz=UTC)
    async with db_pool.acquire() as conn:
        await _insert_binding(
            conn,
            awx_job_id=stale_job_id,
            outcome="running",
            created_at=now - timedelta(hours=2),
        )
        await _insert_binding(
            conn,
            awx_job_id=fresh_job_id,
            outcome="running",
            created_at=now,
        )
        repo = AsyncpgOutcomeRepository(conn)

        discovered = await repo.list_running_execution_bindings(
            max_age_seconds=3600
        )

    discovered_ids = _discovered_job_ids(discovered)
    assert stale_job_id in discovered_ids
    assert fresh_job_id not in discovered_ids


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_list_running_execution_bindings_honours_limit(
    db_pool: asyncpg.Pool,
) -> None:
    """The oldest ``limit`` running rows are returned, oldest first."""
    # Pin the three rows far in the past so they precede any rows left behind
    # by other tests and the limit is deterministic.
    base = datetime(2000, 1, 1, tzinfo=UTC)
    job_ids = [_new_job_id() for _ in range(3)]
    async with db_pool.acquire() as conn:
        for index, job_id in enumerate(job_ids):
            await _insert_binding(
                conn,
                awx_job_id=job_id,
                outcome="running",
                created_at=base + timedelta(seconds=index),
            )
        repo = AsyncpgOutcomeRepository(conn)

        discovered = await repo.list_running_execution_bindings(limit=2)

    assert len(discovered) == 2
    assert _discovered_job_ids(discovered) == set(job_ids[:2])


# ── Terminal transition against the real database ────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_update_execution_binding_terminal_transitions_running_row(
    db_pool: asyncpg.Pool,
) -> None:
    """A real ``running`` row transitions in place to ``failed``."""
    awx_job_id = _new_job_id()
    async with db_pool.acquire() as conn:
        await _insert_binding(conn, awx_job_id=awx_job_id, outcome="running")
        repo = AsyncpgOutcomeRepository(conn)

        result = await repo.update_execution_binding_terminal(
            awx_job_id=str(awx_job_id),
            outcome=ExecutionOutcome.FAILED,
            finished_at=_FINISHED,
        )

        row = await conn.fetchrow(
            "SELECT outcome, finished_at FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )

    assert result.is_updated is True
    assert result.is_conflict is False
    assert result.not_found is False
    assert row is not None
    assert row["outcome"] == "failed"
    assert row["finished_at"] == _FINISHED
    # Transitioned in place — no duplicate row was inserted.
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_transitioned_binding_is_no_longer_discovered(
    db_pool: asyncpg.Pool,
) -> None:
    """After a terminal transition the binding drops out of discovery."""
    awx_job_id = _new_job_id()
    async with db_pool.acquire() as conn:
        await _insert_binding(conn, awx_job_id=awx_job_id, outcome="running")
        repo = AsyncpgOutcomeRepository(conn)

        before = await repo.list_running_execution_bindings()
        assert awx_job_id in _discovered_job_ids(before)

        await repo.update_execution_binding_terminal(
            awx_job_id=str(awx_job_id),
            outcome=ExecutionOutcome.CANCELLED,
            finished_at=_FINISHED,
        )

        after = await repo.list_running_execution_bindings()

    assert awx_job_id not in _discovered_job_ids(after)


# ── Reconciler end-to-end against the real repository ────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_reconciler_discovers_and_transitions_running_binding(
    db_pool: asyncpg.Pool,
) -> None:
    """The full discovery → lookup → persist path against a real database.

    A fake AWX lookup reports the job as ``failed``; the real repository
    discovers the ``running`` row, transitions it to ``failed`` in place, and
    the next discovery pass no longer sees it.
    """
    awx_job_id = _new_job_id()
    async with db_pool.acquire() as conn:
        await _insert_binding(conn, awx_job_id=awx_job_id, outcome="running")
        repo = AsyncpgOutcomeRepository(conn)
        lookup = _FakeAWXLookup(
            {awx_job_id: AWXJobState(status="failed", finished_at=_FINISHED)}
        )

        summary = await ExecutionReconciler(
            repository=repo, awx_lookup=lookup
        ).reconcile()

        row = await conn.fetchrow(
            "SELECT outcome, finished_at FROM execution_bindings"
            " WHERE awx_job_id = $1",
            awx_job_id,
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM execution_bindings WHERE awx_job_id = $1",
            awx_job_id,
        )
        remaining = await repo.list_running_execution_bindings()

    assert awx_job_id in lookup.calls
    ours = [r for r in summary.results if r.awx_job_id == awx_job_id]
    assert len(ours) == 1
    assert ours[0].kind is BindingResultKind.UPDATED
    assert ours[0].outcome is ExecutionOutcome.FAILED

    assert row is not None
    assert row["outcome"] == "failed"
    assert row["finished_at"] == _FINISHED
    assert count == 1
    # Terminal rows are never re-discovered — repeated passes are idempotent.
    assert awx_job_id not in _discovered_job_ids(remaining)
