# ADR 0003: Port Allocation in Postgres

> **Status: SUPERSEDED (2026-07-16, issue #207)**
>
> This ADR has been superseded by the observability service refactor.
> Port allocation was an execution-era concern — the Gateway no longer
> manages workspace provisioning or opencode serve instances.  The
> `workspaces` table and port allocation logic have been removed from
> the codebase.

## Status

Superseded (was Accepted) - removed during issue #207 observability refactor.

## Original Body (retained for historical reference)

## Context

Each `opencode serve` instance running as a systemd service on the Runner VM needs a unique port. The port range is 10000-10999, allowing up to 1000 concurrent instances.

The allocation source of truth could live in:
1. **Postgres** — the Gateway tracks used ports in the `workspaces` table
2. **VM file** — a port bitmap or lock file on the Runner VM, managed by Ansible playbooks
3. **Both** — Postgres as source of truth, VM file as cache

## Decision

Use Postgres as the sole source of truth for port allocation. The AWX playbook asks the Gateway "what port should I use?" rather than self-allocating on the VM.

## Rationale

- The Gateway already tracks workspace state in Postgres; the port is a natural column on the `workspaces` table
- Centralised state avoids file-locking races across concurrent playbook runs
- Port state is visible via the Gateway API for debugging and observability
- Simplified playbook logic — no port-scanning or file-locking on the VM side
- The port range (1000 ports) is small enough that a simple `SELECT port FROM workspaces WHERE port IS NOT NULL` to find the next free port is trivially fast

## Consequences

Positive:
- No cross-VM race conditions for port allocation
- Port state is queryable via the API
- Simpler Ansible playbook logic

Negative:
- The Gateway must be reachable during workspace creation (if it's down, ports can't be allocated)
- Port cleanup must happen when workspaces are removed (the `cleanup_status` column tracks this)

## Alternatives Considered

**VM file-based allocation**: Avoids Gateway dependency during workspace creation but introduces file-locking races and makes port state invisible to the API.

**Both Postgres + VM file**: Adds synchronization complexity without clear benefit at MVP scale.
