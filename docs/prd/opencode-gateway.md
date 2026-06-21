# PRD: OpenCode Gateway

## Problem Statement

OpenCode is powerful as a coding agent, but moving from an interactive session to a headless, API-driven automation pipeline introduces operational gaps that the raw CLI and REST API do not address alone. As a platform engineer integrating OpenCode into a larger workflow, I need answers to questions that OpenCode itself does not track: Which runner VM owns this workspace? Which OpenCode session belongs to which job? Is there enough disk space on the runner? Is the opencode serve process healthy? When should old workspaces be cleaned up? Where are the diffs and logs for a completed job? How do external systems trigger, observe, and abort work reliably? Without a gateway layer, every integration — whether from AWX, EDA, GitLab, GitHub, or Paperclip — has to reinvent the same state machine, observation loop, and cleanup policy. This leads to fragile scripts, duplicated logic, inconsistent observability, and no central audit trail.

## Solution

OpenCode Gateway is a portable execution control plane that provides a stable orchestration API around OpenCode. It stores job and runner state in Postgres, calls the OpenCode Serve API for coding sessions, and delegates infrastructure actions — create workspace, start/stop opencode, collect state, clean up — to executor plugins. The Gateway never reaches into the runner VM directly; every infrastructure operation goes through the executor plugin interface.

For the MVP, the default executor plugin is AWX, which runs Ansible playbooks against a persistent Runner VM. The Runner VM hosts workspace directories under a controlled `/opencode` filesystem, and each workspace runs its own `opencode serve` instance managed by systemd via a templated unit (`opencode@workspace.service`). The Gateway allocates ports from a Postgres-managed range (10000-10999), and AWX playbooks query the Gateway for the port rather than self-allocating.

External callers — whether Paperclip, GitLab CI, GitHub Actions, AWX job templates, or EDA rulebooks — interact with the Gateway API to submit jobs, check status, retrieve diffs, and approve or abort work. The Gateway handles the rest: runner selection, health checks, workspace lifecycle, OpenCode session management, and result collection.

## User Stories

1. As a Platform Engineer, I want to submit a coding job with a repo URL and task description, so that OpenCode works against my repository.
2. As a Platform Engineer, I want to check the status of a submitted job, so that I know whether it's running, completed, or failed.
3. As a Platform Engineer, I want to retrieve the diff produced by a completed job, so that I can review the changes before committing.
4. As a Platform Engineer, I want to abort a running job, so that I can stop work that is incorrect or taking too long.
5. As a Platform Engineer, I want the Gateway to select an appropriate runner automatically, so that I don't have to manage runner assignment manually.
6. As a Platform Engineer, I want to pin a job to a specific runner or label, so that I can control where the work executes.
7. As a Platform Engineer, I want to pass environment variables and configuration to a job, so that OpenCode has the context it needs.
8. As a Platform Engineer, I want the Gateway to reject my job when no healthy runner is available, so that I get a clear failure instead of a hung request.
9. As a Platform Engineer, I want to retrieve the full log output from an OpenCode session, so that I can debug unexpected behavior.
10. As a Gateway Operator, I want to see all registered runners and their health status, so that I know whether the system can accept jobs.
11. As a Gateway Operator, I want to inspect runner observations (disk usage, memory, load), so that I can diagnose resource pressure before it causes failures.
12. As a Gateway Operator, I want to trigger workspace cleanup on a runner, so that I can free disk space before it blocks new jobs.
13. As a Gateway Operator, I want to receive alerts when runner disk usage exceeds 80%, so that I can intervene before jobs are blocked.
14. As a Gateway Operator, I want to manually mark a runner as offline for maintenance, so that new jobs are not dispatched to it.
15. As a Gateway Operator, I want to view all active workspaces on a runner with their size and last activity, so that I can make cleanup decisions.
16. As a Paperclip/Agent Manager, I want to submit a Gateway job from my agent workflow, so that I can delegate coding execution to OpenCode.
17. As a Paperclip/Agent Manager, I want to receive job completion callbacks with diff and summary, so that my workflow can continue automatically.
18. As a Paperclip/Agent Manager, I want to know whether a job needs human approval, so that I can pause and wait for a decision before proceeding.
19. As a Paperclip/Agent Manager, I want to query the Gateway for all jobs associated with my workflow run, so that I can correlate results.
20. As a Paperclip/Agent Manager, I want the Gateway to return a structured job result (status, diff URL, branch name, MR URL, session ID), so that I can pass it downstream without parsing unstructured output.
21. As an AWX Admin, I want to define the job templates that the AWX executor plugin calls, so that infrastructure actions run correctly.
22. As an AWX Admin, I want AWX playbooks to return structured JSON results (port, URL, workspace path), so that the Gateway can consume them deterministically.
23. As an AWX Admin, I want to know which Gateway jobs map to which AWX job IDs, so that I can troubleshoot failures in the AWX UI.
24. As a Security Auditor, I want to see a log of all job submissions, executor actions, and approval decisions, so that I can audit system usage.
25. As a Security Auditor, I want to verify that the Gateway never holds infrastructure secrets, so that a Gateway breach cannot compromise runner access.
26. As a Security Auditor, I want to see which user or system requested each approval and who approved or denied it, so that I can enforce separation of duties.
27. As a Developer, I want to run the Gateway with a local executor for development, so that I don't need AWX or a Runner VM to test changes.
28. As a Developer, I want to seed a test database with sample jobs and runners, so that I can verify API behavior without a full environment.
29. As a Developer, I want to run the Gateway's integration tests against a real Postgres instance, so that SQLAlchemy model and migration behavior is validated.

## Implementation Decisions

The Gateway is organized into four layers: the API layer, the core engine, the executor plugin interface, and the OpenCode Serve client.

The API layer exposes REST endpoints for jobs, runners, workspaces, observations, and approvals. Every endpoint performs authentication via API key middleware from day one. Responses follow a consistent envelope with status, data, and error fields.

The core engine contains configuration (Pydantic settings), a policy module for pre-flight checks (disk pressure, runner health, concurrent job limits), and a background scheduler for periodic tasks such as polling runner state and expiring stale workspaces.

The executor plugin interface defines seven async methods: `create_workspace`, `start_opencode`, `stop_opencode`, `restart_opencode`, `collect_state`, `cleanup_workspace`, and `cancel_job`. Each method accepts and returns typed Pydantic models. The base plugin is abstract; concrete implementations are registered in an `EXECUTOR_REGISTRY` dict. The `get_executor()` factory reads `GATEWAY_EXECUTOR_TYPE` from settings and looks up the corresponding class in the registry. The default executor is `local` (shell-based, for development); AWX is planned as the production executor. Future SSH and Kubernetes plugins can be added by implementing the interface and registering the class. Crucially, `provision_runner` is not in the base interface — the Runner VM is persistent and managed out of band.

The OpenCode Serve client is an httpx-based wrapper that communicates with the OpenCode Serve REST API over the internal network. It exposes methods for health checks, session CRUD, task submission, diff retrieval, and abort. The client is designed so that the Gateway interacts with OpenCode through a protocol interface, making it testable with a mock server.

The database has eight tables: `gateway_jobs`, `runners`, `workspaces`, `runner_observations`, `workspace_observations`, `opencode_instance_observations`, `approvals`, and `job_events`. Observations are stored in separate tables per domain entity (per ADR 0001), with a bigserial PK and a composite index on runner/workspace and observed_at. This allows efficient time-range queries for dashboards and alerting.

Port allocation is managed in Postgres from a fixed range (10000-10999, per ADR 0003). When a workspace is created, the Gateway atomically selects the next available port. The AWX playbook asks the Gateway "what port?" rather than self-allocating, avoiding collisions without requiring a distributed lock service.

The security model follows ADR 0004: the Gateway never holds infrastructure secrets. AWX owns the SSH keys to the Runner VM. The Gateway authenticates to AWX via an API token configured at deployment time. Communication between the Gateway and OpenCode Serve is internal-network-only; OpenCode Serve is never exposed publicly. All requests, executor actions, and approval decisions are logged for audit.

## Testing Decisions

- A good test validates external behavior through the API, not implementation details.
- Test the Gateway API endpoints with httpx TestClient against a test database.
- Test the executor plugin interface with a mock executor that returns canned responses.
- Test the OpenCode client against a mock HTTP server that simulates OpenCode Serve responses.
- Test the policy engine (disk pressure checks, runner health, pre-flight gates) in isolation with synthetic observation data.
- Unit tests for: `core/config`, `core/policy`, `executors` (with mock), `opencode/client` (with mock).
- Integration tests for: API endpoints with real Postgres; migrations up and down.
- Prioritize the three deep modules — `executors/base`, `opencode/client`, `core/policy` — for the highest test coverage, as they carry the most business logic and abstraction surface.

## Out of Scope

- Full multi-tenant SaaS support with org-level isolation.
- Complex Kubernetes runner pod orchestration (the Runner VM is persistent for MVP).
- Containerizing every developer toolchain into the runner image.
- Real-time collaborative UI for monitoring jobs.
- Full autonomous approval framework that self-approves based on policies.
- Deep parsing of all OpenCode internal SQLite database or config files.
- Direct commits to protected branches without review or MR.
- Running untrusted arbitrary commands without an approval gate.
- Making AWX mandatory for all users — other executor plugins (SSH, local shell, Kubernetes) can be added in later phases.
- Exposing OpenCode Serve to the public internet.
- Replacing Paperclip or becoming a general agent orchestration platform.

## Further Notes

- The Gateway's Postgres stores orchestration state only. OpenCode's own SQLite database is separate and remains under OpenCode's control. The Gateway does not read or write OpenCode's internal state directly.
- Git is the source of truth for code. The Gateway orchestrates execution around it — it creates workspaces from repos, runs OpenCode against them, collects diffs, and optionally creates branches and MRs.
- The minimum demo flow: User submits job with repo URL and task → Gateway creates a job row (status: pending) → Gateway checks runner health → Gateway calls AWX executor to create workspace → AWX playbook checks out the repo and starts opencode serve on an allocated port → Gateway sends the task to OpenCode via its HTTP API → OpenCode produces a diff → Gateway records the result (status: completed, diff URL, branch) → User polls GET /jobs/{id} and retrieves the diff.
- This PRD covers the full MVP scope. Phase 1 (API skeleton with FastAPI, health endpoint, Postgres connection, basic models, and POST/GET for jobs) should be the first implementation milestone.
