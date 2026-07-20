# OpenCode Gateway — Domain Language

An observability service for headless OpenCode serve instances. Collects
telemetry, monitors health, and provides a REST API for observability data.

> **Refactor note (issue #207):** This project is being refactored from an
> execution control plane into an observability service. Execution-era
> subsystems (executor plugins, job scheduling, workspace lifecycle,
> policy engine) have been removed. The vocabulary below reflects the
> observability service identity. Future slices will add observability-
> specific concepts.

## Language

**Gateway**:
The observability service. Collects telemetry from Runner VMs, stores
metrics in Postgres, and exposes them through a REST API.
_Avoid_: Backend, server, controller

**OpenCode Serve**:
A long-running headless API process managed by systemd on the Runner VM.
Owns coding sessions, messages, diffs, and tool execution.
_Avoid_: opencode daemon, opencode service (in generic sense)

**Runner VM**:
A persistent virtual machine that hosts workspace directories and
systemd-managed opencode serve instances. Provides the native toolchain
for code editing tasks.
_Avoid_: Worker, node, agent

**Paperclip**:
An agent/work orchestration layer that coordinates agents, goals, task
assignment, governance, budgets, and higher-level workflows. Paperclip
can sit above the Gateway, calling the Gateway API to retrieve
observability data.
_Avoid_: Gateway, execution control plane

## Architecture Note

The Gateway uses a layered architecture:

- **app/api/** — REST endpoints
- **app/core/** — Configuration, auth, logging, factory
- **app/db/** — Postgres pool, migrations, ORM models
- **frontend/** — Aurora Glass telemetry dashboard (vanilla HTML/CSS/JS SPA served at `/`)

Additional layers will be added in future slices.

## Relationship with Paperclip

The Gateway does **not** replace Paperclip — they operate at different
layers. Paperclip coordinates agents and higher-level work. The Gateway
provides observability into the OpenCode infrastructure that Paperclip
manages.

## Flagged Ambiguities

- (none yet)
