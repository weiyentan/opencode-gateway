"""Unit tests for the asyncpg-backed AFK OutcomeRepository (issue #448).

These tests mock asyncpg and verify the *write semantics* encoded in the SQL
issued by the repository:

* engineering events are immutable facts — inserted with ``ON CONFLICT DO
  NOTHING`` (re-delivery no-ops);
* ``delivery_log`` is replay-safe via ``ON CONFLICT (provider, delivery_id)
  DO NOTHING``;
* entity links are enrich-only — confidence raised with ``GREATEST`` (never
  lowered), evidence appended (``||``), superseded links marked with an
  ``UPDATE ... SET superseded_at`` (never deleted);
* unresolved correlations are written to ``unresolved_correlations`` only.

Integration tests against docker-compose Postgres live under
``tests/integration/test_afk_outcomes_repository.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from afk_outcomes import (
    RESOLVER_VERSION,
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
    RunEntityLink,
    RunSessionLink,
    RunStatus,
    UnresolvedCorrelation,
    UnresolvedReason,
)
from tests.conftest import mock_row

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

RUN_ID = "01J0000000000000000000000001"
STARTED = datetime(2026, 8, 13, 8, 0, 0, tzinfo=UTC)
FINISHED = datetime(2026, 8, 13, 10, 10, 29, tzinfo=UTC)
REPO = "weiyentan/opencode-gateway"


def _build_run() -> AFKRun:
    """A compact run exercising resolved + unresolved correlations and links."""
    return AFKRun(
        afk_run_id=RUN_ID,
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Consolidated run",
        started_at=STARTED,
        finished_at=FINISHED,
        entities=[
            EngineeringEntity(
                entity_id="issue:437",
                entity_type=EntityType.ISSUE,
                provider=Provider.GITHUB,
                repository=REPO,
                number=437,
            ),
            EngineeringEntity(
                entity_id="issue:436",
                entity_type=EntityType.ISSUE,
                provider=Provider.GITHUB,
                repository=REPO,
                number=436,
            ),
        ],
        events=[
            EngineeringEvent(
                event_id="issue:437:opened",
                event_type="opened",
                provider=Provider.GITHUB,
                entity_id="issue:437",
                occurred_at=STARTED,
                actor="wyautomation",
                payload={},
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-resolved",
                afk_run_id=RUN_ID,
                entity_id="issue:437",
                correlation_confidence=1.0,
                method="issue_resolved",
                evidence=[
                    CorrelationEvidence(
                        kind="commit_message_reference",
                        source_entity_id="commit:abc",
                        detail="resolves #437",
                        weight=1.0,
                    ),
                ],
            ),
            Correlation(
                correlation_id="corr-unresolved",
                afk_run_id=RUN_ID,
                entity_id="issue:436",
                correlation_confidence=0.1,
                method="issue_mention",
                evidence=[
                    CorrelationEvidence(
                        kind="issue_mention",
                        source_entity_id="change_request:442",
                        detail="mentioned #436",
                        weight=0.1,
                    ),
                ],
            ),
        ],
        outcome=EngineeringOutcome(
            status=EngineeringOutcomeStatus.MERGED,
            change_request_ids=["change_request:442"],
            resolved_issue_ids=["issue:437"],
            merge_event_id="merge_event:442",
            merged_at=FINISHED,
        ),
        entity_links=[
            RunEntityLink(
                afk_run_id=RUN_ID,
                entity_id="issue:437",
                role="resolved",
                correlation_confidence=1.0,
            ),
            RunEntityLink(
                afk_run_id=RUN_ID,
                entity_id="issue:436",
                role="referenced",
                correlation_confidence=0.1,
            ),
        ],
        session_links=[
            RunSessionLink(
                afk_run_id=RUN_ID,
                session_id="00000000-0000-0000-0000-000000000001",
                external_session_id="ses_01J000000000000000000000001",
                started_at=STARTED,
                finished_at=FINISHED,
            ),
        ],
    )


def _execute_calls(conn: AsyncMock) -> list[str]:
    """Return the SQL string of every ``conn.execute`` call, in order."""
    return [call.args[0] for call in conn.execute.call_args_list]


def _calls_matching(conn: AsyncMock, pattern: str) -> list[tuple]:
    """Return (sql, params) for every execute call whose SQL matches ``pattern``."""
    return [
        (call.args[0], call.args[1:])
        for call in conn.execute.call_args_list
        if re.search(pattern, call.args[0])
    ]


# ── Engineering events — immutable facts ────────────────────────────────────


def test_save_inserts_events_with_conflict_ignore(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO engineering_events")
    assert calls, "no engineering_events insert issued"
    sql = calls[0][0]
    assert (
        "ON CONFLICT (provider, repository, entity_type, external_id, event_type, occurred_at)"
        in sql
    )
    assert "DO NOTHING" in sql


def test_save_event_redelivery_is_a_noop_by_sql(mock_conn: AsyncMock) -> None:
    """Re-saving the same run issues conflict-ignore (never an UPDATE) for events."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    for sql in _execute_calls(mock_conn):
        if "engineering_events" in sql:
            assert "DO NOTHING" in sql, f"event write must be conflict-ignore: {sql!r}"
            assert "DO UPDATE" not in sql


# ── delivery_log — replay-safe ──────────────────────────────────────────────


def test_save_delivery_log_uses_on_conflict_do_nothing(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO delivery_log")
    assert calls, "no delivery_log insert issued"
    sql = calls[0][0]
    assert "ON CONFLICT (provider, delivery_id) DO NOTHING" in sql
    # delivery keyed on (provider, afk_run_id) — the run's own identity
    assert calls[0][1] == ("github", RUN_ID, RUN_ID, "completed")


def test_save_upserts_run_before_logging_delivery(mock_conn: AsyncMock) -> None:
    """The run row must exist before delivery_log references it.

    ``delivery_log.afk_run_id`` has a non-deferrable FK to
    ``afk_runs.afk_run_id``; on the first save of a new run the upsert must
    execute before the delivery insert, otherwise the FK is violated.
    """
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    sqls = _execute_calls(mock_conn)
    run_idx = next(i for i, sql in enumerate(sqls) if "INSERT INTO afk_runs" in sql)
    delivery_idx = next(
        i for i, sql in enumerate(sqls) if "INSERT INTO delivery_log" in sql
    )
    assert run_idx < delivery_idx, (
        "afk_runs upsert must precede delivery_log insert (non-deferrable FK): "
        f"afk_runs@{run_idx}, delivery_log@{delivery_idx}"
    )


# ── Entity links — enrich-only ──────────────────────────────────────────────


def test_save_entity_link_raises_confidence_and_appends_evidence(
    mock_conn: AsyncMock,
) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO afk_run_entities")
    assert calls, "no afk_run_entities insert issued"
    sql = calls[0][0]
    # raise confidence — never a bare overwrite
    assert "GREATEST(" in sql
    assert "afk_run_entities.correlation_confidence" in sql
    # append evidence
    assert "afk_run_entities.evidence || EXCLUDED.evidence" in sql
    # the derived link stores method/confidence/evidence/resolver_version
    assert "correlation_method" in sql
    assert "resolver_version" in sql
    assert "evidence" in sql
    assert "correlation_confidence" in sql
    # lineage provenance columns (issue #456)
    assert "owning_change_request_id" in sql
    assert "correlation_source" in sql


def test_save_marks_superseded_links_without_delete(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    supersede = _calls_matching(mock_conn, r"UPDATE afk_run_entities")
    assert supersede, "no supersede UPDATE issued"
    sql = supersede[0][0]
    assert "SET superseded_at = now()" in sql
    assert "afk_run_id <> $5" in sql
    assert "correlation_confidence < $6" in sql


def test_save_does_not_reactivate_superseded_links(mock_conn: AsyncMock) -> None:
    """A superseded link stays superseded when the same mapping is re-delivered.

    The enrich-only semantics require ``superseded_at`` to be preserved on
    conflict: the ``DO UPDATE`` set must not reset it back to ``NULL``.
    """
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO afk_run_entities")
    assert calls, "no afk_run_entities insert issued"
    sql = calls[0][0]
    # Everything after DO UPDATE SET is the conflict-update clause; it must
    # not mention superseded_at (so a null incoming value never erases a
    # populated superseded_at).
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "superseded_at" not in set_clause, (
        "entity-link upsert must not reset superseded_at on conflict: "
        f"conflict SET clause contains superseded_at: {set_clause!r}"
    )


def test_save_never_issues_delete(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    for sql in _execute_calls(mock_conn):
        assert "DELETE" not in sql.upper(), f"enrich-only repository issued DELETE: {sql!r}"


# ── Unresolved correlations — stored only in unresolved_correlations ────────


def test_save_writes_resolved_correlations_nowhere_unresolved(
    mock_conn: AsyncMock,
) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    unresolved_calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    assert len(unresolved_calls) == 1, (
        f"expected exactly one unresolved correlation (issue:436), got {len(unresolved_calls)}"
    )
    # the unresolved row is the low-confidence mention (issue:436), not 437
    args = unresolved_calls[0][1]
    assert args[3] == "436"  # external_id


def test_save_unresolved_correlation_raises_confidence(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    sql = calls[0][0]
    assert "GREATEST(" in sql
    assert "evidence = unresolved_correlations.evidence || EXCLUDED.evidence" in sql
    assert "afk_run_id = COALESCE" not in sql


def test_unresolved_upsert_conflict_target_includes_afk_run_id(
    mock_conn: AsyncMock,
) -> None:
    """The low-confidence path keys on the run id and never rewrites it."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    assert len(calls) == 1, "expected one low-confidence unresolved correlation"
    sql = calls[0][0]
    assert (
        "ON CONFLICT (provider, repository, entity_type, external_id, afk_run_id, method)"
        in sql
    )
    assert "afk_run_id = COALESCE" not in sql
    assert "GREATEST(" in sql
    assert "evidence = unresolved_correlations.evidence || EXCLUDED.evidence" in sql
    # afk_run_id is bound as the 5th positional arg (index 4) and is the run id.
    assert calls[0][1][4] == RUN_ID


def test_save_entity_link_afk_run_id_is_not_null(mock_conn: AsyncMock) -> None:
    """The entity-link insert always passes a non-null afk_run_id."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO afk_run_entities")
    assert len(calls) == 2, "expected two entity-link inserts"
    for _, args in calls:
        assert args[0] == RUN_ID, "afk_run_id must be the run's ULID (NOT NULL)"


# ── Engine unresolved outcomes — persisted via save_unresolved ────────────────


def _build_unresolved(
    reason: UnresolvedReason = UnresolvedReason.AMBIGUOUS,
    *,
    afk_run_id: str = RUN_ID,
    candidates: list[str] | None = None,
) -> UnresolvedCorrelation:
    return UnresolvedCorrelation(
        unresolved_id="01J0000000000000000000000002",
        afk_run_id=afk_run_id,
        entity_id=afk_run_id,
        reason=reason,
        candidates=(
            candidates
            if candidates is not None
            else ["change_request:300", "change_request:310"]
        ),
        evidence=[
            CorrelationEvidence(
                kind="title_match",
                source_entity_id="change_request:300",
                detail="title=Fix caching bug",
                weight=1.0,
            )
        ],
    )


def test_save_unresolved_persists_ambiguous_with_reason_and_candidates(
    mock_conn: AsyncMock,
) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()
    unresolved = _build_unresolved(UnresolvedReason.AMBIGUOUS)

    import asyncio

    asyncio.run(repo.save_unresolved(run, [unresolved], repository=REPO))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    assert len(calls) == 1
    sql = calls[0][0]
    assert "reason" in sql
    assert "candidates" in sql
    assert "ON CONFLICT (provider, repository, entity_type, external_id, afk_run_id, method)" in sql
    args = calls[0][1]
    assert args[0] == "github"  # provider
    assert args[1] == REPO  # repository
    assert args[2] == "afk_run"  # run-level sentinel entity_type
    assert args[3] == RUN_ID  # external_id == run id
    assert args[4] == RUN_ID  # afk_run_id == run id
    assert args[5] == "ambiguous"  # method mirrors reason
    assert args[6] == "ambiguous"  # reason
    assert "change_request:300" in args[8]  # candidates JSON


def test_save_unresolved_persists_unmatched_with_empty_candidates(
    mock_conn: AsyncMock,
) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()
    unresolved = _build_unresolved(UnresolvedReason.UNMATCHED, candidates=[])

    import asyncio

    asyncio.run(repo.save_unresolved(run, [unresolved], repository=REPO))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    assert len(calls) == 1
    args = calls[0][1]
    assert args[5] == "unmatched"  # method
    assert args[6] == "unmatched"  # reason
    assert args[8] == "[]"  # empty candidates


def test_save_unresolved_is_enrich_only(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()
    unresolved = _build_unresolved(UnresolvedReason.AMBIGUOUS)

    import asyncio

    asyncio.run(repo.save_unresolved(run, [unresolved], repository=REPO))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    sql = calls[0][0]
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "COALESCE(EXCLUDED.reason" in set_clause
    assert "COALESCE(EXCLUDED.candidates" in set_clause
    assert "evidence = unresolved_correlations.evidence || EXCLUDED.evidence" in set_clause
    assert "afk_run_id = COALESCE" not in set_clause


def test_save_unresolved_noop_when_empty(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save_unresolved(run, [], repository=REPO))

    calls = _calls_matching(mock_conn, r"INSERT INTO unresolved_correlations")
    assert calls == []


# ── read path ───────────────────────────────────────────────────────────────


def test_get_returns_none_when_missing(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(return_value=None)
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    assert asyncio.run(repo.get("missing")) is None


def test_get_reconstructs_run(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "afk_run_id": RUN_ID,
                "provider": "github",
                "status": "completed",
                "title": "Consolidated run",
                "started_at": STARTED,
                "finished_at": FINISHED,
                "outcome_status": "merged",
                "outcome": {
                    "status": "merged",
                    "change_request_ids": ["change_request:442"],
                    "resolved_issue_ids": ["issue:437"],
                    "merge_event_id": "merge_event:442",
                    "merged_at": FINISHED.isoformat(),
                },
            }
        )
    )
    mock_conn.fetch = AsyncMock(
        side_effect=[
            # entity_rows
            [
                mock_row(
                    {
                        "provider": "github",
                        "repository": REPO,
                        "entity_type": "issue",
                        "external_id": "437",
                        "owning_change_request_id": None,
                        "role": "resolved",
                        "correlation_method": "issue_resolved",
                        "correlation_source": "direct",
                        "correlation_confidence": 1.0,
                        "evidence": [],
                    }
                ),
            ],
            # session_rows
            [
                mock_row(
                    {
                        "session_id": "00000000-0000-0000-0000-000000000001",
                        "external_session_id": "ses_01J000000000000000000000001",
                        "started_at": STARTED,
                        "finished_at": FINISHED,
                    }
                ),
            ],
            # event_rows
            [],
        ]
    )

    import asyncio

    run = asyncio.run(repo.get(RUN_ID))

    assert run is not None
    assert run.afk_run_id == RUN_ID
    assert run.provider == Provider.GITHUB
    assert run.status == RunStatus.COMPLETED
    assert run.outcome is not None
    assert run.outcome.status == EngineeringOutcomeStatus.MERGED
    assert [link.entity_id for link in run.entity_links] == ["issue:437"]
    assert run.entity_links[0].role == "resolved"
    assert len(run.session_links) == 1
    assert run.session_links[0].external_session_id == "ses_01J000000000000000000000001"


def test_repository_satisfies_protocol_signature() -> None:
    """The implementation keeps the #444 Protocol surface: save(run), get(id)."""
    import inspect

    from afk_outcomes.interfaces import OutcomeRepository

    save = inspect.signature(AsyncpgOutcomeRepository.save)
    get = inspect.signature(AsyncpgOutcomeRepository.get)
    assert list(save.parameters) == ["self", "run"]
    assert list(get.parameters) == ["self", "afk_run_id"]
    assert inspect.iscoroutinefunction(AsyncpgOutcomeRepository.save)
    assert inspect.iscoroutinefunction(AsyncpgOutcomeRepository.get)
    # The concrete class explicitly inherits the #444 Protocol.
    assert OutcomeRepository in AsyncpgOutcomeRepository.__mro__


# ── Multi-repo event isolation (issue #499) ────────────────────────────────


def test_get_events_scoped_by_provider_repository_entity_type_external_id(
    mock_conn: AsyncMock,
) -> None:
    """The event retrieval subquery must scope by (provider, repository, entity_type, external_id).

    Verifies the SQL text contains the full 4-column tuple in the subquery.
    """
    repo = AsyncpgOutcomeRepository(mock_conn)
    run_id = "01J0000000000000000000000001"

    # Mock the run row
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "afk_run_id": run_id,
                "provider": "github",
                "status": "completed",
                "title": "Multi-repo run",
                "started_at": STARTED,
                "finished_at": FINISHED,
                "outcome_status": "merged",
                "outcome": {
                    "status": "merged",
                    "change_request_ids": ["change_request:442"],
                    "resolved_issue_ids": ["issue:437"],
                    "merge_event_id": "merge_event:442",
                    "merged_at": FINISHED.isoformat(),
                },
            }
        )
    )

    # Mock entity_rows, session_rows, event_rows
    mock_conn.fetch = AsyncMock(
        side_effect=[
            # entity_rows — one entity in REPO
            [
                mock_row(
                    {
                        "provider": "github",
                        "repository": REPO,
                        "entity_type": "issue",
                        "external_id": "437",
                        "owning_change_request_id": None,
                        "role": "resolved",
                        "correlation_method": "issue_resolved",
                        "correlation_source": "direct",
                        "correlation_confidence": 1.0,
                        "evidence": [],
                    }
                ),
            ],
            # session_rows
            [],
            # event_rows
            [
                mock_row(
                    {
                        "provider": "github",
                        "repository": REPO,
                        "entity_type": "issue",
                        "external_id": "437",
                        "event_type": "opened",
                        "occurred_at": STARTED,
                        "provider_event_id": "evt-001",
                        "actor": "wyautomation",
                        "payload": {},
                    }
                ),
            ],
        ]
    )

    import asyncio

    run = asyncio.run(repo.get(run_id))
    assert run is not None
    assert len(run.events) == 1
    assert run.events[0].entity_id == "issue:437"

    # Verify the SQL subquery uses the full 4-column tuple
    event_sql = mock_conn.fetch.call_args_list[2].args[0]
    assert "(provider, repository, entity_type, external_id) IN (" in event_sql, (
        "event query must scope by (provider, repository, entity_type, external_id)"
    )
    assert "SELECT provider, repository, entity_type, external_id FROM afk_run_entities" in event_sql


def test_get_events_does_not_mix_repositories(mock_conn: AsyncMock) -> None:
    """Two repos with the same short entity_id must produce isolated event sets.

    Run A (repo-a, issue:437) must NOT see events from repo-b's issue:437.
    This test verifies the SQL subquery scopes by the full 4-column tuple by
    inspecting the SQL text — the mock returns all rows, so we verify the
    query structure, not the mock output.
    """
    repo_a_id = "01J0000000000000000000000A1"
    repo_b_id = "01J0000000000000000000000B1"
    repo_a = "org/repo-a"
    repo_b = "org/repo-b"

    def _mock_get(run_id: str, run_repo: str) -> AsyncMock:
        m = AsyncMock()
        m.fetchrow = AsyncMock(
            return_value=mock_row(
                {
                    "afk_run_id": run_id,
                    "provider": "github",
                    "status": "completed",
                    "title": f"Run in {run_repo}",
                    "started_at": STARTED,
                    "finished_at": FINISHED,
                    "outcome_status": "merged",
                    "outcome": {
                        "status": "merged",
                        "change_request_ids": ["change_request:442"],
                        "resolved_issue_ids": ["issue:437"],
                        "merge_event_id": "merge_event:442",
                        "merged_at": FINISHED.isoformat(),
                    },
                }
            )
        )
        m.fetch = AsyncMock(
            side_effect=[
                # entity_rows
                [
                    mock_row(
                        {
                            "provider": "github",
                            "repository": run_repo,
                            "entity_type": "issue",
                            "external_id": "437",
                            "owning_change_request_id": None,
                            "role": "resolved",
                            "correlation_method": "issue_resolved",
                            "correlation_source": "direct",
                            "correlation_confidence": 1.0,
                            "evidence": [],
                        }
                    ),
                ],
                # session_rows
                [],
                # event_rows — includes events from BOTH repos to test isolation
                [
                    mock_row(
                        {
                            "provider": "github",
                            "repository": run_repo,
                            "entity_type": "issue",
                            "external_id": "437",
                            "event_type": "opened",
                            "occurred_at": STARTED,
                            "provider_event_id": f"evt-{run_id}",
                            "actor": "wyautomation",
                            "payload": {},
                        }
                    ),
                    mock_row(
                        {
                            "provider": "github",
                            "repository": "org/repo-b" if run_repo == repo_a else repo_a,
                            "entity_type": "issue",
                            "external_id": "437",
                            "event_type": "opened",
                            "occurred_at": STARTED,
                            "provider_event_id": "evt-other",
                            "actor": "wyautomation",
                            "payload": {},
                        }
                    ),
                ],
            ]
        )
        return m

    import asyncio

    # Run A — verify the SQL subquery uses the full 4-column tuple
    conn_a = _mock_get(repo_a_id, repo_a)
    repo_a_impl = AsyncpgOutcomeRepository(conn_a)
    run_a = asyncio.run(repo_a_impl.get(repo_a_id))
    assert run_a is not None

    # Verify the SQL subquery structure (the mock returns all rows, so we
    # check the query text instead of the result count)
    event_sql = conn_a.fetch.call_args_list[2].args[0]
    assert "(provider, repository, entity_type, external_id) IN (" in event_sql, (
        "event query must scope by (provider, repository, entity_type, external_id)"
    )
    assert "SELECT provider, repository, entity_type, external_id FROM afk_run_entities" in event_sql

    # Run B — same SQL verification
    conn_b = _mock_get(repo_b_id, repo_b)
    repo_b_impl = AsyncpgOutcomeRepository(conn_b)
    run_b = asyncio.run(repo_b_impl.get(repo_b_id))
    assert run_b is not None

    event_sql_b = conn_b.fetch.call_args_list[2].args[0]
    assert "(provider, repository, entity_type, external_id) IN (" in event_sql_b


def test_get_entities_dedup_scoped_by_full_tuple(mock_conn: AsyncMock) -> None:
    """The seen_entities dedup set must use (provider, repository, entity_type, external_id).

    If the same entity_id appears in two different repositories, both must be
    included in the reconstructed run's entities list.
    """
    repo = AsyncpgOutcomeRepository(mock_conn)
    run_id = "01J0000000000000000000000001"

    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "afk_run_id": run_id,
                "provider": "github",
                "status": "completed",
                "title": "Cross-repo dedup test",
                "started_at": STARTED,
                "finished_at": FINISHED,
                "outcome_status": "merged",
                "outcome": {
                    "status": "merged",
                    "change_request_ids": ["change_request:442"],
                    "resolved_issue_ids": ["issue:437"],
                    "merge_event_id": "merge_event:442",
                    "merged_at": FINISHED.isoformat(),
                },
            }
        )
    )

    mock_conn.fetch = AsyncMock(
        side_effect=[
            # entity_rows — same entity_id in two repos
            [
                mock_row(
                    {
                        "provider": "github",
                        "repository": "org/repo-a",
                        "entity_type": "issue",
                        "external_id": "437",
                        "owning_change_request_id": None,
                        "role": "resolved",
                        "correlation_method": "issue_resolved",
                        "correlation_source": "direct",
                        "correlation_confidence": 1.0,
                        "evidence": [],
                    }
                ),
                mock_row(
                    {
                        "provider": "github",
                        "repository": "org/repo-b",
                        "entity_type": "issue",
                        "external_id": "437",
                        "owning_change_request_id": None,
                        "role": "resolved",
                        "correlation_method": "issue_resolved",
                        "correlation_source": "direct",
                        "correlation_confidence": 1.0,
                        "evidence": [],
                    }
                ),
            ],
            # session_rows
            [],
            # event_rows
            [],
        ]
    )

    import asyncio

    run = asyncio.run(repo.get(run_id))
    assert run is not None
    # Both entities should be present (same entity_id, different repos)
    assert len(run.entities) == 2, (
        f"expected 2 entities (same entity_id in 2 repos), got {len(run.entities)}"
    )
    repos = {e.repository for e in run.entities}
    assert repos == {"org/repo-a", "org/repo-b"}


def test_resolver_version_is_recorded(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    run = _build_run()

    import asyncio

    asyncio.run(repo.save(run))

    calls = _calls_matching(mock_conn, r"INSERT INTO afk_run_entities")
    assert RESOLVER_VERSION == "2"
    # resolver_version is passed as the 12th positional arg (index 11)
    assert calls[0][1][11] == RESOLVER_VERSION
