"""Integration test for the develop → review → fix multi-stage AFK lifecycle (issue #642).

Regression test for the reported bug (GitHub PR #636): when three AWX jobs
(develop, review, fix) share the same ``afk_run_id``, the Gateway used to
reject the review and fix jobs with HTTP 409 after the develop job
completed.  Slices A (#639) and B (#641) removed that completed-run
rejection (ADR 0028); this test verifies the fix end-to-end against the
real database:

* **Job 9260 (develop)** completes successfully under ``afk_run_id``.
* **Job 9261 (review)** starts and is accepted under the same
  ``afk_run_id``.
* **Job 9262 (fix)** starts and is accepted under the same
  ``afk_run_id``.
* All three terminal outcomes are persisted independently (no dedup by the
  shared ``afk_run_id``).
* No false ``409 Conflict`` occurs for any job.
* AWX execution outcomes are historical child facts: they never close or
  reopen the AFK Run, so the run stays ``pending`` (open) while the PR/MR
  is open (ADR 0028).
* A provider merge event (reconciliation/backfill reconstruction) finalizes
  the run to ``completed`` with ``outcome_status`` ``merged``.
* A provider close-without-merge finalizes a run to ``completed`` with the
  distinct ``outcome_status`` ``closed``.
* All three AWX jobs are independently queryable under the same AFK Run
  (single-binding GET by AWX job id, resource-history GET, and the
  change-request detail read model).

The lifecycle finalization path exercised here is the same one the AFK
Outcome Consumer's reconciliation loop and the AFK Backfill CLI use
(``AsyncpgOutcomeRepository.save`` — the reconstruct → persist seam) —
provider change-request events (merge/close) are the sole authority for
run finalization.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_afk_lifecycle_multi_stage.py -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(defaulting to the ``docker-compose.test.yml`` service on port 5433).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Request

from afk_outcomes.models import (
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    Provider,
    RunEntityLink,
    RunStatus,
)
from afk_outcomes.repository import AsyncpgOutcomeRepository
from app.core.identity import hash_token

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

_API_KEY = "test-api-key"

# The dedicated AWX execution-binding client name — matches the constant in
# app.api.afk_executions (issue #550).
_AWX_CLIENT_NAME = "awx-execution-bindings"

_RUNS_PATH = "/api/v1/afk/executions/runs"
_EXECUTIONS_PATH = "/api/v1/afk/executions"

# The change-request identity all three stages target.  ``repository`` is
# the normalized identity stored on afk_runs / execution_bindings (scheme
# dropped, ``github.com`` host retained — see normalize_repository_url).
_REPOSITORY_NORMALIZED = "github.com/acme/proj"
_REPOSITORY_URL = "https://github.com/acme/proj"
_CR_NUMBER = "9260"


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


@pytest.fixture(scope="module")
def _integration_db_available() -> bool:
    if not asyncio.run(_can_connect()):
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )
    return True


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    """Module-scoped pool on the migrated test database.

    alembic's ``env.py`` calls ``asyncio.run()``, which cannot execute
    inside this fixture's event loop (pytest-asyncio auto mode) — the
    upgrade runs on a plain worker thread so env.py gets its own loop.
    Teardown drops every public table and closes the pool so the next
    module starts from a clean schema state.
    """
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        alembic.command.upgrade(alembic_cfg, "head")

    try:
        await asyncio.get_running_loop().run_in_executor(None, _upgrade)
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        await asyncio.get_running_loop().run_in_executor(None, _upgrade)

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        )
    await pool.close()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_state(db_pool: asyncpg.Pool) -> None:
    """Truncate the AFK lifecycle tables before every test.

    The module-scoped pool keeps one migrated schema across the whole
    module; each test provisions its own AFK Run bound to the same canonical
    change request (the 1:1 ``uq_afk_runs_change_request_identity``
    invariant admits only one owning run), so every test starts from a clean
    slate.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE afk_runs CASCADE")
        await conn.execute("TRUNCATE engineering_events CASCADE")
        await conn.execute("TRUNCATE delivery_log CASCADE")
        await conn.execute("TRUNCATE unresolved_correlations CASCADE")
    yield


# ── Builders ─────────────────────────────────────────────────────────────────


async def _seed_awx_client(conn: asyncpg.Connection) -> None:
    """Seed the dedicated AWX execution-binding client + collector credential.

    The credential hash matches the ``_API_KEY`` bearer token so both layers
    of auth pass.  Idempotent — ``opencode_clients.name`` carries a UNIQUE
    constraint, so re-seeding the same module-scoped database reuses the row.
    """
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1)"
        " ON CONFLICT (name) DO NOTHING RETURNING id",
        _AWX_CLIENT_NAME,
    )
    if client_id is None:
        client_id = await conn.fetchval(
            "SELECT id FROM opencode_clients WHERE name = $1", _AWX_CLIENT_NAME
        )
    await conn.execute(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

    ``Request`` must be imported at module level (never locally): FastAPI
    resolves the string annotation of the ``get_session`` override through
    the defining module's globals, and a local import would make ``request``
    fall back to a query parameter (422 on every route).
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.factory import create_app
    from app.db.session import get_session

    os.environ.setdefault("GATEWAY_ENV", "development")

    app = create_app(configure_logging=False)

    async def _override_get_session(request: Request):
        conn = await db_pool.acquire()
        try:
            yield conn
        finally:
            await db_pool.release(conn)

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )


def _provision_payload(**overrides: object) -> dict:
    """Build a ``POST /runs`` provisioning payload for one AFK Run."""
    payload: dict = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": f"eda-{uuid.uuid4().hex[:16]}",
        "repository": _REPOSITORY_URL,
        "trigger_type": "eda",
        "title": "Develop-Loop: Implement issue #9260",
    }
    payload.update(overrides)
    return payload


def _cr_binding_payload(**overrides: object) -> dict:
    """Build a ``POST /runs/{id}/change-request`` binding payload."""
    payload: dict = {
        "provider": "github",
        "repository": _REPOSITORY_URL,
        "external_id": _CR_NUMBER,
    }
    payload.update(overrides)
    return payload


def _job_resource() -> dict:
    """The canonical change-request identity all three stages share."""
    return {
        "provider": "github",
        "repository": _REPOSITORY_URL,
        "resource_type": "pull_request",
        "resource_number": _CR_NUMBER,
    }


def _running_payload(
    awx_job_id: int, job_template_id: int, afk_run_id: str, *, session: str, title: str
) -> dict:
    """Build a phase-one ``running`` provisioning payload for one AWX job."""
    return {
        "awx_job": {"job_id": str(awx_job_id), "job_template_id": job_template_id},
        "outcome": "running",
        "afk_run_id": afk_run_id,
        "trigger_type": "eda",
        "source_event_id": f"evt-{awx_job_id}-start",
        "external_session_id": session,
        "title": title,
        "resource": _job_resource(),
    }


def _terminal_update_payload(*, finished_at: str, session: str) -> dict:
    """Build a phase-two ``PATCH`` terminal-update payload."""
    return {
        "outcome": "completed",
        "finished_at": finished_at,
        "external_session_id": session,
        "resource": _job_resource(),
    }


def _reconstructed_run(afk_run_id: str, cr_number: str, *, merged: bool) -> AFKRun:
    """Reconstruct one run the way the reconciliation/backfill path does.

    The AFK Outcome Consumer's reconcile loop and the AFK Backfill CLI both
    pull a window of provider engineering facts, correlate them into an
    :class:`AFKRun`, and persist it via ``AsyncpgOutcomeRepository.save``.
    ``save`` is the only write seam that carries the provider change-request
    state (merged / closed) onto ``afk_runs.status`` / ``outcome_status``.
    The reconstruction also carries the canonical change-request lifecycle
    fact (``change_request.merged`` / ``change_request.closed``) so the
    read models derive the provider state from the same immutable facts the
    live consumer writes.
    """
    started = datetime(2026, 9, 3, 8, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    provider_state = "merged" if merged else "closed"
    outcome_status = (
        EngineeringOutcomeStatus.MERGED if merged else EngineeringOutcomeStatus.CLOSED
    )
    event_type = "change_request.merged" if merged else "change_request.closed"
    entities = [
        EngineeringEntity(
            entity_id=f"change_request:{cr_number}",
            entity_type=EntityType.CHANGE_REQUEST,
            provider=Provider.GITHUB,
            repository=_REPOSITORY_NORMALIZED,
            number=int(cr_number),
            title=f"Implement issue #{cr_number}",
            state=provider_state,
        ),
        EngineeringEntity(
            entity_id=f"issue:{cr_number}",
            entity_type=EntityType.ISSUE,
            provider=Provider.GITHUB,
            repository=_REPOSITORY_NORMALIZED,
            number=int(cr_number),
        ),
    ]
    if merged:
        entities.append(
            EngineeringEntity(
                entity_id=f"merge_event:{cr_number}",
                entity_type=EntityType.MERGE_EVENT,
                provider=Provider.GITHUB,
                repository=_REPOSITORY_NORMALIZED,
                number=int(cr_number),
                created_at=finished,
            )
        )
    event_suffix = event_type.rsplit(".", 1)[1]
    return AFKRun(
        afk_run_id=afk_run_id,
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title=f"Implement issue #{cr_number}",
        started_at=started,
        finished_at=finished,
        entities=entities,
        events=[
            EngineeringEvent(
                event_id=f"change_request:{cr_number}:{event_suffix}",
                event_type=event_type,
                provider=Provider.GITHUB,
                entity_id=f"change_request:{cr_number}",
                occurred_at=finished,
                actor="wyautomation",
                payload={},
            ),
        ],
        correlations=[
            Correlation(
                correlation_id=f"corr-{afk_run_id}",
                afk_run_id=afk_run_id,
                entity_id=f"change_request:{cr_number}",
                correlation_confidence=1.0,
                method="explicit_run_id",
                evidence=[
                    CorrelationEvidence(
                        kind="commit_message_reference",
                        source_entity_id=f"commit:{afk_run_id}",
                        detail=f"resolves #{cr_number}",
                    )
                ],
            ),
            Correlation(
                correlation_id=f"corr-issue-{afk_run_id}",
                afk_run_id=afk_run_id,
                entity_id=f"issue:{cr_number}",
                correlation_confidence=1.0,
                method="issue_resolved",
                evidence=[],
            ),
        ],
        outcome=EngineeringOutcome(
            status=outcome_status,
            change_request_ids=[f"change_request:{cr_number}"],
            resolved_issue_ids=[f"issue:{cr_number}"],
            merge_event_id=f"merge_event:{cr_number}" if merged else None,
            merged_at=finished if merged else None,
        ),
        entity_links=[
            RunEntityLink(
                afk_run_id=afk_run_id,
                entity_id=f"change_request:{cr_number}",
                role="resolved",
                correlation_confidence=1.0,
            ),
            RunEntityLink(
                afk_run_id=afk_run_id,
                entity_id=f"issue:{cr_number}",
                role="resolved",
                correlation_confidence=1.0,
            ),
        ],
        session_links=[],
    )


async def _provision_and_bind(client: object) -> str:
    """Provision one AFK Run and bind the canonical change request to it."""
    prov = await client.post(_RUNS_PATH, json=_provision_payload())
    assert prov.status_code == 201, prov.text
    run_id = prov.json()["data"]["afk_run_id"]
    assert prov.json()["data"]["status"] == "pending"

    bind = await client.post(
        f"{_RUNS_PATH}/{run_id}/change-request", json=_cr_binding_payload()
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["data"]["afk_run_id"] == run_id
    return run_id


async def _run_one_stage(
    client: object,
    *,
    awx_job_id: int,
    job_template_id: int,
    afk_run_id: str,
    session: str,
    title: str,
    started_at: str,
    finished_at: str,
) -> tuple[int, int]:
    """Drive one AWX job through running → completed under the shared run.

    Returns ``(running_status, terminal_status)`` — both are non-409 for a
    healthy multi-stage lifecycle (issue #639 / ADR 0028).
    """
    running = await client.post(
        _EXECUTIONS_PATH,
        json=_running_payload(
            awx_job_id,
            job_template_id,
            afk_run_id,
            session=session,
            title=title,
        ),
    )
    assert running.status_code == 201, running.text
    binding = running.json()["data"]
    assert binding["outcome"] == "running"
    assert binding["afk_run_id"] == afk_run_id

    terminal = await client.patch(
        f"{_EXECUTIONS_PATH}/{awx_job_id}",
        json=_terminal_update_payload(finished_at=finished_at, session=session),
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["data"]["outcome"] == "completed"
    assert terminal.json()["data"]["afk_run_id"] == afk_run_id
    return running.status_code, terminal.status_code


# ── The three-stage lifecycle (the regression scenario) ──────────────────────


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_develop_review_fix_share_one_afk_run_id_no_false_409(
    db_pool: asyncpg.Pool,
) -> None:
    """Develop → review → fix (jobs 9260 → 9261 → 9262) share one afk_run_id.

    The regression scenario from GitHub PR #636: after the develop job
    completes, the review and fix jobs must be accepted under the same
    ``afk_run_id`` — never rejected with a false 409 (issue #639 / ADR
    0028).  All three terminal outcomes persist independently and every job
    is independently queryable under the run.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)
    async with client as c:
        run_id = await _provision_and_bind(c)

        # Collect every write-path status code — the regression assertion is
        # that NO job is rejected with a false 409 Conflict.
        stage_statuses: list[tuple[int, int]] = []

        # Job 9260 (develop) — runs and completes successfully.
        stage_statuses.append(
            await _run_one_stage(
                c,
                awx_job_id=9260,
                job_template_id=132,
                afk_run_id=run_id,
                session="ses_dev_9260",
                title="Develop-Loop: Implemented issue #9260",
                started_at="2026-09-03T09:00:00Z",
                finished_at="2026-09-03T10:00:00Z",
            )
        )

        # Job 9261 (review) — starts and is accepted under the SAME run.
        stage_statuses.append(
            await _run_one_stage(
                c,
                awx_job_id=9261,
                job_template_id=222,
                afk_run_id=run_id,
                session="ses_review_9261",
                title="MR Review Runner",
                started_at="2026-09-03T10:30:00Z",
                finished_at="2026-09-03T11:00:00Z",
            )
        )

        # Job 9262 (fix) — starts and is accepted under the SAME run.
        stage_statuses.append(
            await _run_one_stage(
                c,
                awx_job_id=9262,
                job_template_id=222,
                afk_run_id=run_id,
                session="ses_fix_9262",
                title="MR Review Runner fix round",
                started_at="2026-09-03T11:30:00Z",
                finished_at="2026-09-03T12:00:00Z",
            )
        )

        # No false 409 Conflict for any job: every phase of every stage was
        # accepted (201 for the running provisioning, 200 for the terminal
        # transition).
        assert stage_statuses == [(201, 200), (201, 200), (201, 200)], (
            f"expected no 409 across develop→review→fix, got {stage_statuses}"
        )

        # All three jobs are independently queryable by AWX job id under the
        # same afk_run_id.
        for job_id in (9260, 9261, 9262):
            single = await c.get(f"{_EXECUTIONS_PATH}/{job_id}")
            assert single.status_code == 200, single.text
            assert single.json()["data"]["afk_run_id"] == run_id
            assert single.json()["data"]["outcome"] == "completed"

        # The resource-history read returns the full three-stage history.
        history = await c.get(
            _EXECUTIONS_PATH,
            params={
                "provider": "github",
                "repository_url": _REPOSITORY_URL,
                "entity_type": "change_request",
                "entity_number": _CR_NUMBER,
            },
        )
        assert history.status_code == 200, history.text
        binding_ids = sorted(
            b["awx_job"]["job_id"] for b in history.json()["data"]["bindings"]
        )
        assert binding_ids == ["9260", "9261", "9262"], binding_ids
        assert all(
            b["afk_run_id"] == run_id
            for b in history.json()["data"]["bindings"]
        )

    # Database state: three bindings under one run, each terminal outcome
    # persisted independently.
    async with db_pool.acquire() as conn:
        bindings = await conn.fetch(
            "SELECT awx_job_id, outcome, afk_run_id FROM execution_bindings"
            " WHERE afk_run_id = $1 ORDER BY awx_job_id",
            run_id,
        )
        assert [(b["awx_job_id"], b["outcome"]) for b in bindings] == [
            (9260, "completed"),
            (9261, "completed"),
            (9262, "completed"),
        ]
        assert all(b["afk_run_id"] == run_id for b in bindings)
        assert len(bindings) == 3
        # Exactly one AFK Run owns the whole lifecycle.
        run_count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs WHERE afk_run_id = $1", run_id
        )
        assert run_count == 1


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_awx_outcomes_never_close_run_while_change_request_open(
    db_pool: asyncpg.Pool,
) -> None:
    """The AFK Run stays open (pending) while the PR/MR is open.

    ADR 0028 / issue #639: AWX execution outcomes are historical child facts
    and never close or reopen the AFK Run.  After all three AWX terminal
    outcomes, ``afk_runs.status`` remains ``pending`` and no engineering
    outcome has been derived — only a provider change-request event
    finalizes the run.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)
    async with client as c:
        run_id = await _provision_and_bind(c)
        for job, tpl, session, title, start, finish in [
            (9260, 132, "ses_dev_9260", "Develop-Loop job", "2026-09-03T09:00:00Z", "2026-09-03T10:00:00Z"),
            (9261, 222, "ses_review_9261", "Review job", "2026-09-03T10:30:00Z", "2026-09-03T11:00:00Z"),
            (9262, 222, "ses_fix_9262", "Fix job", "2026-09-03T11:30:00Z", "2026-09-03T12:00:00Z"),
        ]:
            await _run_one_stage(
                c,
                awx_job_id=job,
                job_template_id=tpl,
                afk_run_id=run_id,
                session=session,
                title=title,
                started_at=start,
                finished_at=finish,
            )

    # No 409 occurred for any job (each stage returned 200/201 above).
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, outcome_status, outcome, finished_at"
            " FROM afk_runs WHERE afk_run_id = $1",
            run_id,
        )
        assert row is not None
        # The run is still open — pending, with no provider outcome derived
        # and no finished_at (AWX outcomes never finalize it).
        assert row["status"] == "pending"
        assert row["outcome_status"] is None
        assert row["outcome"] is None
        assert row["finished_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_provider_merge_event_finalizes_run_to_completed(
    db_pool: asyncpg.Pool,
) -> None:
    """A provider merge event finalizes the AFK Run to completed/merged.

    Once the change request is merged, the reconciliation/backfill path
    reconstructs the run with the merged engineering outcome and persists it
    via ``AsyncpgOutcomeRepository.save`` — the same seam the AFK Outcome
    Consumer's reconcile loop and the AFK Backfill CLI use.  The run
    transitions ``pending`` → ``completed`` with ``outcome_status``
    ``merged``, and the change-request detail read model reflects the merged
    provider state while still surfacing all three AWX executions.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)
    async with client as c:
        run_id = await _provision_and_bind(c)
        for job, tpl, session, title, start, finish in [
            (9260, 132, "ses_dev_9260", "Develop-Loop job", "2026-09-03T09:00:00Z", "2026-09-03T10:00:00Z"),
            (9261, 222, "ses_review_9261", "Review job", "2026-09-03T10:30:00Z", "2026-09-03T11:00:00Z"),
            (9262, 222, "ses_fix_9262", "Fix job", "2026-09-03T11:30:00Z", "2026-09-03T12:00:00Z"),
        ]:
            await _run_one_stage(
                c,
                awx_job_id=job,
                job_template_id=tpl,
                afk_run_id=run_id,
                session=session,
                title=title,
                started_at=start,
                finished_at=finish,
            )

        # The provider merge event is reconciled onto the same run.
        async with db_pool.acquire() as conn:
            repo = AsyncpgOutcomeRepository(conn)
            await repo.save(_reconstructed_run(run_id, _CR_NUMBER, merged=True))

        # The run is finalized: completed with a merged engineering outcome.
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, outcome_status, finished_at"
                " FROM afk_runs WHERE afk_run_id = $1",
                run_id,
            )
            assert row["status"] == "completed"
            assert row["outcome_status"] == "merged"
            assert row["finished_at"] is not None

        # The change-request detail reflects the merged provider state and
        # still surfaces all three executions under the one run.
        detail = await c.get(
            "/api/v1/afk-outcomes/change-requests/github/"
            f"{_REPOSITORY_NORMALIZED}/{_CR_NUMBER}"
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["change_request"]["provider_state"] == "merged"
        assert [e["awx_job"]["job_id"] for e in data["executions"]] == [
            "9260",
            "9261",
            "9262",
        ]
        assert all(e["afk_run_id"] == run_id for e in data["executions"])
        assert [r["afk_run_id"] for r in data["afk_runs"]] == [run_id]
        assert data["afk_runs"][0]["outcome_status"] == "merged"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_provider_close_without_merge_is_distinct_terminal_status(
    db_pool: asyncpg.Pool,
) -> None:
    """Provider close-without-merge produces a distinct terminal status.

    A change request that is closed without being merged finalizes the AFK
    Run to ``completed`` with ``outcome_status`` ``closed`` — observably
    distinct from the merged terminal status, so delivered work is never
    conflated with abandoned work.
    """
    async with db_pool.acquire() as conn:
        await _seed_awx_client(conn)

    client = _build_app(db_pool)
    async with client as c:
        # A run whose develop job completed but whose change request was
        # later closed without merging (no review/fix stages needed).
        run_id = await _provision_and_bind(c)
        await _run_one_stage(
            c,
            awx_job_id=9260,
            job_template_id=132,
            afk_run_id=run_id,
            session="ses_dev_9260",
            title="Develop-Loop: Implemented issue #9260",
            started_at="2026-09-03T09:00:00Z",
            finished_at="2026-09-03T10:00:00Z",
        )

        # The provider close-without-merge event is reconciled onto the run.
        async with db_pool.acquire() as conn:
            repo = AsyncpgOutcomeRepository(conn)
            await repo.save(_reconstructed_run(run_id, _CR_NUMBER, merged=False))

        # Distinct terminal status: completed + closed (never merged).
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, outcome_status, finished_at"
                " FROM afk_runs WHERE afk_run_id = $1",
                run_id,
            )
            assert row["status"] == "completed"
            assert row["outcome_status"] == "closed"
            assert row["finished_at"] is not None

        # The read model reports the closed provider state, distinct from
        # the merged state asserted by the merge test.
        detail = await c.get(
            "/api/v1/afk-outcomes/change-requests/github/"
            f"{_REPOSITORY_NORMALIZED}/{_CR_NUMBER}"
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["change_request"]["provider_state"] == "closed"
        assert data["afk_runs"][0]["outcome_status"] == "closed"
        assert data["change_request"]["merged_at"] is None
