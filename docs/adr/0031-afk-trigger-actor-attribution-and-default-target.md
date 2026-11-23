# ADR 0031: AFK Trigger Actor Attribution and Default Target Resolution

## Status

Proposed (23 November 2026)

## Context

An AFK label can be applied by different authorised users, and the selected
execution target must follow the person who initiated the AFK run. The
provider event carries that account identity, but the identity must survive
beyond webhook processing so that later reviewer, developer, and recovery
stages can continue the same run.

The account that applied the label is distinct from the issue or change-request
author and from the AWX service account that launches jobs. AWX's
`launched_by` value therefore cannot serve as the original triggering actor.
Provider numeric IDs would be stable, but the agreed operator-managed routing
configuration is keyed by provider-qualified usernames. Operators will update
the mapping when a username changes.

The Gateway already persists an immutable `logical_target` on the canonical
AFK Run (migration 0051, issue #856). This ADR does not introduce another
target store or AFK Run identity.

## Decision

### Capture the original actor

For an authenticated provider event that starts an AFK Run, FastAPI EDA
Gateway captures the username from the validated event:

- GitHub: `sender.login`
- GitLab: `user.username`

The mapping key is provider-qualified: `github:<username>` or
`gitlab:<username>`. FastAPI EDA Gateway owns actor-to-target resolution.
It passes both the captured actor and the selected logical target to the
canonical OpenCode Gateway run-provisioning API. OpenCode Gateway persists
the actor on the canonical AFK Run alongside its existing target.

The stored actor is the original trigger actor. A later `/afk_review`,
`@developer`, reviewer verdict, retry, or AWX service identity must not
replace it.

### Resolve targets and apply the default

Authorization is evaluated before target resolution:

1. Reject an unauthorised or unidentified actor.
2. If the authorised actor has a configured mapping, select that logical
   target.
3. If the authorised actor has no mapping, select `default`.

This default applies to every authorised AFK label trigger, regardless of
provider or repository. A missing username is not treated as an unmapped
authorised user.

### Preserve target affinity through the lifecycle

The logical target chosen for a new run is stored on the canonical AFK Run
and remains immutable. Reviewer, developer, recovery, and other continuation
stages use the target already stored on that run, even when a different
account or an automation identity performs the later action.

For a legacy run whose `logical_target` is absent, continuation resolves the
original stored actor using the current actor-to-target mapping. If that
mapping is absent, the continuation uses `default`. A continuation must not
be held solely because the run has no target binding.

The EDA configuration remains a separate mapping from
`(logical target, lifecycle stage)` to an allowlisted AWX job template.
AWX inventory and credentials determine the actual host and execution
identity.

### Identity and configuration ownership

Actor mapping keys use provider-qualified usernames, not numeric provider
IDs. They are routing configuration keys, not a claim that usernames are
permanent provider identities. When a provider username changes, operators
update the corresponding mapping. An already-persisted run target remains
unchanged by a configuration edit.

FastAPI EDA Gateway owns the actor-to-target mapping and resolution.
OpenCode Gateway owns the canonical AFK Run record and persists its original
actor and immutable target. The EDA/GitOps configuration owns stage-to-AWX
template mapping. These responsibilities remain separate.

## Consequences

- The initial AFK label event must preserve the validated provider username
  through FastAPI EDA Gateway and the OpenCode Gateway provisioning request.
- OpenCode Gateway needs an additive nullable actor field for legacy-run
  compatibility, including request validation, persistence, and readback.
  Existing `logical_target` storage and immutability remain unchanged.
- FastAPI EDA Gateway must apply the same `default` fallback for every
  authorised unmapped label trigger and for a legacy continuation whose
  original actor has no current mapping.
- The routing configuration contains separate actor-to-target and
  target-stage-to-AWX-template maps.
- Mapping a username differently affects newly resolved runs and legacy runs
  without a stored target. It does not move a run that already has an
  immutable target.
- GitHub and GitLab provider-event tests must prove actor extraction, mapping,
  authorization-before-fallback, default selection, and continuation affinity.
  Live routing changes and canaries remain separate rollout decisions.

## Out of Scope

- Using issue/PR/MR authorship or AWX `launched_by` as the trigger actor.
- Inferring or inventing a username when the validated webhook does not
  identify one.
- Automatic migration of an existing run to a different target after a
  mapping change.
- Allowing event payloads to select arbitrary AWX templates or inventory IDs.
- Introducing a second AFK Run ID or independently mutable target authority.

## Related

- [AFK Run ID Database Relationships (ADR 0026)](0026-afk-run-id-database-relationships.md)
- [Immutable logical target migration 0051](../../alembic/versions/0051_afk_run_logical_target.py)
