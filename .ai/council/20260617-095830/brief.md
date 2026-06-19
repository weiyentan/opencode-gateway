# Council Brief: LLM Session Information Storage

## Session ID
20260617-095830

## Idea Statement
Evaluate the viability of storing detailed information about each LLM (OpenCode) coding session in the OpenCode Gateway application — going beyond the current minimal tracking to capture richer session metadata for observability, cost tracking, audit, and analytics purposes.

## Current State
The Gateway already orchestrates OpenCode Serve instances (headless LLM coding agents running on Runner VMs). Each **Job** corresponds to one OpenCode coding session. The current PostgreSQL schema stores:

- **gateway_jobs**: `opencode_session_id` (string reference), `diff` (code output), `task_summary`, `repo_url`, `env_vars`, `branch_name`, `mr_url`, `workflow_run_id`, status timestamps
- **workspaces**: path, port, service_name, cleanup lifecycle
- **opencode_instance_observations**: periodic snapshots of OpenCode Serve instance status (version, health)
- **job_events**: state machine transition events (approvals, aborts)

What is **not** currently stored:
- Which LLM model served the session (model name/version)
- Token usage statistics (input tokens, output tokens, estimated cost)
- Session duration breakdown (wall-clock time, LLM thinking time, tool execution time)
- Tool calls made during the session (which tools, success/failure)
- Message/conversation summary or metadata
- Model hyperparameters (temperature, max_tokens, system prompt)
- Error rates, retry counts, or failure modes

## Context from Workspace
This is the first Council session for this repository. No prior Council artifacts exist.

The application is at a stable MVP stage (all endpoints implemented, 650+ tests). Key architectural boundaries:
- **Gateway** coordinates execution but does NOT own session content — that lives in OpenCode Serve (on Runner VMs)
- **OpenCode Serve** owns coding sessions, messages, diffs, and tool execution
- **Executor Plugin** (AWX or local) handles infrastructure actions
- Current observation model uses time-series snapshots (not event-driven)
- ADR 0004 establishes that Gateway never holds infrastructure secrets

## Known Assumptions
1. "LLM session information" refers to OpenCode Serve coding sessions, not lower-level LLM API calls
2. The desired information (model name, token counts, etc.) is available from the OpenCode Serve API
3. Storing this data in the Gateway's PostgreSQL database is the proposed approach
4. This would serve Paperclip, operators, and cost-analytics use cases
5. The additional storage and query cost is acceptable

## Open Questions for Council
1. **Source of truth**: Should this data live in the Gateway, or should the Gateway simply index/query it from OpenCode Serve on demand?
2. **Data model**: Should this be new tables, extensions to existing tables (gateway_jobs), or a separate event store?
3. **Retrieval mechanism**: Does OpenCode Serve expose the needed session metadata via its REST API today, or would this require OpenCode Serve changes too?
4. **Granularity**: Per-session summary metrics, or per-message/tool-call-level detail?
5. **Consumption**: Who queries this data, and for what purpose (cost allocation, debugging, compliance, billing)?
6. **Cost trade-off**: What's the storage cost vs. value proposition for this additional data?
7. **Boundary**: Does storing richer session metadata violate the Gateway↔OpenCode Serve responsibility boundary?

## What Success Looks Like
A clear recommendation (proceed/refine/reject) with:
- A concrete data model proposal if "proceed"
- Identified integration points with OpenCode Serve API
- Storage and retrieval cost estimates
- A delivery plan sliced into 15-30 minute implementation issues
