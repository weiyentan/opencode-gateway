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
from unittest.mock import AsyncMock

import pytest

from afk_outcomes import AsyncpgOutcomeRepository
from afk_outcomes.models import (
    EntityType,
    ExecutionBinding,
    ExecutionOutcome,
    Provider,
)
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
) -> ExecutionBinding:
    """Build a minimal ExecutionBinding for testing."""
    return ExecutionBinding(
        binding_id="",
        awx_job={"job_id": awx_job_id, "job_template_id": 42},
        external_session_id=external_session_id,
        resource=RESOURCE,
        outcome=outcome,
        title=title,
    )


def _calls_matching(conn: AsyncMock, pattern: str) -> list[tuple]:
    """Return (sql, params) for every fetch call whose SQL matches ``pattern``."""
    return [
        (call.args[0], call.args[1:])
        for call in conn.fetch.call_args_list
        if re.search(pattern, call.args[0])
    ]


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
