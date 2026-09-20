"""T1 read-path SQL-shape tests (issue #708).

These tests pin the *structure* and *parameter ordering* of the SQL emitted
by every optimized Gateway read path, independent of database row contents:

- Agent Runs list (count + data)
- Grouped aggregates (total, raw grouped, client,project rollup hybrid)
- Records (count + data, every sort mode)
- Records-with-context (count + data)
- AFK run summaries (count + data)

Rationale
---------
Read-path optimizations replace one query shape with another (for example a
correlated subquery with a CTE, or a raw scan with a rollup join).  A
functional parity test can still pass while the candidate query silently
falls back to the old shape — for example if a filter makes the optimized
path ineligible.  Pinning the emitted SQL keeps that fallback visible and
fails loudly.

These tests deliberately assert *shape* (the presence and relative order of
clauses, CTE names, joins, ordering and parameter slots) rather than exact
whitespace, so reformatting the query does not break them.  They never
assert that a sequential scan is absent: sequential scans are a legitimate
plan choice and plan selection is exercised by the live protocol in
``tests/integration/test_read_path_live_protocol.py``.

Protocol: see ``docs/adr/0017-migration-0019-index-measurement.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock

_CLIENT_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()
_START = datetime(2025, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
_END = datetime(2025, 7, 31, 23, 59, 59, tzinfo=timezone.utc)

_RANGE = {
    "start_date": _START.isoformat(),
    "end_date": _END.isoformat(),
}


# ══════════════════════════════════════════════════════════════════════════
#  SQL capture helpers
# ══════════════════════════════════════════════════════════════════════════


def _fetch_calls(mock_conn: AsyncMock) -> list[tuple[str, tuple]]:
    """Return ``(sql, params)`` for every ``conn.fetch`` call, in order."""
    return [(c.args[0], c.args[1:]) for c in mock_conn.fetch.call_args_list]


def _fetchrow_calls(mock_conn: AsyncMock) -> list[tuple[str, tuple]]:
    return [(c.args[0], c.args[1:]) for c in mock_conn.fetchrow.call_args_list]


def _fetchval_calls(mock_conn: AsyncMock) -> list[tuple[str, tuple]]:
    return [(c.args[0], c.args[1:]) for c in mock_conn.fetchval.call_args_list]


def _norm(sql: str) -> str:
    """Collapse whitespace so multi-line SQL compares on clause sequence."""
    return " ".join(sql.split())


# ══════════════════════════════════════════════════════════════════════════
#  Aggregates
# ══════════════════════════════════════════════════════════════════════════


class TestAggregatesSqlShape:
    @pytest.mark.asyncio
    async def test_total_sql_shape_and_param_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Total aggregates: one fetchrow, provider breakdown rides the same
        statement, parameters ordered dates → client → model → session."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    **_RANGE,
                    "client_id": str(_CLIENT_ID),
                    "model": "gpt-4",
                    "session_id": str(_SESSION_ID),
                },
            )

        assert resp.status_code == 200
        assert mock_conn.fetch.call_count == 0
        calls = _fetchrow_calls(mock_conn)
        assert len(calls) == 1
        sql, params = calls[0]
        flat = _norm(sql)

        # Structure: one statement carrying the provider breakdown subquery.
        assert "jsonb_object_agg" in flat
        assert "COALESCE(NULLIF(p2.provider, ''), 'unknown') AS provider_key" in flat
        assert "FROM usage_events our" in flat
        assert "JOIN observed_models om ON om.id = our.model_id" in flat
        # The provider subquery reuses the $1..$5 filter placeholders.
        assert "p2.reported_at >= $1" in flat
        assert "p2.reported_at <= $2" in flat
        assert "p2.client_id = $3" in flat
        assert "om2.model_name = $4" in flat
        assert "p2.session_id = $5" in flat

        # Parameter ordering is authoritative.
        assert params[0] == _START
        assert params[1] == _END
        assert params[2] == _CLIENT_ID
        assert params[3] == "gpt-4"
        assert params[4] == _SESSION_ID

    @pytest.mark.asyncio
    async def test_grouped_sql_shape_and_param_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Raw grouped aggregates: one fetch with provider-breakdown CTEs and
        ORDER BY group_value."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    **_RANGE,
                    "client_id": str(_CLIENT_ID),
                    "model": "gpt-4",
                    "group_by": "model",
                },
            )

        assert resp.status_code == 200
        calls = _fetch_calls(mock_conn)
        assert len(calls) == 1
        sql, params = calls[0]
        flat = _norm(sql)

        assert "WITH inner_agg AS" in flat
        assert "GROUPING SETS" in flat
        assert "FILTER (WHERE is_total)" in flat
        assert "FILTER (WHERE NOT is_total)" in flat
        assert flat.rstrip().endswith("ORDER BY group_value")
        # Provider counts alias the raw provider to a stable key.
        assert "COALESCE(NULLIF(our.provider, ''), 'unknown') AS provider_key" in flat

        assert params[0] == _START
        assert params[1] == _END
        assert params[2] == _CLIENT_ID
        assert params[3] == "gpt-4"

    @pytest.mark.asyncio
    async def test_project_dimension_joins_sessions_and_projects(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A project grouping adds the sessions + source-projects joins while
        keeping a single query budget (provider CTE rides the same statement)."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "project"},
            )

        assert resp.status_code == 200
        calls = _fetch_calls(mock_conn)
        assert len(calls) == 1
        flat = _norm(calls[0][0])
        assert "LEFT JOIN sessions s ON s.id = our.session_id" in flat
        assert "LEFT JOIN opencode_source_projects osp" in flat

    @pytest.mark.asyncio
    async def test_client_project_rollup_hybrid_is_two_queries_in_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """client,project: rollup additive totals first, then the raw
        distinct-count query.  Both must share the same label keying."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "client,project"},
            )

        assert resp.status_code == 200
        calls = _fetch_calls(mock_conn)
        assert len(calls) == 2, "rollup path is a hybrid read of exactly 2 queries"

        rollup_sql, rollup_params = calls[0]
        rollup_flat = _norm(rollup_sql)
        assert "FROM client_project_rollup r" in rollup_flat
        assert "FROM usage_events" not in rollup_flat
        assert "COALESCE(oc.canonical_name, oc.name) || '|' || rl.project_label" in rollup_flat
        assert "r.day >= ($1 AT TIME ZONE 'UTC')::date" in rollup_flat
        assert "r.day <= ($2 AT TIME ZONE 'UTC')::date" in rollup_flat
        assert rollup_params[0] == _START
        assert rollup_params[1] == _END

        count_sql, count_params = calls[1]
        count_flat = _norm(count_sql)
        assert "FROM usage_events r" in count_flat
        assert "WITH usage_with_label AS" in count_flat
        assert "provider_breakdown AS" in count_flat
        assert "COALESCE(oc.canonical_name, oc.name) || '|' || ul.project_label" in count_flat
        assert "(r.reported_at AT TIME ZONE 'UTC')::date >= ($1 AT TIME ZONE 'UTC')::date" in count_flat
        assert "r.project_id IS NOT NULL" in count_flat
        assert count_params[0] == _START
        assert count_params[1] == _END

    @pytest.mark.asyncio
    async def test_client_project_with_model_filter_falls_back_to_raw_scan(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The rollup cannot express a model filter, so the path must fall
        back to the raw usage_events scan (single query)."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    **_RANGE,
                    "group_by": "client,project",
                    "model": "gpt-4",
                },
            )

        assert resp.status_code == 200
        calls = _fetch_calls(mock_conn)
        assert len(calls) == 1
        flat = _norm(calls[0][0])
        assert "FROM usage_events our" in flat
        assert "FROM client_project_rollup" not in flat
        assert "WITH inner_agg AS" in flat


# ══════════════════════════════════════════════════════════════════════════
#  Records
# ══════════════════════════════════════════════════════════════════════════


class TestRecordsSqlShape:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sort_by", "sort_dir", "expected_order"),
        [
            ("source_created_at", "desc", "ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) desc"),
            ("source_created_at", "asc", "ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) asc"),
            ("reported_at", "desc", "ORDER BY our.reported_at desc"),
            ("reported_at", "asc", "ORDER BY our.reported_at asc"),
            ("ingested_at", "desc", "ORDER BY our.first_ingested_at desc"),
            ("ingested_at", "asc", "ORDER BY our.first_ingested_at asc"),
        ],
    )
    async def test_records_sort_modes(
        self,
        client: AsyncClient,
        mock_conn: AsyncMock,
        sort_by: str,
        sort_dir: str,
        expected_order: str,
    ):
        """Count then data, with the order column + direction resolved from
        the requested sort mode."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={
                    **_RANGE,
                    "limit": "10",
                    "offset": "0",
                    "sort_by": sort_by,
                    "sort_dir": sort_dir,
                },
            )

        assert resp.status_code == 200
        assert mock_conn.fetchval.call_count == 1
        count_sql = _norm(_fetchval_calls(mock_conn)[0][0])
        assert count_sql.startswith("SELECT COUNT(*) FROM usage_events our")
        assert "JOIN observed_models om" in count_sql

        calls = _fetch_calls(mock_conn)
        assert len(calls) == 1
        data_sql, params = calls[0]
        flat = _norm(data_sql)
        assert expected_order in flat
        # Limit/offset occupy the slots after the date params.
        assert "LIMIT $3" in flat
        assert "OFFSET $4" in flat
        assert params[0] == _START
        assert params[1] == _END
        assert params[2] == 10
        assert params[3] == 0

    @pytest.mark.asyncio
    async def test_records_filters_parameter_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Filter placeholders are ordered client → model → session after the
        fixed date placeholders, and limit/offset follow them."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={
                    **_RANGE,
                    "client_id": str(_CLIENT_ID),
                    "model": "gpt-4",
                    "session_id": str(_SESSION_ID),
                    "limit": "25",
                    "offset": "50",
                },
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        flat = _norm(count_sql)
        assert "our.reported_at >= $1" in flat
        assert "our.reported_at <= $2" in flat
        assert "our.client_id = $3" in flat
        assert "om.model_name = $4" in flat
        assert "our.session_id = $5" in flat
        assert count_params == (_START, _END, _CLIENT_ID, "gpt-4", _SESSION_ID)

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat_data = _norm(data_sql)
        assert "LIMIT $6" in flat_data
        assert "OFFSET $7" in flat_data
        assert data_params == (
            _START, _END, _CLIENT_ID, "gpt-4", _SESSION_ID, 25, 50
        )


# ══════════════════════════════════════════════════════════════════════════
#  Records with context
# ══════════════════════════════════════════════════════════════════════════


class TestRecordsWithContextSqlShape:
    @pytest.mark.asyncio
    async def test_sql_shape_and_parameter_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Count + data with the context join, canonical order column, and
        filter parameter ordering project → session → agent → model."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    **_RANGE,
                    "project_id": "proj-1",
                    "session_id": str(_SESSION_ID),
                    "agent": "code-editor",
                    "model": "gpt-4",
                    "limit": "10",
                    "offset": "20",
                },
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        flat = _norm(count_sql)
        assert flat.startswith("SELECT COUNT(*) FROM usage_events our")
        assert "our.reported_at >= $1" in flat
        assert "our.reported_at <= $2" in flat
        assert "s.project_id = $3" in flat
        assert "our.session_id = $4" in flat
        assert "s.agent = $5" in flat
        assert "om.model_name = $6" in flat
        assert count_params == (
            _START, _END, "proj-1", _SESSION_ID, "code-editor", "gpt-4"
        )

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat_data = _norm(data_sql)
        assert "ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) DESC" in flat_data
        assert "osc.title AS session_title" in flat_data
        assert "LIMIT $7" in flat_data
        assert "OFFSET $8" in flat_data
        assert data_params == (
            _START, _END, "proj-1", _SESSION_ID, "code-editor", "gpt-4", 10, 20
        )


# ══════════════════════════════════════════════════════════════════════════
#  Agent Runs
# ══════════════════════════════════════════════════════════════════════════


class TestAgentRunsSqlShape:
    @pytest.mark.asyncio
    async def test_list_sql_shape_and_param_slots(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Count + data; data carries the CTE enrichment chain, source-created
        ordering (nulls last), and limit/offset in the final slots."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"limit": "10", "offset": "0"},
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        assert _norm(count_sql) == "SELECT COUNT(*) FROM sessions s WHERE TRUE"
        assert count_params == ()

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat = _norm(data_sql)
        for cte in ("WITH page_ids AS", "page AS", "usage_agg AS", "provider_agg AS",
                    "child_counts AS", "todo_counts AS"):
            assert cte in flat, f"missing CTE {cte}"
        assert "LEFT JOIN child_counts cc" in flat
        assert "LEFT JOIN todo_counts tc" in flat
        assert "ORDER BY s.last_message_at DESC NULLS LAST" in flat
        assert "LIMIT $1" in flat
        assert "OFFSET $2" in flat
        assert data_params == (10, 0)

    @pytest.mark.asyncio
    async def test_status_filter_uses_shared_reference_timestamp(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """With a status filter, both statements bind a single reference
        timestamp parameter so count and data agree on boundary rows."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={"status": "running", "limit": "5", "offset": "5"},
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        count_flat = _norm(count_sql)
        assert "($1)" in count_flat or "= $1" in count_flat
        # Status filter is $1, reference timestamp is $2.
        assert count_params[0] == "running"
        assert isinstance(count_params[1], datetime)
        assert "s.last_message_at > $2 - interval" in count_flat

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        data_flat = _norm(data_sql)
        assert "page_ids" in data_flat
        assert "status_expr" in data_flat or "running" in data_flat
        assert "s.last_message_at > $2 - interval" in data_flat
        assert "LIMIT $3" in data_flat
        assert "OFFSET $4" in data_flat
        assert data_params[0] == "running"
        assert data_params[1] == count_params[1], (
            "data and count must bind the identical reference timestamp"
        )
        assert data_params[2:] == (5, 5)

    @pytest.mark.asyncio
    async def test_filter_parameter_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Agent-run filters are ordered client → last-message window →
        agent → project, matching placeholder numbering."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/agent-runs",
                params={
                    "client_id": str(_CLIENT_ID),
                    "from_date": _START.isoformat(),
                    "to_date": _END.isoformat(),
                    "agent": "code-editor",
                    "external_project_id": "proj-1",
                },
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        flat = _norm(count_sql)
        assert "s.client_id = $1" in flat
        assert "s.last_message_at >= $2" in flat
        assert "s.last_message_at <= $3" in flat
        assert "s.agent = $4" in flat
        assert "s.project_id = $5" in flat
        assert count_params == (
            _CLIENT_ID, _START, _END, "code-editor", "proj-1"
        )

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat_data = _norm(data_sql)
        assert "LIMIT $6" in flat_data
        assert "OFFSET $7" in flat_data
        assert data_params[:5] == count_params
        assert data_params[5:] == (50, 0)


# ══════════════════════════════════════════════════════════════════════════
#  AFK run summaries
# ══════════════════════════════════════════════════════════════════════════


class TestAfkRunSummarySqlShape:
    @pytest.mark.asyncio
    async def test_list_sql_shape_and_param_slots(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Count + data over afk_runs, ordered by last_seen_at DESC, with the
        repository EXISTS filter first and window filters in declared order."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get(
                "/api/v1/afk-outcomes/runs",
                params={
                    "repository": "acme/proj",
                    "started_from": _START.isoformat(),
                    "started_to": _END.isoformat(),
                    "outcome": "merged",
                    "origin": "gitlab",
                    "limit": "10",
                    "offset": "20",
                },
            )

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        flat = _norm(count_sql)
        assert flat.startswith("SELECT COUNT(*) FROM afk_runs r WHERE")
        assert "EXISTS (SELECT 1 FROM afk_run_entities re" in flat
        assert "re.repository = $1" in flat
        assert "r.started_at >= $2" in flat
        assert "r.started_at <= $3" in flat
        assert "r.outcome_status = $4" in flat
        assert "r.provider = $5" in flat
        assert count_params[0] == "acme/proj"
        assert count_params[1] == _START
        assert count_params[2] == _END
        assert count_params[3] == "merged"
        assert count_params[4] == "gitlab"

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat_data = _norm(data_sql)
        assert "FROM afk_runs r" in flat_data
        assert "ORDER BY r.last_seen_at DESC" in flat_data
        assert "LIMIT $6" in flat_data
        assert "OFFSET $7" in flat_data
        assert data_params[:5] == count_params
        assert data_params[5:] == (10, 20)

    @pytest.mark.asyncio
    async def test_empty_filters_use_true_predicate(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """With no filters the WHERE clause is the TRUE sentinel and the
        limit/offset are the only bound parameters."""
        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get("/api/v1/afk-outcomes/runs")

        assert resp.status_code == 200
        count_sql, count_params = _fetchval_calls(mock_conn)[0]
        assert _norm(count_sql) == "SELECT COUNT(*) FROM afk_runs r WHERE TRUE"
        assert count_params == ()

        data_sql, data_params = _fetch_calls(mock_conn)[0]
        flat = _norm(data_sql)
        assert "WHERE TRUE" in flat
        assert "LIMIT $1" in flat
        assert "OFFSET $2" in flat
        assert data_params == (50, 0)
