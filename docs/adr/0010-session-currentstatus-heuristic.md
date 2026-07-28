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

Use an inline heuristic (option 2) on the Agent Run Summary view model. The first-slice `currentStatus` mapping is:

| Condition | `currentStatus` |
|---|---|
| No messages observed for the session, OR `last_message_at` is null / absent | `unknown` |
| `last_message_at` is recent (within the configured recency threshold, e.g. the last N minutes) | `running` |
| Not `running`, has messages, **and** `parent_session_id` is set | `blocked` |
| Not `running`, has messages, `parent_session_id` is **not** set | `completed` |
| Fallback — session is very old or does not fit any above bucket cleanly | `unknown` |

The recency threshold is a Gateway-side configuration parameter (not hard-coded), defaulting to a value that aligns with typical agent session idle timeouts (e.g. 5–15 minutes). The exact default should be determined during implementation based on observed OpenCode serve session cadence.

## Rationale

- **No upstream dependency**: The heuristic relies only on data the Gateway already collects — usage records, session context, and `last_message_at`. No new collector queries or schema changes are needed.
- **Synchronous and cheap**: The computation is a simple set of column checks on data already loaded for the Agent Run Summary. No background jobs, caches, or separate endpoints.
- **Graceful degradation**: Missing data (no messages, null `last_message_at`) produces `unknown` rather than a misleading status. The fallback `unknown` clause prevents hard classification errors on edge cases.
- **Cautious classification**: `blocked` requires both quietness AND a parent session ID — it is deliberately conservative to avoid misclassifying a genuinely stalled root session or a completed child session as blocked.
- **Easy to iterate**: Because the heuristic is inline, the mapping can be tuned or extended in a single place without schema migrations or job redeployments.

## Consequences

Positive:

- `currentStatus` is available immediately as part of the Agent Run Summary without additional infrastructure.
- The heuristic can be refined based on real-world usage without coupling to upstream OpenCode releases.

Negative:

- The heuristic will be wrong in some edge cases (e.g. a child session that is genuinely still running but quiet, or a root session that is idle but not yet completed).
- There is no feedback loop from OpenCode to correct misclassifications. Operators seeing wrong statuses must adjust the heuristic or accept the limitation.

Caveat:

> This `currentStatus` heuristic is a Gateway-level inference derived from available OpenCode session signals (message presence, message recency, parent/child relationship). It is **not** upstream-authoritative lifecycle truth. OpenCode does not expose a native session status column. The heuristic may misclassify sessions in edge cases and should be treated as a best-effort approximation.
