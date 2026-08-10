# Aurora Glass Performance Optimization — Handoff Document

**Generated:** 2026-08-06  
**Source Session:** Grill-with-docs review session  
**Related PRD:** [`docs/prd/aurora-glass-performance-optimization.md`](../prd/aurora-glass-performance-optimization.md)

---

## 1. Session Summary

This handoff documents a **grill-with-docs session** that reviewed 6 recommendations from an external code review of the Aurora Glass application. The session covered:

- **Performance investigation findings** — profiling data showing where time is spent in API requests
- **Database indexes** — Alembic migration `0019` creating 8 targeted indexes on frequently queried tables
- **Frontend decisions** — token semantics clarification, Live Events rename, caching strategy, freshness indicators
- **Code change priorities** — application-layer optimizations to reduce API response times

The session concluded that the primary bottleneck is **not the database**, but rather the application layer and external API integrations.

---

## 2. Performance Investigation Findings

### Key Metrics

| Layer | Response Time | Assessment |
|-------|--------------|------------|
| Database queries | 18–89 ms | Fast — not the bottleneck |
| External API responses | 5–6 seconds | Slow — major contributor |
| Traefik POST request logs | ~13.4 seconds average | Total end-to-end latency |

### Bottleneck Analysis

- **Application layer, not database** — Database queries complete quickly (under 100ms), but total API response times average over 13 seconds
- **External API calls** are the dominant source of latency (5–6 seconds each)
- **Database connection issues** observed — "Connection reset by peer" errors suggest connection pool exhaustion or idle timeout problems

### Implications

Optimizing database queries alone will have minimal impact. The focus must be on:
1. Reducing the number of sequential external API calls
2. Profiling application code to identify hot paths
3. Tuning database connection pools to prevent resets

---

## 3. Code Changes Needed

### 3.1 Application Profiling (Priority: High)

Add timing instrumentation throughout the application to identify exactly where time is spent:

```python
# Suggested approach: add per-operation timing decorators
import time
from functools import wraps

def timed_operation(name):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = fn(*args, **kwargs)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"[{name}] {elapsed:.1f}ms")
            return result
        return wrapper
    return decorator
```

**Focus areas for instrumentation:**
- API endpoint entry/exit points
- Individual database query execution
- External API call durations
- Status computation functions
- Session resolution logic

### 3.2 Query Optimization (Priority: High)

**Goal:** Reduce the total number of queries or combine them into fewer batch operations.

**Actions:**
- Audit the query plan for the `/api/status` and dashboard endpoints
- Look for N+1 query patterns (especially around sessions, projects, and events)
- Consider eager-loading related entities via ORM joins instead of separate queries
- Use `selectinload` or `joinedload` strategies in SQLAlchemy where appropriate

### 3.3 Middleware Overhead (Priority: Medium)

**Check for:**
- Unnecessary serialization/deserialization in middleware layers
- Redundant authentication/authorization checks on internal routes
- Logging or audit hooks firing on every request with expensive operations
- Request body parsing that isn't needed for read-only endpoints

**Action:** Review the middleware stack and disable or optimize any non-critical processing.

### 3.4 Status Calculation — `_compute_status` (Priority: High)

The `_compute_status` function likely aggregates data from multiple sources to determine the overall system status. This is a candidate for optimization:

**Review checklist:**
- Is it fetching redundant data?
- Can results be cached rather than recomputed on every request?
- Are there unnecessary iterations over large collections?
- Could precomputed status values be stored and updated incrementally?

**Suggested improvement:** Introduce a short-lived cache (e.g., 30-second TTL) for computed status values if real-time accuracy isn't required.

### 3.5 Session Resolution (Priority: High)

Session context resolution involves joining sessions with their associated projects. This is a known complex operation:

**Review checklist:**
- Are all joined fields actually needed for the current endpoint?
- Can the join be pushed down to the database level instead of Python-side filtering?
- Is there pagination or limiting applied too late (after full dataset retrieval)?
- Could a materialized view or summary table help?

**Suggested improvement:** Add explicit column selection (`session.query(Session.id, Session.name, ...)`) instead of loading full session objects when only specific fields are needed.

### 3.6 Connection Pool Tuning (Priority: Medium)

The "Connection reset by peer" errors indicate the database connection pool needs tuning:

**Recommended settings to investigate:**
- `pool_size` — increase if connections are being exhausted
- `max_overflow` — adjust to allow temporary burst capacity
- `pool_recycle` — set to avoid stale connections (recommend 1800 seconds / 30 minutes)
- `pool_pre_ping` — enable to validate connections before use

**Example SQLAlchemy engine configuration:**
```python
engine = create_engine(
    database_url,
    pool_size=20,
    max_overflow=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)
```

---

## 4. Frontend Changes (Already Decided)

These decisions were agreed upon during the grill-with-docs session and are ready for implementation:

### 4.1 Token Semantics Clarification

- **"Total Tokens"** → renamed to **"Active Tokens"** to better reflect what the metric represents
- Prevents confusion between historical totals vs. currently active resources

### 4.2 Live Events → Operational Events

- **"Live Events"** → renamed to **"Operational Events"**
- More accurately describes the nature of the event stream
- Aligns with operational terminology used elsewhere in the platform

### 4.3 Client Metadata Caching

- Implement client-side caching for metadata lookups
- **TTL: 10 minutes** — balances freshness with performance
- Reduces repeated API calls for static or slowly-changing metadata

### 4.4 Freshness Indicators

- **Global timestamp** — display the last data refresh time at the top of the dashboard
- **Per-panel states** — show individual panel freshness (e.g., "Updated 2m ago" or "Stale")
- Helps operators understand data currency at a glance

### 4.5 KPI Label Clarification

- Add date ranges to historical KPI labels (e.g., "Avg Latency (Last 7 Days)")
- Clarify that health-related KPIs show **current** state, not historical averages
- Prevents misinterpretation of metrics by operators

---

## 5. Database Changes

### Alembic Migration 0019

Migration `0019` has been prepared with **8 targeted indexes** on frequently queried tables. These indexes should significantly improve query performance for dashboard and status endpoints.

**To apply:** Run the migration via container startup:
```bash
alembic upgrade head
```

The migration includes indexes on:
- Sessions (foreign keys and commonly filtered columns)
- Projects (lookup columns)
- Events (timestamp and type columns)
- Related junction tables

---

## 6. Recommended Execution Order

| Phase | Task | Estimated Effort | Dependencies |
|-------|------|-----------------|--------------|
| 1 | Apply Alembic migration 0019 | Low | None |
| 2 | Add timing instrumentation | Low | None |
| 3 | Profile application under load | Low | Phase 2 |
| 4 | Optimize `_compute_status` | Medium | Phase 3 |
| 5 | Optimize session resolution queries | Medium | Phase 3 |
| 6 | Reduce query count / add caching | Medium | Phase 3 |
| 7 | Tune connection pool settings | Low | None |
| 8 | Implement frontend changes | Medium | None |
| 9 | End-to-end testing and verification | Medium | Phases 4-7, 8 |

---

## 7. Files to Review

| File | Purpose |
|------|---------|
| `docs/prd/aurora-glass-performance-optimization.md` | Full PRD with detailed requirements |
| `alembic/versions/0019_*.py` | Database migration with 8 indexes |
| Backend models/controllers | Session resolution, status computation, event queries |
| Frontend dashboard components | Token labels, event naming, caching logic |

---

*End of handoff document.*
