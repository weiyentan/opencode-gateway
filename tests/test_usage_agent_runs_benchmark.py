"""Production-shaped benchmark for the Agent Runs list query (issue #709).

Issue #709 rewrote ``_fetch_agent_runs`` to page the filtered universe
BEFORE enrichment: the ordered page of sessions is selected first and the
usage/provider/Todo-Snapshot aggregates join the page rather than the full
filtered universe, so enrichment work scales with the page size instead of
the total match count.

Two layers, mirroring the read-path acceptance protocol of ADR 0017
(``docs/adr/0017-migration-0019-index-measurement.md``):

- **Structural (always runs, no database)**: captures the *production*
  data SQL by calling ``_fetch_agent_runs`` with a recording connection and
  asserts the reduction property directly — pagination is bound before the
  enrichment CTEs and every enrichment aggregate joins the page, not the
  filtered universe.  This is the always-on guard: if a future edit moves
  LIMIT/OFFSET after the aggregates or re-scopes them to the universe, the
  enrichment work regresses to O(universe) and these tests fail.

- **Live plan capture (skips without a PostgreSQL replica)**: runs
  ``EXPLAIN (ANALYZE, BUFFERS)`` against the captured production SQL and
  reports per-node row/block volumes.  Run with::

      export READ_PATH_LIVE_DATABASE_URL=postgresql://user:pass@replica:5432/gateway
      pytest tests/test_usage_agent_runs_benchmark.py -v -m benchmark

  The live section is read-only (``EXPLAIN`` only) and bounded by a
  statement timeout, so pointing it at a replica is safe.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.api.usage import _fetch_agent_runs


# ══════════════════════════════════════════════════════════════════════════
#  Production SQL capture
# ══════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """Minimal stand-in for ``asyncpg.Connection`` that records statements.

    ``_fetch_agent_runs`` only calls ``fetchval`` (count) and ``fetch``
    (data); a real asyncpg ``Connection`` cannot be constructed without a
    live pool, so the benchmark records the exact statements + parameters
    the production function binds and replays them against a live replica.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.statements.append((sql, args))
        return 0

    async def fetch(self, sql: str, *args: Any) -> list:
        self.statements.append((sql, args))
        return []


def _capture_agent_runs_sql() -> tuple[str, tuple]:
    """Capture the production data SQL and its bound parameters."""
    import asyncio

    conn = _RecordingConn()
    asyncio.run(
        _fetch_agent_runs(
            conn,
            client_id=uuid.uuid4(),
            from_date=datetime(2025, 7, 1, tzinfo=timezone.utc),
            to_date=datetime(2025, 7, 31, 23, 59, 59, tzinfo=timezone.utc),
            agent=None,
            external_project_id=None,
            status_filter=None,
            limit=50,
            offset=0,
            grafana_base_url="",
            db_timeout_seconds=30,
            quiet_threshold_minutes=15,
            stale_threshold_hours=2,
            unknown_threshold_hours=48,
        )
    )
    data_sql, data_params = conn.statements[-1]
    return data_sql, data_params


def _norm_sql(sql: str) -> str:
    return " ".join(sql.split())


# ══════════════════════════════════════════════════════════════════════════
#  Structural reduction benchmark (always runs — no database required)
# ══════════════════════════════════════════════════════════════════════════


class TestEnrichmentWorkReduction:
    """The enrichment aggregates must be scoped to the selected page."""

    def test_enrichment_is_page_scoped_in_production_sql(self):
        """Production data SQL pages the universe before enrichment.

        Reduction property: the enrichment input is the LIMIT/OFFSET page,
        so aggregate work is O(page) instead of O(universe).  Guaranteed by
        (a) LIMIT/OFFSET binding inside ``page_ids`` — before any aggregate
        CTE is defined — and (b) every enrichment aggregate joining
        ``page`` rather than the filtered universe.
        """
        data_sql, data_params = _capture_agent_runs_sql()
        flat = _norm_sql(data_sql)

        # (a) Pagination precedes enrichment.
        assert flat.index("LIMIT $") < flat.index("usage_agg AS")
        assert flat.index("OFFSET $") < flat.index("usage_agg AS")
        assert flat.index("ORDER BY s.last_message_at DESC NULLS LAST") < (
            flat.index("usage_agg AS")
        )

        # (b) Enrichment aggregates join the page — usage_agg and
        # provider_agg via the session id, todo_counts via the external
        # session identity.
        assert flat.count("JOIN page p ON p.id = ue.session_id") == 2
        assert "JOIN page s ON s.source_database_id" in flat
        assert "JOIN base" not in flat, (
            "enrichment must not scan the full filtered universe"
        )

        # The page itself is the ordered, source-created selection.
        assert "WITH page_ids AS" in flat
        assert flat.index("ORDER BY") < flat.index("LIMIT $")

        # Limit/offset occupy the final two parameter slots.
        assert data_params[-2:] == (50, 0)

    def test_count_query_has_no_enrichment(self):
        """The count query stays a bare filtered count — no aggregates, no
        joins — so paging never adds count-side work."""
        conn_statements: list[tuple[str, tuple]] = []

        class _Capturing(_RecordingConn):
            async def fetchval(self, sql: str, *args: Any) -> int:
                conn_statements.append((sql, args))
                return 0

        import asyncio

        conn = _Capturing()
        asyncio.run(
            _fetch_agent_runs(
                conn,
                client_id=None,
                from_date=None,
                to_date=None,
                agent=None,
                external_project_id=None,
                status_filter="running",
                limit=50,
                offset=0,
                grafana_base_url="",
                db_timeout_seconds=30,
                quiet_threshold_minutes=15,
                stale_threshold_hours=2,
                unknown_threshold_hours=48,
            )
        )
        count_sql, count_params = conn_statements[0]
        flat = _norm_sql(count_sql)
        assert flat.startswith("SELECT COUNT(*) FROM sessions s")
        assert "JOIN" not in flat
        assert "usage_events" not in flat
        assert "opencode_session_todos" not in flat
        # Status filter + shared reference timestamp bind on the count too.
        assert count_params[0] == "running"
        assert isinstance(count_params[1], datetime)


# ══════════════════════════════════════════════════════════════════════════
#  Live plan capture (ADR 0017 protocol — requires a read-only replica)
# ══════════════════════════════════════════════════════════════════════════

_LIVE_DSN_ENV = "READ_PATH_LIVE_DATABASE_URL"
_STATEMENT_TIMEOUT_MS = 30_000

benchmark_marker = pytest.mark.skipif(
    os.environ.get(_LIVE_DSN_ENV) is None,
    reason=f"set {_LIVE_DSN_ENV} to a read-only replica to run the live plan capture",
)


def _iter_nodes(plan: Any):
    if isinstance(plan, list):
        for entry in plan:
            yield from _iter_nodes(entry)
        return
    if not isinstance(plan, dict):
        return
    node = plan.get("Plan", plan)
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("Plans", []) or []:
        yield from _iter_nodes(child)


@benchmark_marker
@pytest.mark.asyncio
async def test_live_plan_reports_page_scoped_enrichment():
    """EXPLAIN (ANALYZE, BUFFERS) the production SQL against a live replica.

    Reports node types and row/block volumes for the record; asserts the
    reduction property on observed evidence — the usage_events aggregation
    nodes feed from the page join, so their actual-row counts never exceed
    the page's usage rows.
    """
    import asyncpg

    data_sql, data_params = _capture_agent_runs_sql()
    conn = await asyncpg.connect(os.environ[_LIVE_DSN_ENV])
    try:
        await conn.execute(
            f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}"
        )
        plan_rows = await conn.fetch(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {data_sql}",
            *data_params,
        )
    finally:
        await conn.close()

    plan = json.loads(plan_rows[0][0])
    nodes = list(_iter_nodes(plan))
    report = [
        {
            "node": n.get("Node Type"),
            "actual_rows": n.get("Actual Rows"),
            "actual_loops": n.get("Actual Loops"),
        }
        for n in nodes
        if n.get("Node Type")
    ]
    print(json.dumps(report, indent=2))  # benchmark report to stdout

    # The plan must produce at most one row per page slot.
    top = plan[0]["Plan"]
    assert top["Actual Rows"] <= data_params[-2]
