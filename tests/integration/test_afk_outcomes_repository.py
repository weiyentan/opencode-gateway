"""Integration tests for the AFK OutcomeRepository (issue #448).

Runs against the docker-compose Postgres (port 5433) and verifies the actual
database-enforced write semantics:

* event identity UNIQUE constraint rejects duplicate events; re-delivery no-ops;
* ``delivery_log`` replay-safe via UNIQUE + ON CONFLICT DO NOTHING;
* enrich-only semantics — confidence raised, never silently lowered; evidence
  appended; superseded links marked (never deleted);
* ``afk_run_entities.afk_run_id`` NOT NULL enforced;
* unresolved correlations stored in ``unresolved_correlations`` only.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/ -v -m integration
    docker compose -f docker-compose.test.yml down -v

Connection parameters come from the standard Gateway environment variables
(defaulting to the ``docker-compose.test.yml`` service).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from afk_outcomes import (
    AFKRun,
    AsyncpgOutcomeRepository,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    ExecutionOutcome,
    EntityType,
    Provider,
    ReferenceSource,
    ResourceSessionAssociation,
    RunEntityLink,
    RunStatus,
    UnresolvedCorrelation,
    UnresolvedReason,
)

_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJ_ROOT / "alembic.ini"

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
REPO = "weiyentan/opencode-gateway"


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


@pytest.fixture(scope="module")
async def db_pool(_integration_db_available: bool) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)

    def _upgrade() -> None:
        # alembic/env.py calls asyncio.run(), which cannot run inside this
        # fixture's event loop (pytest-asyncio auto mode) — execute it on a
        # plain worker thread so env.py gets its own event loop.
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


# ── Builders ─────────────────────────────────────────────────────────────────


def _make_run(
    afk_run_id: str,
    *,
    entity_id: str = "issue:437",
    external_id: str = "437",
    role: str = "resolved",
    confidence: float = 1.0,
    method: str = "issue_resolved",
    status: RunStatus = RunStatus.COMPLETED,
    with_outcome: bool = True,
) -> AFKRun:
    started = datetime(2026, 8, 13, 8, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 13, 10, 10, 29, tzinfo=UTC)
    return AFKRun(
        afk_run_id=afk_run_id,
        provider=Provider.GITHUB,
        status=status,
        title="Integration run",
        started_at=started,
        finished_at=finished,
        entities=[
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.ISSUE,
                provider=Provider.GITHUB,
                repository=REPO,
                number=437,
            ),
        ],
        events=[
            EngineeringEvent(
                event_id=f"{entity_id}:opened",
                event_type="opened",
                provider=Provider.GITHUB,
                entity_id=entity_id,
                occurred_at=started,
                actor="wyautomation",
                payload={},
            ),
        ],
        correlations=[
            Correlation(
                correlation_id=f"corr-{afk_run_id}",
                afk_run_id=afk_run_id,
                entity_id=entity_id,
                correlation_confidence=confidence,
                method=method,
                evidence=[
                    CorrelationEvidence(
                        kind="commit_message_reference",
                        source_entity_id=f"commit:{afk_run_id}",
                        detail=f"resolves #{external_id}",
                        weight=confidence,
                    ),
                ],
            ),
        ],
        outcome=(
            EngineeringOutcome(
                status=EngineeringOutcomeStatus.MERGED,
                change_request_ids=["change_request:442"],
                resolved_issue_ids=[entity_id],
            )
            if with_outcome
            else None
        ),
        entity_links=[
            RunEntityLink(
                afk_run_id=afk_run_id,
                entity_id=entity_id,
                role=role,
                correlation_confidence=confidence,
            ),
        ],
        session_links=[],
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_identity_rejects_duplicate_events(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run = _make_run("01J0000000000000000000000001")

        await repo.save(run)
        await repo.save(run)  # re-delivery

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events "
            "WHERE external_id = '437' AND event_type = 'opened'"
        )
        assert count == 1, f"expected 1 event after re-delivery, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delivery_log_replay_safe(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run = _make_run("01J0000000000000000000000002")

        await repo.save(run)
        await repo.save(run)  # re-delivery

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM delivery_log WHERE delivery_id = '01J0000000000000000000000002'"
        )
        assert count == 1, f"expected 1 delivery-log row after re-delivery, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confidence_raised_never_lowered(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_id = "01J0000000000000000000000003"

        await repo.save(_make_run(run_id, confidence=0.5))
        await repo.save(_make_run(run_id, confidence=0.9))  # raise
        await repo.save(_make_run(run_id, confidence=0.3))  # attempt to lower

        confidence = await conn.fetchval(
            "SELECT correlation_confidence FROM afk_run_entities WHERE afk_run_id = $1",
            run_id,
        )
        assert confidence == pytest.approx(0.9), (
            f"confidence should stay raised at 0.9, got {confidence}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_appended_not_erased(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_id = "01J0000000000000000000000004"

        await repo.save(_make_run(run_id, method="issue_resolved", confidence=1.0))
        await repo.save(_make_run(run_id, method="change_request_merged", confidence=1.0))

        evidence = await conn.fetchval(
            "SELECT evidence FROM afk_run_entities WHERE afk_run_id = $1", run_id
        )
        # evidence is appended (jsonb array concatenation), so the first delivery's
        # evidence is still present alongside the second delivery's.
        assert len(evidence) >= 2, f"evidence should be appended, got {evidence!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_links_marked_not_deleted(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)

        # Run A links the entity weakly; run B links it more confidently.
        await repo.save(
            _make_run("01J0000000000000000000000005", confidence=0.5, role="referenced")
        )
        await repo.save(
            _make_run("01J0000000000000000000000006", confidence=0.9, role="resolved")
        )

        # Both rows still exist (no hard-delete) …
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_run_entities WHERE external_id = '437'"
        )
        assert count == 2, f"expected 2 entity links (superseded not deleted), got {count}"

        # … but run A's weaker link is marked superseded.
        superseded = await conn.fetchval(
            "SELECT superseded_at FROM afk_run_entities "
            "WHERE afk_run_id = '01J0000000000000000000000005'"
        )
        assert superseded is not None, "run A's weaker link should be marked superseded"

        active = await conn.fetchval(
            "SELECT superseded_at FROM afk_run_entities "
            "WHERE afk_run_id = '01J0000000000000000000000006'"
        )
        assert active is None, "run B's stronger link should remain active"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_link_not_reactivated_on_redelivery(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)

        run_a = "01J0000000000000000000000008"
        run_b = "01J0000000000000000000000009"

        # Run A links the entity weakly; run B links it more confidently.
        await repo.save(_make_run(run_a, confidence=0.5, role="referenced"))
        await repo.save(_make_run(run_b, confidence=0.9, role="resolved"))

        # Run A is now superseded by run B.
        before = await conn.fetchval(
            "SELECT superseded_at FROM afk_run_entities WHERE afk_run_id = $1",
            run_a,
        )
        assert before is not None, "run A should be superseded before re-delivery"

        # Re-deliver run A (a Kafka replay of the same 0.5 confidence).  The
        # enrich-only conflict update must NOT clear its superseded_at.
        await repo.save(_make_run(run_a, confidence=0.5, role="referenced"))

        after = await conn.fetchval(
            "SELECT superseded_at FROM afk_run_entities WHERE afk_run_id = $1",
            run_a,
        )
        assert after is not None, (
            "re-delivering a superseded link must not re-activate it "
            "(superseded_at was reset to NULL)"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_afk_run_entities_afk_run_id_not_null_enforced(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run = _make_run("01J0000000000000000000000007")
        await repo.save(run)

        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                """INSERT INTO afk_run_entities
                   (afk_run_id, provider, repository, entity_type, external_id, role,
                    correlation_confidence, evidence)
                   VALUES (NULL, 'github', $1, 'issue', '999', 'resolved', 1.0, '[]')""",
                REPO,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_correlations_stored_only_in_unresolved_table(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        # a run whose only link is a low-confidence "referenced" mention
        run = _make_run(
            "01J0000000000000000000000008",
            role="referenced",
            confidence=0.1,
            method="issue_mention",
            with_outcome=False,
        )
        await repo.save(run)

        # the low-confidence correlation is in unresolved_correlations…
        unresolved = await conn.fetchval(
            "SELECT COUNT(*) FROM unresolved_correlations "
            "WHERE external_id = '437' AND method = 'issue_mention'"
        )
        assert unresolved == 1, f"expected 1 unresolved correlation, got {unresolved}"

        # …and NOT promoted to a resolved entity link's correlation_method.
        link_method = await conn.fetchval(
            "SELECT correlation_method FROM afk_run_entities "
            "WHERE afk_run_id = '01J0000000000000000000000008'"
        )
        assert link_method is not None  # the link still carries the (weak) method


# ── Run-scoped unresolved-correlation identity (migration 0027) ───────────────


def _build_unresolved_item(
    afk_run_id: str,
    reason: UnresolvedReason,
    *,
    candidates: list[str] | None = None,
) -> UnresolvedCorrelation:
    return UnresolvedCorrelation(
        unresolved_id=f"unresolved-{afk_run_id}",
        afk_run_id=afk_run_id,
        entity_id=afk_run_id,
        reason=reason,
        candidates=candidates if candidates is not None else [],
        evidence=[
            CorrelationEvidence(
                kind="title_match",
                source_entity_id="change_request:300",
                detail="title=Fix caching bug",
                weight=1.0,
            )
        ],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_same_run_upsert_enriches_single_row(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_id = "01J0000000000000000000000099"

        await repo.save(
            _make_run(
                run_id,
                entity_id="issue:901",
                external_id="901",
                role="referenced",
                confidence=0.1,
                method="issue_mention",
                with_outcome=False,
            )
        )
        await repo.save(
            _make_run(
                run_id,
                entity_id="issue:901",
                external_id="901",
                role="referenced",
                confidence=0.1,
                method="issue_mention",
                with_outcome=False,
            )
        )

        rows = await conn.fetch(
            "SELECT afk_run_id, evidence FROM unresolved_correlations "
            "WHERE external_id = '901' AND method = 'issue_mention'"
        )
        assert len(rows) == 1, f"same-run re-save must enrich one row, got {len(rows)}"
        assert rows[0]["afk_run_id"] == run_id
        assert len(rows[0]["evidence"]) == 2, "evidence should be appended across re-saves"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_different_runs_produce_independent_rows(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_a = "01J0000000000000000000000100"
        run_b = "01J0000000000000000000000101"

        await repo.save(
            _make_run(
                run_a,
                entity_id="issue:902",
                external_id="902",
                role="referenced",
                confidence=0.1,
                method="issue_mention",
                with_outcome=False,
            )
        )
        await repo.save(
            _make_run(
                run_b,
                entity_id="issue:902",
                external_id="902",
                role="referenced",
                confidence=0.1,
                method="issue_mention",
                with_outcome=False,
            )
        )

        rows = await conn.fetch(
            "SELECT afk_run_id, evidence FROM unresolved_correlations "
            "WHERE external_id = '902' AND method = 'issue_mention' "
            "ORDER BY afk_run_id"
        )
        assert len(rows) == 2, (
            f"two runs of the same entity must yield independent rows, got {len(rows)}"
        )
        assert {r["afk_run_id"] for r in rows} == {run_a, run_b}
        for row in rows:
            assert len(row["evidence"]) == 1, "evidence must be isolated per run"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_ambiguous_rows_across_runs_independent(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_a = "01J0000000000000000000000200"
        run_b = "01J0000000000000000000000201"

        await repo.save_unresolved(
            AFKRun(afk_run_id=run_a, provider=Provider.GITHUB, status=RunStatus.COMPLETED),
            [_build_unresolved_item(run_a, UnresolvedReason.AMBIGUOUS)],
            repository=REPO,
        )
        await repo.save_unresolved(
            AFKRun(afk_run_id=run_b, provider=Provider.GITHUB, status=RunStatus.COMPLETED),
            [_build_unresolved_item(run_b, UnresolvedReason.AMBIGUOUS)],
            repository=REPO,
        )

        rows = await conn.fetch(
            "SELECT afk_run_id, external_id FROM unresolved_correlations "
            "WHERE entity_type = 'afk_run' AND method = 'ambiguous' "
            "ORDER BY afk_run_id"
        )
        assert len(rows) == 2, f"ambiguous rows across runs must be independent, got {len(rows)}"
        for row in rows:
            assert row["external_id"] == row["afk_run_id"], (
                "run-level rows keep external_id == afk_run_id"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_unmatched_rows_across_runs_independent(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        run_a = "01J0000000000000000000000300"
        run_b = "01J0000000000000000000000301"

        await repo.save_unresolved(
            AFKRun(afk_run_id=run_a, provider=Provider.GITHUB, status=RunStatus.COMPLETED),
            [_build_unresolved_item(run_a, UnresolvedReason.UNMATCHED)],
            repository=REPO,
        )
        await repo.save_unresolved(
            AFKRun(afk_run_id=run_b, provider=Provider.GITHUB, status=RunStatus.COMPLETED),
            [_build_unresolved_item(run_b, UnresolvedReason.UNMATCHED)],
            repository=REPO,
        )

        rows = await conn.fetch(
            "SELECT afk_run_id, external_id FROM unresolved_correlations "
            "WHERE entity_type = 'afk_run' AND method = 'unmatched' "
            "ORDER BY afk_run_id"
        )
        assert len(rows) == 2, f"unmatched rows across runs must be independent, got {len(rows)}"
        for row in rows:
            assert row["external_id"] == row["afk_run_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_scoped_unique_constraint_exists_post_upgrade(
    db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        present = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_constraint "
            "  WHERE conname = 'uq_unresolved_correlations_entity_run_method'"
            ")"
        )
        assert present is True, (
            "uq_unresolved_correlations_entity_run_method must exist after 0027"
        )


# ── Exact resource↔session associations (migration 0034) ─────────────────────


def _make_association(
    *,
    external_session_id: str,
    resource_type: EntityType,
    resource_number: str,
    source_reference: list[ReferenceSource] | None = None,
) -> ResourceSessionAssociation:
    return ResourceSessionAssociation(
        external_session_id=external_session_id,
        provider=Provider.GITHUB,
        repository=REPO,
        resource_type=resource_type,
        resource_number=resource_number,
        source_reference=source_reference or [],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_association_twice_persists_one_row(db_pool: asyncpg.Pool) -> None:
    """Saving the same (resource, session) pair twice persists exactly one row.

    The second save is an idempotent conflict update: it must not duplicate
    the row, ``source_reference`` stays write-once (first insert wins), and
    ``last_seen_at`` is advanced to track re-observation recency.
    """
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)

        first = _make_association(
            external_session_id="ses_assoc_001",
            resource_type=EntityType.ISSUE,
            resource_number="9001",
            source_reference=[ReferenceSource(field="title", detail="9001")],
        )
        second = _make_association(
            external_session_id="ses_assoc_001",
            resource_type=EntityType.ISSUE,
            resource_number="9001",
            source_reference=[ReferenceSource(field="project", detail="9001")],
        )

        await repo.save_associations([first])

        # Backdate last_seen_at so a re-observation that advances it is
        # observably later than first_seen_at.
        await conn.execute(
            "UPDATE resource_session_associations SET last_seen_at = now() - interval '1 hour' "
            "WHERE provider = 'github' AND repository = $1 "
            "AND resource_type = 'issue' AND resource_number = '9001' "
            "AND external_session_id = 'ses_assoc_001'",
            REPO,
        )

        await repo.save_associations([second])  # re-observation

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM resource_session_associations "
            "WHERE provider = 'github' AND repository = $1 "
            "AND resource_type = 'issue' AND resource_number = '9001' "
            "AND external_session_id = 'ses_assoc_001'",
            REPO,
        )
        assert count == 1, f"expected 1 association row, got {count}"

        row = await conn.fetchrow(
            "SELECT source_reference, first_seen_at, last_seen_at "
            "FROM resource_session_associations "
            "WHERE provider = 'github' AND repository = $1 "
            "AND resource_type = 'issue' AND resource_number = '9001' "
            "AND external_session_id = 'ses_assoc_001'",
            REPO,
        )
        # write-once provenance: the first insert's source_reference wins
        assert row["source_reference"] == [{"field": "title", "detail": "9001"}]
        # recency: the conflict update advanced last_seen_at past first_seen_at
        assert row["last_seen_at"] > row["first_seen_at"]


# ── Multi-repo event isolation (issue #499) ─────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_repo_events_isolated_by_repository(db_pool: asyncpg.Pool) -> None:
    """Two repositories with the same entity_id (issue:437) must produce
    isolated event sets when retrieved via get().

    Writes events for both repos, saves two runs, and verifies each run's
    get() returns only its own repository's events.
    """
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        repo_a_id = "01J0000000000000000000000R1"
        repo_b_id = "01J0000000000000000000000R2"
        repo_a = "org/repo-a"
        repo_b = "org/repo-b"
        started = datetime(2026, 8, 13, 8, 0, 0, tzinfo=UTC)
        finished = datetime(2026, 8, 13, 10, 10, 29, tzinfo=UTC)

        # Run A — repo-a, issue:437
        run_a = AFKRun(
            afk_run_id=repo_a_id,
            provider=Provider.GITHUB,
            status=RunStatus.COMPLETED,
            title="Run A",
            started_at=started,
            finished_at=finished,
            entities=[
                EngineeringEntity(
                    entity_id="issue:437",
                    entity_type=EntityType.ISSUE,
                    provider=Provider.GITHUB,
                    repository=repo_a,
                    number=437,
                ),
            ],
            events=[
                EngineeringEvent(
                    event_id="issue:437:opened",
                    event_type="opened",
                    provider=Provider.GITHUB,
                    entity_id="issue:437",
                    occurred_at=started,
                    actor="wyautomation",
                    payload={},
                ),
            ],
            correlations=[],
            entity_links=[
                RunEntityLink(
                    afk_run_id=repo_a_id,
                    entity_id="issue:437",
                    role="resolved",
                    correlation_confidence=1.0,
                ),
            ],
            session_links=[],
        )

        # Run B — repo-b, issue:437 (same entity_id, different repo)
        run_b = AFKRun(
            afk_run_id=repo_b_id,
            provider=Provider.GITHUB,
            status=RunStatus.COMPLETED,
            title="Run B",
            started_at=started,
            finished_at=finished,
            entities=[
                EngineeringEntity(
                    entity_id="issue:437",
                    entity_type=EntityType.ISSUE,
                    provider=Provider.GITHUB,
                    repository=repo_b,
                    number=437,
                ),
            ],
            events=[
                EngineeringEvent(
                    event_id="issue:437:closed",
                    event_type="closed",
                    provider=Provider.GITHUB,
                    entity_id="issue:437",
                    occurred_at=finished,
                    actor="wyautomation",
                    payload={},
                ),
            ],
            correlations=[],
            entity_links=[
                RunEntityLink(
                    afk_run_id=repo_b_id,
                    entity_id="issue:437",
                    role="resolved",
                    correlation_confidence=1.0,
                ),
            ],
            session_links=[],
        )

        await repo.save(run_a)
        await repo.save(run_b)

        # Verify raw storage: both events exist in engineering_events
        all_events = await conn.fetch(
            "SELECT repository, event_type FROM engineering_events "
            "WHERE entity_type = 'issue' AND external_id = '437' "
            "ORDER BY repository"
        )
        assert len(all_events) == 2, (
            f"expected 2 events total (one per repo), got {len(all_events)}"
        )
        assert {r["repository"] for r in all_events} == {repo_a, repo_b}

        # Verify get() isolation: Run A sees only repo-a's event
        fetched_a = await repo.get(repo_a_id)
        assert fetched_a is not None
        assert len(fetched_a.events) == 1, (
            f"Run A expected 1 event, got {len(fetched_a.events)}"
        )
        assert fetched_a.events[0].repository == repo_a
        assert fetched_a.events[0].event_type == "opened"

        # Verify get() isolation: Run B sees only repo-b's event
        fetched_b = await repo.get(repo_b_id)
        assert fetched_b is not None
        assert len(fetched_b.events) == 1, (
            f"Run B expected 1 event, got {len(fetched_b.events)}"
        )
        assert fetched_b.events[0].repository == repo_b
        assert fetched_b.events[0].event_type == "closed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_association_unique_constraint_enforced_at_sql_level(
    db_pool: asyncpg.Pool,
) -> None:
    """A second direct INSERT of the same association key is rejected.

    Proves the ``UNIQUE (provider, repository, resource_type, resource_number,
    external_session_id)`` constraint exists at the raw-SQL level.
    """
    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        await repo.save_associations(
            [
                _make_association(
                    external_session_id="ses_assoc_002",
                    resource_type=EntityType.CHANGE_REQUEST,
                    resource_number="9002",
                )
            ]
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO resource_session_associations"
                " (external_session_id, provider, repository, resource_type, resource_number)"
                " VALUES ($1, $2, $3, $4, $5)",
                "ses_assoc_002",
                "github",
                REPO,
                "change_request",
                "9002",
            )


# ── Execution-binding session links (issue #618) ─────────────────────────────


async def _seed_gateway_session(
    conn: asyncpg.Connection, *, external_session_id: str
) -> uuid.UUID:
    """Insert one client/credential/source-database/session chain; return the
    internal ``sessions.id`` UUID."""
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"cli-{uuid.uuid4()}",
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        "dummy-token-hash",
        "pref",
    )
    database_id = await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id)"
        " VALUES ($1, $2) RETURNING id",
        credential_id,
        client_id,
    )
    started = datetime(2026, 8, 13, 8, 0, 0, tzinfo=UTC)
    return await conn.fetchval(
        "INSERT INTO sessions"
        " (client_id, source_database_id, external_session_id,"
        "  first_message_at, last_message_at)"
        " VALUES ($1, $2, $3, $4, $4) RETURNING id",
        client_id,
        database_id,
        external_session_id,
        started,
    )


async def _seed_execution_afk_run(conn: asyncpg.Connection, run_id: str) -> None:
    """Insert a provisional afk_runs row the execution can attach to."""
    await conn.execute(
        "INSERT INTO afk_runs (afk_run_id, provider, status, first_seen_at, last_seen_at)"
        " VALUES ($1, $2, 'pending', now(), now())",
        run_id,
        "github",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_persists_afk_run_session_link(db_pool: asyncpg.Pool) -> None:
    """Creating an execution binding with afk_run_id + external_session_id
    persists the afk_run_sessions row with the resolved internal session id."""
    async with db_pool.acquire() as conn:
        external_session_id = f"ses_618_{uuid.uuid4().hex[:8]}"
        internal_session_id = await _seed_gateway_session(
            conn, external_session_id=external_session_id
        )
        await _seed_execution_afk_run(conn, run_id := "01J61800000000000000000001")

        repo = AsyncpgOutcomeRepository(conn)
        result = await repo.create_or_replay_afk_execution_binding(
            awx_job_id="618001",
            job_template_id=7,
            provider=Provider.GITHUB,
            repository=REPO,
            resource_number="6181",
            external_session_id=external_session_id,
            outcome=ExecutionOutcome.COMPLETED,
            title="Issue 618 test",
            afk_run_id=run_id,
            ulid_source=__import__("afk_outcomes.serialization", fromlist=["SequenceULID"]).SequenceULID(
                1_700_000_000_000, start=1
            ),
        )
        assert result.is_created is True

        row = await conn.fetchrow(
            "SELECT session_id, external_session_id FROM afk_run_sessions"
            " WHERE afk_run_id = $1 AND external_session_id = $2",
            run_id,
            external_session_id,
        )
        assert row is not None
        assert row["external_session_id"] == external_session_id
        assert row["session_id"] == internal_session_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_unresolved_session_keeps_external_id(
    db_pool: asyncpg.Pool,
) -> None:
    """A binding whose external session id matches no Gateway session persists
    the link with session_id NULL, retaining the external id."""
    async with db_pool.acquire() as conn:
        await _seed_execution_afk_run(conn, run_id := "01J61800000000000000000002")
        external_session_id = f"ses_618_unresolved_{uuid.uuid4().hex[:8]}"

        repo = AsyncpgOutcomeRepository(conn)
        result = await repo.create_or_replay_afk_execution_binding(
            awx_job_id="618002",
            job_template_id=7,
            provider=Provider.GITHUB,
            repository=REPO,
            resource_number="6182",
            external_session_id=external_session_id,
            outcome=ExecutionOutcome.COMPLETED,
            title="Issue 618 unresolved",
            afk_run_id=run_id,
            ulid_source=__import__("afk_outcomes.serialization", fromlist=["SequenceULID"]).SequenceULID(
                1_700_000_000_000, start=2
            ),
        )
        assert result.is_created is True

        row = await conn.fetchrow(
            "SELECT session_id, external_session_id FROM afk_run_sessions"
            " WHERE afk_run_id = $1 AND external_session_id = $2",
            run_id,
            external_session_id,
        )
        assert row is not None
        assert row["session_id"] is None
        assert row["external_session_id"] == external_session_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_replay_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """Replaying the identical binding never duplicates the afk_run_sessions row."""
    async with db_pool.acquire() as conn:
        external_session_id = f"ses_618_replay_{uuid.uuid4().hex[:8]}"
        await _seed_gateway_session(conn, external_session_id=external_session_id)
        await _seed_execution_afk_run(conn, run_id := "01J61800000000000000000003")

        repo = AsyncpgOutcomeRepository(conn)
        ulid_source = __import__("afk_outcomes.serialization", fromlist=["SequenceULID"]).SequenceULID(
            1_700_000_000_000, start=3
        )
        kwargs = dict(
            awx_job_id="618003",
            job_template_id=7,
            provider=Provider.GITHUB,
            repository=REPO,
            resource_number="6183",
            external_session_id=external_session_id,
            outcome=ExecutionOutcome.COMPLETED,
            title="Issue 618 replay",
            afk_run_id=run_id,
            ulid_source=ulid_source,
        )
        await repo.create_or_replay_afk_execution_binding(**kwargs)
        await repo.create_or_replay_afk_execution_binding(**kwargs)

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_run_sessions"
            " WHERE afk_run_id = $1 AND external_session_id = $2",
            run_id,
            external_session_id,
        )
        assert count == 1, f"expected 1 session link after replay, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_fill_in_persists_afk_run_session_link(
    db_pool: asyncpg.Pool,
) -> None:
    """A terminal PATCH supplying a previously-missing session persists the
    afk_run_sessions row without erasing existing values (enrich-only)."""
    async with db_pool.acquire() as conn:
        external_session_id = f"ses_618_terminal_{uuid.uuid4().hex[:8]}"
        internal_session_id = await _seed_gateway_session(
            conn, external_session_id=external_session_id
        )
        await _seed_execution_afk_run(conn, run_id := "01J61800000000000000000004")

        repo = AsyncpgOutcomeRepository(conn)
        # Phase 1 — running provisioning, no session yet.
        await repo.create_or_replay_afk_execution_binding(
            awx_job_id="618004",
            job_template_id=7,
            external_session_id=None,
            outcome=ExecutionOutcome.RUNNING,
            title="Issue 618 two-phase",
            afk_run_id=run_id,
            ulid_source=__import__("afk_outcomes.serialization", fromlist=["SequenceULID"]).SequenceULID(
                1_700_000_000_000, start=4
            ),
        )

        # Phase 2 — terminal update fills the session + resource.
        update = await repo.update_execution_binding_terminal(
            awx_job_id="618004",
            outcome=ExecutionOutcome.COMPLETED,
            finished_at=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
            external_session_id=external_session_id,
            provider=Provider.GITHUB,
            repository=REPO,
            resource_number="6184",
        )
        assert update.is_updated is True

        row = await conn.fetchrow(
            "SELECT session_id, external_session_id FROM afk_run_sessions"
            " WHERE afk_run_id = $1 AND external_session_id = $2",
            run_id,
            external_session_id,
        )
        assert row is not None
        assert row["session_id"] == internal_session_id
        assert row["external_session_id"] == external_session_id
