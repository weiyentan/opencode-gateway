# OpenCode Gateway

A portable execution control plane for running OpenCode as a safe, observable, API-driven coding backend.

## Language

**Gateway**:
The main API and state engine. Receives job requests, tracks state in Postgres, delegates infrastructure actions to executor plugins, and calls OpenCode Serve API for coding sessions.
_Avoid_: Backend, server, controller

**Executor Plugin**:
An abstraction layer that performs infrastructure actions (create workspace, start/stop opencode, collect state, clean up). The MVP default is AWX; other backends can be added later.
_Avoid_: Provider, driver, adapter

**OpenCode Serve**:
A long-running headless API process managed by systemd on the Runner VM. Owns coding sessions, messages, diffs, and tool execution.
_Avoid_: opencode daemon, opencode service (in generic sense)

**Runner VM**:
A persistent virtual machine that hosts workspace directories and systemd-managed opencode serve instances. Provides the native toolchain for code editing tasks.
_Avoid_: Worker, node, agent

**Job**:
A unit of work submitted to the Gateway. Maps to one coding task executed against one workspace via one OpenCode session.
_Avoid_: Task, run, request

**Paperclip**:
An agent/work orchestration layer that coordinates agents, goals, task assignment, governance, budgets, and higher-level workflows. Paperclip can sit above the Gateway, calling the Gateway API to perform coding execution and receiving back diffs, branches, MR URLs, and summaries.
_Avoid_: Gateway, execution control plane

**Workspace**:
A directory on the Runner VM containing a cloned repository and related artifacts. Created per-job, cleaned up according to policy.
_Avoid_: Project directory, working directory, sandbox

**Observation**:
A time-series telemetry snapshot ingested via `POST /observations`. Runner VMs periodically push resource metrics (disk, memory, load) and per-workspace/per-instance status to the Gateway. Stored in domain-specific tables (`runner_observations`, `workspace_observations`, `opencode_instance_observations`) per ADR 0001.
_Avoid_: Log, event, heartbeat (in the telemetry sense)

**Policy Engine**:
A pluggable pre-flight check system that inspects runner observations before a job is accepted. The default implementation (`ObservationBasedPolicy`) checks disk pressure, memory pressure, and telemetry staleness. Runners that exceed configured thresholds are rejected with HTTP 503 (`PolicyViolation`). The runner's database status is updated to reflect the pressure condition. Runners with operator-set statuses (`offline`, `maintenance`) are rejected immediately regardless of observation data; runners set to `online` bypass observation-based checks entirely.
_Avoid_: Guard, validator, admission control

**Runner Statuses** — The Gateway uses a tripartite status model with three separate columns:

### `admin_status` (operator-set)

| Value | Meaning |
|-------|---------|
| `online` | Runner is cleared for dispatch (default for new runners) |
| `offline` | Runner rejects all jobs immediately |
| `maintenance` | Runner is under maintenance and rejects all jobs immediately |

Set via `POST /runners/{id}/admin-status`. **Never overwritten by observation ingestion.**

### `health_status` (observation-derived)

| Value | Meaning |
|-------|---------|
| `HEALTHY` | Runner is accepting jobs |
| `BLOCKED_DISK_PRESSURE` | Disk usage exceeds `disk_threshold_percent` |
| `BLOCKED_MEMORY_PRESSURE` | Memory usage exceeds `memory_threshold_percent` |
| `UNKNOWN` | No recent observations or telemetry is stale |

Set to `HEALTHY` on every observation upsert. The `ObservationBasedPolicy` engine checks disk/memory thresholds against observation data.

### `status` (legacy)

A single-column legacy status that mirrors `admin_status` on operator changes and `health_status` on observation updates. Retained for backward compatibility.

### Runner Admission Model

The Gateway uses **auto-admit** (Option B):

- **New observed runners** — When a runner first appears via `POST /observations`, the upsert SQL omits `admin_status`, so the database column default (`'online'`) applies. `health_status` is hardcoded to `'HEALTHY'`. The runner is immediately eligible for job dispatch.
- **Existing runners** — Observation ingestion never overwrites `admin_status`. An operator-set value of `offline` or `maintenance` persists across subsequent observations. Only `health_status` and the legacy `status` are updated to `HEALTHY` on each observation.
- **Operator override** — `POST /runners/{id}/admin-status` explicitly sets `admin_status` to `online`, `offline`, or `maintenance`, overriding the DB default for that runner.

### Scheduler eligibility

A runner is eligible for job dispatch when:
- `admin_status = 'online'` AND `health_status = 'HEALTHY'`
- **OR** `admin_status = 'online'` (skips health check entirely — bypass mode)

A runner is rejected when:
- `admin_status = 'offline'` or `admin_status = 'maintenance'`
- `admin_status = 'online'` but `health_status != 'HEALTHY'`

## Relationships

- A **Job** targets one **Workspace** on one **Runner VM**
- A **Workspace** is served by one **OpenCode Serve** instance
- An **Executor Plugin** performs infrastructure actions on the **Runner VM**
- The **Gateway** coordinates all of the above and is the only component callers interact with
- **Paperclip** coordinates agents and can call the **Gateway** to execute coding work
- The **Gateway** does NOT replace **Paperclip** — they operate at different layers

## Example dialogue

> **Dev:** "When a **Job** is submitted, does the **Gateway** create the **Workspace** itself?"
> **Domain expert:** "No — it delegates to the **Executor Plugin**. The Gateway doesn't know or care whether that's AWX, SSH, or a local shell."

> **Dev:** "Does the **Gateway** talk directly to the **Runner VM**?"
> **Domain expert:** "Only via the **Executor Plugin**. The Gateway should never SSH into the VM itself — that's the executor's job."

> **Dev:** "Does the Gateway replace Paperclip?"
> **Domain expert:** "No — Paperclip coordinates agents and higher-level work. The Gateway controls OpenCode execution. Paperclip can sit above the Gateway and call it as part of an agent workflow."

## Flagged ambiguities

- (none yet)

### AWX Executor Plugin

**AWX Job Templates** — Three AWX job templates that map to the ExecutorPlugin lifecycle interface:

| Template | extra_vars | Lifecycle methods |
|---|---|---|
| `gateway-create-workspace` | `repo_url`, `branch`, `job_id` | `create_workspace` |
| `gateway-opencode-lifecycle` | `action: start\|stop\|restart`, `workspace_path` | `start_opencode`, `stop_opencode`, `restart_opencode` |
| `gateway-workspace-teardown` | `action: collect\|cleanup`, `workspace_path` | `collect_state`, `cleanup_workspace` |

**AWX API Client** — Thin httpx-based client that calls the AWX REST API with a Bearer token. Uses the same pattern as `OpenCodeServeClient`.

**Gateway AWX Configuration** — All env vars use the `GATEWAY_AWX_*` prefix:
- `GATEWAY_AWX_BASE_URL` — AWX instance URL
- `GATEWAY_AWX_TOKEN` — AWX API Bearer token
- `GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID` — Template ID for workspace creation
- `GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID` — Template ID for opencode lifecycle
- `GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID` — Template ID for teardown/collect
- `GATEWAY_AWX_POLL_INTERVAL_SECONDS` — Seconds between poll retries (default 5)
- `GATEWAY_AWX_TIMEOUT_SECONDS` — Max seconds to wait for a job (default 300)

**AWXApiClient** — Thin httpx-based client that calls the AWX REST API with a Bearer token. Uses the same pattern as `OpenCodeServeClient` with custom exception classes `AWXConnectionError`, `AWXTimeoutError`, `AWXHTTPError`, and `AWXJobError`.

**AWXExecutorPlugin** — Concrete `ExecutorPlugin` implementation that maps the lifecycle methods to AWX job template launches. Receives an `AWXApiClient` instance and template IDs via dependency injection from the executor factory. Does not read env vars directly.

**Lifecycle AWX Job ID Persistence** — The AWX executor tracks AWX job IDs for
all lifecycle steps (not just `create_workspace`) via an `_executor_job_ids`
mapping of Gateway job UUID → AWX job ID. After `start_opencode` launches
successfully, the API layer persists this AWX job ID as `executor_job_id` on
the `gateway_jobs` database row. The `abort_job` endpoint passes
`gateway_job_id` to `stop_opencode` and `cleanup_workspace` as well, so
cross-process cancellation can target the currently-active lifecycle AWX job
even when the abort request lands in a different process than the one that
launched it. Without this mechanism, only `create_workspace` would have its
AWX job ID persisted, and cancellation of subsequent lifecycle jobs would
rely solely on in-memory state that does not survive process boundaries.
