# ADR 0030: Superseded ADR retention policy

## Status

Accepted (2026-09-09, issue #664)

## Context

This repository is heavily agent-assisted: autonomous implementers gather
context by reading the README, CONTEXT.md, and `docs/adr/` before making
changes. Superseded ADRs stayed in the working tree "for historical
reference", but their context cost now outweighs their archaeology value:

- they bloat agent doc-gathering — every implementer must read and discard
  them to confirm they are stale;
- they confuse autonomous implementers, who cannot always tell a superseded
  decision from a live one without cross-checking statuses and superseding
  ADRs;
- the observability refactor (issue #207) removed the execution-era
  subsystems those ADRs described, and the producer-owned normalized-event
  contract (fast-api-eda-gateway ADR 0005) moved the contract authority out
  of this repo — the decisions below are definitively historical.

## Decision

Superseded ADRs are **deleted from the working tree**. Git history is the
recovery mechanism: nothing is lost, only unlinked. A superseding ADR must
carry a "supersedes NNN" breadcrumb so readers can follow a decision forward
without stale files in the tree. Recovery is always via git history
(`git log --all -- <path>`).

### Deleted ADRs (2026-09-09, issue #664)

| ADR | Title | Superseded by | Recovery |
|-----|-------|---------------|----------|
| 0002 | Executor Plugin Interface Design | Observability service refactor (issue #207) — execution-era subsystems removed from the codebase | `git log --all -- docs/adr/0002-executor-plugin-interface.md` |
| 0003 | Port Allocation in Postgres | Observability service refactor (issue #207) — workspace provisioning and port management removed from the codebase | `git log --all -- docs/adr/0003-postgres-port-allocation.md` |
| 0020 | Normalized Provider Event Mapping Bridge (Stage 2) | fast-api-eda-gateway ADR 0005 — the producer owns the normalized-event contract; pinned artifacts live in `docs/contracts/normalized-event-v1/` | `git log --all -- docs/adr/0020-normalized-provider-event-mapping-bridge.md` |

## Consequences

- `docs/adr/` contains only current, accepted ADRs plus this retention
  policy; the README ADR index no longer lists deleted ADRs.
- Readers who need a superseded decision's content recover it from git
  history with the commands above — the working tree no longer carries
  stale decision documents that bloat agent doc-gathering.
- Going forward, an ADR that supersedes an earlier decision deletes the
  earlier ADR from the working tree and records the "supersedes NNN"
  breadcrumb in its own status/context.
