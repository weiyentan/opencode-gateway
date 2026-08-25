"""Unit tests for the provisional lifecycle repository methods (issue #589).

These tests mock asyncpg and verify the *write semantics* encoded in the
SQL issued by the repository:

* **Idempotent provisioning** — keyed on ``(provider, host,
  source_event_id)``; replay returns the existing row without mutation;
  conflicting replay is flagged without mutation.
* **Recovery without predecessor mutation** — a recovery lifecycle
  references its predecessor via ``recovered_from_afk_run_id`` and issues
  only reads against the predecessor; a missing predecessor is flagged.
* **1:1 lifecycle<->change_request binding** — explicit, idempotent, and
  conflict-aware; the change request cannot belong to two lifecycles.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from afk_outcomes import AsyncpgOutcomeRepository
from afk_outcomes.models import EntityType, Provider, TriggerType
from afk_outcomes.repository import (
    ChangeRequestBindingResult,
    ChangeRequestLookupResult,
    ProvisionAFKRunResult,
)
from afk_outcomes.serialization import SequenceULID
from tests.conftest import mock_row

_ULID_SOURCE = SequenceULID(1_700_000_000_000, start=1)

_PREDECESSOR_ID = "01HPRED000000000000000001"
# The 26-char ULID produced by _ULID_SOURCE's first call.
_NEW_ULID = "01HF7YAT000000000000000001"


def _run(coro):
    return asyncio.run(coro)


def _provision_payload(
    *,
    provider: Provider = Provider.GITHUB,
    host: str = "awx-01.internal",
    source_event_id: str = "eda-1234",
    repository: str = "github.com/acme/proj",
    trigger_type: TriggerType = TriggerType.EDA,
    title: str | None = "Implement auth",
    recovered_from_afk_run_id: str | None = None,
) -> dict:
    """Build keyword arguments for ``provision_afk_run``."""
    return {
        "provider": provider,
        "host": host,
        "source_event_id": source_event_id,
        "repository": repository,
        "trigger_type": trigger_type,
        "title": title,
        "recovered_from_afk_run_id": recovered_from_afk_run_id,
        "ulid_source": _ULID_SOURCE,
    }


def _existing_row(
    *,
    afk_run_id: str = _NEW_ULID,
    repository: str = "github.com/acme/proj",
    trigger_type: str = "eda",
    title: str | None = "Implement auth",
    recovered_from_afk_run_id: str | None = None,
) -> MagicMock:
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "repository": repository,
            "trigger_type": trigger_type,
            "title": title,
            "recovered_from_afk_run_id": recovered_from_afk_run_id,
        }
    )


def _bound_run_row(
    *,
    afk_run_id: str = _NEW_ULID,
    cr_provider: str | None = None,
    cr_repository: str | None = None,
    cr_external_id: str | None = None,
) -> MagicMock:
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "change_request_provider": cr_provider,
            "change_request_repository": cr_repository,
            "change_request_external_id": cr_external_id,
        }
    )


def _calls_matching(conn: AsyncMock, pattern: str) -> list[tuple]:
    """Return (sql, params) for every fetch/fetchrow/execute call matching ``pattern``."""
    results = []
    for call in conn.fetch.call_args_list + conn.fetchrow.call_args_list:
        if call.args and re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    for call in conn.execute.call_args_list:
        if call.args and re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    return results


def _insert_calls(conn: AsyncMock) -> list[tuple]:
    return _calls_matching(conn, r"INSERT INTO afk_runs")


def _update_calls(conn: AsyncMock) -> list[tuple]:
    return _calls_matching(conn, r"UPDATE afk_runs")


# ══════════════════════════════════════════════════════════════════════════
#  provision_afk_run — first creation
# ══════════════════════════════════════════════════════════════════════════


def test_provision_first_call_creates_lifecycle(mock_conn: AsyncMock) -> None:
    """First call inserts the afk_runs row and returns is_created=True."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(
        return_value=[mock_row({"afk_run_id": _NEW_ULID})]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert isinstance(result, ProvisionAFKRunResult)
    assert result.is_created is True
    assert result.is_conflict is False
    assert result.predecessor_missing is False
    assert result.afk_run_id == _NEW_ULID
    assert len(result.afk_run_id) == 26  # ULID length


def test_provision_inserts_pending_status_and_provenance(mock_conn: AsyncMock) -> None:
    """The INSERT carries status='pending' plus the lifecycle provenance columns."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(
        return_value=[mock_row({"afk_run_id": _NEW_ULID})]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    _run(repo.provision_afk_run(**_provision_payload()))

    calls = _insert_calls(mock_conn)
    assert len(calls) == 1
    sql, args = calls[0]
    assert "'pending'" in sql
    for col in (
        "host",
        "source_event_id",
        "repository",
        "trigger_type",
        "change_request_provider",
        "change_request_repository",
        "change_request_external_id",
        "recovered_from_afk_run_id",
    ):
        assert col in sql, f"INSERT missing column: {col}"
    # args: ulid, provider, title, host, source_event_id, repository,
    # trigger_type, recovered_from_afk_run_id
    assert isinstance(args[0], str) and len(args[0]) == 26  # ULID
    assert args[1] == "github"
    assert args[2] == "Implement auth"
    assert args[3] == "awx-01.internal"
    assert args[4] == "eda-1234"
    assert args[5] == "github.com/acme/proj"
    assert args[6] == "eda"
    assert args[7] is None


def test_provision_uses_on_conflict_with_partial_predicate(
    mock_conn: AsyncMock,
) -> None:
    """The INSERT is conflict-safe against the partial provisioning-key index."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(
        return_value=[mock_row({"afk_run_id": _NEW_ULID})]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    _run(repo.provision_afk_run(**_provision_payload()))

    calls = _insert_calls(mock_conn)
    sql = calls[0][0]
    assert "ON CONFLICT (provider, host, source_event_id)" in sql
    assert "WHERE host IS NOT NULL AND source_event_id IS NOT NULL" in sql
    assert "DO NOTHING" in sql
    assert "RETURNING afk_run_id" in sql


def test_provision_race_falls_back_to_winner_row(mock_conn: AsyncMock) -> None:
    """A concurrent insert that loses the race returns the winner's afk_run_id.

    The winner's full row is re-read; a matching payload classifies the lost
    race as an idempotent replay (``is_conflict=False``).
    """
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            _existing_row(afk_run_id="01HWINNER00000000000000001"),
        ]
    )
    mock_conn.fetch = AsyncMock(return_value=[])

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert result.is_created is False
    assert result.is_conflict is False
    assert result.afk_run_id == "01HWINNER00000000000000001"


def test_provision_race_with_different_payload_is_conflict(
    mock_conn: AsyncMock,
) -> None:
    """A lost race whose winner carries a different payload is a conflict."""
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            _existing_row(
                afk_run_id="01HWINNER00000000000000001",
                repository="github.com/acme/other",
            ),
        ]
    )
    mock_conn.fetch = AsyncMock(return_value=[])

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert result.is_created is False
    assert result.is_conflict is True
    assert result.afk_run_id == "01HWINNER00000000000000001"


# ══════════════════════════════════════════════════════════════════════════
#  provision_afk_run — idempotent replay and conflict
# ══════════════════════════════════════════════════════════════════════════


def test_provision_replay_returns_existing_without_mutation(mock_conn: AsyncMock) -> None:
    """Identical replay returns the existing row and issues no INSERT."""
    mock_conn.fetchrow = AsyncMock(return_value=_existing_row())

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert result.afk_run_id == _NEW_ULID
    assert result.is_created is False
    assert result.is_conflict is False
    assert _insert_calls(mock_conn) == []


def test_provision_replay_with_different_payload_is_conflict(
    mock_conn: AsyncMock,
) -> None:
    """The same key with a different payload returns is_conflict without mutation."""
    mock_conn.fetchrow = AsyncMock(
        return_value=_existing_row(title="Different title")
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert result.is_conflict is True
    assert result.is_created is False
    assert _insert_calls(mock_conn) == []


def test_provision_conflict_detects_repository_difference(mock_conn: AsyncMock) -> None:
    """A replay changing only the repository identity is a conflict."""
    mock_conn.fetchrow = AsyncMock(
        return_value=_existing_row(repository="github.com/acme/other")
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.provision_afk_run(**_provision_payload()))

    assert result.is_conflict is True


# ══════════════════════════════════════════════════════════════════════════
#  provision_afk_run — recovery relationships
# ══════════════════════════════════════════════════════════════════════════


def test_provision_recovery_references_predecessor_without_mutation(
    mock_conn: AsyncMock,
) -> None:
    """A recovery lifecycle carries recovered_from_afk_run_id and never
    mutates the predecessor (reads only, no UPDATE against it)."""
    mock_conn.fetchrow = AsyncMock(
        side_effect=[None, mock_row({"afk_run_id": _PREDECESSOR_ID})]
    )
    mock_conn.fetch = AsyncMock(
        return_value=[mock_row({"afk_run_id": _NEW_ULID})]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _provision_payload(
        trigger_type=TriggerType.RECOVERY,
        recovered_from_afk_run_id=_PREDECESSOR_ID,
    )
    result = _run(repo.provision_afk_run(**payload))

    assert result.is_created is True
    calls = _insert_calls(mock_conn)
    assert len(calls) == 1
    args = calls[0][1]
    assert args[7] == _PREDECESSOR_ID
    # No UPDATE of any kind was issued against the predecessor.
    assert _update_calls(mock_conn) == []
    # The predecessor lookup is a plain SELECT (never a mutation).
    select_calls = _calls_matching(mock_conn, r"SELECT afk_run_id FROM afk_runs")
    assert len(select_calls) == 1
    assert "WHERE afk_run_id = $1" in select_calls[0][0]


def test_provision_recovery_missing_predecessor_is_flagged(
    mock_conn: AsyncMock,
) -> None:
    """A recovered_from_afk_run_id that references no run is flagged."""
    mock_conn.fetchrow = AsyncMock(side_effect=[None, None])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _provision_payload(
        trigger_type=TriggerType.RECOVERY,
        recovered_from_afk_run_id="01HMISSING0000000000000001",
    )
    result = _run(repo.provision_afk_run(**payload))

    assert result.predecessor_missing is True
    assert result.is_created is False
    assert _insert_calls(mock_conn) == []


# ══════════════════════════════════════════════════════════════════════════
#  bind_change_request — first binding
# ══════════════════════════════════════════════════════════════════════════


def _binding_kwargs(
    *,
    provider: Provider = Provider.GITLAB,
    repository: str = "gitlab.com/cnp/cnp",
    external_id: str = "6",
) -> dict:
    return {
        "afk_run_id": _NEW_ULID,
        "provider": provider,
        "repository": repository,
        "external_id": external_id,
    }


def test_bind_first_call_updates_all_three_columns(mock_conn: AsyncMock) -> None:
    """First bind sets the three change-request columns and returns is_bound."""
    mock_conn.fetchrow = AsyncMock(side_effect=[_bound_run_row(), None])
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert isinstance(result, ChangeRequestBindingResult)
    assert result.is_bound is True
    assert result.is_conflict is False
    assert result.run_missing is False

    updates = _update_calls(mock_conn)
    assert len(updates) == 1
    sql, args = updates[0]
    assert "SET change_request_provider = $2" in sql
    assert "change_request_repository = $3" in sql
    assert "change_request_external_id = $4" in sql
    assert "WHERE afk_run_id = $1" in sql
    assert "change_request_provider IS NULL" in sql
    assert args == (_NEW_ULID, "gitlab", "gitlab.com/cnp/cnp", "6")


def test_bind_checks_1_to_1_ownership_before_update(mock_conn: AsyncMock) -> None:
    """The bind pre-checks that no other lifecycle owns the change request."""
    other = _bound_run_row(afk_run_id="01HOTHER000000000000000001")
    mock_conn.fetchrow = AsyncMock(side_effect=[_bound_run_row(), other])
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.is_conflict is True
    assert result.is_bound is False
    assert _update_calls(mock_conn) == []


def test_bind_other_owner_select_filters_on_change_request_identity(
    mock_conn: AsyncMock,
) -> None:
    """The 1:1 pre-check SELECT filters on the flattened change-request identity."""
    mock_conn.fetchrow = AsyncMock(side_effect=[_bound_run_row(), None])
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")

    repo = AsyncpgOutcomeRepository(mock_conn)
    _run(repo.bind_change_request(**_binding_kwargs()))

    selects = _calls_matching(mock_conn, r"SELECT afk_run_id FROM afk_runs")
    assert len(selects) == 1
    sql, args = selects[0]
    assert "WHERE change_request_provider = $1" in sql
    assert "AND change_request_repository = $2" in sql
    assert "AND change_request_external_id = $3" in sql
    assert args == ("gitlab", "gitlab.com/cnp/cnp", "6")


# ══════════════════════════════════════════════════════════════════════════
#  bind_change_request — idempotent replay and conflicts
# ══════════════════════════════════════════════════════════════════════════


def test_bind_replay_same_identity_issues_no_update(mock_conn: AsyncMock) -> None:
    """Re-binding the same identity is a no-op (no flags, no UPDATE)."""
    row = _bound_run_row(
        cr_provider="gitlab",
        cr_repository="gitlab.com/cnp/cnp",
        cr_external_id="6",
    )
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_conn.execute = AsyncMock()

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.is_bound is False
    assert result.is_conflict is False
    assert result.run_missing is False
    assert _update_calls(mock_conn) == []


def test_bind_different_identity_on_bound_run_is_conflict(
    mock_conn: AsyncMock,
) -> None:
    """A lifecycle already carrying a different change request is a conflict."""
    row = _bound_run_row(
        cr_provider="gitlab",
        cr_repository="gitlab.com/cnp/cnp",
        cr_external_id="7",
    )
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_conn.execute = AsyncMock()

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.is_conflict is True
    assert _update_calls(mock_conn) == []


def test_bind_missing_run_is_flagged(mock_conn: AsyncMock) -> None:
    """Binding a change request to an unknown lifecycle returns run_missing."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock()

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.run_missing is True
    assert result.is_bound is False
    assert _update_calls(mock_conn) == []


def test_bind_concurrent_unique_violation_is_conflict(mock_conn: AsyncMock) -> None:
    """A concurrent bind of the same change request surfaces as a conflict."""
    mock_conn.fetchrow = AsyncMock(side_effect=[_bound_run_row(), None])
    mock_conn.execute = AsyncMock(
        side_effect=asyncpg.UniqueViolationError("duplicate key")
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.is_conflict is True
    assert result.is_bound is False


def test_bind_lost_race_reclassifies_as_replay(mock_conn: AsyncMock) -> None:
    """A lost race (0 rows updated) re-reads and classifies replay vs conflict."""
    unbound = _bound_run_row()
    rebound = _bound_run_row(
        cr_provider="gitlab",
        cr_repository="gitlab.com/cnp/cnp",
        cr_external_id="6",
    )
    mock_conn.fetchrow = AsyncMock(side_effect=[unbound, None, rebound])
    mock_conn.execute = AsyncMock(return_value="UPDATE 0")

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = _run(repo.bind_change_request(**_binding_kwargs()))

    assert result.is_conflict is False
    assert result.is_bound is False


# ══════════════════════════════════════════════════════════════════════════
#  get_afk_run_lifecycle
# ══════════════════════════════════════════════════════════════════════════


def _lifecycle_row(**overrides) -> MagicMock:
    row = {
        "afk_run_id": _NEW_ULID,
        "provider": "github",
        "status": "pending",
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
        "change_request_provider": None,
        "change_request_repository": None,
        "change_request_external_id": None,
        "recovered_from_afk_run_id": None,
        "first_seen_at": None,
        "last_seen_at": None,
    }
    row.update(overrides)
    return mock_row(row)


def test_get_lifecycle_returns_none_when_missing(mock_conn: AsyncMock) -> None:
    """Lookup by afk_run_id returns None when no run exists."""
    mock_conn.fetchrow = AsyncMock(return_value=None)

    repo = AsyncpgOutcomeRepository(mock_conn)
    assert _run(repo.get_afk_run_lifecycle("01HMISSING0000000000000001")) is None


def test_get_lifecycle_maps_provisioned_row(mock_conn: AsyncMock) -> None:
    """A provisioned row reconstructs with provenance, identity, and binding."""
    mock_conn.fetchrow = AsyncMock(
        return_value=_lifecycle_row(
            change_request_provider="gitlab",
            change_request_repository="gitlab.com/cnp/cnp",
            change_request_external_id="6",
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    lifecycle = _run(repo.get_afk_run_lifecycle(_NEW_ULID))

    assert lifecycle is not None
    assert lifecycle.afk_run_id == _NEW_ULID
    assert lifecycle.provider == Provider.GITHUB
    assert lifecycle.status == "pending"
    assert lifecycle.host == "awx-01.internal"
    assert lifecycle.source_event_id == "eda-1234"
    assert lifecycle.trigger_type == "eda"
    identity = lifecycle.change_request_identity()
    assert identity is not None
    assert identity.provider == Provider.GITLAB
    assert identity.resource_type == EntityType.CHANGE_REQUEST
    assert identity.resource_number == "6"


def test_get_lifecycle_maps_legacy_row_leniently(mock_conn: AsyncMock) -> None:
    """A legacy row (no lifecycle columns) reads back with None fields."""
    mock_conn.fetchrow = AsyncMock(
        return_value=_lifecycle_row(
            host=None,
            source_event_id=None,
            repository=None,
            trigger_type=None,
            status="completed",
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    lifecycle = _run(repo.get_afk_run_lifecycle(_NEW_ULID))

    assert lifecycle is not None
    assert lifecycle.status == "completed"
    assert lifecycle.host is None
    assert lifecycle.source_event_id is None
    assert lifecycle.repository is None
    assert lifecycle.trigger_type is None
    assert lifecycle.change_request_identity() is None


def test_get_lifecycle_select_includes_all_lifecycle_columns(
    mock_conn: AsyncMock,
) -> None:
    """The SELECT reads every lifecycle column."""
    mock_conn.fetchrow = AsyncMock(return_value=None)

    repo = AsyncpgOutcomeRepository(mock_conn)
    _run(repo.get_afk_run_lifecycle(_NEW_ULID))

    sql = mock_conn.fetchrow.call_args[0][0]
    for col in (
        "host",
        "source_event_id",
        "repository",
        "trigger_type",
        "change_request_provider",
        "change_request_repository",
        "change_request_external_id",
        "recovered_from_afk_run_id",
    ):
        assert col in sql, f"SELECT missing column: {col}"
    assert "WHERE afk_run_id = $1" in sql


# ══════════════════════════════════════════════════════════════════════════
#  get_afk_run_by_change_request — change-request -> owning run lookup
# ══════════════════════════════════════════════════════════════════════════


def _lookup_kwargs(
    *,
    provider: Provider = Provider.GITLAB,
    repository: str = "gitlab.com/cnp/cnp",
    external_id: str = "6",
) -> dict:
    return {
        "provider": provider,
        "repository": repository,
        "external_id": external_id,
    }


@pytest.mark.asyncio
async def test_lookup_returns_owning_run(mock_conn: AsyncMock) -> None:
    """A bound change request resolves to its owning afk_run_id."""
    mock_conn.fetch = AsyncMock(
        return_value=[mock_row({"afk_run_id": _NEW_ULID})]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = await repo.get_afk_run_by_change_request(**_lookup_kwargs())

    assert isinstance(result, ChangeRequestLookupResult)
    assert result.afk_run_id == _NEW_ULID
    assert result.is_conflict is False


@pytest.mark.asyncio
async def test_lookup_queries_only_change_request_binding_columns(
    mock_conn: AsyncMock,
) -> None:
    """The SELECT filters on the three explicit binding columns only."""
    mock_conn.fetch = AsyncMock(return_value=[])

    repo = AsyncpgOutcomeRepository(mock_conn)
    await repo.get_afk_run_by_change_request(**_lookup_kwargs())

    sql, *args = mock_conn.fetch.call_args[0]
    assert "FROM afk_runs" in sql
    assert "WHERE change_request_provider = $1" in sql
    assert "AND change_request_repository = $2" in sql
    assert "AND change_request_external_id = $3" in sql
    assert tuple(args) == ("gitlab", "gitlab.com/cnp/cnp", "6")


@pytest.mark.asyncio
async def test_lookup_unbound_returns_none(mock_conn: AsyncMock) -> None:
    """An unknown or unbound change request returns no owning run."""
    mock_conn.fetch = AsyncMock(return_value=[])

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = await repo.get_afk_run_by_change_request(**_lookup_kwargs())

    assert result.afk_run_id is None
    assert result.is_conflict is False


@pytest.mark.asyncio
async def test_lookup_multiple_owners_is_conflict(mock_conn: AsyncMock) -> None:
    """More than one lifecycle claiming the change request is a conflict."""
    mock_conn.fetch = AsyncMock(
        return_value=[
            mock_row({"afk_run_id": _NEW_ULID}),
            mock_row({"afk_run_id": "01HOTHER000000000000000001"}),
        ]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    result = await repo.get_afk_run_by_change_request(**_lookup_kwargs())

    assert result.is_conflict is True
    assert result.afk_run_id is None


@pytest.mark.asyncio
async def test_lookup_is_read_only(mock_conn: AsyncMock) -> None:
    """The lookup issues only a SELECT — no writes."""
    mock_conn.fetch = AsyncMock(return_value=[])

    repo = AsyncpgOutcomeRepository(mock_conn)
    await repo.get_afk_run_by_change_request(**_lookup_kwargs())

    assert mock_conn.execute.call_count == 0
    assert mock_conn.fetchrow.call_count == 0
