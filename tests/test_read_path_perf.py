"""Read-path benchmark and validation harness for #364.

Measures dashboard read-path performance across synthetic (CI, regression-checking)
and production-shaped (profiling, measurement-only) datasets.

Scenarios:
- Single-session latency: p50/p95 per endpoint, total duration, DB time,
  status computation time, query count
- Concurrent-user: 5-10 parallel requests, per-request latency, pool wait
  time, error rate
- Total dashboard wall-clock: parallel fetch of all 8 API calls (initial
  load + refresh paths)

Baseline artifacts:
- Regression baselines (read_path_baseline.json, concurrent_baseline.json,
  dashboard_wall_clock_baseline.json) are written on first run only.
  Set REGENERATE_BASELINES=1 to force regeneration.
- Profiling output is written to tests/fixtures/profiling-output/ (gitignored).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from tests.test_agent_runs import (
    _EXTERNAL_ID_A,
    _EXTERNAL_ID_B,
    _EXTERNAL_ID_C,
    _mk_child_row,
    _mk_session_row,
    _mk_todo_row,
)
from tests.test_usage import (
    _mk_aggregate_row,
    _mk_record_row,
)
from tests.test_usage import (
    _mk_session_row as _mk_usage_session_row,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════════

_BASELINE_DIR = Path(__file__).parent / "fixtures"
_BASELINE_FILE = _BASELINE_DIR / "read_path_baseline.json"
_PROFILING_OUTPUT_DIR = _BASELINE_DIR / "profiling-output"

# Env var to force regeneration of tracked baselines
_REGENERATE_ENV = "REGENERATE_BASELINES"

# How many times to repeat single-session measurements (more -> better p50/p95)
_WARMUP_ITERATIONS = 2
_MEASUREMENT_ITERATIONS = 10

# Regression threshold: p95 must not exceed baseline_p95 * _REGRESSION_FACTOR
_REGRESSION_FACTOR = 2.0

# Concurrent users for the concurrent-user scenario
_CONCURRENT_USERS_MIN = 5
_CONCURRENT_USERS_MAX = 10

# Session IDs used in synthetic fixture
_SYNTH_SESSION_ID = uuid.uuid4()
_SYNTH_CLIENT_ID = uuid.uuid4()
_SYNTH_SOURCE_DB_ID = uuid.uuid4()

# The 8 dashboard API calls (paralleled via Promise.allSettled in Aurora Glass)
_DASHBOARD_ENDPOINTS: list[tuple[str, str, dict | None, str]] = [
    # (method, path, params, label)
    ("GET", "/api/v1/usage/aggregates",
     {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
     "aggregates"),
    ("GET", "/api/v1/usage/records",
     {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z", "limit": "50"},
     "records"),
    ("GET", "/api/v1/usage/sessions",
     {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
     "sessions"),
    ("GET", "/api/v1/usage/agent-runs",
     None,
     "agent-runs"),
    ("GET", "/api/v1/usage/agent-runs/{session_id}",
     None,
     "agent-run-detail"),
    ("GET", "/api/v1/usage/records-with-context",
     {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z", "limit": "50"},
     "records-with-context"),
    ("GET", "/health",
     None,
     "health"),
    ("GET", "/cursor",
     {"source_database_id": str(_SYNTH_SOURCE_DB_ID)},
     "cursor"),
]

# ══════════════════════════════════════════════════════════════════════════════
#  Timing measurement helpers
# ══════════════════════════════════════════════════════════════════════════════


def _percentile(data: list[float], pct: float) -> float:
    """Compute a percentile from a list of values (linear interpolation)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (pct / 100) * (len(sorted_data) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def _p50(data: list[float]) -> float:
    """50th percentile (median)."""
    if not data:
        return 0.0
    return statistics.median(data)


def _p95(data: list[float]) -> float:
    """95th percentile."""
    if not data:
        return 0.0
    return _percentile(data, 95)


class _TelemetryCapture:
    """Attaches a logging handler to capture ``operation.completed`` events.

    Collects structured ``extra`` fields from the telemetry logger
    (``app.core.telemetry``) so the harness can attribute durations to
    DB queries and status computation.

    Usage::

        with _TelemetryCapture() as cap:
            ...  # exercise endpoints
        db_ms = cap.sum_by_prefix("db.")
        compute_ms = cap.sum_by_prefix("compute.")
    """

    _LOGGER_NAME = "app.core.telemetry"
    _EVENT = "operation.completed"

    def __init__(self):
        self._records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None
        self._logger: logging.Logger | None = None

    def __enter__(self):
        self._logger = logging.getLogger(self._LOGGER_NAME)
        old_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)

        class _Handler(logging.Handler):
            def emit(inner_self, record):
                if record.getMessage() == _TelemetryCapture._EVENT:
                    self._records.append(record)

        self._handler = _Handler()
        self._logger.addHandler(self._handler)
        # store old level for restore
        self._old_level = old_level
        return self

    def __exit__(self, *args):
        if self._handler is not None and self._logger is not None:
            self._logger.removeHandler(self._handler)
            self._logger.setLevel(self._old_level)
        return False

    def sum_by_prefix(self, prefix: str) -> float:
        """Sum ``duration_ms`` for events whose ``event_name`` starts with *prefix*."""
        total = 0.0
        for rec in self._records:
            nm = getattr(rec, "event_name", "")
            if isinstance(nm, str) and nm.startswith(prefix):
                total += getattr(rec, "duration_ms", 0.0)
        return round(total, 3)

    def count_by_prefix(self, prefix: str) -> int:
        """Count events whose ``event_name`` starts with *prefix*."""
        return sum(
            1
            for rec in self._records
            if isinstance(getattr(rec, "event_name", ""), str)
            and getattr(rec, "event_name", "").startswith(prefix)
        )

    @property
    def event_count(self) -> int:
        return len(self._records)

    def reset(self) -> None:
        self._records.clear()


async def _measure_endpoint(
    client: AsyncClient,
    mock_conn: AsyncMock,
    method: str,
    path: str,
    label: str,
    params: dict | None = None,
    session_id_override: uuid.UUID | None = None,
    *,
    telemetry: _TelemetryCapture | None = None,
) -> dict:
    """Call an endpoint once and return timing + query-count data.

    When *telemetry* is provided, ``operation.completed`` events emitted
    by the request are captured and their durations are attributed to
    ``db_time_ms`` and ``status_computation_time_ms`` in the result.

    Returns a dict with keys: endpoint, status_code, duration_ms,
    db_time_ms, status_computation_time_ms, query_count, db_queries, error.
    """
    url = path
    if session_id_override is not None and "{session_id}" in path:
        url = path.replace("{session_id}", str(session_id_override))

    # Reset mock call counters for per-call query counting
    mock_conn.fetch.reset_mock()
    mock_conn.fetchrow.reset_mock()
    mock_conn.fetchval.reset_mock()

    # Reset telemetry capture per call
    if telemetry is not None:
        telemetry.reset()

    start = time.perf_counter()
    error_msg = None
    status_code = None
    try:
        if method == "GET":
            response = await client.get(url, params=params or {})
        else:
            response = await client.request(method, url, params=params or {})
        status_code = response.status_code
        if status_code >= 400:
            error_msg = f"HTTP {status_code}"
    except Exception as exc:
        error_msg = str(exc)
    duration_ms = (time.perf_counter() - start) * 1000

    db_time_ms = 0.0
    status_comp_ms = 0.0
    if telemetry is not None:
        db_time_ms = telemetry.sum_by_prefix("db.")
        status_comp_ms = telemetry.sum_by_prefix("compute.")

    return {
        "endpoint": label,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 3),
        "db_time_ms": db_time_ms,
        "status_computation_time_ms": status_comp_ms,
        "query_count": (
            mock_conn.fetch.call_count
            + mock_conn.fetchrow.call_count
            + mock_conn.fetchval.call_count
        ),
        "db_queries": {
            "fetch": mock_conn.fetch.call_count,
            "fetchrow": mock_conn.fetchrow.call_count,
            "fetchval": mock_conn.fetchval.call_count,
        },
        "error": error_msg,
    }


async def _measure_endpoint_repeated(
    client: AsyncClient,
    mock_conn: AsyncMock,
    method: str,
    path: str,
    label: str,
    params: dict | None = None,
    iterations: int = _MEASUREMENT_ITERATIONS,
    warmup: int = _WARMUP_ITERATIONS,
    session_id_override: uuid.UUID | None = None,
) -> dict:
    """Call an endpoint repeatedly and return aggregated timing stats.

    Returns a dict with: endpoint, p50_ms, p95_ms, min_ms, max_ms, mean_ms,
    durations, db_time_ms, status_computation_time_ms, query_count,
    status_codes, errors, iterations.
    """
    with _TelemetryCapture() as telemetry:
        # Warmup (discard results)
        for _ in range(warmup):
            await _measure_endpoint(
                client, mock_conn, method, path, label, params,
                session_id_override, telemetry=telemetry,
            )

        # Measurement
        results = []
        for _ in range(iterations):
            results.append(
                await _measure_endpoint(
                    client, mock_conn, method, path, label, params,
                    session_id_override, telemetry=telemetry,
                )
            )

    durations = [r["duration_ms"] for r in results]
    query_counts = [r["query_count"] for r in results]
    db_times = [r["db_time_ms"] for r in results]
    status_times = [r["status_computation_time_ms"] for r in results]
    errors = [r for r in results if r["error"] is not None]

    return {
        "endpoint": label,
        "p50_ms": round(_p50(durations), 3),
        "p95_ms": round(_p95(durations), 3),
        "min_ms": round(min(durations), 3),
        "max_ms": round(max(durations), 3),
        "mean_ms": round(statistics.mean(durations) if durations else 0, 3),
        "durations": [round(d, 3) for d in durations],
        "db_time_ms": round(_p50(db_times), 3),
        "status_computation_time_ms": round(_p50(status_times), 3),
        "query_count": statistics.mode(query_counts) if query_counts else 0,
        "status_codes": list(set(r["status_code"] for r in results)),
        "error_count": len(errors),
        "error_rate": round(len(errors) / len(results), 4) if results else 0.0,
        "iterations": iterations,
        "db_queries": results[0]["db_queries"] if results else {},
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Baseline file helpers
# ══════════════════════════════════════════════════════════════════════════════


def _should_regenerate() -> bool:
    """Return True if REGENERATE_BASELINES is set to a truthy value."""
    return os.environ.get(_REGENERATE_ENV, "").strip() in ("1", "true", "yes")


def _load_or_commit_baseline(path: Path, data: dict) -> dict | None:
    """Load existing baseline; write *data* as baseline only on first run.

    When *path* exists: return parsed baseline (no write).
    When *path* does not exist OR REGENERATE_BASELINES is truthy: write
    *data* and return None (this run creates the baseline).

    Returns the baseline dict for comparison, or None if this run generated it.
    """
    _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists() and not _should_regenerate():
        return json.loads(path.read_text())
    path.write_text(json.dumps(data, indent=2))
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Synthetic fixture helpers
# ══════════════════════════════════════════════════════════════════════════════


def _setup_synthetic_mocks(mock_conn: AsyncMock) -> None:
    """Configure mock_conn with deterministic synthetic row data.

    Uses SQL-inspecting factory functions so mock responses regenerate
    fresh rows on every call — they never exhaust across repeated
    warmup/measurement iterations.

    Representative row counts: 1 aggregate row, 50 records, 25 sessions,
    10 agent runs, 1 agent run detail (with children and todos).
    """
    now = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

    # ── Pre-built rows used as templates ──
    _agg_row = _mk_aggregate_row(
        group_value="total",
        record_count=50,
        session_count=25,
        model_count=3,
    )
    _detail_session_row = _mk_session_row(
        session_id=_SYNTH_SESSION_ID,
        client_id=_SYNTH_CLIENT_ID,
        external_session_id=_EXTERNAL_ID_A,
        last_message_at=now - timedelta(minutes=5),
        message_count=10,
        agent="code-editor",
        child_run_count=2,
        session_model="claude-sonnet-4-20250514",
        ctx_present=1,
        ctx_title="My Active Session",
        ctx_session_model="claude-sonnet-4-20250514",
        code_change_count=5,
        code_change_additions=120,
        code_change_deletions=45,
        parent_session_id=None,
        parent_internal_id=None,
    )

    # ── fetchrow: return agg_row for most queries, detail row for session-lookup ──
    def _fetchrow_fn(*args, **kwargs):
        sql = str(args[0]) if args else ""
        if "s.id = $1" in sql:
            return _detail_session_row
        # collector_credentials lookup (cursor endpoint auth)
        if "collector_credentials" in sql:
            cred_row = MagicMock()
            cred_data = {
                "credential_id": uuid.uuid4(),
                "revoked_at": None,
                "last_used_at": now,
                "client_id": _SYNTH_CLIENT_ID,
                "client_name": "test-client",
                "client_is_active": True,
            }
            cred_row.__getitem__.side_effect = cred_data.__getitem__
            return cred_row
        # source_databases lookup (cursor endpoint)
        if "source_databases" in sql:
            db_row = MagicMock()
            db_data = {
                "last_seen_at": now,
                "record_count": 50,
                "is_active": True,
            }
            db_row.__getitem__.side_effect = db_data.__getitem__
            return db_row
        return _agg_row

    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_fn)

    # ── fetchval: always 50 ──
    mock_conn.fetchval = AsyncMock(return_value=50)

    # ── fetch: regenerate fresh rows based on SQL shape ──
    def _fetch_fn(*args, **kwargs):
        sql = str(args[0]) if args else ""

        if "opencode_session_todos" in sql:
            return [
                _mk_todo_row(
                    content=f"Implement feature {chr(65 + i)}",
                    status=["completed", "in_progress", "pending"][i % 3],
                    priority=str(i + 1),
                    position=i,
                )
                for i in range(3)
            ]
        if "parent_session_id = $1" in sql and "ORDER BY last_message_at" in sql:
            return [
                _mk_child_row(
                    external_session_id=_EXTERNAL_ID_B,
                    agent="code-editor-junior",
                    last_message_at=now - timedelta(hours=1),
                ),
                _mk_child_row(
                    external_session_id=_EXTERNAL_ID_C,
                    agent="code-editor-mid",
                    last_message_at=now - timedelta(hours=2),
                ),
            ]
        # Usage records queries (must match BEFORE sessions join to avoid wrong-shape rows).
        # Augments with context fields so records-with-context endpoint gets them.
        if "usage_events" in sql:
            rows = []
            for j in range(50):
                r = _mk_record_row(
                    record_id=uuid.uuid4(),
                    client_id=_SYNTH_CLIENT_ID,
                    session_id=_SYNTH_SESSION_ID,
                    model_name="gpt-4",
                    reported_at=now - timedelta(hours=j),
                )
                # Augment with context fields for records-with-context endpoint
                _orig = r.__getitem__.side_effect

                def _augmented(k, orig=_orig):
                    try:
                        return orig(k)
                    except KeyError:
                        pass
                    return {
                        "agent": "code-editor",
                        "session_title": "Test Session",
                        "project_label": "test-project",
                    }[k]

                r.__getitem__.side_effect = _augmented
                rows.append(r)
            return rows
        # Agent-run list / sessions data queries join sessions + context + project
        if "LEFT JOIN opencode_session_contexts" in sql and "sessions s" in sql:
            return [
                _mk_session_row(
                    session_id=uuid.uuid4(),
                    client_id=_SYNTH_CLIENT_ID,
                    external_session_id=f"ses_{i:04d}",
                    last_message_at=now - timedelta(minutes=i * 10),
                    message_count=i + 5,
                    agent="code-editor",
                    child_run_count=1 if i < 3 else 0,
                    session_model="gpt-4",
                )
                for i in range(10)
            ]
        # Session data query joins sessions + context + project
        if "FROM sessions s" in sql:
            return [
                _mk_usage_session_row(
                    session_id=uuid.uuid4(),
                    client_id=_SYNTH_CLIENT_ID,
                    first_message_at=now - timedelta(days=i),
                    last_message_at=now - timedelta(hours=i),
                    message_count=i + 1,
                )
                for i in range(10)
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch_fn)


# ══════════════════════════════════════════════════════════════════════════════
#  Synthetic fixture — CI regression tests
# ══════════════════════════════════════════════════════════════════════════════


class TestSyntheticSingleSession:
    """Single-session latency benchmarks on synthetic (mocked) data.

    Records p50/p95 per endpoint, DB time, status computation time,
    query counts, and asserts no regression against the stored baseline.
    """

    @pytest.fixture(autouse=True)
    def _setup_mocks(self, mock_conn: AsyncMock) -> None:
        _setup_synthetic_mocks(mock_conn)

    @pytest.mark.asyncio
    async def test_synthetic_single_session_all_endpoints(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Measure p50/p95 for all 8 dashboard endpoints on synthetic data.

        Asserts no regression against stored baseline (p95 <= baseline_p95 * 2.0).
        """
        results: dict[str, dict] = {}

        async with client as c:
            for method, path, params, label in _DASHBOARD_ENDPOINTS:
                sid = _SYNTH_SESSION_ID if "{session_id}" in path else None
                results[label] = await _measure_endpoint_repeated(
                    c, mock_conn, method, path, label, params,
                    session_id_override=sid,
                )

        baseline = _load_or_commit_baseline(_BASELINE_FILE, results)

        # If baseline exists, assert no regression
        if baseline is not None:
            regressions = []
            for label, current in results.items():
                if label not in baseline:
                    continue
                bl = baseline[label]
                threshold = bl["p95_ms"] * _REGRESSION_FACTOR
                if current["p95_ms"] > threshold:
                    regressions.append(
                        f"  {label}: p95 {current['p95_ms']}ms > "
                        f"{threshold}ms (baseline {bl['p95_ms']}ms × {_REGRESSION_FACTOR})"
                    )
            if regressions:
                msg = (
                    f"Performance regression detected in {len(regressions)} endpoint(s):\n"
                    + "\n".join(regressions)
                    + "\n\nCurrent results:\n"
                    + json.dumps(results, indent=2)
                )
                pytest.fail(msg)

        # Sanity checks on results shape
        for label, r in results.items():
            assert r["error_count"] == 0, f"{label}: {r['error_count']} errors"
            assert r["p50_ms"] >= 0, f"{label}: negative p50"
            assert r["p95_ms"] >= 0, f"{label}: negative p95"
            assert r["p95_ms"] >= r["p50_ms"] * 0.5, (
                f"{label}: p95 ({r['p95_ms']}ms) unreasonably below "
                f"p50 ({r['p50_ms']}ms)"
            )
            assert r["iterations"] == _MEASUREMENT_ITERATIONS
            assert r["db_time_ms"] >= 0, f"{label}: negative db_time_ms"
            assert r["status_computation_time_ms"] >= 0, (
                f"{label}: negative status_computation_time_ms"
            )
            # health and cursor may legitimately have zero queries
            if label not in ("health", "cursor"):
                assert r["query_count"] > 0, f"{label}: zero queries"

    @pytest.mark.asyncio
    async def test_db_operations_emit_exactly_one_completed_event_per_query(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Each DB query wrapped in timed_operation emits exactly one event.

        The ``operation.completed`` event is emitted once per query by
        ``timed_operation``, and the nested ``timeout_operation`` wrapper
        emits nothing on success.  The measured ``duration_ms`` includes
        sub-millisecond ``timeout_operation`` wrapper overhead.
        Single-counting is guaranteed by design — there is one
        ``timed_operation`` context manager per query, and it emits its
        event exactly once in its ``finally`` block.
        """
        from collections import Counter

        # Representative endpoint: records (2 queries: count + data)
        label = "records"
        method, path, params = ("GET", "/api/v1/usage/records",
                                {"start_date": "2025-07-01T00:00:00Z",
                                 "end_date": "2025-07-31T23:59:59Z",
                                 "limit": "10"})

        with _TelemetryCapture() as telemetry:
            async with client as c:
                await _measure_endpoint(
                    c, mock_conn, method, path, label, params, telemetry=telemetry,
                )

        # Filter to only db.* events
        db_event_names = [
            getattr(r, "event_name", "")
            for r in telemetry._records
            if isinstance(getattr(r, "event_name", ""), str)
            and getattr(r, "event_name", "").startswith("db.")
        ]

        # Each distinct event name must appear exactly once
        name_counts = Counter(db_event_names)
        duplicates = [n for n, c in name_counts.items() if c > 1]
        assert not duplicates, (
            f"Duplicate operation.completed events detected: {duplicates}. "
            f"All names: {db_event_names}"
        )

        # Must have at least db.query.records.count and db.query.records.data
        assert len(db_event_names) >= 2, (
            f"Expected >=2 db.* operation.completed events, got {len(db_event_names)}: "
            f"{db_event_names}"
        )

    @pytest.mark.asyncio
    async def test_synthetic_concurrent_users(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """5-10 parallel requests hitting aggregates + sessions endpoints.

        Records per-request latency, pool wait time, error rate.
        """
        # Use aggregates as a simple fast endpoint for concurrent testing
        endpoint = ("GET", "/api/v1/usage/aggregates",
                    {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
                    "aggregates")
        method, path, params, label = endpoint

        # Ensure enough fetchrow returns
        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_aggregate_row(group_value="total", record_count=50)
        )

        # Instrument pool acquire timing: wrap create_client to measure
        # time from creation request to connection available.
        # With mocked conn this is ~0, but the measurement pipeline captures it.
        async def _single_request(ri: int):
            """Create a fresh client and measure pool wait time."""
            from tests.conftest import create_client
            acquire_start = time.perf_counter()
            async with create_client(mock_conn) as c:
                acquire_ms = (time.perf_counter() - acquire_start) * 1000
                result = await _measure_endpoint(
                    c, mock_conn, method, path, label, params
                )
            result["pool_wait_ms"] = round(acquire_ms, 3)
            return result

        async with client:
            tasks = []
            start = time.perf_counter()
            for _ in range(_CONCURRENT_USERS_MAX):
                tasks.append(_single_request(_))
            all_results = await asyncio.gather(*tasks)
            wall_clock_ms = (time.perf_counter() - start) * 1000

        durations = [r["duration_ms"] for r in all_results]
        pool_waits = [r.get("pool_wait_ms", 0.0) for r in all_results]
        errors = [r for r in all_results if r["error"] is not None]

        error_rate = len(errors) / len(all_results) if all_results else 0.0

        # Per the contract: concurrent test bounds concurrency in CI
        assert error_rate <= 0.1, (
            f"Concurrent error rate {error_rate:.2%} exceeds 10% threshold. "
            f"Errors: {errors[:5]}"
        )

        # Record results — write-once, read on subsequent runs
        concurrent_data = {
            "concurrent_users": _CONCURRENT_USERS_MAX,
            "wall_clock_ms": round(wall_clock_ms, 3),
            "p50_ms": round(_p50(durations), 3),
            "p95_ms": round(_p95(durations), 3),
            "mean_ms": round(statistics.mean(durations) if durations else 0, 3),
            "min_ms": round(min(durations), 3),
            "max_ms": round(max(durations), 3),
            "pool_wait_p50_ms": round(_p50(pool_waits), 3),
            "pool_wait_p95_ms": round(_p95(pool_waits), 3),
            "error_rate": round(error_rate, 4),
            "error_count": len(errors),
        }
        baseline = _load_or_commit_baseline(
            _BASELINE_DIR / "concurrent_baseline.json", concurrent_data
        )

        # If baseline exists, assert no regression
        if baseline is not None:
            regressions = []
            wall_clock_threshold = baseline["wall_clock_ms"] * _REGRESSION_FACTOR
            if wall_clock_ms > wall_clock_threshold:
                regressions.append(
                    f"  wall_clock: {round(wall_clock_ms, 3)}ms > "
                    f"{round(wall_clock_threshold, 3)}ms "
                    f"(baseline {baseline['wall_clock_ms']}ms × {_REGRESSION_FACTOR})"
                )
            current_p95 = _p95(durations)
            p95_threshold = baseline["p95_ms"] * _REGRESSION_FACTOR
            if current_p95 > p95_threshold:
                regressions.append(
                    f"  p95: {round(current_p95, 3)}ms > "
                    f"{round(p95_threshold, 3)}ms "
                    f"(baseline {baseline['p95_ms']}ms × {_REGRESSION_FACTOR})"
                )
            if regressions:
                msg = (
                    "Performance regression detected in concurrent-user scenario:\n"
                    + "\n".join(regressions)
                    + "\n\nCurrent results:\n"
                    + json.dumps(concurrent_data, indent=2)
                )
                pytest.fail(msg)

    @pytest.mark.asyncio
    async def test_synthetic_dashboard_wall_clock(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Measure total wall-clock for parallel fetch of all 8 API calls.

        Simulates Aurora Glass's Promise.allSettled pattern for initial load
        and 30-second refresh paths.
        """
        async def _fetch_all_endpoints(c: AsyncClient) -> list[dict]:
            """Fetch all dashboard endpoints in parallel."""
            tasks = []
            for method, path, params, label in _DASHBOARD_ENDPOINTS:
                sid = _SYNTH_SESSION_ID if "{session_id}" in path else None
                tasks.append(
                    _measure_endpoint(c, mock_conn, method, path, label, params, sid)
                )
            return await asyncio.gather(*tasks)

        # Reset mocks to factory configuration (handles all 8 endpoints)
        _setup_synthetic_mocks(mock_conn)

        async with client as c:
            # Initial load
            start = time.perf_counter()
            initial_results = await _fetch_all_endpoints(c)
            initial_wall_clock_ms = (time.perf_counter() - start) * 1000

            # 30-second refresh path (second call simulates refresh)
            start = time.perf_counter()
            refresh_results = await _fetch_all_endpoints(c)
            refresh_wall_clock_ms = (time.perf_counter() - start) * 1000

        initial_errors = [r for r in initial_results if r["error"] is not None]
        refresh_errors = [r for r in refresh_results if r["error"] is not None]

        # Both loads should complete with minimal errors
        assert len(initial_errors) <= 1, (
            f"Initial load had {len(initial_errors)} errors: {initial_errors}"
        )
        assert len(refresh_errors) <= 1, (
            f"Refresh load had {len(refresh_errors)} errors: {refresh_errors}"
        )

        # Write-once baseline
        dashboard_data = {
            "initial_load_ms": round(initial_wall_clock_ms, 3),
            "refresh_load_ms": round(refresh_wall_clock_ms, 3),
            "initial_endpoints": len(initial_results),
            "initial_error_count": len(initial_errors),
            "refresh_error_count": len(refresh_errors),
            "endpoints": [r["endpoint"] for r in initial_results],
        }
        baseline = _load_or_commit_baseline(
            _BASELINE_DIR / "dashboard_wall_clock_baseline.json", dashboard_data
        )

        # If baseline exists, assert no regression
        if baseline is not None:
            regressions = []
            initial_threshold = baseline["initial_load_ms"] * _REGRESSION_FACTOR
            if initial_wall_clock_ms > initial_threshold:
                regressions.append(
                    f"  initial_load: {round(initial_wall_clock_ms, 3)}ms > "
                    f"{round(initial_threshold, 3)}ms "
                    f"(baseline {baseline['initial_load_ms']}ms × {_REGRESSION_FACTOR})"
                )
            refresh_threshold = baseline["refresh_load_ms"] * _REGRESSION_FACTOR
            if refresh_wall_clock_ms > refresh_threshold:
                regressions.append(
                    f"  refresh_load: {round(refresh_wall_clock_ms, 3)}ms > "
                    f"{round(refresh_threshold, 3)}ms "
                    f"(baseline {baseline['refresh_load_ms']}ms × {_REGRESSION_FACTOR})"
                )
            if regressions:
                msg = (
                    "Performance regression detected in dashboard wall-clock scenario:\n"
                    + "\n".join(regressions)
                    + "\n\nCurrent results:\n"
                    + json.dumps(dashboard_data, indent=2)
                )
                pytest.fail(msg)


# ══════════════════════════════════════════════════════════════════════════════
#  Production-shaped fixture — profiling mode (no CI assertion)
# ══════════════════════════════════════════════════════════════════════════════


def _setup_production_shaped_mocks(mock_conn: AsyncMock) -> None:
    """Configure mock_conn with anonymized, larger production-shaped data.

    High-volume: 1000+ records, 200 sessions, 100 agent runs, multiple
    children and todos per session.  Uses SQL-inspecting factory functions
    so mock responses never exhaust across repeated iterations.
    """
    now = datetime(2025, 7, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

    # ── fetchrow: aggregate for most, detail for session-lookup ──
    _agg_row = _mk_aggregate_row(
        group_value="model_0", record_count=100, session_count=20, model_count=1
    )
    _detail_id = uuid.uuid4()
    _detail_row = _mk_session_row(
        session_id=_detail_id,
        external_session_id="ses_prd_detail_001",
        last_message_at=now - timedelta(minutes=3),
        message_count=50,
        agent="code-editor-senior",
        session_model="claude-sonnet-4-20250514",
        ctx_present=1,
        ctx_title="Production Session",
        ctx_session_model="claude-sonnet-4-20250514",
        code_change_count=25,
        code_change_additions=500,
        code_change_deletions=200,
        parent_session_id=None,
        parent_internal_id=None,
    )

    def _fetchrow_fn(*args, **kwargs):
        sql = str(args[0]) if args else ""
        if "s.id = $1" in sql:
            return _detail_row
        if "collector_credentials" in sql:
            cred_row = MagicMock()
            cred_data = {
                "credential_id": uuid.uuid4(),
                "revoked_at": None,
                "last_used_at": now,
                "client_id": uuid.uuid4(),
                "client_name": "test-client",
                "client_is_active": True,
            }
            cred_row.__getitem__.side_effect = cred_data.__getitem__
            return cred_row
        if "source_databases" in sql:
            db_row = MagicMock()
            db_data = {
                "last_seen_at": now,
                "record_count": 1000,
                "is_active": True,
            }
            db_row.__getitem__.side_effect = db_data.__getitem__
            return db_row
        return _agg_row

    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_fn)

    # ── fetchval: 1000 records ──
    mock_conn.fetchval = AsyncMock(return_value=1000)

    # ── fetch: regenerate fresh rows per SQL shape ──
    def _fetch_fn(*args, **kwargs):
        sql = str(args[0]) if args else ""

        if "opencode_session_todos" in sql:
            return [
                _mk_todo_row(
                    content=f"Task {i}: description of work item number {i}",
                    status=["completed", "in_progress", "pending", "blocked"][i % 4],
                    priority=str((i % 5) + 1),
                    position=i,
                )
                for i in range(20)
            ]
        if "parent_session_id = $1" in sql and "ORDER BY last_message_at" in sql:
            return [
                _mk_child_row(
                    external_session_id=f"ses_child_{i:03d}",
                    agent=f"subagent_{i % 4}",
                    last_message_at=now - timedelta(hours=i + 1),
                )
                for i in range(5)
            ]
        # Usage records queries (must match BEFORE sessions join to avoid wrong-shape rows).
        if "usage_events" in sql:
            rows = []
            for i in range(1000):
                r = _mk_record_row(
                    record_id=uuid.uuid4(),
                    session_id=uuid.uuid4(),
                    model_name=f"model_{i % 5}",
                    reported_at=now - timedelta(hours=i),
                )
                _orig = r.__getitem__.side_effect

                def _augmented(k, orig=_orig):
                    try:
                        return orig(k)
                    except KeyError:
                        pass
                    return {
                        "agent": "code-editor-senior",
                        "session_title": "Production Session",
                        "project_label": "prod-project",
                    }[k]

                r.__getitem__.side_effect = _augmented
                rows.append(r)
            return rows
        if "LEFT JOIN opencode_session_contexts" in sql and "sessions s" in sql:
            return [
                _mk_session_row(
                    session_id=uuid.uuid4(),
                    external_session_id=f"ses_prd_{i:04d}",
                    last_message_at=now - timedelta(minutes=i * 5),
                    message_count=(i % 30) + 1,
                    agent=f"agent_{i % 8}",
                    child_run_count=i % 5,
                    session_model=f"model_{i % 4}",
                )
                for i in range(100)
            ]
        if "FROM sessions s" in sql:
            return [
                _mk_usage_session_row(
                    session_id=uuid.uuid4(),
                    first_message_at=now - timedelta(days=i % 30),
                    last_message_at=now - timedelta(hours=i),
                    message_count=(i % 20) + 1,
                    project_id=f"proj_{i % 10}" if i % 3 == 0 else None,
                    agent=f"agent_{i % 5}" if i % 2 == 0 else None,
                )
                for i in range(200)
            ]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch_fn)


@pytest.mark.profiling
class TestProductionShapedProfiling:
    """Profiling benchmarks on production-shaped (anonymized, high-volume) data.

    Measurement only — no CI assertion.  Excluded from default test runs via
    the ``profiling`` marker and the ``addopts = ["-m", "not profiling"]``
    setting in ``pyproject.toml``.  Run explicitly with ``pytest -m profiling``.

    Output artifacts are written to ``tests/fixtures/profiling-output/``
    (gitignored directory) so normal test runs leave the tree clean.
    """

    @pytest.fixture(autouse=True)
    def _setup_mocks(self, mock_conn: AsyncMock) -> None:
        _setup_production_shaped_mocks(mock_conn)

    @pytest.mark.asyncio
    async def test_production_single_session_all_endpoints(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Profile p50/p95 for all 8 dashboard endpoints.

        Records total duration, DB time, status computation time, query count.
        Results are logged but not asserted.
        """
        results: dict[str, dict] = {}

        async with client as c:
            for method, path, params, label in _DASHBOARD_ENDPOINTS:
                sid = uuid.uuid4() if "{session_id}" in path else None
                results[label] = await _measure_endpoint_repeated(
                    c, mock_conn, method, path, label, params,
                    session_id_override=sid,
                    iterations=3,  # fewer iterations for profiling
                    warmup=1,
                )

        # Store profiling results in gitignored output directory
        _PROFILING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        profile_file = _PROFILING_OUTPUT_DIR / "profiling_results.json"
        profile_file.write_text(json.dumps(results, indent=2))

        # Log summary (informational only — no assertion)
        for label, r in sorted(results.items()):
            logging.getLogger(__name__).info(
                "Profiling %s: p50=%.1fms p95=%.1fms db=%.1fms comp=%.1fms queries=%d errors=%d",
                label, r["p50_ms"], r["p95_ms"],
                r["db_time_ms"], r["status_computation_time_ms"],
                r["query_count"], r["error_count"],
            )

    @pytest.mark.asyncio
    async def test_production_concurrent_users(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Profile 10 parallel requests for latency, pool wait, error rate."""
        endpoint = ("GET", "/api/v1/usage/aggregates",
                    {"start_date": "2025-07-01T00:00:00Z", "end_date": "2025-07-31T23:59:59Z"},
                    "aggregates")
        method, path, params, label = endpoint

        mock_conn.fetchrow = AsyncMock(
            return_value=_mk_aggregate_row(group_value="total", record_count=1000)
        )

        async with client as c:
            tasks = []
            start = time.perf_counter()
            for _ in range(_CONCURRENT_USERS_MAX):
                tasks.append(
                    _measure_endpoint(c, mock_conn, method, path, label, params)
                )
            all_results = await asyncio.gather(*tasks)
            wall_clock_ms = (time.perf_counter() - start) * 1000

        durations = [r["duration_ms"] for r in all_results]
        errors = [r for r in all_results if r["error"] is not None]

        profile_data = {
            "concurrent_users": _CONCURRENT_USERS_MAX,
            "wall_clock_ms": round(wall_clock_ms, 3),
            "p50_ms": round(_p50(durations), 3),
            "p95_ms": round(_p95(durations), 3),
            "mean_ms": round(statistics.mean(durations) if durations else 0, 3),
            "pool_wait_p50_ms": 0.0,
            "pool_wait_p95_ms": 0.0,
            "error_rate": round(len(errors) / len(all_results), 4) if all_results else 0.0,
        }
        _PROFILING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (_PROFILING_OUTPUT_DIR / "profiling_concurrent.json").write_text(
            json.dumps(profile_data, indent=2)
        )

    @pytest.mark.asyncio
    async def test_production_dashboard_wall_clock(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Profile total dashboard wall-clock on production-shaped data."""
        async def _fetch_all(c: AsyncClient) -> list[dict]:
            tasks = []
            for method, path, params, label in _DASHBOARD_ENDPOINTS:
                sid = uuid.uuid4() if "{session_id}" in path else None
                tasks.append(
                    _measure_endpoint(c, mock_conn, method, path, label, params, sid)
                )
            return await asyncio.gather(*tasks)

        async with client as c:
            start = time.perf_counter()
            initial = await _fetch_all(c)
            initial_ms = (time.perf_counter() - start) * 1000

            start = time.perf_counter()
            refresh = await _fetch_all(c)
            refresh_ms = (time.perf_counter() - start) * 1000

        _PROFILING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (_PROFILING_OUTPUT_DIR / "profiling_dashboard_wall_clock.json").write_text(
            json.dumps({
                "initial_load_ms": round(initial_ms, 3),
                "refresh_load_ms": round(refresh_ms, 3),
                "initial_endpoints": len(initial),
                "initial_errors": sum(1 for r in initial if r["error"]),
                "refresh_errors": sum(1 for r in refresh if r["error"]),
            }, indent=2)
        )
