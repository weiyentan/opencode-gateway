# ADR 0017: Migration 0019 index keep/drop decisions (measured)

## Status

Accepted (2026-08-15)

## Context

Migration `0019_add_performance_indexes.py` (issue #365) added 8
non-unique, `CONCURRENTLY`-created indexes targeting the Aurora Glass
dashboard read paths:

| # | Index | Table / columns |
|---|-------|-----------------|
| 1 | `ix_opencode_usage_records_reported_at` | `opencode_usage_records (reported_at)` |
| 2 | `ix_opencode_usage_records_client_reported_at` | `opencode_usage_records (client_id, reported_at)` |
| 3 | `ix_opencode_usage_records_session_reported_at` | `opencode_usage_records (session_id, reported_at)` |
| 4 | `ix_sessions_client_last_message_at` | `sessions (client_id, last_message_at)` |
| 5 | `ix_sessions_parent_last_message_at` | `sessions (parent_session_id, last_message_at)` |
| 6 | `ix_opencode_session_contexts_session_id` | `opencode_session_contexts (session_id)` |
| 7 | `ix_opencode_session_todos_source_session_position` | `opencode_session_todos (source_database_id, external_session_id, position)` |
| 8 | `ix_opencode_source_projects_source_project` | `opencode_source_projects (source_database_id, external_project_id)` |

Issue #365's evaluation protocol is: baseline the schema **without**
0019, apply 0019, re-run an identical workload, compare `EXPLAIN ANALYZE
BUFFERS` plans and per-query/endpoint latencies, then keep each index
that measurably improves at least one read endpoint and record the rest
as drop decisions for a follow-up migration.

Two facts constrain the measurement:

1. **The #364 harness is mock-DB.** `tests/test_read_path_perf.py`
   measures application-layer latency against `AsyncMock` connections.
   It cannot observe index presence, so it was used only as a
   regression smoke test, not as evidence of index impact. All index
   evidence below comes from a **live PostgreSQL** (`EXPLAIN (ANALYZE,
   BUFFERS)` + timed executions).
2. **The canonical accounting layer moved the read source.** Since
   migration 0021, the usage query endpoints read from `usage_events`,
   not `opencode_usage_records` ("still written but no longer the query
   source"). Indexes #1–3 target the legacy table. The honest
   "baseline without 0019" for the *current* read path is therefore
   head schema minus the 8 indexes, not revision 0018 (which predates
   `usage_events` and would make the read path fail).

## Measurement method

- **Environment:** live PostgreSQL 15 (the `docker-compose.test.yml`
  image) started with podman on host port 5433. Migrations applied with
  `alembic upgrade head` (revisions 0000–0028); the 8 indexes verified
  present with `pg_indexes`.
- **Dataset:** 5 clients / 5 source databases / 5 source identities /
  4 models, 5 000 sessions, 5 000 session contexts, 15 000 todos, 200
  source projects, 50 000 `usage_events`, 6 199 rollup rows, 20 000
  legacy `opencode_usage_records`, `ANALYZE`d. Fixed window
  2025-07-01 → 2025-07-31 (the harness's default dashboard range).
- **Workload:** for each of the 8 read endpoints' underlying queries
  (mirroring `app/api/usage.py` verbatim), capture `EXPLAIN (ANALYZE,
  BUFFERS)` once and time 10 warm-cache executions.
- **Baseline state:** the same schema with the 8 indexes dropped
  (`DROP INDEX` × 8) — everything else identical, so the only variable
  is migration 0019.
- **Query counts:** unchanged by construction — no application code was
  modified; indexes only change plans, never the number of statements
  issued per endpoint.

Latencies below are **p50 (median of 10 warm-cache runs, milliseconds)**.
The dataset is deliberately small (~95 k rows), so several endpoints are
sub-millisecond and noisy; the `EXPLAIN` plan changes are the reliable
signal, and the timings are reported as observed.

| Query (endpoint) | With 0019 | Baseline (no 0019) | Plan change |
|---|---|---|---|
| `aggregates` total | 58.3 | 52.8 | none (identical) |
| `aggregates` grouped (model) | 90.4 | 89.8 | none (identical) |
| `aggregates` client,project rollup | 12.1 | 12.4 | none (identical) |
| `records` count | 17.2 | 18.6 | none |
| `records` data | 54.3 | 57.9 | none (identical) |
| `sessions` count | 1.27 | 0.88 | none |
| `sessions` count (client filter) | 0.60 | 0.82 | none |
| `sessions` data | 10.8 | 9.9 | none (identical) |
| `sessions` data (**client filter**) | **1.5** | **4.6** | **Seq Scan + hash joins → Index Scan Backward (#4) + Index Scans (#6, #8)** |
| `agent-runs` count | 0.63 | 0.53 | none |
| `agent-runs` count (client filter) | 0.37 | 0.79 | none |
| `agent-runs` data | 74.5 | 62.9 | child_counts CTE: Seq Scan + HashAggregate → **Index Only Scan (#5)** |
| `agent-runs` data (client filter) | 18.8 | 20.4 | base: Seq Scan → **Bitmap Index Scan (#4)** |
| `agent-runs/{id}` session | 0.79 | 1.17 | contexts join: Seq Scan (4 999 rows removed) → **Index Scan (#6)** |
| `agent-runs/{id}` children | **0.28** | **1.00** | **Seq Scan + Sort → Index Scan Backward (#5)** |
| `agent-runs/{id}` todos | **0.29** | **1.85** | **Seq Scan + Sort → Index Scan (#7)** |
| `records-with-context` count | 26.4 | 23.9 | none |
| `records-with-context` data | 62.0 | 51.7 | none (identical) |

The parallel `records`/`records-with-context` data queries show ±10 ms
run-to-run variance with byte-identical plans — measurement noise from
parallel sequential scans, not index effect.

## Decision

### Drop — indexes #1–3 (`opencode_usage_records`)

`ix_opencode_usage_records_reported_at`,
`ix_opencode_usage_records_client_reported_at`,
`ix_opencode_usage_records_session_reported_at`.

**Rationale:** No dashboard read endpoint reads `opencode_usage_records`.
The 8 endpoints read the canonical `usage_events` table (migration
0021). A repository grep shows `opencode_usage_records` is read only by
the ingest write/dedup path (`app/api/ingest.py`, which uses the unique
constraint `uq_opencode_usage_records_dedup` on
`(client_id, source_database_id, source_record_id)` — never these three
indexes) and by one-off backfill scripts (`scripts/backfill_usage_events.py`,
`scripts/backfill_cache_write_tokens.py`). No `EXPLAIN` plan in the
workload referenced any of them. The table is still written on every
ingest, so these three indexes are pure write-maintenance overhead with
zero read benefit.

**Recorded for follow-up migration** (not executed here): drop indexes
#1–3 from `opencode_usage_records`.

### Keep — index #4 (`sessions (client_id, last_message_at)`)

**Rationale:** With a `client_id` filter — a first-class filter on both
`/sessions` and `/agent-runs` — this index serves the filter *and* the
`ORDER BY last_message_at DESC` in a single backward index scan.
`sessions` data with client filter: 4.6 ms → 1.5 ms (Seq Scan + top-N
sort → Index Scan Backward, 112 → 51 buffers). `agent-runs` data with
client filter uses a Bitmap Index Scan on the same index.

### Keep — index #5 (`sessions (parent_session_id, last_message_at)`)

**Rationale:** Two read paths. `agent-runs/{id}` children:
`WHERE parent_session_id = $1 ORDER BY last_message_at DESC` — 1.00 ms →
0.28 ms (Seq Scan over 5 000 rows + Sort → Index Scan Backward, 112 → 4
buffers). The `agent-runs` list's `child_counts` CTE (evaluated on every
list request) runs `GROUP BY parent_session_id` — Seq Scan + HashAggregate
(112 buffers) → Index Only Scan (11 buffers).

### Keep — index #6 (`opencode_session_contexts (session_id)`)

**Rationale:** Serves the `s.id = osc.session_id` join used by
`/sessions`, `/agent-runs`, and `agent-runs/{id}`. `agent-runs/{id}`
session query: 0.57 ms → 0.015 ms for the context join (Seq Scan removing
4 999 rows → single Index Scan). Also used per-row in the client-filtered
`sessions` data nested loop. It is **not** used by `records` /
`records-with-context` — those join contexts on
`(source_database_id, external_session_id)`, not `session_id` — nor on
full-table scans (the planner prefers hash joins over all 5 000 rows).

### Keep — index #7 (`opencode_session_todos (source_database_id, external_session_id, position)`)

**Rationale:** `agent-runs/{id}` todos:
`WHERE source_database_id = $1 AND external_session_id = $2 ORDER BY position`
— 1.85 ms → 0.29 ms (Seq Scan + Sort, 320 buffers → Index Scan, 3
buffers); the index also satisfies the `ORDER BY position`. It is **not**
used by the `agent-runs` list `todo_counts` CTE, which aggregates the
entire todos table joined to the full session universe and therefore
stays a full-table hash aggregation regardless of the index — the
dominant cost of the unfiltered agent-runs list is that CTE, not a
lookup.

### Keep — index #8 (`opencode_source_projects (source_database_id, external_project_id)`)

**Rationale:** Serves the `osp.source_database_id = s.source_database_id
AND osp.external_project_id = s.project_id` join used by `/sessions`,
`/agent-runs`, `agent-runs/{id}`, and `records-with-context`. Used as a
Memoize + Index Scan in the client-filtered `sessions` data query, and
its presence steers `agent-runs/{id}` from an inefficient nested loop
(199 rows removed by join filter) to a cheap hash join. On full-table
scans the 200-row projects table is hash-joined and the index is not
needed. (Note: the client,project rollup path's LATERAL lookup matches
projects on `(client_id, external_project_id)`, served by the unique
constraint `uq_opencode_source_projects_source_key`, not this index.)

## Consequences

### Positive

- Three dead indexes on the legacy `opencode_usage_records` table are
  identified for removal, eliminating write-maintenance overhead on a
  table that is still written on every ingest but never read by the
  dashboard.
- Five indexes are confirmed to serve real read paths and are retained
  with evidence, notably the client-filtered `sessions` list and the
  `agent-runs/{id}` children/todos point lookups.

### Negative / limits of the measurement

- The evidence is on a small (~95 k row) synthetic dataset. On a small
  table PostgreSQL frequently prefers sequential scans / hash joins, so
  the absolute wins are sub-millisecond to a few milliseconds and the
  retained indexes' full value only materialises at production scale
  (tens of thousands of sessions, hundreds of thousands of events).
- The agent-runs list's dominant cost is the `todo_counts` full-table
  aggregation, which no 0019 index helps; that is a future optimisation
  opportunity outside this slice.
- The measurement is single-run; sub-millisecond point lookups are
  noisy. Plan changes (`EXPLAIN ANALYZE BUFFERS`) are the primary
  evidence.

## Alternatives Considered

**Drop all 8 indexes.** Rejected: indexes #4–#8 demonstrably change
plans to index scans on real filter/point-lookup paths (client filter,
parent lookup, todo lookup, context join), so dropping them would
regress those paths.

**Keep all 8 indexes.** Rejected: indexes #1–#3 are never used by any
read path and cost write maintenance on an actively-written table.

**Baseline at revision 0018.** Rejected as not meaningful for the
current read path: 0018 predates `usage_events` (0021), so the read
endpoints would not function. The baseline is instead head schema with
the 8 indexes dropped, isolating migration 0019's exact effect.

## Environment note (for future measurement slices)

The repo requires Python ≥ 3.12 (`pyproject.toml` `requires-python`).
Migrations 0024 and 0025 use the module-level `str | None` (PEP 604)
annotation form, which Python 3.9 evaluates at import time and rejects
(`TypeError: unsupported operand type(s) for |`). The host interpreter
is Python 3.9, so `alembic` and the migration-loading tests
(`tests/test_canonical_event_schema.py`, `tests/test_migration_0027.py`,
`tests/test_overlap_index_schema.py`) must be run under Python 3.12 —
this slice used a `python:3.12` container with the worktree mounted.

## Follow-up migration (recorded, not executed)

Drop from `opencode_usage_records`:

- `ix_opencode_usage_records_reported_at`
- `ix_opencode_usage_records_client_reported_at`
- `ix_opencode_usage_records_session_reported_at`
