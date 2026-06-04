# API Handoff 2 — OpenCode Serve REST Discovery

## Project State

The repo at C:\Users\weiye\CascadeProjects\opencode-gateway is essentially empty — README stub, CONTEXT.md (domain language glossary), .git, .status/handoff/ directory. No Python code exists yet.

## Previous Handoff

.status/handoff/api-discovery-handoff.md documented the state before this session. Key points:
- Team ran opencode serve and found it's a Vite/React SPA with internal API client
- Reverse-engineered JS bundle method signatures (session.list, session.get, etc.)
- Three approaches considered: (a) reverse-engineer fully, (b) minimal expected contract, (c) proxy & discover
- Option (b) was recommended but not resolved

## What Happened This Session

**1. Resolved the transport question.** The previous session knew method signatures but not the HTTP wiring. This session discovered the full REST API surface of opencode serve v1.15.13 running locally on port 4899.

**Key discovery**: OpenCode Serve exposes a **REST/HTTP API** (not JSON-RPC or WebSocket). Two surfaces exist:
- Legacy (no prefix): /session, /project, /file, /global/health, etc.
- V2: /api/session, /api/model, /api/provider

SSE for streaming events (/global/event, /event). No WebSocket for API (only PTY terminal).

**2. Approach (b) resolved.** With the transport confirmed as plain REST, the minimal expected contract approach is viable and validated.

**3. Proposed plan** (presented to user but NOT yet approved):
- Create opencode_client/protocol.py — abstract base class
- Create opencode_client/http_client.py — httpx-based transport
- Create opencode_client/__init__.py
- Validate with one real call against local instance
- Document in docs/opencode-serve-api.md

## Key API Endpoints Discovered

**Health**: GET /global/health → {"healthy":true,"version":"1.15.13"}

**Session lifecycle**:
- GET /session?directory=... — list sessions
- POST /session — create (body: parentID, title, agent, model, metadata, permission, workspaceID)
- GET /session/{id} — get session
- GET /session/{id}/diff — get session diff
- DELETE /session/{id} — delete session
- POST /session/{id}/abort — abort session

**Workspace** (experimental):
- GET /experimental/workspace
- POST /experimental/workspace
- DELETE /experimental/workspace/{id}

**Misc**:
- POST /global/dispose — dispose instance
- GET /file/status?directory=... — file status
- GET /global/config — global config
- GET /provider — list providers

## Domain Language (from CONTEXT.md)

Use these terms precisely:
- **Gateway** = the main API/state engine (this repo)
- **Executor Plugin** = abstraction layer for infrastructure actions
- **OpenCode Serve** = headless API process on Runner VM
- **Runner VM** = persistent VM hosting workspaces
- **Job** = unit of work mapped to one coding task
- **Workspace** = directory on Runner VM with cloned repo

## What Remains / Next Steps

1. **User approval needed** before implementation — the plan was presented but not approved
2. **Create project structure** — opencode_client/ package with protocol and HTTP client
3. **Implement OpenCodeClientProtocol** — abstract class with: health(), list_sessions(), get_session(), create_session(), delete_session(), get_session_diff(), list_workspaces(), create_workspace(), dispose(), get_file_status()
4. **Implement OpenCodeServeClient** — httpx.AsyncClient-based transport
5. **Validate** against local opencode serve instance
6. **Document** in docs/opencode-serve-api.md

## Skills to Load in Next Session

- **customize-opencode** — if the next session involves configuring opencode agents/subagents for the Gateway
- No other skills needed for the initial client implementation

## Key Files

- C:\Users\weiye\CascadeProjects\opencode-gateway\CONTEXT.md — domain language
- C:\Users\weiye\CascadeProjects\opencode-gateway\.status\handoff\api-discovery-handoff.md — previous handoff
- C:\Users\weiye\CascadeProjects\opencode-gateway\.status\handoff\handoff.md — broader planning handoff
