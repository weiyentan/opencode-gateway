# ADR 0010: Session currentStatus Heuristic

## Status

Accepted

## Context

ADR 0008 established that Agent Run Summary status is inferred from todo state and session recency, not from an OpenCode-native status column. OpenCode does not expose an authoritative session lifecycle state (e.g. `running`, `completed`, `blocked`) through its SQLite schema. The Gateway must therefore derive a `currentStatus` value from the available signals in the projection data.

The available signals are:

- **Messages exist** — whether any usage records have been observed for the session (i.e. the session has produced at least one message).
- **`last_message_at`** — the timestamp of the most recent observed message for the session.
- **`parent_session_id`** — whether the session is a child (subagent) of another session. Child sessions are typically spawned for a specific task and expire when that task finishes.
- **Recency** — how much wall-clock time has passed since `last_message_at`.

The Gateway has no access to OpenCode's internal session lifecycle, process state, or agent-side signals (e.g. whether the agent process is still alive, or whether the user has explicitly ended the session). Any status derived from these signals is necessarily a heuristic, not an authoritative lifecycle truth.

Two approaches were considered:

1. **Per-session lifecycle endpoint** — expose a separate `/sessions/:id/lifecycle` endpoint that computes and caches status via a background job.
2. **Inline heuristic on the Agent Run Summary** — derive `currentStatus` synchronously as part of the existing Agent Run Summary view model with no separate endpoint or cache.

## Decision

Use an inline heuristic (option 2) on the Agent Run Summary view model. The `currentStatus` mapping (implemented in Python in `_compute_status()` and as a SQL `CASE` expression in `_status_case_expression()`) is:

| Condition | `currentStatus` |
|---|---|
| No messages observed for the session, OR `last_message_at` is null / absent | `unknown` |
| `last_message_at` is recent (within the quiet threshold, `_QUIET_THRESHOLD_MINUTES`, default 60 min) | `running` |
| Session exceeds the unknown threshold (`_UNKNOWN_THRESHOLD_HOURS`, default 24 h) — too old to classify meaningfully | `unknown` |
| Not `running`, not past unknown threshold, has messages, **and** `parent_session_id` is set | `blocked` |
| Not `running`, not past unknown threshold, has messages, no parent, within the stale threshold (`_STALE_THRESHOLD_HOURS`, default 6 h) | `stale` |
| Not `running`, not past unknown threshold, has messages, no parent, beyond the stale threshold | `completed` |

The derivation priority order (implemented in `_compute_status`) is: **unknown → running → unknown (age) → blocked → stale → completed**. Key rules:

- `blocked` has higher priority than `stale` — a session with a parent is always `blocked` even within the stale window.
- `stale` occupies the band between `_QUIET_THRESHOLD_MINUTES` and `_STALE_THRESHOLD_HOURS` for sessions without a parent. It represents an observability gap rather than a confidently terminated session.
- `completed` is returned only after the stale threshold has been exceeded, making it a more confident (but still heuristic) terminal status.

The thresholds are module-level constants in `app/api/usage.py`:

| Constant | Default | Purpose |
|---|---|---|
| `_QUIET_THRESHOLD_MINUTES` | 60 min | Below this → `running`; above this the session is potentially inactive |
| `_STALE_THRESHOLD_HOURS` | 6 h | Observatory gap band — session is inactive but too recent to call `completed` |
| `_UNKNOWN_THRESHOLD_HOURS` | 24 h | Beyond this → `unknown`; too old to classify reliably |

## Rationale

- **No upstream dependency**: The heuristic relies only on data the Gateway already collects — usage records, session context, and `last_message_at`. No new collector queries or schema changes are needed.
- **Synchronous and cheap**: The computation is a simple set of column checks on data already loaded for the Agent Run Summary. No background jobs, caches, or separate endpoints.
- **Graceful degradation**: Missing data (no messages, null `last_message_at`) produces `unknown` rather than a misleading status. The upstream `unknown` check (past `_UNKNOWN_THRESHOLD_HOURS`) prevents hard classification of very old sessions.
- **Stale as observability gap**: The `stale` status fills a middle band between confidently `running` and confidently `completed`. This prevents sessions that have simply gone quiet (e.g. the agent process is alive but not producing output) from being classified as terminal. A `stale` session might resume, making it distinct from `completed`.
- **Blocked priority**: `blocked` is checked before `stale` so that child sessions waiting on a parent are never misidentified as a stale observability gap.
- **Cautious classification**: `blocked` requires both quietness AND a parent session ID — it is deliberately conservative to avoid misclassifying a genuinely stalled root session or a completed child session as blocked.
- **Easy to iterate**: Because the heuristic is inline, the mapping can be tuned or extended in a single place without schema migrations or job redeployments.

## Consequences

Positive:

- `currentStatus` is available immediately as part of the Agent Run Summary without additional infrastructure.
- The heuristic can be refined based on real-world usage without coupling to upstream OpenCode releases.

Negative:

- The heuristic will be wrong in some edge cases (e.g. a child session that is genuinely still running but quiet, a root session that is idle but not yet completed, or a `stale` session that has actually terminated without a final message).
- The three-threshold model (quiet, stale, unknown) adds complexity compared to a simpler two-threshold model. Each threshold must be tuned based on observed session behaviour.
- There is no feedback loop from OpenCode to correct misclassifications. Operators seeing wrong statuses must adjust the heuristic or accept the limitation.

Caveat:

> This `currentStatus` heuristic is a Gateway-level inference derived from available OpenCode session signals (message presence, message recency, parent/child relationship). It is **not** upstream-authoritative lifecycle truth. OpenCode does not expose a native session status column. The heuristic may misclassify sessions in edge cases and should be treated as a best-effort approximation. The `stale` status in particular is an observability-gap label, not a proven terminal state.
