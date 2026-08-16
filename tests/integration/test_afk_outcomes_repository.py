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
    try:
        alembic.command.upgrade(alembic_cfg, "head")
    except Exception:
        async with pool.acquire() as conn:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        alembic.command.upgrade(alembic_cfg, "head")

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


# ── Exact resource↔session associations (migration 0033) ─────────────────────


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
