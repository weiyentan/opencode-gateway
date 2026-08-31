"""Unit tests for the execution-binding repository methods (issue #547, #568).

These tests mock asyncpg and verify the *write semantics* encoded in the SQL
issued by the repository:

* **Atomic insert** — INSERT ON CONFLICT DO NOTHING RETURNING id ensures
  the insert is the linearisation point and returns the inserted row ID
  (or None on conflict).
* **Conflict rejection** — conflicting data for the same AWX job is rejected
  without overwriting the original record.
* **Multiple jobs per resource** — different AWX jobs targeting the same
  GitHub pull request or GitLab merge request are both persisted.
* **Deterministic ordering** — list by provider resource returns all bindings
  in created_at ASC, id ASC order.
* **Failed-then-successful retry** — a failed execution followed by a
  successful retry for the same resource is visible in the history.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import asyncpg

from afk_outcomes import AsyncpgOutcomeRepository
from afk_outcomes.models import (
    EntityType,
    ExecutionBinding,
    ExecutionOutcome,
    Provider,
)
from afk_outcomes.repository import CreateAFKExecutionBindingResult
from afk_outcomes.serialization import SequenceULID
from tests.conftest import mock_row

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

RESOURCE = {
    "provider": Provider.GITHUB,
    "repository": "weiyentan/opencode-gateway",
    "resource_type": EntityType.CHANGE_REQUEST,
    "resource_number": "442",
}


def _make_binding(
    *,
    awx_job_id: str = "100",
    external_session_id: str = "ses_abc123",
    outcome: ExecutionOutcome = ExecutionOutcome.COMPLETED,
    title: str = "Fix caching bug",
    failure_summary: str | None = None,
) -> ExecutionBinding:
    """Build a minimal ExecutionBinding for testing."""
    return ExecutionBinding(
        binding_id="",
        awx_job={"job_id": awx_job_id, "job_template_id": 42},
        external_session_id=external_session_id,
        resource=RESOURCE,
        outcome=outcome,
        title=title,
        failure_summary=failure_summary,
    )


def _calls_matching(conn: AsyncMock, pattern: str) -> list[tuple]:
    """Return (sql, params) for every fetch/execute call whose SQL matches ``pattern``."""
    results = []
    for call in conn.fetch.call_args_list:
        if re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    for call in conn.execute.call_args_list:
        if re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    return results


# ── Idempotent insert ────────────────────────────────────────────────────────


def test_save_execution_binding_uses_on_conflict_do_nothing(mock_conn: AsyncMock) -> None:
    """Atomic insert uses ON CONFLICT DO NOTHING RETURNING id."""
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding = _make_binding(awx_job_id="100")

    import asyncio

    result = asyncio.run(repo.save_execution_binding(binding))
    assert result is not None, "should return inserted row ID on success"

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert calls, "no execution_bindings insert issued"
    sql = calls[0][0]
    assert "ON CONFLICT (awx_job_id) DO NOTHING" in sql
    assert "RETURNING id" in sql


def test_save_execution_binding_writes_all_identity_fields(mock_conn: AsyncMock) -> None:
    """The INSERT includes all provider resource identity and metadata fields."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding = _make_binding(awx_job_id="200", title="Fix caching bug")

    import asyncio

    asyncio.run(repo.save_execution_binding(binding))

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(calls) == 1
    sql = calls[0][0]
    # Verify all expected columns are in the INSERT
    for col in (
        "awx_job_id",
        "job_template_id",
        "external_session_id",
        "provider",
        "repository_url",
        "entity_type",
        "entity_number",
        "outcome",
        "source_event_id",
        "branch",
        "title",
        "failure_reason",
        "failure_summary",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ):
        assert col in sql, f"INSERT missing column: {col}"
    args = calls[0][1]
    assert args[0] == 200  # awx_job_id as int
    assert args[1] == 42  # job_template_id as int
    assert args[2] == "ses_abc123"  # external_session_id
    assert args[3] == "github"  # provider
    assert args[4] == "weiyentan/opencode-gateway"  # repository_url
    assert args[5] == "change_request"  # entity_type
    assert args[6] == "442"  # entity_number
    assert args[7] == "completed"  # outcome
    assert args[10] == "Fix caching bug"  # title
    assert args[12] is None  # failure_summary (after failure_reason, before started_at)


def test_save_execution_binding_writes_failure_summary(mock_conn: AsyncMock) -> None:
    """The INSERT persists the failure_summary column (issue #564)."""
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding = _make_binding(
        awx_job_id="201",
        outcome=ExecutionOutcome.FAILED,
        failure_summary="Process crashed",
    )

    import asyncio

    asyncio.run(repo.save_execution_binding(binding))

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(calls) == 1
    sql = calls[0][0]
    assert "failure_summary" in sql
    args = calls[0][1]
    # failure_summary is the 13th positional parameter (1-indexed).
    assert args[12] == "Process crashed"


# ── Conflict rejection ───────────────────────────────────────────────────────


def test_save_execution_binding_conflict_rejects_overwrite(mock_conn: AsyncMock) -> None:
    """Conflicting data for the same AWX job is rejected (DO NOTHING)."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding_v1 = _make_binding(awx_job_id="300", title="First attempt")
    binding_v2 = _make_binding(awx_job_id="300", title="Conflicting attempt")

    import asyncio

    asyncio.run(repo.save_execution_binding(binding_v1))
    asyncio.run(repo.save_execution_binding(binding_v2))

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(calls) == 2
    # Both use DO NOTHING — the second does not overwrite
    for sql, _ in calls:
        assert "DO NOTHING" in sql
        assert "DO UPDATE" not in sql


# ── Multiple jobs per resource ───────────────────────────────────────────────


def test_save_execution_binding_allows_multiple_jobs_per_resource(
    mock_conn: AsyncMock,
) -> None:
    """Different AWX jobs targeting the same change_request are both persisted."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding_a = _make_binding(awx_job_id="400")
    binding_b = _make_binding(awx_job_id="401")

    import asyncio

    asyncio.run(repo.save_execution_binding(binding_a))
    asyncio.run(repo.save_execution_binding(binding_b))

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(calls) == 2
    # Both succeed — no conflict between different awx_job_ids
    for sql, _ in calls:
        assert "DO NOTHING" in sql


# ── Get by AWX job ID ────────────────────────────────────────────────────────


def test_get_execution_binding_returns_none_when_missing(mock_conn: AsyncMock) -> None:
    """Lookup by AWX job ID returns None when no binding exists."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    result = asyncio.run(repo.get_execution_binding_by_awx_job_id("999"))
    assert result is None


def test_get_execution_binding_null_session_reads_none(mock_conn: AsyncMock) -> None:
    """A NULL external_session_id reconstructs as None, never as ""."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "awx_job_id": 501,
                "job_template_id": 42,
                "external_session_id": None,
                "provider": "github",
                "repository_url": "org/repo",
                "entity_type": "change_request",
                "entity_number": "42",
                "outcome": "completed",
                "source_event_id": None,
                "afk_run_id": None,
                "trigger_type": None,
                "branch": None,
                "title": None,
                "failure_reason": None,
                "started_at": None,
                "finished_at": None,
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    binding = asyncio.run(repo.get_execution_binding_by_awx_job_id("501"))
    assert binding is not None
    assert binding.external_session_id is None


def test_get_execution_binding_rejects_non_numeric_job_id(mock_conn: AsyncMock) -> None:
    """A non-numeric AWX job id raises ValueError, never a bare int() failure."""
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    with pytest.raises(ValueError, match="Invalid AWX job id"):
        asyncio.run(repo.get_execution_binding_by_awx_job_id("abc"))


def test_get_execution_binding_returns_binding(mock_conn: AsyncMock) -> None:
    """Lookup by AWX job ID returns the reconstructed ExecutionBinding."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "awx_job_id": 500,
                "job_template_id": 42,
                "external_session_id": "ses_abc123",
                "provider": "github",
                "repository_url": "weiyentan/opencode-gateway",
                "entity_type": "change_request",
                "entity_number": "442",
                "outcome": "completed",
                "source_event_id": None,
                "afk_run_id": None,
                "trigger_type": None,
                "branch": "main",
                "title": "Fix caching bug",
                "failure_reason": None,
                "started_at": datetime(2026, 8, 21, 8, 0, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    binding = asyncio.run(repo.get_execution_binding_by_awx_job_id("500"))
    assert binding is not None
    assert binding.awx_job.job_id == "500"
    assert binding.awx_job.job_template_id == 42
    assert binding.resource.provider == Provider.GITHUB
    assert binding.resource.repository == "weiyentan/opencode-gateway"
    assert binding.resource.resource_type == EntityType.CHANGE_REQUEST
    assert binding.resource.resource_number == "442"
    assert binding.outcome == ExecutionOutcome.COMPLETED
    assert binding.title == "Fix caching bug"
    assert binding.branch == "main"


def test_get_execution_binding_queries_by_awx_job_id(mock_conn: AsyncMock) -> None:
    """The SELECT queries by awx_job_id."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(repo.get_execution_binding_by_awx_job_id("777"))

    mock_conn.fetchrow.assert_called_once()
    sql = mock_conn.fetchrow.call_args[0][0]
    assert "WHERE awx_job_id = $1" in sql
    args = mock_conn.fetchrow.call_args[0][1:]
    assert args[0] == 777


def test_get_execution_binding_select_includes_afk_run_id_and_trigger_type(
    mock_conn: AsyncMock,
) -> None:
    """The SELECT includes afk_run_id and trigger_type columns."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(repo.get_execution_binding_by_awx_job_id("100"))

    sql = mock_conn.fetchrow.call_args[0][0]
    assert "afk_run_id" in sql
    assert "trigger_type" in sql


def test_get_execution_binding_select_includes_failure_summary(
    mock_conn: AsyncMock,
) -> None:
    """The SELECT includes the failure_summary column (issue #564)."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(repo.get_execution_binding_by_awx_job_id("100"))

    sql = mock_conn.fetchrow.call_args[0][0]
    assert "failure_summary" in sql


def test_get_execution_binding_reads_failure_summary(mock_conn: AsyncMock) -> None:
    """The reconstructed binding carries the persisted failure_summary."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "awx_job_id": 501,
                "job_template_id": 42,
                "external_session_id": "ses_abc123",
                "provider": "github",
                "repository_url": "weiyentan/opencode-gateway",
                "entity_type": "change_request",
                "entity_number": "442",
                "outcome": "failed",
                "source_event_id": None,
                "afk_run_id": None,
                "trigger_type": None,
                "branch": "main",
                "title": "Fix caching bug",
                "failure_reason": "timeout",
                "failure_summary": "Process crashed",
                "started_at": None,
                "finished_at": None,
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    binding = asyncio.run(repo.get_execution_binding_by_awx_job_id("501"))
    assert binding is not None
    assert binding.failure_summary == "Process crashed"


def test_get_execution_binding_legacy_row_failure_summary_defaults_none(
    mock_conn: AsyncMock,
) -> None:
    """Legacy rows without the failure_summary column read back as None."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "awx_job_id": 502,
                "job_template_id": 42,
                "external_session_id": "ses_abc123",
                "provider": "github",
                "repository_url": "org/repo",
                "entity_type": "change_request",
                "entity_number": "42",
                "outcome": "completed",
                "source_event_id": None,
                "afk_run_id": None,
                "trigger_type": None,
                "branch": None,
                "title": None,
                "failure_reason": None,
                # No failure_summary key — legacy row predates the column.
                "started_at": None,
                "finished_at": None,
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    binding = asyncio.run(repo.get_execution_binding_by_awx_job_id("502"))
    assert binding is not None
    assert binding.failure_summary is None


def test_get_execution_binding_maps_afk_run_id_and_trigger_type(
    mock_conn: AsyncMock,
) -> None:
    """The converter maps afk_run_id and trigger_type from the row."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "awx_job_id": 800,
                "job_template_id": 42,
                "external_session_id": "ses_xyz",
                "provider": "github",
                "repository_url": "org/repo",
                "entity_type": "change_request",
                "entity_number": "42",
                "outcome": "completed",
                "source_event_id": None,
                "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWX",
                "trigger_type": "backfill",
                "branch": "main",
                "title": "Test",
                "failure_reason": None,
                "started_at": None,
                "finished_at": None,
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    binding = asyncio.run(repo.get_execution_binding_by_awx_job_id("800"))
    assert binding is not None
    assert binding.afk_run_id == "01JZABCDEFGHJKLMNPQRSTVWX"
    assert binding.trigger_type == "backfill"


def test_list_execution_bindings_select_includes_afk_run_id_and_trigger_type(
    mock_conn: AsyncMock,
) -> None:
    """The list SELECT includes afk_run_id and trigger_type columns."""
    mock_conn.fetch = AsyncMock(return_value=[])
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(
        repo.list_execution_bindings_for_resource(
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="42",
        )
    )

    sql = mock_conn.fetch.call_args[0][0]
    assert "afk_run_id" in sql
    assert "trigger_type" in sql
    assert "failure_summary" in sql


# ── List by provider resource ────────────────────────────────────────────────


def test_list_execution_bindings_for_resource_queries_correctly(
    mock_conn: AsyncMock,
) -> None:
    """The SELECT filters by provider resource identity and orders by created_at."""
    mock_conn.fetch = AsyncMock(return_value=[])
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    result = asyncio.run(
        repo.list_execution_bindings_for_resource(
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="42",
        )
    )
    assert result == []

    mock_conn.fetch.assert_called_once()
    sql = mock_conn.fetch.call_args[0][0]
    assert "WHERE provider = $1" in sql
    assert "AND repository_url = $2" in sql
    assert "AND entity_type = $3" in sql
    assert "AND entity_number = $4" in sql
    assert "ORDER BY created_at ASC" in sql
    args = mock_conn.fetch.call_args[0][1:]
    assert args == ("github", "org/repo", "change_request", "42")


def test_list_execution_bindings_returns_all_bindings(
    mock_conn: AsyncMock,
) -> None:
    """All bindings for a resource are returned in deterministic order."""
    mock_conn.fetch = AsyncMock(
        return_value=[
            mock_row(
                {
                    "id": uuid.uuid4(),
                    "awx_job_id": 100,
                    "job_template_id": 42,
                    "external_session_id": "ses_001",
                    "provider": "github",
                    "repository_url": "org/repo",
                    "entity_type": "change_request",
                    "entity_number": "42",
                    "outcome": "failed",
                    "source_event_id": None,
                    "afk_run_id": None,
                    "trigger_type": None,
                    "branch": "main",
                    "title": "First attempt",
                    "failure_reason": "timeout",
                    "started_at": datetime(2026, 8, 21, 8, 0, tzinfo=UTC),
                    "finished_at": datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
                }
            ),
            mock_row(
                {
                    "id": uuid.uuid4(),
                    "awx_job_id": 101,
                    "job_template_id": 42,
                    "external_session_id": "ses_002",
                    "provider": "github",
                    "repository_url": "org/repo",
                    "entity_type": "change_request",
                    "entity_number": "42",
                    "outcome": "completed",
                    "source_event_id": None,
                    "afk_run_id": "01JZABCDEFGHJKLMNPQRSTVWX",
                    "trigger_type": "eda",
                    "branch": "main",
                    "title": "Retry",
                    "failure_reason": None,
                    "started_at": datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
                    "finished_at": datetime(2026, 8, 21, 11, 0, tzinfo=UTC),
                }
            ),
        ]
    )

    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    bindings = asyncio.run(
        repo.list_execution_bindings_for_resource(
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="42",
        )
    )
    assert len(bindings) == 2
    assert bindings[0].awx_job.job_id == "100"
    assert bindings[0].outcome == ExecutionOutcome.FAILED
    assert bindings[1].awx_job.job_id == "101"
    assert bindings[1].outcome == ExecutionOutcome.COMPLETED


# ── Failed-then-successful retry ─────────────────────────────────────────────


def test_failed_then_successful_retry_both_persisted(mock_conn: AsyncMock) -> None:
    """A failed execution followed by a successful retry for the same resource
    are both persisted with deterministic ordering."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    failed_binding = _make_binding(
        awx_job_id="500",
        outcome=ExecutionOutcome.FAILED,
        title="First attempt",
    )
    success_binding = _make_binding(
        awx_job_id="501",
        outcome=ExecutionOutcome.COMPLETED,
        title="Retry",
    )

    import asyncio

    asyncio.run(repo.save_execution_binding(failed_binding))
    asyncio.run(repo.save_execution_binding(success_binding))

    # Verify both are inserted
    insert_calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(insert_calls) == 2

    # Now mock the fetch for the list query
    mock_conn.fetch = AsyncMock(
        return_value=[
            mock_row(
                {
                    "id": uuid.uuid4(),
                    "awx_job_id": 500,
                    "job_template_id": 42,
                    "external_session_id": "ses_001",
                    "provider": "github",
                    "repository_url": "weiyentan/opencode-gateway",
                    "entity_type": "change_request",
                    "entity_number": "442",
                    "outcome": "failed",
                    "source_event_id": None,
                    "afk_run_id": None,
                    "trigger_type": None,
                    "branch": None,
                    "title": "First attempt",
                    "failure_reason": "timeout",
                    "started_at": None,
                    "finished_at": None,
                }
            ),
            mock_row(
                {
                    "id": uuid.uuid4(),
                    "awx_job_id": 501,
                    "job_template_id": 42,
                    "external_session_id": "ses_002",
                    "provider": "github",
                    "repository_url": "weiyentan/opencode-gateway",
                    "entity_type": "change_request",
                    "entity_number": "442",
                    "outcome": "completed",
                    "source_event_id": None,
                    "afk_run_id": None,
                    "trigger_type": None,
                    "branch": None,
                    "title": "Retry",
                    "failure_reason": None,
                    "started_at": None,
                    "finished_at": None,
                }
            ),
        ]
    )

    bindings = asyncio.run(
        repo.list_execution_bindings_for_resource(
            provider=Provider.GITHUB,
            repository="weiyentan/opencode-gateway",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="442",
        )
    )
    assert len(bindings) == 2
    # Failed first, then successful — ordered by created_at ASC
    assert bindings[0].outcome == ExecutionOutcome.FAILED
    assert bindings[1].outcome == ExecutionOutcome.COMPLETED
    assert bindings[0].title == "First attempt"
    assert bindings[1].title == "Retry"


# ── SQL structure checks ─────────────────────────────────────────────────────


def test_save_execution_binding_no_update_on_conflict(mock_conn: AsyncMock) -> None:
    """The ON CONFLICT clause uses DO NOTHING, never DO UPDATE."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    binding = _make_binding(awx_job_id="600")

    import asyncio

    asyncio.run(repo.save_execution_binding(binding))

    calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    sql = calls[0][0]
    assert "DO UPDATE" not in sql
    assert "DO NOTHING" in sql


def test_list_execution_bindings_orders_by_created_at(mock_conn: AsyncMock) -> None:
    """The list query orders by created_at ASC for deterministic ordering."""
    mock_conn.fetch = AsyncMock(return_value=[])
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(
        repo.list_execution_bindings_for_resource(
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="1",
        )
    )

    sql = mock_conn.fetch.call_args[0][0]
    assert "ORDER BY created_at ASC" in sql
    assert "id ASC" in sql


# ══════════════════════════════════════════════════════════════════════════
#  create_or_replay_afk_execution_binding (issue #584)
# ══════════════════════════════════════════════════════════════════════════


_ULID_SOURCE = SequenceULID(1_700_000_000_000, start=1)


def _binding_payload(
    *,
    awx_job_id: str = "700",
    outcome: ExecutionOutcome = ExecutionOutcome.COMPLETED,
    title: str = "Test task",
    branch: str | None = "main",
    source_event_id: str | None = None,
    external_session_id: str | None = "ses_xyz",
    failure_summary: str | None = None,
) -> dict:
    """Build keyword arguments for create_or_replay_afk_execution_binding."""
    return {
        "awx_job_id": awx_job_id,
        "job_template_id": 42,
        "provider": Provider.GITHUB,
        "repository": "org/repo",
        "resource_number": "42",
        "external_session_id": external_session_id,
        "outcome": outcome,
        "source_event_id": source_event_id,
        "branch": branch,
        "title": title,
        "failure_reason": None,
        "failure_summary": failure_summary,
        "started_at": None,
        "finished_at": None,
        "trigger_type": None,
        "ulid_source": _ULID_SOURCE,
    }


# ── First creation ────────────────────────────────────────────────────────


def test_create_or_replay_first_call_inserts_afk_run_and_binding(
    mock_conn: AsyncMock,
) -> None:
    """First call creates an afk_runs row and an execution_bindings row with afk_run_id."""
    # SELECT returns None (no existing binding)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    # INSERT INTO execution_bindings returns the new id
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert isinstance(result, CreateAFKExecutionBindingResult)
    assert result.is_created is True
    assert result.is_conflict is False
    assert result.binding_id is not None
    assert result.afk_run_id is not None
    assert len(result.afk_run_id) == 26  # ULID length


def test_create_or_replay_first_call_inserts_pending_status(
    mock_conn: AsyncMock,
) -> None:
    """The afk_runs row is inserted with status='pending'."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    # Find the INSERT INTO afk_runs call
    afk_runs_calls = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
    assert len(afk_runs_calls) == 1
    sql = afk_runs_calls[0][0]
    assert "status" in sql
    assert "'pending'" in sql


def test_create_or_replay_first_call_links_afk_run_id(
    mock_conn: AsyncMock,
) -> None:
    """The execution_bindings INSERT includes afk_run_id from the generated ULID."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    # Verify the execution_bindings INSERT includes afk_run_id
    binding_calls = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
    assert len(binding_calls) == 1
    sql = binding_calls[0][0]
    assert "afk_run_id" in sql
    assert "trigger_type" in sql
    # The ULID should be among the parameters
    args = binding_calls[0][1]
    assert result.afk_run_id in args


def test_create_or_replay_first_call_sets_provider(
    mock_conn: AsyncMock,
) -> None:
    """The afk_runs row carries the provider from the payload."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    afk_runs_calls = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
    args = afk_runs_calls[0][1]
    # args: new_ulid, provider.value, title, started_at, finished_at
    assert args[1] == "github"


# ── Idempotent replay ────────────────────────────────────────────────────


def test_create_or_replay_idempotent_returns_existing_ids(
    mock_conn: AsyncMock,
) -> None:
    """Same awx_job_id + same payload returns existing afk_run_id and binding_id."""
    existing_run_id = "01HXYZ0000000000000000001"
    existing_binding_id = uuid.uuid4()

    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": existing_binding_id,
                "afk_run_id": existing_run_id,
                "awx_job_id": 700,
                "outcome": "completed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert result.afk_run_id == existing_run_id
    assert result.binding_id == existing_binding_id
    assert result.is_conflict is False
    assert result.is_created is False


def test_create_or_replay_idempotent_does_not_mutate(
    mock_conn: AsyncMock,
) -> None:
    """Idempotent replay issues no INSERT statements."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000002",
                "awx_job_id": 701,
                "outcome": "completed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(awx_job_id="701")

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    # No INSERT calls should have been made (only the SELECT)
    all_calls = mock_conn.fetch.call_args_list + mock_conn.fetchrow.call_args_list
    for call in all_calls:
        sql = call.args[0] if call.args else ""
        assert "INSERT" not in sql, f"Unexpected INSERT on replay: {sql}"


# ── Conflicting replay ───────────────────────────────────────────────────


def test_create_or_replay_conflict_returns_conflict_signal(
    mock_conn: AsyncMock,
) -> None:
    """Same awx_job_id + different payload returns is_conflict=True."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000003",
                "awx_job_id": 702,
                "outcome": "completed",
                "title": "Original title",
                "branch": "main",
                "failure_reason": None,
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    # Different title → conflict
    payload = _binding_payload(awx_job_id="702", title="Different title")

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert result.is_conflict is True
    assert result.is_created is False
    assert result.afk_run_id == "01HXYZ0000000000000000003"


def test_create_or_replay_conflict_does_not_mutate(
    mock_conn: AsyncMock,
) -> None:
    """Conflicting replay issues no INSERT statements."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000004",
                "awx_job_id": 703,
                "outcome": "completed",
                "title": "Original",
                "branch": "main",
                "failure_reason": None,
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(awx_job_id="703", title="Conflicting")

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    all_calls = mock_conn.fetch.call_args_list + mock_conn.fetchrow.call_args_list
    for call in all_calls:
        sql = call.args[0] if call.args else ""
        assert "INSERT" not in sql, f"Unexpected INSERT on conflict: {sql}"


def test_create_or_replay_conflict_detects_outcome_difference(
    mock_conn: AsyncMock,
) -> None:
    """Different outcome for same awx_job_id is also a conflict."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000005",
                "awx_job_id": 704,
                "outcome": "completed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(
        awx_job_id="704", outcome=ExecutionOutcome.FAILED
    )

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert result.is_conflict is True


def test_create_or_replay_conflict_detects_failure_summary_difference(
    mock_conn: AsyncMock,
) -> None:
    """A different failure_summary for the same awx_job_id is a conflict."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000006",
                "awx_job_id": 705,
                "outcome": "failed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "failure_summary": "Original summary",
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(
        awx_job_id="705",
        outcome=ExecutionOutcome.FAILED,
        failure_summary="Different summary",
    )

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert result.is_conflict is True


def test_create_or_replay_idempotent_with_same_failure_summary(
    mock_conn: AsyncMock,
) -> None:
    """Same awx_job_id + same failure_summary is an idempotent replay."""
    existing_binding_id = uuid.uuid4()
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": existing_binding_id,
                "afk_run_id": "01HXYZ0000000000000000007",
                "awx_job_id": 706,
                "outcome": "failed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "failure_summary": "Process crashed",
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(
        awx_job_id="706",
        outcome=ExecutionOutcome.FAILED,
        failure_summary="Process crashed",
    )

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    assert result.is_conflict is False
    assert result.binding_id == existing_binding_id


def test_create_or_replay_omitted_optional_values_are_idempotent(
    mock_conn: AsyncMock,
) -> None:
    """Omitted optional fields do not conflict with stored callback values."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000008",
                "awx_job_id": 707,
                "outcome": "failed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": "runner_failed",
                "failure_summary": "Process crashed",
                "source_event_id": None,
                "external_session_id": "ses_xyz",
                "started_at": datetime(2026, 8, 1, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 2, tzinfo=UTC),
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(awx_job_id="707", outcome=ExecutionOutcome.FAILED)
    payload["supplied_fields"] = {
        "awx_job",
        "outcome",
        "trigger_type",
        "provider",
        "repository",
        "resource_number",
    }

    import asyncio

    result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    assert result.is_conflict is False


def test_create_or_replay_explicit_null_optional_value_conflicts(
    mock_conn: AsyncMock,
) -> None:
    """An explicitly supplied null conflicts with a stored non-null value."""
    mock_conn.fetchrow = AsyncMock(
        return_value=mock_row(
            {
                "id": uuid.uuid4(),
                "afk_run_id": "01HXYZ0000000000000000009",
                "awx_job_id": 708,
                "outcome": "failed",
                "title": "Test task",
                "branch": "main",
                "failure_reason": None,
                "failure_summary": "Process crashed",
                "source_event_id": None,
                "external_session_id": "ses_xyz",
            }
        )
    )

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload(awx_job_id="708", outcome=ExecutionOutcome.FAILED)
    payload["supplied_fields"] = {
        "outcome",
        "title",
        "branch",
        "external_session_id",
        "failure_summary",
    }

    import asyncio

    result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    assert result.is_conflict is True


# ── Rollback / no orphaned rows ──────────────────────────────────────────


def test_create_or_replay_rollback_leaves_no_orphaned_afk_runs(
    mock_conn: AsyncMock,
) -> None:
    """If the execution_bindings INSERT fails, the afk_runs INSERT is rolled back."""
    # SELECT returns None (no existing binding)
    mock_conn.fetchrow = AsyncMock(return_value=None)

    async def _fetch_side_effect(sql, *args):
        if "INSERT INTO execution_bindings" in sql:
            raise asyncpg.UniqueViolationError(
                "duplicate key value violates unique constraint"
            )
        return []

    mock_conn.execute = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(side_effect=_fetch_side_effect)

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    with pytest.raises(asyncpg.UniqueViolationError):
        asyncio.run(
            repo.create_or_replay_afk_execution_binding(**payload)
        )

    # Verify the transaction context was entered (savepoint)
    mock_conn.transaction.assert_called()


# ── Pending status verification ──────────────────────────────────────────


def test_create_or_replay_pending_status_in_sql(
    mock_conn: AsyncMock,
) -> None:
    """The afk_runs INSERT explicitly sets status='pending', not any other value."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    afk_runs_calls = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
    sql = afk_runs_calls[0][0]
    # Must contain 'pending' as the status value
    assert "'pending'" in sql
    # Must NOT contain other RunStatus values in the INSERT
    for status in ("running", "completed", "blocked", "stale", "failed", "cancelled"):
        assert f"'{status}'" not in sql, f"afk_runs INSERT should not contain status '{status}'"


def test_create_or_replay_null_outcome_fields(
    mock_conn: AsyncMock,
) -> None:
    """The afk_runs row has NULL outcome_status and outcome on first creation."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()

    import asyncio

    asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

    afk_runs_calls = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
    sql = afk_runs_calls[0][0]
    assert "outcome_status" in sql
    assert "outcome" in sql
    # Verify NULL values are passed for outcome fields
    # outcome_status and outcome are hardcoded as NULL in the SQL
    assert "NULL" in sql


def test_create_or_replay_uses_ulid_source(
    mock_conn: AsyncMock,
) -> None:
    """The ULID source is called and its value used as afk_run_id."""
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])

    mock_ulid_source = MagicMock()
    mock_ulid_source.next_ulid.return_value = "01TESTULID00000000000000001"

    repo = AsyncpgOutcomeRepository(mock_conn)
    payload = _binding_payload()
    payload["ulid_source"] = mock_ulid_source

    import asyncio

    result = asyncio.run(
        repo.create_or_replay_afk_execution_binding(**payload)
    )

    mock_ulid_source.next_ulid.assert_called_once()
    assert result.afk_run_id == "01TESTULID00000000000000001"


# ══════════════════════════════════════════════════════════════════════════
#  update_execution_binding_terminal (issue #590)
# ══════════════════════════════════════════════════════════════════════════


def _terminal_row(**overrides) -> dict:
    """Build a mock execution_bindings row as seen by the FOR UPDATE select."""
    row = {
        "id": uuid.uuid4(),
        "outcome": "running",
        "finished_at": None,
        "failure_reason": None,
        "failure_summary": None,
        "external_session_id": None,
        "provider": None,
        "repository_url": None,
        "entity_type": None,
        "entity_number": None,
        "afk_run_id": None,
    }
    row.update(overrides)
    return row


def _run_update(repo: AsyncpgOutcomeRepository, **kwargs):
    import asyncio

    return asyncio.run(repo.update_execution_binding_terminal(**kwargs))


class TestUpdateExecutionBindingTerminal:
    def test_not_found_returns_not_found(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetchrow = AsyncMock(return_value=None)
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED
        )

        assert result.not_found is True
        assert result.is_updated is False
        assert result.is_conflict is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_uses_select_for_update(self, mock_conn: AsyncMock) -> None:
        """Concurrency is serialized by locking the row with FOR UPDATE."""
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_terminal_row()))
        repo = AsyncpgOutcomeRepository(mock_conn)

        _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.FAILED)

        sql = mock_conn.fetchrow.call_args[0][0]
        assert "FOR UPDATE" in sql

    def test_running_transition_updates_row(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_terminal_row()))
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            finished_at=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
            failure_reason="Timeout",
        )

        assert result.is_updated is True
        assert result.is_conflict is False
        updates = _calls_matching(mock_conn, r"UPDATE execution_bindings")
        assert len(updates) == 1
        args = updates[0][1]
        # outcome, finished_at, failure_reason carried in the UPDATE params
        assert args[1] == "failed"
        assert args[2] == datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
        assert args[3] == "Timeout"

    def test_running_fills_null_session_and_resource(self, mock_conn: AsyncMock) -> None:
        """Supplied fill-ins populate stored NULLs without erasing anything."""
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_terminal_row()))
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_found_later",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="7",
        )

        assert result.is_updated is True
        updates = _calls_matching(mock_conn, r"UPDATE execution_bindings")
        args = updates[0][1]
        assert args[4] == "ses_found_later"
        assert args[5] == "github"
        assert args[6] == "org/repo"
        assert args[7] == "change_request"
        assert args[8] == "7"

    def test_running_conflicting_session_is_conflict(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(_terminal_row(external_session_id="ses_stored"))
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_different",
        )

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_conflicting_resource_is_conflict(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    provider="github",
                    repository_url="org/repo",
                    entity_type="change_request",
                    entity_number="7",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="99",
        )

        assert result.is_conflict is True
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_completed_without_identity_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A completed transition with neither stored nor supplied identity conflicts."""
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_terminal_row()))
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED)

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_completed_with_stored_resource_but_no_session_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A stored resource alone cannot satisfy a completed transition."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    provider="github",
                    repository_url="org/repo",
                    entity_type="change_request",
                    entity_number="7",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED)

        assert result.is_conflict is True
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_completed_with_stored_session_but_no_resource_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A stored session alone cannot satisfy a completed transition."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(_terminal_row(external_session_id="ses_stored"))
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED)

        assert result.is_conflict is True
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_completed_with_stored_identity_transitions(
        self, mock_conn: AsyncMock
    ) -> None:
        """A completed transition succeeds when the stored row already has both."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    external_session_id="ses_stored",
                    provider="github",
                    repository_url="org/repo",
                    entity_type="change_request",
                    entity_number="7",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED)

        assert result.is_updated is True
        assert result.is_conflict is False
        assert len(_calls_matching(mock_conn, r"UPDATE execution_bindings")) == 1

    def test_terminal_identical_replay_is_idempotent(self, mock_conn: AsyncMock) -> None:
        finished = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(outcome="completed", finished_at=finished)
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            finished_at=finished,
        )

        assert result.is_updated is False
        assert result.is_conflict is False
        assert result.not_found is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_running_fills_null_failure_summary(self, mock_conn: AsyncMock) -> None:
        """A supplied failure_summary fills a stored NULL (issue #564)."""
        mock_conn.fetchrow = AsyncMock(return_value=mock_row(_terminal_row()))
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_summary="Process crashed",
        )

        assert result.is_updated is True
        assert result.is_conflict is False
        updates = _calls_matching(mock_conn, r"UPDATE execution_bindings")
        args = updates[0][1]
        # failure_summary carried in the UPDATE params (position 10).
        assert args[9] == "Process crashed"

    def test_running_omitted_failure_summary_keeps_stored(self, mock_conn: AsyncMock) -> None:
        """An omitted failure_summary never erases a stored value (non-erasing)."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(_terminal_row(failure_summary="Stored summary"))
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_reason="Timeout",
        )

        assert result.is_updated is True
        assert result.is_conflict is False
        updates = _calls_matching(mock_conn, r"UPDATE execution_bindings")
        args = updates[0][1]
        # The stored failure_summary is preserved, not erased by None.
        assert args[9] == "Stored summary"

    def test_running_conflicting_failure_summary_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A supplied failure_summary contradicting a stored value is a conflict."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(_terminal_row(failure_summary="Original summary"))
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_summary="Different summary",
        )

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_terminal_replay_with_matching_failure_summary_is_idempotent(
        self, mock_conn: AsyncMock
    ) -> None:
        """A terminal replay repeating the stored failure_summary is idempotent."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(outcome="failed", failure_summary="Process crashed")
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_summary="Process crashed",
        )

        assert result.is_updated is False
        assert result.is_conflict is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_terminal_replay_omitting_failure_metadata_is_idempotent(
        self, mock_conn: AsyncMock
    ) -> None:
        """Omitted optional failure metadata does not conflict with stored values."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    outcome="failed",
                    failure_reason="Timeout",
                    failure_summary="Process crashed",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
        )

        assert result.is_updated is False
        assert result.is_conflict is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_terminal_replay_with_different_failure_summary_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A terminal replay with a different failure_summary is a conflict."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(outcome="failed", failure_summary="Original summary")
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_summary="Different summary",
        )

        assert result.is_conflict is True
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_completed_transition_with_stored_failure_summary_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A completed transition never ends with failure metadata: a stored
        failure_summary (from phase one) is rejected after merge (issue #564)."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    failure_summary="Stored summary",
                    external_session_id="ses_stored",
                    provider="github",
                    repository_url="org/repo",
                    entity_type="change_request",
                    entity_number="7",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.COMPLETED)

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_terminal_conflicting_outcome_is_conflict(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(_terminal_row(outcome="completed"))
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo, awx_job_id="900", outcome=ExecutionOutcome.FAILED
        )

        assert result.is_conflict is True
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []

    def test_non_terminal_outcome_raises(self, mock_conn: AsyncMock) -> None:
        repo = AsyncpgOutcomeRepository(mock_conn)

        with pytest.raises(ValueError, match="terminal"):
            _run_update(repo, awx_job_id="900", outcome=ExecutionOutcome.RUNNING)


class TestCreateOrReplayNullableResource:
    def test_concurrent_binding_loser_cleans_up_auto_provisioned_run(
        self, mock_conn: AsyncMock
    ) -> None:
        """A losing concurrent binding insert must not leave an orphan run."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                None,  # no existing change-request owner
                mock_row({"id": uuid.uuid4(), "afk_run_id": "01WINNER00000000000000001"}),
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        payload = _binding_payload(awx_job_id="900")
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.afk_run_id == "01WINNER00000000000000001"
        deletes = _calls_matching(mock_conn, r"DELETE FROM afk_runs")
        assert len(deletes) == 1

    def test_running_provision_writes_null_resource_columns(
        self, mock_conn: AsyncMock
    ) -> None:
        """Issue #590: a resource-less running provision persists NULLs."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[None, mock_row({"afk_run_id": "01SUPPLIED00000000000000001"})]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload(outcome=ExecutionOutcome.RUNNING)
        payload["provider"] = None
        payload["repository"] = None
        payload["resource_number"] = None
        payload["external_session_id"] = None
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.run_missing is False
        inserts = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
        args = inserts[0][1]
        # provider / repository_url / entity_type / entity_number are NULL
        assert args[3] is None
        assert args[4] is None
        assert args[5] is None
        assert args[6] is None
        assert args[2] is None  # external_session_id

    def test_legacy_auto_provision_without_provider_raises(
        self, mock_conn: AsyncMock
    ) -> None:
        """Auto-provisioning a run requires a provider (API schema guarantees)."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["provider"] = None
        payload["repository"] = None
        payload["resource_number"] = None
        payload["afk_run_id"] = None
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        with pytest.raises(ValueError, match="provider"):
            asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))


class TestCreateOrReplayProviderCompatibility:
    """create_or_replay_afk_execution_binding resource vs afk_run provider
    (issue #600 review, Option A).

    ``afk_runs.provider`` records where the lifecycle originated
    (trigger/source provenance) and is intentionally independent of the
    canonical change-request provider carried by the binding tuple itself.
    A supplied ``afk_run_id`` therefore never gates the execution's resource
    provider against the run's stored provider.
    """

    def test_run_provider_is_source_provenance_not_a_gate(
        self, mock_conn: AsyncMock
    ) -> None:
        """A resource whose provider differs from the run's stored provider is
        accepted — the run's provider is trigger/source provenance, not the
        canonical change-request provider (issue #600 review)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SUPPLIED00000000000000001",
                        "provider": "gitlab",
                        "change_request_provider": None,
                        "change_request_repository": None,
                        "change_request_external_id": None,
                    }
                ),
                None,  # no other lifecycle owns this change request
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_conflict is False
        assert result.run_missing is False
        # The unbound lifecycle is bound to the execution's change request —
        # the canonical provider is the tuple's own (github), not the run's.
        # Issue #606 adds a second UPDATE afk_runs: the transactional
        # status convergence.
        binds = _calls_matching(mock_conn, r"UPDATE afk_runs")
        assert len(binds) == 2
        assert binds[0][1][1] == "github"
        assert binds[0][1][2] == "org/repo"
        assert binds[0][1][3] == "42"
        assert "SET status" in binds[1][0]

    def test_matching_run_provider_proceeds(self, mock_conn: AsyncMock) -> None:
        """A resource matching the run's provider attaches normally and binds the
        unbound lifecycle to the change request (issue #600 review)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {"afk_run_id": "01SUPPLIED00000000000000001", "provider": "github"}
                ),
                None,  # no other lifecycle owns this change request
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_conflict is False
        # The unbound lifecycle is made authoritative for the execution's PR.
        # Issue #606 adds a second UPDATE afk_runs: the transactional
        # status convergence.
        binds = _calls_matching(mock_conn, r"UPDATE afk_runs")
        assert len(binds) == 2
        assert binds[0][1][1] == "github"
        assert binds[0][1][2] == "org/repo"
        assert binds[0][1][3] == "42"
        assert "SET status" in binds[1][0]

    def test_mismatched_run_change_request_tuple_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A resource contradicting the run's bound change-request tuple is rejected."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SUPPLIED00000000000000001",
                        "provider": "github",
                        "change_request_provider": "github",
                        "change_request_repository": "org/other-repo",
                        "change_request_external_id": "99",
                    }
                ),
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_conflict is True
        assert result.is_created is False
        assert result.run_missing is False
        assert _calls_matching(mock_conn, r"INSERT INTO execution_bindings") == []
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []

    def test_matching_run_change_request_tuple_proceeds(
        self, mock_conn: AsyncMock
    ) -> None:
        """A resource matching the run's bound change-request tuple attaches normally."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SUPPLIED00000000000000001",
                        "provider": "github",
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "42",
                    }
                ),
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_conflict is False

    def test_run_without_stored_provider_is_still_bound(
        self, mock_conn: AsyncMock
    ) -> None:
        """A run without a stored provider carries no provenance constraint, but
        the unbound lifecycle is still bound to the execution's change request
        (issue #600 review)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row({"afk_run_id": "01SUPPLIED00000000000000001"}),
                None,  # no other lifecycle owns this change request
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_conflict is False
        # Issue #606: the change-request bind plus the transactional status
        # convergence are the only afk_runs UPDATEs.
        assert len(_calls_matching(mock_conn, r"UPDATE afk_runs")) == 2

    def test_change_request_owned_by_another_lifecycle_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """A resource already owned by another lifecycle is a conflict (1:1
        invariant) — the execution cannot introduce it on this run (issue #600
        review)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {"afk_run_id": "01SUPPLIED00000000000000001", "provider": "github"}
                ),
                mock_row({"afk_run_id": "01SOMEONELSE0000000000000001"}),  # owned
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_conflict is True
        assert result.is_created is False
        assert _calls_matching(mock_conn, r"INSERT INTO execution_bindings") == []
        assert _calls_matching(mock_conn, r"UPDATE afk_runs") == []


class TestCreateOrReplayAutoProvisionedChangeRequest:
    """Auto-provisioned lifecycle change-request binding (issue #600 review,
    finding #5; PR #600 blocker).

    When no ``afk_run_id`` is supplied and the execution carries a complete
    change-request identity, the freshly-created ``afk_runs`` row persists
    the change-request columns in the same transaction.  When the canonical
    change request already owns a lifecycle, the pre-check *reuses* that
    owner (``is_reused=True``, validated through
    ``_apply_change_request_binding``) instead of returning a conflict, and
    a savepoint-wrapped INSERT turns a concurrent first-discovery race into
    winner adoption — never a second lifecycle, never a 500.
    """

    def test_auto_created_run_persists_change_request_columns(
        self, mock_conn: AsyncMock
    ) -> None:
        """The auto-provisioned afk_runs INSERT carries the change-request
        identity columns."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        inserts = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
        assert len(inserts) == 1
        sql, args = inserts[0]
        assert "change_request_provider" in sql
        assert "change_request_repository" in sql
        assert "change_request_external_id" in sql
        # args: run_id, provider, title, started_at, finished_at,
        #       cr_provider, cr_repository, cr_external_id
        assert args[0] == result.afk_run_id
        assert args[1] == "github"
        assert args[5] == "github"
        assert args[6] == "org/repo"
        assert args[7] == "42"

    def test_auto_provision_precheck_hit_reuses_existing_lifecycle(
        self, mock_conn: AsyncMock
    ) -> None:
        """A change request already owned by a lifecycle is *reused* — the
        binding attaches to the owner's afk_run_id (is_reused=True) and no
        second afk_runs row is inserted (PR #600 blocker)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SOMEONELSE0000000000000001",
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "42",
                    }
                ),  # pre-check: the canonical CR owns this lifecycle
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_reused is True
        assert result.is_created is False
        assert result.is_conflict is False
        assert result.afk_run_id == "01SOMEONELSE0000000000000001"
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []
        inserts = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
        assert len(inserts) == 1
        assert "01SOMEONELSE0000000000000001" in inserts[0][1]

    def test_auto_provision_unique_violation_loser_adopts_winner(
        self, mock_conn: AsyncMock
    ) -> None:
        """A concurrent first discovery that slips past the pre-check is not a
        conflict — the UniqueViolationError loser re-reads the winner
        lifecycle and attaches its execution to it (PR #600 blocker)."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                None,  # pre-check: no owner yet
                mock_row(
                    {
                        "afk_run_id": "01WINNER0000000000000000001",
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "42",
                    }
                ),  # loser re-read: the winner's lifecycle
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock(
            side_effect=[
                asyncpg.UniqueViolationError("duplicate key"),  # afk_runs INSERT
                "UPDATE 1",  # _apply_change_request_binding UPDATE
                "INSERT 0 1",  # afk_run_sessions upsert (issue #618)
                "UPDATE 1",  # issue #606 status convergence UPDATE
            ]
        )

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_reused is True
        assert result.is_created is False
        assert result.is_conflict is False
        assert result.afk_run_id == "01WINNER0000000000000000001"
        inserts = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
        assert len(inserts) == 1
        assert "01WINNER0000000000000000001" in inserts[0][1]
        # The savepoint pattern is preserved — the afk_runs INSERT ran inside
        # a savepoint transaction (rolled back on the violation).
        mock_conn.transaction.assert_called()

    def test_auto_provision_first_discovery_is_created_not_reused(
        self, mock_conn: AsyncMock
    ) -> None:
        """A first discovery with no pre-check hit and no violation is a
        normal creation — is_created=True, is_reused=False."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_reused is False
        assert result.is_conflict is False

    def test_auto_created_run_without_resource_writes_no_change_request(
        self, mock_conn: AsyncMock
    ) -> None:
        """A resource-less auto-provision keeps the legacy INSERT without
        change-request columns and without the pre-check."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["repository"] = None
        payload["resource_number"] = None

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        inserts = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
        assert len(inserts) == 1
        sql, _args = inserts[0]
        assert "change_request_provider" not in sql


class TestCreateOrReplaySameLifecyclePrecheckReplay:
    """Same-lifecycle ownership found by the 1:1 pre-check (issue #600
    review, finding #6).

    When the pre-check finds that the *requested lifecycle itself* owns the
    change request (a concurrent identical bind committed between our read
    of the run and the pre-check), the complete tuple is re-read and
    verified — an identical tuple is an idempotent replay, anything else is
    a genuine conflict.
    """

    def test_same_lifecycle_same_change_request_in_precheck_proceeds(
        self, mock_conn: AsyncMock
    ) -> None:
        """The pre-check finding the requested lifecycle as the owner of the
        identical change request is an idempotent replay — the binding
        proceeds instead of returning a false 409."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SUPPLIED00000000000000001",
                        "provider": "github",
                        "change_request_provider": None,
                        "change_request_repository": None,
                        "change_request_external_id": None,
                    }
                ),
                # Pre-check: the requested lifecycle itself owns the CR.
                mock_row({"afk_run_id": "01SUPPLIED00000000000000001"}),
                # Re-read: the complete tuple matches.
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "42",
                    }
                ),
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_created is True
        assert result.is_conflict is False
        # The idempotent replay never re-binds; the only afk_runs UPDATE is
        # the issue #606 transactional status convergence.
        binds = _calls_matching(mock_conn, r"UPDATE afk_runs")
        assert len(binds) == 1
        assert "SET status" in binds[0][0]

    def test_same_lifecycle_different_change_request_in_precheck_is_conflict(
        self, mock_conn: AsyncMock
    ) -> None:
        """The pre-check finding the requested lifecycle as owner, but the
        re-read tuple differing, is a genuine conflict — the lifecycle was
        rebound to a different identity."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row(
                    {
                        "afk_run_id": "01SUPPLIED00000000000000001",
                        "provider": "github",
                        "change_request_provider": None,
                        "change_request_repository": None,
                        "change_request_external_id": None,
                    }
                ),
                mock_row({"afk_run_id": "01SUPPLIED00000000000000001"}),
                # Re-read: a DIFFERENT tuple — the lifecycle was rebound.
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "org/other-repo",
                        "change_request_external_id": "99",
                    }
                ),
            ]
        )
        mock_conn.fetch = AsyncMock()
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert result.is_conflict is True
        assert result.is_created is False
        assert _calls_matching(mock_conn, r"INSERT INTO execution_bindings") == []
        assert _calls_matching(mock_conn, r"UPDATE afk_runs") == []


class TestUpdateExecutionBindingTerminalLifecycleAuthority:
    """update_execution_binding_terminal lifecycle change-request authority
    (issue #600 review)."""

    def test_patch_binds_unbound_lifecycle(self, mock_conn: AsyncMock) -> None:
        """A resource filled by the terminal update binds an unbound owning
        lifecycle to it."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_terminal_row(afk_run_id="01SUPPLIED00000000000000001")),
                mock_row(
                    {
                        "change_request_provider": None,
                        "change_request_repository": None,
                        "change_request_external_id": None,
                    }
                ),
                None,  # no other lifecycle owns this change request
            ]
        )
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_terminal",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="7",
        )

        assert result.is_updated is True
        assert result.is_conflict is False
        # The unbound lifecycle is bound to the execution's change request.
        # Issue #606 adds a second UPDATE afk_runs: the transactional
        # status convergence.
        binds = _calls_matching(mock_conn, r"UPDATE afk_runs")
        assert len(binds) == 2
        assert binds[0][1][1] == "github"
        assert binds[0][1][2] == "org/repo"
        assert binds[0][1][3] == "7"
        assert "SET status" in binds[1][0]
        assert len(_calls_matching(mock_conn, r"UPDATE execution_bindings")) == 1

    def test_patch_resource_conflicts_with_lifecycle(self, mock_conn: AsyncMock) -> None:
        """Issue #600 review scenario: the owning lifecycle is bound to PR #5 but
        the terminal update fills PR #99 — a conflict that never mutates the
        execution row."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_terminal_row(afk_run_id="01SUPPLIED00000000000000001")),
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "5",
                    }
                ),
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_terminal",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="99",
        )

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []
        assert _calls_matching(mock_conn, r"UPDATE afk_runs") == []

    def test_patch_resource_matches_lifecycle_proceeds(self, mock_conn: AsyncMock) -> None:
        """A filled resource matching the owning lifecycle's bound change request
        proceeds."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_terminal_row(afk_run_id="01SUPPLIED00000000000000001")),
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "7",
                    }
                ),
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_terminal",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="7",
        )

        assert result.is_updated is True
        assert result.is_conflict is False
        assert len(_calls_matching(mock_conn, r"UPDATE execution_bindings")) == 1

    def test_patch_orphaned_afk_run_id_is_conflict(self, mock_conn: AsyncMock) -> None:
        """A terminal update whose afk_run_id resolves to no lifecycle conflicts —
        authority cannot be established."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_terminal_row(afk_run_id="01ORPHANED0000000000000000")),
                None,  # no such run
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_terminal",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="7",
        )

        assert result.is_conflict is True
        assert result.is_updated is False
        assert _calls_matching(mock_conn, r"UPDATE execution_bindings") == []


# ══════════════════════════════════════════════════════════════════════════
#  afk_run_sessions persistence from execution bindings (issue #618)
# ══════════════════════════════════════════════════════════════════════════


def _session_link_calls(mock_conn: AsyncMock) -> list[tuple]:
    """Return (sql, params) for every call that inserts into afk_run_sessions."""
    return _calls_matching(mock_conn, r"INSERT INTO afk_run_sessions")


def _session_resolution_calls(mock_conn: AsyncMock) -> list[tuple]:
    """Return (sql, params) for every call that resolves a sessions.id."""
    return _calls_matching(mock_conn, r"SELECT id FROM sessions")


class TestCreateOrReplaySessionLink:
    """create_or_replay_afk_execution_binding persists afk_run_sessions (issue #618)."""

    def test_creation_upserts_session_link_when_session_supplied(
        self, mock_conn: AsyncMock
    ) -> None:
        """A binding created with both afk_run_id and external_session_id writes
        an afk_run_sessions row in the same transaction."""
        internal_session_id = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [mock_row({"id": uuid.uuid4()})],  # execution_bindings INSERT
                [mock_row({"id": internal_session_id})],  # session resolution
                [],  # _project_afk_run_status outcome read
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload(external_session_id="ses_618")

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))
        assert result.is_created is True

        # The internal session id was resolved from the sessions table.
        resolution = _session_resolution_calls(mock_conn)
        assert len(resolution) == 1
        assert "WHERE external_session_id = $1" in resolution[0][0]
        assert resolution[0][1][0] == "ses_618"

        # The enrich-only upsert carries the run id + external + internal id.
        links = _session_link_calls(mock_conn)
        assert len(links) == 1
        sql, args = links[0]
        assert "ON CONFLICT (afk_run_id, external_session_id) DO UPDATE" in sql
        assert args[0] == result.afk_run_id
        assert args[1] == str(internal_session_id)
        assert args[2] == "ses_618"

    def test_creation_keeps_external_session_id_when_unresolved(
        self, mock_conn: AsyncMock
    ) -> None:
        """No matching Gateway session -> session_id stays None, external id kept."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [mock_row({"id": uuid.uuid4()})],  # execution_bindings INSERT
                [],  # session resolution -- no match
                [],  # _project_afk_run_status outcome read
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload(external_session_id="ses_unresolved_618")

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))
        assert result.is_created is True

        links = _session_link_calls(mock_conn)
        assert len(links) == 1
        sql, args = links[0]
        assert args[0] == result.afk_run_id
        assert args[1] is None  # unresolved internal session id
        assert args[2] == "ses_unresolved_618"

    def test_creation_skips_session_link_when_no_session_supplied(
        self, mock_conn: AsyncMock
    ) -> None:
        """Running provisioning without a session creates no afk_run_sessions row."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row({"afk_run_id": "01SUPPLIED00000000000000001"}),  # run exists
            ]
        )
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [],  # _project_afk_run_status completed-rejection read
                [mock_row({"id": uuid.uuid4()})],  # execution_bindings INSERT
                [],  # _project_afk_run_status outcome read (converge)
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload(
            outcome=ExecutionOutcome.RUNNING,
            external_session_id=None,
        )
        payload["provider"] = None
        payload["repository"] = None
        payload["resource_number"] = None
        payload["afk_run_id"] = "01SUPPLIED00000000000000001"
        payload["ulid_source"] = _ULID_SOURCE

        import asyncio

        result = asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))
        assert result.is_created is True
        assert _session_link_calls(mock_conn) == []
        assert _session_resolution_calls(mock_conn) == []

    def test_idempotent_replay_never_writes_session_link(
        self, mock_conn: AsyncMock
    ) -> None:
        """An identical replay returns early and issues no afk_run_sessions write."""
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                {
                    "id": uuid.uuid4(),
                    "afk_run_id": "01HXYZ0000000000000000001",
                    "awx_job_id": 700,
                    "outcome": "completed",
                    "title": "Test task",
                    "branch": "main",
                    "failure_reason": None,
                    "failure_summary": None,
                    "source_event_id": None,
                    "external_session_id": "ses_xyz",
                }
            )
        )
        repo = AsyncpgOutcomeRepository(mock_conn)
        payload = _binding_payload()

        import asyncio

        asyncio.run(repo.create_or_replay_afk_execution_binding(**payload))

        assert _session_link_calls(mock_conn) == []
        assert _session_resolution_calls(mock_conn) == []


class TestUpdateTerminalSessionLink:
    """update_execution_binding_terminal persists afk_run_sessions (issue #618)."""

    def test_terminal_fill_in_upserts_session_link(self, mock_conn: AsyncMock) -> None:
        """A terminal update filling a previously-missing session writes the
        afk_run_sessions row in the same transaction (enrich-only)."""
        internal_session_id = uuid.uuid4()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(
                    _terminal_row(
                        afk_run_id="01SUPPLIED00000000000000001",
                        provider="github",
                        repository_url="org/repo",
                        entity_type="change_request",
                        entity_number="7",
                    )
                ),
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "org/repo",
                        "change_request_external_id": "7",
                    }
                ),
            ]
        )
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [mock_row({"id": internal_session_id})],  # session resolution
                [],  # _project_afk_run_status outcome read
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_id="ses_terminal_618",
            provider=Provider.GITHUB,
            repository="org/repo",
            resource_number="7",
        )

        assert result.is_updated is True
        resolution = _session_resolution_calls(mock_conn)
        assert len(resolution) == 1
        assert resolution[0][1][0] == "ses_terminal_618"

        links = _session_link_calls(mock_conn)
        assert len(links) == 1
        sql, args = links[0]
        assert "ON CONFLICT (afk_run_id, external_session_id) DO UPDATE" in sql
        assert args[0] == "01SUPPLIED00000000000000001"
        assert args[1] == str(internal_session_id)
        assert args[2] == "ses_terminal_618"

    def test_terminal_update_without_session_writes_no_link(
        self, mock_conn: AsyncMock
    ) -> None:
        """A failed/cancelled transition without a session never writes a link."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(_terminal_row(afk_run_id="01SUPPLIED00000000000000001")),
            ]
        )
        mock_conn.fetch = AsyncMock(
            side_effect=[
                [],  # _project_afk_run_status outcome read
            ]
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.FAILED,
            failure_reason="Timeout",
        )

        assert result.is_updated is True
        assert _session_link_calls(mock_conn) == []
        assert _session_resolution_calls(mock_conn) == []

    def test_identical_terminal_replay_writes_no_link(
        self, mock_conn: AsyncMock
    ) -> None:
        """An idempotent terminal replay never re-writes the session link."""
        finished = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
        mock_conn.fetchrow = AsyncMock(
            return_value=mock_row(
                _terminal_row(
                    outcome="completed",
                    finished_at=finished,
                    external_session_id="ses_stored",
                    provider="github",
                    repository_url="org/repo",
                    entity_type="change_request",
                    entity_number="7",
                    afk_run_id="01SUPPLIED00000000000000001",
                )
            )
        )
        mock_conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(mock_conn)

        result = _run_update(
            repo,
            awx_job_id="900",
            outcome=ExecutionOutcome.COMPLETED,
            finished_at=finished,
        )

        assert result.is_updated is False
        assert result.is_conflict is False
        assert _session_link_calls(mock_conn) == []
        assert _session_resolution_calls(mock_conn) == []
