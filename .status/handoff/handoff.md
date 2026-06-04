# OpenCode Gateway — Planning Handoff

## Current State

The repo at `C:\Users\weiye\CascadeProjects\opencode-gateway` is essentially empty — just a README stub and `.git`. No code exists yet.

## Decisions Resolved (so far)

1. **Tech stack**: Python/FastAPI (confirmed)
2. **ORM/database layer**: SQLAlchemy async + Alembic for migrations (confirmed)
3. **OpenCode Serve API spec**: User does not have one. We started exploring the local `opencode serve` installation (v1.15.13) to discover API endpoints.

## Work Done

- Created `CONTEXT.md` with domain language: Gateway, Executor Plugin, OpenCode Serve, Runner VM, Job, Workspace, and their relationships.
- Started the grill-with-docs planning process with the long LLM planning brief document.
- Installed and ran `opencode serve` locally on port 4899. It's a Vite/React SPA.
- Discovered API client methods from the JS bundle:
  - `c.client.session.list({directory})` → Session[]
  - `c.client.session.get({sessionID})` → Session
  - `c.client.session.update({sessionID, directory, time})` → void
  - `c.client.project.list()` → Project[]
  - `c.client.project.update({projectID, directory, name})` → void
  - `c.client.file.status({directory})` → FileStatus[]
  - `c.client.worktree.list({directory})` → Worktree[]
  - `c.client.worktree.create({directory})` → Worktree
  - `c.client.worktree.remove({directory, worktreeRemoveInput})` → Worktree
  - `c.client.worktree.reset({directory, worktreeResetInput})` → Worktree
  - `c.client.instance.dispose({directory})` → void
- The API transport layer wasn't fully reverse-engineered, but the server URL defaults to `localhost:4096` in web mode or the page origin.
- The CLI opens a web UI — not a pure REST API. The API is consumed internally by the web app.

## Decisions Pending (next questions to ask)

The grilling session was interrupted mid-stream. Questions left to ask:

1. **OpenCode API discovery approach**: Since `opencode serve` is a SPA with internal API, should we:
   a) Reverse-engineer the OpenCode serve API by inspecting its source (look for the npm package or GitHub)?
   b) Design the Gateway's OpenCode client around a minimal expected contract and fill in details later?
   c) Run opencode serve and proxy its API calls to discover the full surface area?

2. **Project structure**: Confirm the repo layout (the brief suggests `app/`, `charts/`, `config/`, `docs/`, `examples/`, `tests/`).

3. **Database schema**: Confirm the Postgres schema tables (the brief has detailed proposals).

4. **Executor plugin interface**: Confirm the base class design.

5. **Port allocation strategy**: Should ports come from Postgres or VM files?

6. **Security model**: How should Gateway ↔ AWX ↔ Runner VM credentials flow?

## Skills to Load in Next Session

- `grill-with-docs` — to continue the structured decision-making process
- `write-a-skill` — if the user wants to formalize the planning into a reusable skill

## Key Files

- `C:\Users\weiye\CascadeProjects\opencode-gateway\CONTEXT.md` — domain language glossary
- `C:\Users\weiye\CascadeProjects\opencode-gateway\README.md` — stub only
