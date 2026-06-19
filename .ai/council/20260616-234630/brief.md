# Council Brief: OpenCode Gateway Feasibility Review

## Session ID
20260616-234630

## Idea Statement
Review the feasibility of the OpenCode Gateway project — an already-built portable execution control plane for running OpenCode as a safe, observable, API-driven coding backend. Although the project has been implemented, this Council evaluates whether the design decisions, architecture, and scope were sound, and whether the project as built solves the right problem in the right way.

## Project Summary
OpenCode Gateway is a FastAPI-based REST service that fills the orchestration gap around headless OpenCode sessions. It coordinates job submission, runner VM management via executor plugins (local shell for dev, AWX for production), PostgreSQL-based job/workspace/observation tracking, pre-flight policy checks (disk/memory pressure), and diff retrieval. The architecture is four-layered: API endpoints, core engine (settings, policy, scheduler), executor plugin interface, and OpenCode Serve client.

The project is ~95% complete per its PRD — 17 of 18 planned issues are implemented, with only the Paperclip integration adapter (#13) remaining. A total of 650+ tests across 28 test files exist. Four ADRs document key architectural decisions.

## Context from Workspace
- **No prior Council sessions exist** — this is the first Council run for this project.
- **No handoff files** found in `.status/handoff/`.
- **PRD** defines 29 user stories across platform engineers, gateway operators, Paperclip agents, AWX admins, security auditors, and developers.
- **4 ADRs** cover: separate observation tables, executor plugin interface, Postgres port allocation, and the no-infra-secrets policy.
- **READNE.md** documents full API surface, project structure, and Docker support.
- **CONTEXT.md** establishes precise domain language and key relationships.

## Known Assumptions
1. OpenCode Serve is a stable, long-running HTTP API that can be managed as a systemd service.
2. Runner VMs are persistent (not ephemeral or container-based for MVP).
3. AWX is the right default executor for production because the team already operates AWX.
4. Postgres is an appropriate state store for orchestration metadata at expected scale.
5. The Gateway should not replace Paperclip — they are complementary layers.
6. The four-layer architecture (API → Core → Executor → OpenCode Client) is the right separation of concerns.
7. The MVP port range (10000-10999, 1000 ports) is sufficient.

## Open Questions for the Council
1. Is Gateway a necessary component, or could existing tools (AWX + OpenCode directly) achieve the same outcomes with less complexity?
2. Does the four-layer architecture introduce unnecessary indirection, or is it appropriate for the problem domain?
3. Are the executor plugin interface and the AWX executor genuinely clean abstractions, or do they leak AWX-specific concerns?
4. Is Postgres the right choice for observation time-series data, or should a purpose-built TSDB be considered?
5. Does the MVP scope avoid the "second-system effect" — or has over-engineering crept in?
6. Are there security or operational risks in the Gateway → AWX → Runner VM chain that were not addressed?
7. Is the Paperclip integration adapter (#13) genuinely out of scope for MVP, or should it have been part of the initial design?
8. Would a simpler architecture (e.g., a thin proxy without Postgres) have been more appropriate?

## What Success Looks Like
The Council produces a clear verdict (proceed/refine/reject) with actionable rationale. For this already-built project, success means either:
- **Validation**: The project was well-architected and is in a good state to continue toward production readiness.
- **Actionable critique**: Specific areas where the approach was suboptimal are identified, with concrete recommendations for remediation.
- The output serves as a retrospective learning artifact for the team.
