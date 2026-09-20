"""Usage read-path fixture parity tests (issue #708, T2).

Covers grouped aggregates, Records, and Records-with-context through the
HTTP surface.  Each scenario is independently runnable in CI with a mocked
connection and asserts the canonical response contract — fields, totals,
null handling, filters, ordering, and empty/out-of-range pages.

Parity principle
----------------
These tests compare the read endpoint's observable output against the
canonical expectation for the fixture rows the query returns.  They do not
assert *how* the SQL derives the value (that is the T1 SQL-shape suite) and
they never assert a sequential scan is absent — plan choice is validated by
the live protocol in ``tests/integration/test_read_path_live_protocol.py``.

Protocol reference: ``docs/adr/0017-migration-0019-index-measurement.md``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

import pytest
from httpx import AsyncClient

from tests.test_usage import (
    _mk_aggregate_row,
    _mk_record_row,
    _mk_rwc_record_row,
)

_CLIENT_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()
_T0 = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2025, 7, 16, 13, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2025, 7, 16, 14, 0, 0, tzinfo=timezone.utc)

_RANGE = {
    "start_date": "2025-07-01T00:00:00Z",
    "end_date": "2025-07-31T23:59:59Z",
}


def _agg_row(**overrides) -> MagicMock:
    """Aggregate row that also carries the ``provider_breakdown`` JSON column."""
    raw = overrides.pop("provider_breakdown", None)
    row = _mk_aggregate_row(**overrides)
    if raw is None:
        raw = {}
    _base = row.__getitem__.side_effect
    extra = {"provider_breakdown": json.dumps(raw) if isinstance(raw, dict) else raw}

    def _side(k, _base=_base, _extra=extra):
        if k in _extra:
            return _extra[k]
        return _base(k)

    row.__getitem__.side_effect = _side
    return row


def _rollup_row(
    *,
    group_value: str,
    project_label: str | None,
    total_input_tokens: int = 100,
    total_output_tokens: int = 50,
    cache_read_tokens: int = 10,
    cache_write_tokens: int = 5,
    cost: Decimal | None = Decimal("0.01"),
) -> MagicMock:
    """A ``_fetch_aggregates_rollup`` additive-totals row."""
    row = MagicMock()
    data = {
        "group_value": group_value,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cached_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cache_read_tokens": cache_read_tokens,
        "total_cache_write_tokens": cache_write_tokens,
        "total_estimated_cost_usd": cost,
        "project_label": project_label,
    }
    row.__getitem__.side_effect = data.__getitem__
    return row


def _count_row(
    *,
    group_value: str,
    record_count: int,
    session_count: int,
    model_count: int,
    provider_breakdown: str = "{}",
) -> MagicMock:
    """A ``_fetch_aggregates_rollup`` distinct-count row."""
    row = MagicMock()
    data = {
        "group_value": group_value,
        "record_count": record_count,
        "session_count": session_count,
        "model_count": model_count,
        "provider_breakdown": provider_breakdown,
    }
    row.__getitem__.side_effect = data.__getitem__
    return row


# ══════════════════════════════════════════════════════════════════════════
#  Aggregates
# ══════════════════════════════════════════════════════════════════════════


class TestAggregateProviderBreakdown:
    @pytest.mark.asyncio
    async def test_total_provider_breakdown_includes_unknown(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Unknown provider is surfaced under the literal ``unknown`` key and
        never dropped from the breakdown."""
        mock_conn.fetchrow = AsyncMock(return_value=_agg_row(
            provider_breakdown={"openai": 2, "unknown": 1},
        ))
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get("/api/v1/usage/aggregates", params=_RANGE)

        assert resp.status_code == 200
        data = resp.json()["data"][0]
        assert data["provider_breakdown"] == {"openai": 2, "unknown": 1}

    @pytest.mark.asyncio
    async def test_grouped_provider_breakdown_per_group(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[
            _agg_row(group_value="gpt-4", provider_breakdown={"openai": 3}),
            _agg_row(group_value="claude", provider_breakdown={"anthropic": 2, "unknown": 1}),
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "model"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data[0]["group_value"] == "gpt-4"
        assert data[0]["provider_breakdown"] == {"openai": 3}
        assert data[1]["provider_breakdown"] == {"anthropic": 2, "unknown": 1}

    @pytest.mark.asyncio
    async def test_null_breakdown_defaults_to_empty_dict(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _agg_row()
        _base = row.__getitem__.side_effect
        row.__getitem__.side_effect = lambda k, _b=_base: (
            None if k == "provider_breakdown" else _b(k)
        )
        mock_conn.fetchrow = AsyncMock(return_value=row)
        mock_conn.fetch = AsyncMock(return_value=[])

        async with client as c:
            resp = await c.get("/api/v1/usage/aggregates", params=_RANGE)

        assert resp.status_code == 200
        assert resp.json()["data"][0]["provider_breakdown"] == {}


class TestAggregateSupportedDimensions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "group_by",
        ["client", "model", "session", "day", "week", "month", "project", "agent"],
    )
    async def test_every_supported_dimension_is_accepted(
        self, client: AsyncClient, mock_conn: AsyncMock, group_by: str
    ):
        mock_conn.fetch = AsyncMock(return_value=[
            _agg_row(group_value="only", project_label="only", agent="only"),
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": group_by},
            )

        assert resp.status_code == 200
        assert resp.json()["data"][0]["group_value"] == "only"

    @pytest.mark.asyncio
    async def test_multi_dimension_grouping_preserves_pipe_group_value(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[
            _agg_row(group_value="code-editor|gpt-4", agent="code-editor"),
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "agent,model"},
            )

        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["group_value"] == "code-editor|gpt-4"

    @pytest.mark.asyncio
    async def test_empty_range_returns_empty_list_and_total_zero(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Empty range: grouped returns [] and the total row returns zeroed
        additive totals (never null)."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            grouped = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "model"},
            )
            total = await c.get("/api/v1/usage/aggregates", params=_RANGE)

        assert grouped.status_code == 200
        assert grouped.json()["data"] == []

        assert total.status_code == 200
        row = total.json()["data"][0]
        assert row["group_value"] == "total"
        assert row["total_input_tokens"] == 0
        assert row["total_output_tokens"] == 0
        assert row["record_count"] == 0
        assert row["provider_breakdown"] == {}


class TestAggregateRollupParity:
    @pytest.mark.asyncio
    async def test_rollup_totals_merge_with_canonical_counts(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Hybrid read parity: additive totals come from the rollup row while
        counts come from the canonical-event count row, merged by
        ``group_value``; a rollup group with no count row degrades to zero
        counts instead of being dropped."""
        group = "Acme|My Project"
        mock_conn.fetch = AsyncMock(side_effect=[
            [_rollup_row(
                group_value=group,
                project_label="My Project",
                total_input_tokens=1000,
                total_output_tokens=200,
                cache_read_tokens=300,
                cache_write_tokens=40,
                cost=Decimal("1.23"),
            )],
            [_count_row(
                group_value=group,
                record_count=12,
                session_count=3,
                model_count=2,
                provider_breakdown=json.dumps({"gitlab": 7, "unknown": 5}),
            )],
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "client,project"},
            )

        assert resp.status_code == 200
        assert mock_conn.fetch.call_count == 2
        row = resp.json()["data"][0]
        assert row["group_value"] == group
        assert row["project_label"] == "My Project"
        # Additive totals from the rollup.
        assert row["total_input_tokens"] == 1000
        assert row["total_output_tokens"] == 200
        assert row["total_cache_read_tokens"] == 300
        assert row["total_cache_write_tokens"] == 40
        # Counts from the canonical-event scan.
        assert row["record_count"] == 12
        assert row["session_count"] == 3
        assert row["model_count"] == 2
        assert row["provider_breakdown"] == {"gitlab": 7, "unknown": 5}

    @pytest.mark.asyncio
    async def test_rollup_group_without_counts_defaults_counts_to_zero(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        group = "Solo|Lonely"
        mock_conn.fetch = AsyncMock(side_effect=[
            [_rollup_row(group_value=group, project_label="Lonely")],
            [],
        ])
        mock_conn.fetchrow = AsyncMock(return_value=None)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={**_RANGE, "group_by": "client,project"},
            )

        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["record_count"] == 0
        assert row["session_count"] == 0
        assert row["model_count"] == 0
        assert row["provider_breakdown"] == {}


# ══════════════════════════════════════════════════════════════════════════
#  Records
# ══════════════════════════════════════════════════════════════════════════


class TestRecordsParity:
    @pytest.mark.asyncio
    async def test_required_fields_and_enrichment_present(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _mk_record_row(
            record_id=uuid.uuid4(),
            provider="openai",
            mode="chat",
            finish_reason="stop",
            reasoning_tokens=20,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reported_at=_T0,
            ingested_at=_T1,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/records", params=_RANGE)

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        for field in (
            "id", "client_id", "source_database_id", "session_id", "model_name",
            "input_tokens", "output_tokens", "cached_tokens", "provider",
            "mode", "finish_reason", "reasoning_tokens", "cache_read_tokens",
            "cache_write_tokens", "estimated_cost_usd", "reported_at",
            "ingested_at", "loki_search_url", "active_tokens",
        ):
            assert field in item, f"missing field {field}"
        assert item["active_tokens"] == item["input_tokens"] + item["output_tokens"]
        assert item["loki_search_url"] is not None

    @pytest.mark.asyncio
    async def test_null_optional_fields_serialise_as_null(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Null enrichment columns stay null and do not obscure base totals."""
        row = _mk_record_row(
            provider=None,
            mode=None,
            finish_reason=None,
            reasoning_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            cost=None,
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/records", params=_RANGE)

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["provider"] is None
        assert item["mode"] is None
        assert item["finish_reason"] is None
        assert item["reasoning_tokens"] is None
        assert item["cache_read_tokens"] is None
        assert item["cache_write_tokens"] is None
        assert item["estimated_cost_usd"] is None
        assert item["input_tokens"] == 100
        assert item["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_duplicate_timestamps_preserve_db_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Rows sharing a timestamp keep the database's returned order — the
        read path must not reorder them in Python."""
        ids = [uuid.uuid4() for _ in range(3)]
        rows = [_mk_record_row(record_id=rid, reported_at=_T0) for rid in ids]
        mock_conn.fetch = AsyncMock(return_value=rows)
        mock_conn.fetchval = AsyncMock(return_value=3)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={**_RANGE, "sort_by": "reported_at", "sort_dir": "desc"},
            )

        assert resp.status_code == 200
        returned = [i["id"] for i in resp.json()["data"]["items"]]
        assert returned == [str(rid) for rid in ids]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sort_by", "sort_dir", "uses_coalesce"),
        [
            ("source_created_at", "desc", True),
            ("source_created_at", "asc", True),
            ("reported_at", "desc", False),
            ("ingested_at", "asc", False),
        ],
    )
    async def test_sort_mode_and_context_timestamp_fallback(
        self,
        client: AsyncClient,
        mock_conn: AsyncMock,
        sort_by: str,
        sort_dir: str,
        uses_coalesce: bool,
    ):
        """``source_created_at`` ordering must fall back to ``reported_at``
        when the Session Context timestamp is null; other sort modes use the
        requested column directly."""
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={**_RANGE, "sort_by": sort_by, "sort_dir": sort_dir},
            )

        assert resp.status_code == 200
        sql = " ".join(mock_conn.fetch.call_args[0][0].split())
        if uses_coalesce:
            assert (
                f"ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) {sort_dir}"
                in sql
            )
        else:
            expected = {
                "reported_at": "our.reported_at",
                "ingested_at": "our.first_ingested_at",
            }[sort_by]
            assert f"ORDER BY {expected} {sort_dir}" in sql

    @pytest.mark.asyncio
    async def test_adjacent_pages_are_disjoint_and_metadata_correct(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Two adjacent pages carry the requested offsets and disjoint rows."""
        page_one = [_mk_record_row(record_id=uuid.uuid4()) for _ in range(2)]
        page_two = [_mk_record_row(record_id=uuid.uuid4()) for _ in range(2)]
        mock_conn.fetchval = AsyncMock(return_value=4)
        mock_conn.fetch = AsyncMock(side_effect=[page_one, page_two])

        async with client as c:
            first = await c.get(
                "/api/v1/usage/records",
                params={**_RANGE, "limit": "2", "offset": "0"},
            )
            second = await c.get(
                "/api/v1/usage/records",
                params={**_RANGE, "limit": "2", "offset": "2"},
            )

        assert first.json()["data"]["offset"] == 0
        assert second.json()["data"]["offset"] == 2
        first_ids = {i["id"] for i in first.json()["data"]["items"]}
        second_ids = {i["id"] for i in second.json()["data"]["items"]}
        assert first_ids.isdisjoint(second_ids)

    @pytest.mark.asyncio
    async def test_out_of_range_page_is_empty_with_total_intact(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=7)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={**_RANGE, "limit": "10", "offset": "500"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 7
        assert data["offset"] == 500

    @pytest.mark.asyncio
    async def test_filters_are_reflected_in_params(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records",
                params={
                    **_RANGE,
                    "client_id": str(_CLIENT_ID),
                    "model": "gpt-4",
                    "session_id": str(_SESSION_ID),
                },
            )

        assert resp.status_code == 200
        count_params = mock_conn.fetchval.call_args[0][1:]
        assert _CLIENT_ID in count_params
        assert "gpt-4" in count_params
        assert _SESSION_ID in count_params


# ══════════════════════════════════════════════════════════════════════════
#  Records with context
# ══════════════════════════════════════════════════════════════════════════


class TestRecordsWithContextParity:
    @pytest.mark.asyncio
    async def test_context_enrichment_fields_present(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        row = _mk_rwc_record_row(
            agent="code-editor",
            session_title="Fix login bug",
            project_label="my-project",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/records-with-context", params=_RANGE)

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["agent"] == "code-editor"
        assert item["session_title"] == "Fix login bug"
        assert item["project_label"] == "my-project"
        assert item["loki_search_url"] is not None

    @pytest.mark.asyncio
    async def test_null_context_still_serialises(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A record whose session has no context row (null title/agent) still
        serialises with an ``unknown`` project label."""
        row = _mk_rwc_record_row(
            agent=None,
            session_title=None,
            project_label="unknown",
        )
        mock_conn.fetch = AsyncMock(return_value=[row])
        mock_conn.fetchval = AsyncMock(return_value=1)

        async with client as c:
            resp = await c.get("/api/v1/usage/records-with-context", params=_RANGE)

        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["agent"] is None
        assert item["session_title"] is None
        assert item["project_label"] == "unknown"

    @pytest.mark.asyncio
    async def test_ordering_is_source_created_desc(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=0)

        async with client as c:
            resp = await c.get("/api/v1/usage/records-with-context", params=_RANGE)

        assert resp.status_code == 200
        sql = " ".join(mock_conn.fetch.call_args[0][0].split())
        assert (
            "ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) DESC"
            in sql
        )

    @pytest.mark.asyncio
    async def test_filters_and_adjacent_pages(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        page_one = [_mk_rwc_record_row() for _ in range(2)]
        page_two = [_mk_rwc_record_row() for _ in range(2)]
        mock_conn.fetchval = AsyncMock(return_value=4)
        mock_conn.fetch = AsyncMock(side_effect=[page_one, page_two])

        async with client as c:
            first = await c.get(
                "/api/v1/usage/records-with-context",
                params={
                    **_RANGE,
                    "project_id": "proj-1",
                    "agent": "code-editor",
                    "model": "gpt-4",
                    "limit": "2",
                    "offset": "0",
                },
            )
            second = await c.get(
                "/api/v1/usage/records-with-context",
                params={**_RANGE, "limit": "2", "offset": "2"},
            )

        assert first.status_code == 200
        assert second.json()["data"]["offset"] == 2
        first_ids = {i["id"] for i in first.json()["data"]["items"]}
        second_ids = {i["id"] for i in second.json()["data"]["items"]}
        assert first_ids.isdisjoint(second_ids)

        count_params = mock_conn.fetchval.call_args_list[0][0][1:]
        assert "proj-1" in count_params
        assert "code-editor" in count_params
        assert "gpt-4" in count_params

    @pytest.mark.asyncio
    async def test_empty_page_is_well_formed(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=2)

        async with client as c:
            resp = await c.get(
                "/api/v1/usage/records-with-context",
                params={**_RANGE, "limit": "50", "offset": "99"},
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 2
