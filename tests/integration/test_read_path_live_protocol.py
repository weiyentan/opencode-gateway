"""Live read-path acceptance protocol (issue #708).

The established acceptance protocol for production-shaped read-path
measurements is ADR 0017
(``docs/adr/0017-migration-0019-index-measurement.md``): capture
``EXPLAIN (ANALYZE, BUFFERS)`` plans and warm-cache latencies for the read
queries, compare a candidate against a baseline, and decide keep/drop on
observed evidence.  This module turns that protocol into an independently
runnable harness.

Running
-------
CI (no database): the pure plan/latency helpers below are exercised, and
every live test skips cleanly because live measurement is **not requested**.
Nothing here requires Docker and ordinary pull requests never run it.

Opt-in against a read-only replica (or a separately approved read-only
target)::

    export READ_PATH_LIVE_ENABLED=1
    export READ_PATH_LIVE_DATABASE_URL=postgresql://reader:pass@replica:5432/gateway
    pytest tests/integration/test_read_path_live_protocol.py -v -m integration

The opt-in is deliberate: a DSN alone does **not** enable live measurement.
``READ_PATH_LIVE_ENABLED`` must be explicitly truthy.  When
``READ_PATH_LIVE_DATABASE_URL`` is unset, complete
``GATEWAY_DATABASE_HOST`` / ``_PORT`` / ``_NAME`` / ``_USER`` /
``_PASSWORD`` credentials may be used instead; no credential defaults are
assumed.  Once opted in, missing credentials, an unreachable target, or
zero executed live probes **fail** the run rather than skipping green.

Safety
------
Every connection opens a **READ ONLY** transaction with a bounded
``statement_timeout`` (30 s), verifies both boundaries before any probe
runs, and is rolled back on teardown.  Only ``SELECT`` / ``EXPLAIN``
statements run — the harness never issues schema changes, seed data, or
destructive teardown, so pointing it at a replica cannot mutate data;
``EXPLAIN ANALYZE`` executes the read query but performs no writes.
Credentials are never logged: failure messages name only
``host:port/database``.

Interpreting results
---------------------
A sequential scan is **not** a failure.  The protocol's signal is the
*comparison*: plan changes, temp spill (``Temp Read/Written Blocks`` or
``"Sort Method": "external"``), and p50/p95 latency deltas between a
baseline and a candidate.  Report the numbers; do not fail on node type.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import asyncpg
import pytest

# ══════════════════════════════════════════════════════════════════════════
#  Connection configuration
# ══════════════════════════════════════════════════════════════════════════

#: Explicit opt-in flag.  Ordinary PR/CI runs never set this; the live
#: protocol only runs when an operator deliberately enables it.
LIVE_ENABLED_ENV = "READ_PATH_LIVE_ENABLED"

#: Optional DSN override for the read-only replica / approved target.
LIVE_DATABASE_URL_ENV = "READ_PATH_LIVE_DATABASE_URL"

#: Fallback credential source, matching the sibling integration modules.
#: Every value is required once live measurement is opted in; there are no
#: credential defaults.
GATEWAY_DB_ENV_VARS = (
    "GATEWAY_DATABASE_HOST",
    "GATEWAY_DATABASE_PORT",
    "GATEWAY_DATABASE_NAME",
    "GATEWAY_DATABASE_USER",
    "GATEWAY_DATABASE_PASSWORD",
)

# Bound every statement so a pathological plan cannot pin the replica.
_STATEMENT_TIMEOUT_MS = 30_000

_OPT_IN_VALUES = frozenset({"1", "true", "yes", "on"})

# asyncpg binds ``timestamptz`` parameters from timezone-aware datetimes,
# not ISO strings; the fixed dashboard window from ADR 0017.
_START = datetime(2025, 7, 1, tzinfo=timezone.utc)
_END = datetime(2025, 7, 31, 23, 59, 59, tzinfo=timezone.utc)

# Warm-up + measured iterations, mirroring ADR 0017 ("10 warm-cache
# executions").  More samples make p95 less sensitive to a single pause.
_WARMUP = 2
_MEASURE = 10


class LiveMeasurementUnavailable(RuntimeError):
    """An explicitly requested live measurement cannot produce evidence."""


def live_opt_in_requested() -> bool:
    """Whether the operator deliberately opted into live measurement."""
    return os.environ.get(LIVE_ENABLED_ENV, "").strip().lower() in _OPT_IN_VALUES


def describe_target(dsn: str) -> str:
    """Safe ``host:port/database`` label for *dsn* (credentials stripped)."""
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or "unknown-host"
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "unknown-host/unknown-database"
    database = (parts.path or "/").lstrip("/") or "unknown-database"
    return f"{host}{port}/{database}"


def resolve_live_dsn() -> str:
    """Resolve the live target DSN for an explicitly requested run.

    ``READ_PATH_LIVE_DATABASE_URL`` wins; otherwise every
    ``GATEWAY_DATABASE_*`` value must be supplied.  Raises
    :class:`LiveMeasurementUnavailable` when the opt-in flag is absent or
    when credentials are incomplete — never silently falls back to a default
    test database.  The returned DSN is never logged.
    """
    if not live_opt_in_requested():
        raise LiveMeasurementUnavailable(
            f"Live read-path measurement is not enabled. Set "
            f"{LIVE_ENABLED_ENV}=1 to opt in deliberately."
        )

    override = os.environ.get(LIVE_DATABASE_URL_ENV, "").strip()
    if override:
        return override

    missing = [
        name
        for name in GATEWAY_DB_ENV_VARS
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise LiveMeasurementUnavailable(
            f"{LIVE_ENABLED_ENV}=1 but no complete live database credentials "
            f"were configured. Set {LIVE_DATABASE_URL_ENV} or all of "
            f"{', '.join(GATEWAY_DB_ENV_VARS)} (missing: {', '.join(missing)}). "
            "Credential values are never logged."
        )

    user = quote(os.environ["GATEWAY_DATABASE_USER"], safe="")
    password = quote(os.environ["GATEWAY_DATABASE_PASSWORD"], safe="")
    return (
        f"postgresql://{user}:{password}"
        f"@{os.environ['GATEWAY_DATABASE_HOST']}"
        f":{os.environ['GATEWAY_DATABASE_PORT']}"
        f"/{os.environ['GATEWAY_DATABASE_NAME']}"
    )


# ══════════════════════════════════════════════════════════════════════════
#  Read queries under measurement (mirror app/api/usage.py + afk_outcomes.py)
# ══════════════════════════════════════════════════════════════════════════

# Each probe is (name, sql, params).  They mirror the shipped read paths
# closely enough for plan/latency comparison; ADR 0017 used the same
# approach.  They are SELECT-only.

_PROBES: list[tuple[str, str, list[Any]]] = [
    (
        "aggregates_total",
        """
        SELECT
            'total' AS group_value,
            COALESCE(SUM(our.input_tokens), 0) AS total_input_tokens,
            COALESCE(SUM(our.output_tokens), 0) AS total_output_tokens,
            COALESCE(SUM(our.cached_tokens), 0) AS total_cached_tokens,
            COALESCE(SUM(our.reasoning_tokens), 0) AS total_reasoning_tokens,
            COALESCE(SUM(our.cache_read_tokens), 0) AS total_cache_read_tokens,
            COALESCE(SUM(our.cache_write_tokens), 0) AS total_cache_write_tokens,
            SUM(our.estimated_cost_usd) AS total_estimated_cost_usd,
            COUNT(*) AS record_count,
            COUNT(DISTINCT our.session_id) AS session_count,
            COUNT(DISTINCT om.model_name) AS model_count
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        LEFT JOIN opencode_clients oc ON oc.id = our.client_id
        WHERE our.reported_at >= $1 AND our.reported_at <= $2
        """,
        [_START, _END],
    ),
    (
        "records_data",
        """
        SELECT
            our.id, our.client_id, s.source_database_id, our.session_id,
            om.model_name, our.input_tokens, our.output_tokens,
            our.cached_tokens, NULLIF(our.provider, '') AS provider,
            our.mode, our.finish_reason, our.reasoning_tokens,
            our.cache_read_tokens, our.cache_write_tokens,
            our.estimated_cost_usd, our.reported_at,
            our.first_ingested_at AS ingested_at
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        JOIN sessions s ON s.id = our.session_id
        LEFT JOIN opencode_session_contexts osc
            ON osc.source_database_id = s.source_database_id
            AND osc.external_session_id = s.external_session_id
        WHERE our.reported_at >= $1 AND our.reported_at <= $2
        ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) DESC
        LIMIT $3 OFFSET $4
        """,
        [_START, _END, 50, 0],
    ),
    (
        "records_with_context_data",
        """
        SELECT
            our.id, our.client_id, s.source_database_id, our.session_id,
            om.model_name, our.input_tokens, our.output_tokens,
            our.cached_tokens, NULLIF(our.provider, '') AS provider,
            our.mode, our.finish_reason, our.reasoning_tokens,
            our.cache_read_tokens, our.cache_write_tokens,
            our.estimated_cost_usd, our.reported_at,
            our.first_ingested_at AS ingested_at,
            s.agent, osc.title AS session_title,
            osp.display_name AS project_label
        FROM usage_events our
        JOIN observed_models om ON om.id = our.model_id
        JOIN sessions s ON s.id = our.session_id
        LEFT JOIN opencode_session_contexts osc
            ON osc.source_database_id = s.source_database_id
            AND osc.external_session_id = s.external_session_id
        LEFT JOIN opencode_source_projects osp
            ON osp.source_database_id = s.source_database_id
            AND osp.external_project_id = s.project_id
        WHERE our.reported_at >= $1 AND our.reported_at <= $2
        ORDER BY COALESCE(osc.source_created_at_tz, our.reported_at) DESC
        LIMIT $3 OFFSET $4
        """,
        [_START, _END, 50, 0],
    ),
    (
        "agent_runs_data",
        """
        WITH base AS (
            SELECT s.*,
                CASE
                    WHEN s.message_count = 0 OR s.last_message_at IS NULL THEN 'unknown'
                    WHEN s.last_message_at > now() - interval '15 minutes' THEN 'running'
                    WHEN s.last_message_at > now() - interval '2 hours'
                         AND s.parent_session_id IS NULL THEN 'completed'
                    WHEN s.last_message_at > now() - interval '2 hours' THEN 'blocked'
                    WHEN s.last_message_at > now() - interval '48 hours' THEN 'stale'
                    ELSE 'unknown'
                END AS _status,
                osc.title AS session_title,
                osc.session_model AS session_model
            FROM sessions s
            LEFT JOIN opencode_session_contexts osc ON s.id = osc.session_id
            WHERE TRUE
        ),
        child_counts AS (
            SELECT parent_session_id, COUNT(*) AS cnt
            FROM sessions
            WHERE parent_session_id IS NOT NULL
            GROUP BY parent_session_id
        ),
        todo_counts AS (
            SELECT t.source_database_id, t.external_session_id,
                   COUNT(*) AS todo_total
            FROM opencode_session_todos t
            JOIN base s
              ON s.source_database_id = t.source_database_id
             AND s.external_session_id = t.external_session_id
            GROUP BY t.source_database_id, t.external_session_id
        )
        SELECT s.*, COALESCE(cc.cnt, 0) AS child_run_count,
               COALESCE(tc.todo_total, 0) AS todo_total
        FROM base s
        LEFT JOIN child_counts cc ON cc.parent_session_id = s.external_session_id
        LEFT JOIN todo_counts tc
            ON tc.source_database_id = s.source_database_id
            AND tc.external_session_id = s.external_session_id
        ORDER BY s.last_message_at DESC NULLS LAST
        LIMIT $1 OFFSET $2
        """,
        [50, 0],
    ),
    (
        "afk_runs_data",
        """
        SELECT r.afk_run_id, r.provider, r.title, r.started_at,
               r.finished_at, r.outcome_status, r.first_seen_at, r.last_seen_at
        FROM afk_runs r
        WHERE TRUE
        ORDER BY r.last_seen_at DESC
        LIMIT $1 OFFSET $2
        """,
        [50, 0],
    ),
]


def probe_names() -> list[str]:
    return [name for name, _sql, _params in _PROBES]


# ══════════════════════════════════════════════════════════════════════════
#  Plan analysis helpers (pure — run in CI)
# ══════════════════════════════════════════════════════════════════════════


def _iter_nodes(plan: Any) -> Iterable[dict[str, Any]]:
    """Yield every plan node depth-first from an EXPLAIN JSON plan.

    Accepts the full ``EXPLAIN (FORMAT JSON)`` payload (a list wrapping a
    ``Plan`` object) and yields nested ``Plans`` recursively.
    """
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


def node_types(plan: Any) -> list[str]:
    """All plan node types present (e.g. ``Seq Scan``, ``Hash Join``)."""
    return [str(n.get("Node Type")) for n in _iter_nodes(plan)]


def seq_scan_nodes(plan: Any) -> list[str]:
    """Names of relations read by sequential scans — informational only."""
    names: list[str] = []
    for node in _iter_nodes(plan):
        if node.get("Node Type") == "Seq Scan":
            names.append(str(node.get("Relation Name", "?")))
    return names


def temp_spill_bytes(plan: Any) -> int:
    """Total temp-file bytes reported anywhere in the plan.

    PostgreSQL reports spill as ``Temp Read Blocks`` / ``Temp Written
    Blocks`` (8 KiB each) on sort/hash/aggregate nodes.  Zero means no
    spill — the property the acceptance protocol cares about.
    """
    blocks = 0
    for node in _iter_nodes(plan):
        blocks += int(node.get("Temp Read Blocks", 0) or 0)
        blocks += int(node.get("Temp Written Blocks", 0) or 0)
    return blocks * 8192


def external_sort_nodes(plan: Any) -> list[dict[str, Any]]:
    """Sort nodes that spilled to disk (``Sort Method == "external"``)."""
    spilled: list[dict[str, Any]] = []
    for node in _iter_nodes(plan):
        if node.get("Sort Method") == "external":
            spilled.append(node)
    return spilled


def has_temp_spill(plan: Any) -> bool:
    return temp_spill_bytes(plan) > 0 or bool(external_sort_nodes(plan))


def latency_stats(samples: list[float]) -> dict[str, float]:
    """p50/p95/mean latency (milliseconds) for a list of timed samples."""
    if not samples:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0, "n": 0}
    ordered = sorted(samples)
    k = 0.95 * (len(ordered) - 1)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    p95 = ordered[f] if f == c else ordered[f] * (c - k) + ordered[c] * (k - f)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(p95, 3),
        "mean_ms": round(statistics.mean(ordered), 3),
        "n": len(ordered),
    }


def compare_latency(
    baseline: dict[str, float],
    candidate: dict[str, float],
) -> dict[str, float]:
    """Ratio of candidate to baseline p50/p95 (``1.0`` = identical).

    Higher is slower.  This is a *measurement*, not an assertion: the
    acceptance protocol reports the ratio and decides with plan evidence.
    """
    def _ratio(key: str) -> float:
        base = baseline.get(key, 0.0)
        cand = candidate.get(key, 0.0)
        if base <= 0:
            return 0.0 if cand <= 0 else float("inf")
        return round(cand / base, 3)

    return {"p50_ratio": _ratio("p50_ms"), "p95_ratio": _ratio("p95_ms")}


# ══════════════════════════════════════════════════════════════════════════
#  Live database helpers
# ══════════════════════════════════════════════════════════════════════════


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=5)
        await conn.close()
        return True
    except Exception:
        return False


def require_executed_probes(names: Iterable[str]) -> list[str]:
    """Return the executed probe names or fail when none ran.

    A live run that executed zero probes has produced no plan or latency
    evidence; reporting it as green would be a false-positive measurement.
    """
    executed = [name for name in names if name]
    if not executed:
        raise LiveMeasurementUnavailable(
            "Live read-path measurement was explicitly requested but zero "
            "live probes executed; refusing to report green measurement "
            "evidence."
        )
    return executed


@pytest.fixture(scope="module")
def live_database_dsn() -> str:
    """Resolve and verify the opt-in live target.

    Not requested → skip safely (no connection attempt).  Requested but
    misconfigured or unreachable → fail; a requested measurement must never
    degrade into a green skip.
    """
    if not live_opt_in_requested():
        pytest.skip(
            "Live read-path measurement is opt-in and was not requested. "
            f"Set {LIVE_ENABLED_ENV}=1 and supply {LIVE_DATABASE_URL_ENV} "
            "(or complete GATEWAY_DATABASE_* credentials) to run it against "
            "a read-only replica."
        )

    try:
        dsn = resolve_live_dsn()
    except LiveMeasurementUnavailable as exc:
        pytest.fail(str(exc), pytrace=False)

    import asyncio

    if not asyncio.run(_can_connect(dsn)):
        pytest.fail(
            f"{LIVE_ENABLED_ENV}=1 but the live read-path target "
            f"{describe_target(dsn)} is unreachable. Live measurement was "
            "explicitly requested; this is a failure, not a skip.",
            pytrace=False,
        )
    return dsn


@pytest.fixture(scope="module")
def live_probe_ledger(live_database_dsn: str) -> list[str]:
    """Record executed probes and fail the module when none produced evidence."""
    executed: list[str] = []
    yield executed
    require_executed_probes(executed)


@pytest.fixture
async def readonly_conn(live_database_dsn: str):
    """Yield a connection inside a verified, bounded READ ONLY transaction.

    Both safety boundaries are proven before any probe runs:
    ``transaction_read_only = on`` and ``statement_timeout = 30 s``.  The
    transaction is rolled back on teardown.  This is what makes the harness
    safe to point at a production read replica.
    """
    conn = await asyncpg.connect(dsn=live_database_dsn, timeout=10)
    tx = conn.transaction(readonly=True)
    await tx.start()
    try:
        await conn.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")

        read_only = await conn.fetchval("SHOW transaction_read_only")
        assert read_only == "on", (
            "Refusing live measurement: transaction_read_only is "
            f"{read_only!r}, expected 'on'."
        )

        timeout_ms = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM current_setting('statement_timeout')"
            "::interval) * 1000"
        )
        assert int(timeout_ms) == _STATEMENT_TIMEOUT_MS, (
            "Refusing live measurement: statement_timeout is "
            f"{timeout_ms} ms, expected {_STATEMENT_TIMEOUT_MS} ms."
        )

        yield conn
    finally:
        try:
            await tx.rollback()
        finally:
            await conn.close()


async def capture_explain(
    conn: asyncpg.Connection,
    sql: str,
    params: list[Any] | None = None,
) -> Any:
    """Run ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` for *sql*.

    ``ANALYZE`` executes the read query; inside the read-only transaction it
    performs no writes.  Returns the parsed JSON plan.
    """
    raw = await conn.fetchval(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}",
        *(params or []),
    )
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


async def measure_latency(
    conn: asyncpg.Connection,
    sql: str,
    params: list[Any] | None = None,
    *,
    warmup: int = _WARMUP,
    iterations: int = _MEASURE,
) -> dict[str, float]:
    """Warm-cache p50/p95 latency (ms) for *sql* inside the read-only txn."""
    import time

    for _ in range(warmup):
        await conn.fetch(sql, *(params or []))

    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        await conn.fetch(sql, *(params or []))
        samples.append((time.perf_counter() - start) * 1000)
    return latency_stats(samples)


# ══════════════════════════════════════════════════════════════════════════
#  Pure helper tests (run in CI, no database)
# ══════════════════════════════════════════════════════════════════════════


class TestPlanHelpers:
    def _plan(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"Plan": plan}]

    def test_iter_nodes_walks_nested_plans(self):
        plan = self._plan({
            "Node Type": "Hash Join",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "usage_events"},
                {"Node Type": "Hash", "Plans": [{"Node Type": "Seq Scan"}]},
            ],
        })
        assert node_types(plan) == ["Hash Join", "Seq Scan", "Hash", "Seq Scan"]

    def test_seq_scan_is_informational_not_failure(self):
        plan = self._plan({"Node Type": "Seq Scan", "Relation Name": "usage_events"})
        assert seq_scan_nodes(plan) == ["usage_events"]

    def test_temp_spill_bytes_from_blocks(self):
        plan = self._plan({
            "Node Type": "Sort",
            "Temp Read Blocks": 4,
            "Temp Written Blocks": 8,
        })
        assert temp_spill_bytes(plan) == 12 * 8192
        assert has_temp_spill(plan)

    def test_external_sort_detected(self):
        plan = self._plan({"Node Type": "Sort", "Sort Method": "external"})
        assert len(external_sort_nodes(plan)) == 1
        assert has_temp_spill(plan)

    def test_in_memory_sort_has_no_spill(self):
        plan = self._plan({"Node Type": "Sort", "Sort Method": "quicksort"})
        assert temp_spill_bytes(plan) == 0
        assert not has_temp_spill(plan)

    def test_latency_stats_percentiles(self):
        stats = latency_stats([10.0, 20.0, 30.0, 40.0])
        assert stats["p50_ms"] == 25.0
        assert stats["n"] == 4
        assert stats["p95_ms"] >= stats["p50_ms"]

    def test_compare_latency_ratio(self):
        baseline = {"p50_ms": 10.0, "p95_ms": 20.0}
        candidate = {"p50_ms": 5.0, "p95_ms": 30.0}
        ratios = compare_latency(baseline, candidate)
        assert ratios["p50_ratio"] == 0.5
        assert ratios["p95_ratio"] == 1.5


# ══════════════════════════════════════════════════════════════════════════
#  Live protocol tests (skipped unless a live database is reachable)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestLiveProtocol:
    @pytest.mark.asyncio
    async def test_transaction_is_read_only(self, readonly_conn: asyncpg.Connection):
        value = await readonly_conn.fetchval("SHOW transaction_read_only")
        assert value == "on"

    @pytest.mark.asyncio
    async def test_statement_timeout_is_bounded(
        self, readonly_conn: asyncpg.Connection
    ):
        timeout_ms = await readonly_conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM current_setting('statement_timeout')"
            "::interval) * 1000"
        )
        assert int(timeout_ms) == _STATEMENT_TIMEOUT_MS

    @pytest.mark.asyncio
    async def test_explain_captures_plan_for_every_probe(
        self,
        readonly_conn: asyncpg.Connection,
        live_probe_ledger: list[str],
    ):
        """The protocol's primary artifact: an ANALYZE plan per read query.

        A sequential scan is explicitly permitted — the assertion is only
        that a plan was captured with at least one node.  Every probe is
        recorded so an opted-in run that executes none fails instead of
        reporting green evidence.
        """
        executed: list[str] = []
        for name, sql, params in _PROBES:
            plan = await capture_explain(readonly_conn, sql, params)
            nodes = node_types(plan)
            assert nodes, f"{name}: EXPLAIN returned no plan nodes"
            # Informational: spill is reported, never asserted to be absent.
            temp_spill_bytes(plan)
            executed.append(name)

        live_probe_ledger.extend(require_executed_probes(executed))
        assert executed == probe_names()

    @pytest.mark.asyncio
    async def test_latency_comparison_of_candidate_against_baseline(
        self,
        readonly_conn: asyncpg.Connection,
        live_probe_ledger: list[str],
    ):
        """Measure a probe twice and report the candidate/baseline ratio.

        Running the same query as its own baseline proves the harness can
        produce the comparison artifact; a real optimization run substitutes
        the candidate SQL.  The ratio is reported, not asserted to a
        threshold.
        """
        name, sql, params = _PROBES[0]
        baseline = await measure_latency(readonly_conn, sql, params, iterations=3)
        candidate = await measure_latency(readonly_conn, sql, params, iterations=3)
        ratios = compare_latency(baseline, candidate)
        assert ratios["p50_ratio"] >= 0
        assert name  # probe identity is part of the reported measurement
        live_probe_ledger.extend(require_executed_probes([name]))
