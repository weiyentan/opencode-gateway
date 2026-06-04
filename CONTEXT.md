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
