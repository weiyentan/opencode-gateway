# PRD: Aurora Glass Backend Performance Optimization

**Origin:** Grill-with-docs session (2026-08-06)
**Status:** Draft
**Created:** 2026-08-06

---

## Problem Statement

The Aurora Glass dashboard's backend read APIs are slow. End-to-end API response times average over 13 seconds, while database queries individually complete in 18–89ms. The application layer — query construction, status computation, and multi-query sequential execution — is the primary bottleneck, not the database itself.

Key symptoms:

1. **Agent Run Detail executes 5 sequential queries** — each detail request hits the database 5 times (session, parent, children, context, todos) with no batching or consolidation.
2. **No request-level timing exists** — there is no middleware or instrumentation to measure where time is spent in any endpoint.
3. **Connection pool lacks asyncpg-native tuning** — `pool_recycle` and `pool_pre_ping` are SQLAlchemy concepts not applicable to asyncpg; the current pool config has no stale-connection detection or recycling.
4. **Migration 0019 effectiveness is unproven** — 8 indexes were prepared but never measured against the actual query plans used by the dashboard read endpoints.
5. **Status logic is duplicated** — `_compute_status()` (Python) and `_status_case_expression()` (SQL) must be kept in sync manually, creating drift risk.
6. **No concurrent-user testing exists** — single-user benchmarks do not reveal pool saturation or contention.

## Solution

A schema-neutral backend performance optimization that adds permanent structured timing, profiles and reduces query counts, measures migration impact, and tunes connection pool behavior — all without changing API response schemas or introducing new tables.

### Structured Timing Infrastructure

Add a shared timing module under `app/core/` that provides:

- **Request-level middleware**: total request duration, endpoint name, HTTP status code, correlation ID.
- **Operation-level helpers**: database query duration, `_compute_status` duration, and any synchronous external call durations.
- **Structured log emission**: stable event names and fields; no raw SQL, session IDs, tokens, or response bodies logged.
- **Layered timeout helpers**: bounded timeouts for database queries, external calls, status computation, and total endpoint duration.

Use `time.perf_counter()` and structured logging only. No new dependencies.

### Read Path Optimization

Profile and optimize the existing read endpoints in `app/api/usage.py`:

- **Agent Run Detail** (`_fetch_agent_run_detail`): consolidate 5 sequential queries into fewer CTEs or JOINs where possible.
- **Agent Run List** (`_fetch_agent_runs`): audit query plan and reduce query count.
- **Sessions** (`_fetch_sessions`): audit for N+1 patterns and query-count reduction.
- **Aggregates** (`_fetch_aggregates`): verify single-query path is optimal.
- **Health** (`_health_summary`): instrument timing around collector and source-database probes.

Use raw SQL profiling (`EXPLAIN ANALYZE BUFFERS`) against actual dashboard queries. Fix N+1 behavior by rewriting or batching raw SQL, selecting only required columns, and applying limits before materialization.

### Migration 0019 Measurement

> **Status (issue #365, 2026-08-15): completed.** Per-index keep/drop
> decisions with before/after `EXPLAIN` plans and latencies are documented
> in [ADR 0017](../adr/0017-migration-0019-index-measurement.md): indexes
> #4–#8 retained, indexes #1–#3 (`opencode_usage_records`) recorded for
> removal in a follow-up migration.

Evaluate migration 0019 in isolation:

1. Establish a baseline on the current schema.
2. Apply migration 0019 in a comparable environment.
3. Rerun the same workload.
4. Capture query plans and latency before/after.
5. Keep indexes only if they measurably improve dashboard read latency.

Do not assume database is the bottleneck. The primary optimization target is the application layer.

### Asyncpg Pool Tuning

Add asyncpg-native pool settings:

- `database_max_inactive_connection_lifetime` — recycle stale connections (asyncpg-native replacement for SQLAlchemy's `pool_recycle`).
- `database_connection_timeout` — already exists (default 30s); validate under concurrent load.

Add configurable timeout budgets as `GATEWAY_` environment variables:

- `GATEWAY_DATABASE_TIMEOUT_SECONDS` — per-query budget.
- `GATEWAY_STATUS_COMPUTATION_TIMEOUT_SECONDS` — `_compute_status` budget.
- `GATEWAY_TOTAL_REQUEST_TIMEOUT_SECONDS` — endpoint total budget (must be less than 30s ceiling).

Measure pool saturation, wait time, and stale-connection behavior before changing settings. Change only if evidence reproduces the problem on the read path.

### Runtime Algorithm Switch

For materially different algorithms (e.g., consolidated vs. original query), provide a temporary runtime switch (environment variable or config flag) for side-by-side comparison and rollback. Do not add flags for simple query/index or instrumentation changes.

## User Stories

1. As a backend developer, I want structured timing logs for every dashboard read request, so that I can identify where time is spent without ad-hoc profiling.
2. As a backend developer, I want per-operation timing (database, status computation, external calls), so that I can attribute latency to the correct layer.
3. As a backend developer, I want layered timeout budgets for each operation, so that a slow dependency cannot consume the entire request budget.
4. As a backend developer, I want the Agent Run Detail endpoint to execute fewer queries, so that detail-overlay latency is reduced.
5. As a backend developer, I want raw SQL query plans profiled against actual dashboard queries, so that optimization is evidence-based.
6. As a backend developer, I want migration 0019 measured in isolation, so that index effectiveness is proven rather than assumed.
7. As a backend developer, I want asyncpg pool saturation and stale-connection behavior measured, so that pool settings are changed only when justified.
8. As a backend developer, I want pool recycling configured using asyncpg-native settings, so that "Connection reset by peer" errors are addressed without SQLAlchemy assumptions.
9. As a backend developer, I want a temporary runtime switch for materially different algorithms, so that old and new paths can be compared safely.
10. As a backend developer, I want no new tables or columns added, so that schema complexity is not increased prematurely.
11. As a backend developer, I want all existing API schemas, status semantics, filtering, pagination, and ordering preserved exactly, so that the frontend session can proceed without changes.
12. As a backend developer, I want both isolated and concurrent-user testing, so that pool contention and saturation are visible.
13. As a backend developer, I want both a deterministic synthetic fixture and a production-shaped anonymized dataset for benchmarks, so that CI regression checks and realistic profiling are both covered.
14. As a backend developer, I want `_compute_status` thresholds verified for alignment between Python and SQL, so that status semantics do not drift.
15. As a backend developer, I want the health endpoint instrumented for timing, so that Operational Events probe latency is visible.
16. As a backend developer, I want temporary full-rate timing logs during the baseline period, so that volume can be reduced after the baseline is established.
17. As a backend developer, I want structured log fields that can later be aggregated into metrics, so that adding a metrics exporter is a drop-in upgrade.
18. As a backend developer, I want the same timing infrastructure used across all read endpoints, so that instrumentation is consistent and maintainable.
19. As a backend developer, I want benchmark results captured as artifacts, so that optimization impact can be reviewed and compared across iterations.
20. As a backend developer, I want query-count reduction to be the primary optimization, before adding caching or precomputation.

## Implementation Decisions

### Modules to Build/Modify

1. **`app/core/telemetry.py`** (new) — Shared timing infrastructure: request middleware, operation helpers, layered timeout helpers, structured log emission.
2. **`app/api/usage.py`** (modify) — Add instrumentation wrappers around each read handler; optimize `_fetch_agent_run_detail` query count; audit `_fetch_agent_runs`, `_fetch_sessions`, `_fetch_aggregates`.
3. **`app/api/health.py`** (modify) — Add timing around health check probes.
4. **`app/core/config.py`** (modify) — Add asyncpg pool settings and timeout budget settings.
5. **`app/db/session.py`** (modify) — Pass new pool settings to asyncpg pool constructor.
6. **`tests/test_telemetry.py`** (new) — Unit tests for timing infrastructure.
7. **`tests/test_read_path_perf.py`** (new) — Read-path benchmark and regression tests.
8. **`tests/test_agent_runs.py`** (modify) — Add regression coverage for optimized queries.
9. **`tests/test_usage.py`** (modify) — Add regression coverage for optimized queries.

### API Contracts

- No new endpoints.
- No response schema changes.
- No status semantics changes.
- No filtering, pagination, or ordering changes.
- All existing tests must continue to pass.

### Schema Changes

- None beyond validating migration 0019 (measurement only, not part of this PRD's implementation).
- No new tables, columns, materialized views, or indexes.

### Instrumentation Architecture

- Request-level timing captured via FastAPI middleware or `BaseHTTPMiddleware`.
- Operation-level timing captured via context-manager helpers (e.g., `async with timed_operation("db_query"):`).
- Each timing event emits: event name, endpoint, operation type, duration_ms, success/failure, correlation_id.
- Correlation ID propagated from request header or generated per-request.

### Timeout Budget Hierarchy

```
total endpoint budget (e.g., 20s)
  └── database query budget (e.g., 5s)
  └── status computation budget (e.g., 2s)
  └── external call budget (e.g., 3s) [if any]
```

Exact values chosen after baseline profiling, not guessed upfront.

### Connection Pool Configuration

```python
# asyncpg-native pool settings (via app/core/config.py)
database_min_connections = 2          # existing
database_max_connections = 10         # existing
database_connection_timeout = 30      # existing
database_max_inactive_connection_lifetime = 1800  # new, default 1800s (30 min)
```

### Status Logic Alignment

- Verify `_compute_status()` Python thresholds match `_status_case_expression()` SQL thresholds.
- Document both in one place.
- If drift is found, fix it but do not change the intended semantics.

### Migration 0019 Evaluation Protocol

1. Capture baseline: run benchmark on current schema, record query plans and latencies.
2. Apply migration 0019.
3. Rerun identical benchmark.
4. Compare: query plans, query latencies, total endpoint latencies.
5. Decision: keep all indexes that measurably improve at least one read endpoint; drop any that do not.

### Logging Policy

- **Baseline period**: record all included read requests.
- **Post-baseline**: reduce to slow-request threshold + error logging + low-rate sampling.
- **Never log**: raw SQL, session IDs, tokens, response bodies, or PII.

## Testing Decisions

### Good Test Characteristics

- Tests external behavior (response shape, status semantics, timing fields) rather than implementation details.
- Uses mock-based fixtures following existing `conftest.py` patterns (`mock_conn`, `_mk_session_row()`, etc.).
- Benchmark tests use deterministic synthetic data for CI and production-shaped anonymized data for realistic profiling.

### Modules Tested

1. **`tests/test_telemetry.py`** — Timing middleware, operation helpers, timeout helpers, structured log emission. Mock-based, no real DB.
2. **`tests/test_read_path_perf.py`** — Benchmark tests: synthetic fixture, production-shaped dataset, single-session and concurrent-user scenarios, migration 0019 before/after.
3. **`tests/test_agent_runs.py`** — Extend existing tests to cover optimized query paths. Verify `_compute_status` alignment.
4. **`tests/test_usage.py`** — Extend existing tests to cover optimized query paths. Verify no regressions in response shape.

### Prior Art

- Existing test files use `httpx.AsyncClient` with `ASGITransport(app=app)` for HTTP-level tests.
- Database mocked via `app.dependency_overrides[get_session]` with `conftest.py` fixtures.
- Row builders (`_mk_session_row()`, `_mk_aggregate_row()`) return `MagicMock` objects with `__getitem__.side_effect`.
- Auth tests set `GATEWAY_API_KEY` in `os.environ`.

### Benchmark Test Structure

- **Synthetic fixture**: deterministic dataset, small, fast, runs in CI. Asserts no regression against previous baseline.
- **Production-shaped dataset**: anonymized, realistic row counts and distributions. Used for profiling, not CI assertion.
- **Concurrent-user test**: bounded concurrency (e.g., 5–10 parallel requests), measures pool saturation and contention.
- **Migration 0019 comparison**: same workload, two schema states, compared side by side.

## Out of Scope

- **Frontend changes**: Aurora Glass UI, caching, renaming, freshness indicators — tracked separately.
- **Ingestion path**: `/ingest` endpoint, Kafka consumer, collector traffic.
- **Schema redesign**: No new tables, columns, materialized views, or indexes (beyond validating migration 0019).
- **Broad response caching**: Deferred unless profiling proves `_compute_status` is a hotspot.
- **Metrics exporter**: Structured logs only; metrics pipeline is a future upgrade.
- **New API endpoints**: No new endpoints created.
- **Feature flags for all changes**: Runtime switches only for materially different algorithms.
- **Real-time streaming**: WebSocket/SSE for Operational Events.
- **Mobile or third-party integrations**.
- **UI theme or visual redesign**.

## Further Notes

1. **Schema-neutral optimization**: The entire first pass operates on existing tables and queries. If profiling later proves that read-time computation requires precomputation, that decision will be made with evidence and tracked as a separate effort.

2. **Raw SQL throughout**: All queries are hand-written with asyncpg. The `selectinload`/`joinedload` advice from the original handoff does not apply. Optimization uses raw SQL rewriting, CTEs, JOINs, and column selection.

3. **External API calls are out of scope**: The handoff's 5–6 second external API finding relates to the Kafka consumer path (`/ingest`), which is explicitly excluded. The dashboard read path has no synchronous external HTTP calls — only database queries and Python computation.

4. **No feature flags for simple changes**: Only materially different algorithms (e.g., consolidated vs. original query) get a runtime switch. Simple index, query, and instrumentation changes ship directly.

5. **Baseline before optimization**: Establish timing baselines before any optimization. All subsequent changes are measured against the baseline, not against each other.

6. **`_compute_status` is a heuristic**: The status computation is best-effort (no authoritative terminal signal from OpenCode). Optimizing its performance must not change its semantics.
