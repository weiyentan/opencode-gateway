"""Closure-episode projection integration tests (issue #524).

Covers the seams between the pure-domain projector and the persistence /
consumer layers:

* the repository recompute is a DB-local, event-triggered projection over
  the immutable ``engineering_events`` facts — the projection writes are
  deterministic upserts (links, episodes, unresolved), episodes are
  reconciled (current vs superseded, never deleted), and the whole
  projection is rebuildable from facts;
* the consumer ``_persist`` triggers the recompute only AFTER the facts
  transaction commits, and a failing projector never blocks valid
  ingestion (facts still commit, offsets still advance, no DLQ);
* the Alembic migration (0036) and the documentation-style ORM models carry
  the three projection tables with the episode/link/unresolved constraints;
* against a real Postgres (skipped when unavailable): the full
  change-request -> issue closure flow through the consumer path, a
  rebuild-from-facts convergence test, and recompute idempotence.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from afk_outcomes.models import (
    CLOSURE_RESOLVER_VERSION,
    ClosureEpisodeStatus,
    ClosureLinkKind,
    ClosureLinkState,
    ClosureUnresolved,
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)
from afk_outcomes.repository import (
    AsyncpgOutcomeRepository,
    _issue_links_from_payload,
    _to_closure_fact,
)
from app.consumer.afk_consumer import AFKOutcomeConsumer
from app.core.repository import normalize_repository_url
from app.db.models.afk import ClosureEpisode, ClosureLink

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

# ══════════════════════════════════════════════════════════════════════════════
#  Pure repository write-path tests (mock connection)
# ══════════════════════════════════════════════════════════════════════════════


def _execute_calls(conn: AsyncMock) -> list[str]:
    return [call.args[0] for call in conn.execute.call_args_list]


def test_upsert_closure_link_is_deterministic_conflict_update(mock_conn: AsyncMock) -> None:
    """Link state is a recomputed view: conflict-updated toward the latest
    derivation, stamped revoked only while revoked, never deleted."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    link = ClosureLink(
        change_request_provider=Provider.GITLAB,
        change_request_repository="gitlab.com/group/proj",
        change_request_external_id="6",
        issue_provider=Provider.GITLAB,
        issue_repository="gitlab.com/group/proj",
        issue_external_id="1",
        kind=ClosureLinkKind.DECLARES_CLOSURE,
        state=ClosureLinkState.ACTIVE,
    )

    asyncio.run(repo._upsert_closure_link(link))

    calls = [
        call.args
        for call in mock_conn.execute.call_args_list
        if "closure_links" in call.args[0]
    ]
    assert len(calls) == 1
    sql, *args = calls[0]
    normalized = " ".join(sql.split())
    assert (
        "ON CONFLICT (change_request_provider, change_request_repository, "
        "change_request_external_id, issue_provider, issue_repository, "
        "issue_external_id, kind)" in normalized
    )
    assert "state = EXCLUDED.state" in normalized
    assert "revoked_at = CASE WHEN EXCLUDED.state = 'revoked' THEN now() ELSE NULL END" in normalized
    assert "DELETE" not in normalized.upper()


# ── focused: $8 type consistency for both active and revoked states ─────────


def test_upsert_closure_link_active_state_produces_null_revoked_at(
    mock_conn: AsyncMock,
) -> None:
    """An ACTIVE link inserts with revoked_at=NULL: the CASE WHEN must not
    stamp revoked_at when state is 'active'."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    link = ClosureLink(
        change_request_provider=Provider.GITHUB,
        change_request_repository="github.com/org/repo",
        change_request_external_id="100",
        issue_provider=Provider.GITHUB,
        issue_repository="github.com/org/repo",
        issue_external_id="1",
        kind=ClosureLinkKind.DECLARES_CLOSURE,
        state=ClosureLinkState.ACTIVE,
    )

    asyncio.run(repo._upsert_closure_link(link))

    calls = [
        call.args
        for call in mock_conn.execute.call_args_list
        if "closure_links" in call.args[0]
    ]
    assert len(calls) == 1
    sql, *args = calls[0]
    normalized = " ".join(sql.split())
    # state parameter is the 8th positional arg (index 7)
    assert args[7] == "active"
    # The VALUES CASE WHEN must evaluate to NULL for active state.
    assert "CASE WHEN $8::varchar = 'revoked' THEN now() ELSE NULL END" in normalized
    assert "$8::varchar" in normalized
    assert "$8::text" not in normalized


def test_upsert_closure_link_revoked_state_stamps_revoked_at(
    mock_conn: AsyncMock,
) -> None:
    """A REVOKED link inserts with revoked_at=now(): the CASE WHEN must
    stamp revoked_at when state is 'revoked'."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    link = ClosureLink(
        change_request_provider=Provider.GITHUB,
        change_request_repository="github.com/org/repo",
        change_request_external_id="100",
        issue_provider=Provider.GITHUB,
        issue_repository="github.com/org/repo",
        issue_external_id="1",
        kind=ClosureLinkKind.DECLARES_CLOSURE,
        state=ClosureLinkState.REVOKED,
    )

    asyncio.run(repo._upsert_closure_link(link))

    calls = [
        call.args
        for call in mock_conn.execute.call_args_list
        if "closure_links" in call.args[0]
    ]
    assert len(calls) == 1
    sql, *args = calls[0]
    normalized = " ".join(sql.split())
    # state parameter is the 8th positional arg (index 7)
    assert args[7] == "revoked"
    # The VALUES CASE WHEN must evaluate to now() for revoked state.
    assert "CASE WHEN $8::varchar = 'revoked' THEN now() ELSE NULL END" in normalized
    assert "$8::varchar" in normalized
    assert "$8::text" not in normalized


def test_upsert_closure_link_on_conflict_clears_revoked_at_for_active(
    mock_conn: AsyncMock,
) -> None:
    """On conflict, an ACTIVE state must clear revoked_at (re-activation)."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    link = ClosureLink(
        change_request_provider=Provider.GITHUB,
        change_request_repository="github.com/org/repo",
        change_request_external_id="200",
        issue_provider=Provider.GITHUB,
        issue_repository="github.com/org/repo",
        issue_external_id="2",
        kind=ClosureLinkKind.REFERENCES,
        state=ClosureLinkState.ACTIVE,
    )

    asyncio.run(repo._upsert_closure_link(link))

    calls = [
        call.args[0]
        for call in mock_conn.execute.call_args_list
        if "closure_links" in call.args[0]
    ]
    assert len(calls) == 1
    normalized = " ".join(calls[0].split())
    # DO UPDATE SET must clear revoked_at on re-activation
    assert (
        "revoked_at = CASE WHEN EXCLUDED.state = 'revoked' THEN now() ELSE NULL END"
        in normalized
    )


def test_upsert_closure_link_state_parameter_has_consistent_type(
    mock_conn: AsyncMock,
) -> None:
    """Regression: $8 used in both VALUES and CASE WHEN must use one consistent
    type so PostgreSQL does not raise ``AmbiguousParameterError``.

    The fix explicitly casts $8 as varchar in both the INSERT VALUES and the
    CASE WHEN comparison, so PostgreSQL sees one consistent type.
    """
    import re

    repo = AsyncpgOutcomeRepository(mock_conn)
    for state in (ClosureLinkState.ACTIVE, ClosureLinkState.REVOKED):
        mock_conn.reset_mock()
        link = ClosureLink(
            change_request_provider=Provider.GITLAB,
            change_request_repository="gitlab.com/group/proj",
            change_request_external_id="6",
            issue_provider=Provider.GITLAB,
            issue_repository="gitlab.com/group/proj",
            issue_external_id="1",
            kind=ClosureLinkKind.DECLARES_CLOSURE,
            state=state,
        )
        asyncio.run(repo._upsert_closure_link(link))

        sql_calls = [
            call.args[0]
            for call in mock_conn.execute.call_args_list
            if "closure_links" in call.args[0]
        ]
        assert len(sql_calls) == 1, f"expected one closure_links call for state={state}"
        normalized = " ".join(sql_calls[0].split())

        # $8 must be explicitly typed consistently in both usages.
        assert "$8::text" not in normalized, (
            f"spurious ::text cast on $8 reintroduces type ambiguity:\n{normalized}"
        )

        assert normalized.count("$8::varchar") >= 2
        case_matches = re.findall(r"CASE WHEN (\S+) = 'revoked'", normalized)
        assert any(
            m == "$8::varchar" for m in case_matches
        ), f"typed $8 must appear in CASE WHEN: {case_matches}"


def test_upsert_closure_unresolved_is_versioned_conflict_update(mock_conn: AsyncMock) -> None:
    """Unresolved records are keyed by (issue, closed_at, reason) and
    conflict-updated — versioned, never deleted, never tie-broken."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    record = ClosureUnresolved(
        issue_provider=Provider.GITLAB,
        issue_repository="gitlab.com/group/proj",
        issue_external_id="1",
        closed_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        reason="ambiguous",
    )

    asyncio.run(repo._upsert_closure_unresolved(record))

    calls = [
        call.args
        for call in mock_conn.execute.call_args_list
        if "closure_unresolved" in call.args[0]
    ]
    assert len(calls) == 1
    sql, *args = calls[0]
    normalized = " ".join(sql.split())
    assert "ON CONFLICT (issue_provider, issue_repository, issue_external_id, closed_at, reason)" in normalized
    assert "candidates = EXCLUDED.candidates" in normalized
    assert "DELETE" not in normalized.upper()
    # resolver version recorded on every unresolved row
    assert CLOSURE_RESOLVER_VERSION in args


def test_recompute_skips_irrelevant_event_types(mock_conn: AsyncMock) -> None:
    """An event type outside the closure-relevant vocabulary never touches
    the projection (no reads, no writes)."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    entity = EngineeringEntity(
        entity_id="issue:1",
        entity_type=EntityType.ISSUE,
        provider=Provider.GITLAB,
        repository="gitlab.com/group/proj",
    )
    event = EngineeringEvent(
        event_id="issue:1:updated",
        event_type="issue.updated",
        provider=Provider.GITLAB,
        entity_id="issue:1",
        occurred_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
    )

    asyncio.run(
        repo.recompute_closure_projection(
            seed_event=event,
            seed_entity=entity,
            normalize_repository=normalize_repository_url,
        )
    )

    mock_conn.execute.assert_not_called()
    mock_conn.fetch.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
#  JSONB boundary decoding (issue #540)
# ══════════════════════════════════════════════════════════════════════════════

_IDENTITY = lambda value: value  # noqa: E731 - identity normalizer for unit tests


def _snapshot_targets(snapshot) -> list[tuple[str, str]]:
    """Return the (repository, number) pairs of a snapshot's references."""
    return [(t.repository, t.number) for t in snapshot.references]


def test_issue_links_from_dict_payload_projects_references() -> None:
    """A dictionary-shaped ``issue_links`` payload projects its references."""
    raw = {
        "references": [
            {"repository": "gitlab.com/group/proj", "number": "1"},
            {"repository": "gitlab.com/group/proj", "number": "2"},
        ],
        "declares_closure": [],
    }
    snapshot = _issue_links_from_payload(raw, _IDENTITY)
    assert snapshot is not None
    assert _snapshot_targets(snapshot) == [
        ("gitlab.com/group/proj", "1"),
        ("gitlab.com/group/proj", "2"),
    ]
    assert snapshot.declares_closure == []


def test_issue_links_from_json_string_payload_projects_references() -> None:
    """A JSON-string ``issue_links`` payload (asyncpg JSONB shape) decodes and
    projects its references — the regression this issue fixes."""
    raw = json.dumps(
        {
            "references": [
                {"repository": "gitlab.com/group/proj", "number": "1"},
            ],
            "declares_closure": [],
        }
    )
    snapshot = _issue_links_from_payload(raw, _IDENTITY)
    assert snapshot is not None
    assert _snapshot_targets(snapshot) == [("gitlab.com/group/proj", "1")]


def test_issue_links_missing_or_empty_metadata_yields_none() -> None:
    """A payload without an ``issue_links`` object (or with empty sets) yields
    no snapshot — a missing field is never a revocation."""
    assert _issue_links_from_payload(None, _IDENTITY) is None
    assert _issue_links_from_payload({}, _IDENTITY) is None
    assert (
        _issue_links_from_payload(
            {"references": [], "declares_closure": []}, _IDENTITY
        )
        is None
    )


def test_issue_links_malformed_json_yields_none_without_crashing() -> None:
    """Malformed JSON in the ``issue_links`` value is tolerated: no snapshot,
    no exception — the committed fact is preserved and closure metadata
    omitted."""
    assert _issue_links_from_payload("{not valid json", _IDENTITY) is None
    assert _issue_links_from_payload("[]", _IDENTITY) is None
    assert _issue_links_from_payload('"a string"', _IDENTITY) is None


def test_issue_links_skips_malformed_entries_keeps_valid_ones() -> None:
    """Within a valid payload, malformed individual link entries are skipped
    while valid entries in the same payload are retained."""
    raw = {
        "references": [
            {"repository": "gitlab.com/group/proj", "number": "1"},
            {"repository": "gitlab.com/group/proj", "number": 2},  # non-str number
            "not-a-dict",
            {"repository": "gitlab.com/group/proj"},  # missing number
            {"number": "5"},  # missing repository
            {"repository": "gitlab.com/group/proj", "number": "3"},
        ],
        "declares_closure": [],
    }
    snapshot = _issue_links_from_payload(raw, _IDENTITY)
    assert snapshot is not None
    assert _snapshot_targets(snapshot) == [
        ("gitlab.com/group/proj", "1"),
        ("gitlab.com/group/proj", "3"),
    ]


def test_issue_links_references_and_declares_closure_stay_distinct() -> None:
    """``references`` and ``declares_closure`` are projected into separate
    buckets and never conflated."""
    raw = {
        "references": [{"repository": "gitlab.com/group/proj", "number": "1"}],
        "declares_closure": [{"repository": "gitlab.com/group/proj", "number": "2"}],
    }
    snapshot = _issue_links_from_payload(raw, _IDENTITY)
    assert snapshot is not None
    assert _snapshot_targets(snapshot) == [("gitlab.com/group/proj", "1")]
    assert [(t.repository, t.number) for t in snapshot.declares_closure] == [
        ("gitlab.com/group/proj", "2")
    ]


def test_to_closure_fact_accepts_dict_and_json_string_payloads() -> None:
    """``_to_closure_fact`` tolerates both a dict payload and a JSON-string
    payload (the whole JSONB column returned by asyncpg as a string)."""
    base = dict(
        provider=Provider.GITLAB,
        repository="gitlab.com/group/proj",
        entity_type=EntityType.CHANGE_REQUEST,
        external_id="6",
        event_type="change_request.opened",
        occurred_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        observed_via="webhook",
        normalize=_IDENTITY,
    )
    payload_dict = {
        "issue_links": {
            "references": [{"repository": "gitlab.com/group/proj", "number": "1"}],
            "declares_closure": [],
        }
    }
    payload_str = json.dumps(payload_dict)

    fact_dict = _to_closure_fact(payload=payload_dict, **base)
    fact_str = _to_closure_fact(payload=payload_str, **base)

    assert fact_dict.issue_links is not None
    assert fact_str.issue_links is not None
    assert _snapshot_targets(fact_dict.issue_links) == [
        ("gitlab.com/group/proj", "1")
    ]
    assert _snapshot_targets(fact_str.issue_links) == [
        ("gitlab.com/group/proj", "1")
    ]


def test_to_closure_fact_malformed_payload_omits_closure_metadata() -> None:
    """A malformed or non-object payload does not crash ``_to_closure_fact``:
    the fact is still built (the committed fact is preserved) with closure
    metadata omitted."""
    base = dict(
        provider=Provider.GITLAB,
        repository="gitlab.com/group/proj",
        entity_type=EntityType.CHANGE_REQUEST,
        external_id="6",
        event_type="change_request.opened",
        occurred_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        observed_via="webhook",
        normalize=_IDENTITY,
    )
    for bad_payload in ("{not valid json", "[]", '"a string"', 42, None):
        fact = _to_closure_fact(payload=bad_payload, **base)
        assert fact.issue_links is None, f"payload {bad_payload!r} leaked closure metadata"
        assert fact.event_type == "change_request.opened"


# ══════════════════════════════════════════════════════════════════════════════
#  Consumer-path wiring + failure injection (mocked connection)
# ══════════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """A fake asyncpg connection that records transaction/recompute ordering."""

    def __init__(self, order: list[str], *, fail_recompute: bool = False) -> None:
        self._order = order
        self._fail_recompute = fail_recompute
        self.execute = AsyncMock(return_value="OK")
        self.fetch = AsyncMock(return_value=[])

    def transaction(self):
        return _RecordingTransaction(self._order)

    async def recompute(self, *, seed_event, seed_entity, normalize_repository) -> None:
        self._order.append("recompute")
        if self._fail_recompute:
            raise RuntimeError("projector exploded")


class _RecordingTransaction:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def __aenter__(self) -> _RecordingTransaction:
        self._order.append("tx_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._order.append("tx_exit")
        return False


class _AcquireCtx:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


class _FakeAdapter:
    provider = Provider.GITLAB

    async def fetch_entities(self, repository, *, since=None, until=None):
        return []

    async def fetch_events(self, repository, *, since=None, until=None):
        return []


def _mk_msg(value: dict) -> MagicMock:
    from aiokafka.structs import ConsumerRecord

    msg = MagicMock(spec=ConsumerRecord)
    msg.value = json.dumps(value).encode("utf-8")
    msg.offset = 42
    msg.partition = 0
    msg.topic = "engineering.events.normalized"
    msg.key = None
    msg.headers = ()
    return msg


def _valid_payload() -> dict:
    return {
        "schema_version": "1.0",
        "event_type": "normalized",
        "provider": "gitlab",
        "delivery_id": "11111111-2222-3333-4444-555555555555",
        "resource": {
            "type": "merge_request",
            "repository_url": "https://gitlab.com/group/proj",
            "number": 6,
        },
        "action": "merged",
        "occurred_at": "2026-08-01T10:00:00Z",
        "ingested_at": "2026-08-01T10:01:00Z",
        "actor": "carol",
        "redacted_payload": {
            "reference": {
                "provider": "gitlab",
                "delivery_id": "11111111-2222-3333-4444-555555555555",
            }
        },
    }


def _make_consumer(conn: _RecordingConn) -> AFKOutcomeConsumer:
    return AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=_Pool(conn),  # type: ignore[arg-type]
        provider=Provider.GITLAB,
        repository="group/proj",
        adapter=_FakeAdapter(),
        max_retries=1,
        initial_backoff=0.0,
        max_backoff=0.0,
    )


@pytest.mark.asyncio
async def test_persist_recomputes_projection_after_facts_transaction() -> None:
    """The recompute runs only after the facts transaction commits — write
    boundary: facts first, projection second."""
    order: list[str] = []
    conn = _RecordingConn(order)
    consumer = _make_consumer(conn)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock(
        side_effect=lambda *args, **kwargs: order.append("commit")
    )
    consumer._producer = AsyncMock()
    with patch.object(
        AsyncpgOutcomeRepository,
        "recompute_closure_projection",
        side_effect=conn.recompute,
    ):
        await consumer._process_message(_mk_msg(_valid_payload()))

    assert order == ["tx_enter", "tx_exit", "recompute", "commit"]
    consumer._producer.send_and_wait.assert_not_called()
    sqls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("INSERT INTO delivery_log" in s for s in sqls)
    assert any("INSERT INTO engineering_events" in s for s in sqls)


@pytest.mark.asyncio
async def test_projection_failure_never_blocks_ingestion() -> None:
    """A failing projector must never block valid ingestion: facts still
    commit, the offset still advances, nothing routes to the DLQ."""
    order: list[str] = []
    conn = _RecordingConn(order, fail_recompute=True)
    consumer = _make_consumer(conn)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock(
        side_effect=lambda *args, **kwargs: order.append("commit")
    )
    consumer._producer = AsyncMock()
    with patch.object(
        AsyncpgOutcomeRepository,
        "recompute_closure_projection",
        side_effect=conn.recompute,
    ):
        await consumer._process_message(_mk_msg(_valid_payload()))

    # facts committed + offset committed; the recompute failure was absorbed
    assert order == ["tx_enter", "tx_exit", "recompute", "commit"]
    consumer._producer.send_and_wait.assert_not_called()
    consumer._consumer.commit.assert_called_once()
    sqls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("INSERT INTO engineering_events" in s for s in sqls)


@pytest.mark.asyncio
async def test_persist_passes_normalizer_and_seed_to_recompute() -> None:
    """The consumer hands the recompute the committed event/entity and the
    application's repository-URL normalizer."""
    conn = _RecordingConn([])
    consumer = _make_consumer(conn)
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    with patch.object(
        AsyncpgOutcomeRepository, "recompute_closure_projection", new_callable=AsyncMock
    ) as mock_recompute:
        await consumer._process_message(_mk_msg(_valid_payload()))

    mock_recompute.assert_awaited_once()
    kwargs = mock_recompute.call_args.kwargs
    assert kwargs["seed_event"].event_type == "change_request.merged"
    assert kwargs["seed_entity"].entity_type is EntityType.CHANGE_REQUEST
    assert kwargs["normalize_repository"] is normalize_repository_url


# ══════════════════════════════════════════════════════════════════════════════
#  Migration 0036 + ORM schema
# ══════════════════════════════════════════════════════════════════════════════

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"
_MIGRATION_PATH = _ALEMBIC_DIR / "versions" / "0036_closure_episode_projection.py"


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0036 migration module by file path (versions/ is not a package)."""
    spec = importlib.util.spec_from_file_location("closure_migration_0036", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _ddl(model) -> str:
    compiled = CreateTable(model.__table__).compile(dialect=postgresql.dialect())
    return str(compiled)


def test_migration_module_declares_revision_0036() -> None:
    module = _load_migration_module()
    assert module.revision == "0036"
    assert module.down_revision == "0035"


def test_migration_module_imports_on_py39() -> None:
    """Importing the 0036 migration must not trip the ``str | None`` 3.9 error."""
    _load_migration_module()


def test_closure_episode_orm_model_carries_episode_shape() -> None:
    """The episode table documents both endpoint identities, the status, the
    superseded marker, and provenance columns."""
    ddl = _ddl(ClosureEpisode)
    for column in (
        "issue_provider",
        "issue_repository",
        "issue_external_id",
        "opened_at",
        "closed_at",
        "status",
        "change_request_provider",
        "change_request_repository",
        "change_request_external_id",
        "resolver_version",
        "derived_at",
        "superseded_at",
    ):
        assert column in ddl, f"closure_episodes missing {column}"
    assert not ClosureEpisode.__table__.columns["status"].nullable


def test_closure_link_orm_model_carries_both_endpoints_and_state() -> None:
    """The link table documents both endpoint identities, the relationship
    kind, the derived state, and the revocation stamp."""
    ddl = _ddl(ClosureLink)
    for column in (
        "change_request_provider",
        "change_request_repository",
        "change_request_external_id",
        "issue_provider",
        "issue_repository",
        "issue_external_id",
        "kind",
        "state",
        "revoked_at",
        "resolver_version",
        "derived_at",
    ):
        assert column in ddl, f"closure_links missing {column}"
    assert "uq_closure_links_identity" in ddl


def test_migration_0036_upgrade_is_additive_and_guarded_on_py39() -> None:
    """0036 only creates the three projection tables (no drop/alter).

    Skips on Python 3.9 where the pre-existing 0024/0025 migrations trip the
    module-level ``str | None`` import error (alembic loads the whole chain).
    """
    from alembic.command import upgrade

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0035:0036", sql=True)
    except BaseException as exc:  # noqa: BLE001 - re-raise unless pre-existing
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level); "
                "run on Python >=3.12 to exercise the offline render."
            )
        raise
    sql = buf.getvalue()
    for table in ("closure_links", "closure_episodes", "closure_unresolved"):
        assert f"CREATE TABLE {table}" in sql, f"missing CREATE TABLE {table}"
    assert "DROP" not in sql
    assert "ALTER TABLE" not in sql


# ══════════════════════════════════════════════════════════════════════════════
#  Real-Postgres integration (skipped when the test DB is unavailable)
# ══════════════════════════════════════════════════════════════════════════════

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 5433
_DEFAULT_DB = "opencode_gateway_test"
_DEFAULT_USER = "opencode_test"
_DEFAULT_PASSWORD = "opencode_test"


def _dsn() -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{_DEFAULT_HOST}:{_DEFAULT_PORT}/{_DEFAULT_DB}"
    )


async def _can_connect() -> bool:
    import asyncpg

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
async def db_pool(_integration_db_available: bool):
    import asyncpg

    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    import alembic.command
    import alembic.config

    sync_url = _dsn().replace("postgresql://", "postgresql+psycopg://")
    alembic_cfg = alembic.config.Config(str(_PROJ_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
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


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _entity(entity_type: EntityType, external_id: str, repository: str) -> EngineeringEntity:
    return EngineeringEntity(
        entity_id=f"{entity_type.value}:{external_id}",
        entity_type=entity_type,
        provider=Provider.GITLAB,
        repository=repository,
    )


def _event(
    entity_type: EntityType,
    event_type: str,
    external_id: str,
    occurred_at: datetime,
    *,
    payload: dict | None = None,
    observed_via: str = "webhook",
) -> EngineeringEvent:
    return EngineeringEvent(
        event_id=f"{entity_type.value}:{external_id}:{event_type.split('.')[-1]}",
        event_type=event_type,
        provider=Provider.GITLAB,
        entity_id=f"{entity_type.value}:{external_id}",
        occurred_at=occurred_at,
        payload=payload or {},
        observed_via=observed_via,
    )


async def _ingest(
    conn,
    *,
    delivery_id: str,
    entity: EngineeringEntity,
    event: EngineeringEvent,
    recompute: bool = True,
) -> None:
    repo = AsyncpgOutcomeRepository(conn)
    await repo.record_event(
        provider=Provider.GITLAB,
        delivery_id=delivery_id,
        entity=entity,
        event=event,
    )
    if recompute:
        await repo.recompute_closure_projection(
            seed_event=event,
            seed_entity=entity,
            normalize_repository=normalize_repository_url,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_flow_projects_inferred_cross_repo_episode(db_pool) -> None:
    """The full same-repo + cross-repo GitLab flow through the consumer path
    projects exactly one inferred episode with independent endpoint keys."""
    cr_repo = "gitlab.com/application/api"
    issue_repo = "gitlab.com/platform/tracking"
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

    opened = _event(
        EntityType.CHANGE_REQUEST,
        "change_request.opened",
        "10",
        now,
        payload={
            "issue_links": {
                "references": [],
                "declares_closure": [
                    {
                        # raw producer URL — normalized by the recompute
                        "repository": "https://gitlab.com/platform/tracking",
                        "number": "25",
                    }
                ],
            }
        },
    )
    merged = _event(EntityType.CHANGE_REQUEST, "change_request.merged", "10", now + timedelta(minutes=5))
    closed = _event(EntityType.ISSUE, "issue.closed", "25", now + timedelta(minutes=10))

    async with db_pool.acquire() as conn:
        await _ingest(
            conn,
            delivery_id="delivery-1",
            entity=_entity(EntityType.CHANGE_REQUEST, "10", cr_repo),
            event=opened,
        )
        await _ingest(
            conn,
            delivery_id="delivery-2",
            entity=_entity(EntityType.CHANGE_REQUEST, "10", cr_repo),
            event=merged,
        )
        await _ingest(
            conn,
            delivery_id="delivery-3",
            entity=_entity(EntityType.ISSUE, "25", issue_repo),
            event=closed,
        )

        episode = await conn.fetchrow(
            "SELECT status, issue_repository, issue_external_id, "
            "change_request_repository, change_request_external_id, "
            "superseded_at, resolver_version "
            "FROM closure_episodes WHERE issue_external_id = '25'"
        )
        assert episode is not None, "no closure episode projected"
        assert episode["status"] == ClosureEpisodeStatus.INFERRED.value
        # independent change-request and issue repository keys
        assert episode["issue_repository"] == issue_repo
        assert episode["change_request_repository"] == cr_repo
        assert episode["change_request_external_id"] == "10"
        assert episode["superseded_at"] is None
        assert episode["resolver_version"] == CLOSURE_RESOLVER_VERSION

        link = await conn.fetchrow(
            "SELECT state, kind FROM closure_links "
            "WHERE issue_repository = $1 AND issue_external_id = '25'",
            issue_repo,
        )
        assert link is not None
        assert link["state"] == ClosureLinkState.ACTIVE.value
        assert link["kind"] == ClosureLinkKind.DECLARES_CLOSURE.value

        unresolved_count = await conn.fetchval("SELECT count(*) FROM closure_unresolved")
        assert unresolved_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_and_unresolved_are_versioned_never_tie_broken(db_pool) -> None:
    """Two merged declaring change requests produce an ambiguous episode with
    a versioned unresolved record carrying both candidates."""
    repo = "gitlab.com/cloudnative-pg"
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    snapshot = {"issue_links": {"references": [], "declares_closure": [{"repository": f"https://gitlab.com/{repo.removeprefix('gitlab.com/')}", "number": "1"}]}}

    async with db_pool.acquire() as conn:
        for cr_number, base in (("6", 0), ("8", 60)):
            await _ingest(
                conn,
                delivery_id=f"delivery-cr{cr_number}-open",
                entity=_entity(EntityType.CHANGE_REQUEST, cr_number, repo),
                event=_event(
                    EntityType.CHANGE_REQUEST,
                    "change_request.opened",
                    cr_number,
                    now + timedelta(seconds=base),
                    payload=snapshot,
                ),
            )
            await _ingest(
                conn,
                delivery_id=f"delivery-cr{cr_number}-merge",
                entity=_entity(EntityType.CHANGE_REQUEST, cr_number, repo),
                event=_event(
                    EntityType.CHANGE_REQUEST,
                    "change_request.merged",
                    cr_number,
                    now + timedelta(seconds=base + 30),
                ),
            )
        await _ingest(
            conn,
            delivery_id="delivery-close",
            entity=_entity(EntityType.ISSUE, "1", repo),
            event=_event(EntityType.ISSUE, "issue.closed", "1", now + timedelta(seconds=300)),
        )

        episode = await conn.fetchrow(
            "SELECT status, change_request_external_id FROM closure_episodes "
            "WHERE issue_external_id = '1'"
        )
        assert episode["status"] == ClosureEpisodeStatus.AMBIGUOUS.value
        assert episode["change_request_external_id"] is None  # never tie-broken

        record = await conn.fetchrow(
            "SELECT reason, candidates, resolver_version FROM closure_unresolved "
            "WHERE issue_external_id = '1'"
        )
        assert record is not None
        assert record["reason"] == "ambiguous"
        assert sorted(c["external_id"] for c in record["candidates"]) == ["6", "8"]
        assert record["resolver_version"] == CLOSURE_RESOLVER_VERSION


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_from_facts_converges(db_pool) -> None:
    """The projection is rebuildable from facts: ingest facts without any
    recompute (projector down), then replay every stored fact through the
    recompute — the projection converges to the same inferred episode."""
    repo = "gitlab.com/cloudnative-pg"
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

    async with db_pool.acquire() as conn:
        facts = [
            (
                "delivery-open",
                _entity(EntityType.CHANGE_REQUEST, "6", repo),
                _event(
                    EntityType.CHANGE_REQUEST,
                    "change_request.opened",
                    "6",
                    now,
                    payload={
                        "issue_links": {
                            "references": [],
                            "declares_closure": [
                                {"repository": f"https://gitlab.com/{repo.removeprefix('gitlab.com/')}", "number": "1"}
                            ],
                        }
                    },
                ),
            ),
            (
                "delivery-merge",
                _entity(EntityType.CHANGE_REQUEST, "6", repo),
                _event(EntityType.CHANGE_REQUEST, "change_request.merged", "6", now + timedelta(minutes=5)),
            ),
            (
                "delivery-close",
                _entity(EntityType.ISSUE, "1", repo),
                _event(EntityType.ISSUE, "issue.closed", "1", now + timedelta(minutes=10)),
            ),
        ]
        # projector down: facts commit, projection stays empty
        for delivery_id, entity, event in facts:
            await _ingest(conn, delivery_id=delivery_id, entity=entity, event=event, recompute=False)
        assert await conn.fetchval("SELECT count(*) FROM closure_episodes") == 0

        # rebuild: replay every stored fact through the same event-triggered seam
        rows = await conn.fetch(
            "SELECT provider, repository, entity_type, external_id, event_type, "
            "occurred_at, observed_via, payload FROM engineering_events"
        )
        repo_instance = AsyncpgOutcomeRepository(conn)
        for row in rows:
            entity = _entity(EntityType(row["entity_type"]), row["external_id"], row["repository"])
            event = EngineeringEvent(
                event_id=f"{row['entity_type']}:{row['external_id']}:{row['event_type']}",
                event_type=row["event_type"],
                provider=Provider(row["provider"]),
                entity_id=f"{row['entity_type']}:{row['external_id']}",
                occurred_at=row["occurred_at"],
                payload=row["payload"] or {},
                observed_via=row["observed_via"],
            )
            await repo_instance.recompute_closure_projection(
                seed_event=event,
                seed_entity=entity,
                normalize_repository=normalize_repository_url,
            )

        episode = await conn.fetchrow(
            "SELECT status, change_request_external_id FROM closure_episodes "
            "WHERE issue_external_id = '1'"
        )
        assert episode is not None
        assert episode["status"] == ClosureEpisodeStatus.INFERRED.value
        assert episode["change_request_external_id"] == "6"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_is_idempotent(db_pool) -> None:
    """Re-running the recompute for the same fact converges — one episode row,
    one link row, never duplicates."""
    repo = "gitlab.com/cloudnative-pg"
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    entity = _entity(EntityType.CHANGE_REQUEST, "6", repo)
    event = _event(
        EntityType.CHANGE_REQUEST,
        "change_request.opened",
        "6",
        now,
        payload={
            "issue_links": {
                "references": [],
                "declares_closure": [
                    {"repository": f"https://gitlab.com/{repo.removeprefix('gitlab.com/')}", "number": "1"}
                ],
            }
        },
    )

    async with db_pool.acquire() as conn:
        for _ in range(2):
            await _ingest(conn, delivery_id="delivery-same", entity=entity, event=event)

        assert await conn.fetchval("SELECT count(*) FROM closure_episodes") == 1
        assert await conn.fetchval("SELECT count(*) FROM closure_links") == 1
        episode = await conn.fetchrow("SELECT status FROM closure_episodes")
        assert episode["status"] == ClosureEpisodeStatus.PENDING.value


# ══════════════════════════════════════════════════════════════════════════════
#  Real-Postgres: $8 type-consistency regression (issue #559)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closure_link_active_state_revoked_at_is_null(db_pool) -> None:
    """Against real PostgreSQL: an ACTIVE closure link inserts with
    revoked_at=NULL — the $8 parameter must be consistently typed so
    the CASE WHEN evaluates correctly."""
    cr_repo = "gitlab.com/org/cr"
    issue_repo = "gitlab.com/org/issue"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        link = ClosureLink(
            change_request_provider=Provider.GITLAB,
            change_request_repository=cr_repo,
            change_request_external_id="50",
            issue_provider=Provider.GITLAB,
            issue_repository=issue_repo,
            issue_external_id="1",
            kind=ClosureLinkKind.DECLARES_CLOSURE,
            state=ClosureLinkState.ACTIVE,
        )
        await repo._upsert_closure_link(link)

        row = await conn.fetchrow(
            "SELECT state, revoked_at FROM closure_links "
            "WHERE change_request_external_id = '50' AND issue_external_id = '1'"
        )
        assert row is not None, "ACTIVE closure link not inserted"
        assert row["state"] == ClosureLinkState.ACTIVE.value
        assert row["revoked_at"] is None, (
            "revoked_at must be NULL for ACTIVE links"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closure_link_revoked_state_revoked_at_is_stamped(db_pool) -> None:
    """Against real PostgreSQL: a REVOKED closure link inserts with
    revoked_at=now() — the $8 parameter must be consistently typed so
    the CASE WHEN evaluates correctly."""
    cr_repo = "gitlab.com/org/cr"
    issue_repo = "gitlab.com/org/issue"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)
        link = ClosureLink(
            change_request_provider=Provider.GITLAB,
            change_request_repository=cr_repo,
            change_request_external_id="51",
            issue_provider=Provider.GITLAB,
            issue_repository=issue_repo,
            issue_external_id="1",
            kind=ClosureLinkKind.DECLARES_CLOSURE,
            state=ClosureLinkState.REVOKED,
        )
        await repo._upsert_closure_link(link)

        row = await conn.fetchrow(
            "SELECT state, revoked_at FROM closure_links "
            "WHERE change_request_external_id = '51' AND issue_external_id = '1'"
        )
        assert row is not None, "REVOKED closure link not inserted"
        assert row["state"] == ClosureLinkState.REVOKED.value
        assert row["revoked_at"] is not None, (
            "revoked_at must be stamped for REVOKED links"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_closure_link_revoked_to_active_clears_revoked_at(db_pool) -> None:
    """Against real PostgreSQL: re-activating a REVOKED link clears revoked_at.
    This exercises both the INSERT CASE WHEN and the DO UPDATE CASE WHEN
    with the same $8 parameter type."""
    cr_repo = "gitlab.com/org/cr"
    issue_repo = "gitlab.com/org/issue"

    async with db_pool.acquire() as conn:
        repo = AsyncpgOutcomeRepository(conn)

        # First: insert as REVOKED
        revoked_link = ClosureLink(
            change_request_provider=Provider.GITLAB,
            change_request_repository=cr_repo,
            change_request_external_id="52",
            issue_provider=Provider.GITLAB,
            issue_repository=issue_repo,
            issue_external_id="1",
            kind=ClosureLinkKind.DECLARES_CLOSURE,
            state=ClosureLinkState.REVOKED,
        )
        await repo._upsert_closure_link(revoked_link)

        row = await conn.fetchrow(
            "SELECT state, revoked_at FROM closure_links "
            "WHERE change_request_external_id = '52' AND issue_external_id = '1'"
        )
        assert row["state"] == ClosureLinkState.REVOKED.value
        assert row["revoked_at"] is not None

        # Second: upsert as ACTIVE (re-activation)
        active_link = ClosureLink(
            change_request_provider=Provider.GITLAB,
            change_request_repository=cr_repo,
            change_request_external_id="52",
            issue_provider=Provider.GITLAB,
            issue_repository=issue_repo,
            issue_external_id="1",
            kind=ClosureLinkKind.DECLARES_CLOSURE,
            state=ClosureLinkState.ACTIVE,
        )
        await repo._upsert_closure_link(active_link)

        row = await conn.fetchrow(
            "SELECT state, revoked_at FROM closure_links "
            "WHERE change_request_external_id = '52' AND issue_external_id = '1'"
        )
        assert row["state"] == ClosureLinkState.ACTIVE.value
        assert row["revoked_at"] is None, (
            "revoked_at must be cleared on re-activation (ACTIVE state)"
        )
