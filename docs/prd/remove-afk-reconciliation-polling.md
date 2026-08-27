# Remove AFK Reconciliation Polling

## Problem Statement

The AFK Outcome Consumer currently wires an automatic scheduled reconciliation timer that periodically triggers reconciliation against provider APIs. This polling behavior is undesirable: it introduces an implicit, in-process scheduling mechanism that runs provider API calls outside of any explicit trigger, complicates the consumer's startup lifecycle, and makes terminal-state convergence timing implicit rather than deliberate. The timer loop also couples the consumer's runtime behavior to a cadence that is better owned by an explicit scheduling mechanism.

## Solution

Remove the AFK Outcome Consumer's automatic scheduled reconciliation timer wiring. The consumer's startup will perform Kafka consumption and live event persistence only — no provider API calls and no immediate reconciliation. The reconciliation methods, provider adapters, and cadence/window settings are preserved for archival purposes; they are not deleted as part of this feature. Explicit/manual convergence remains available through the AFK Backfill CLI (`scripts/afk_backfill.py`), which is retained for terminal-state convergence. Merged/closed convergence may wait until an explicit backfill run is invoked.

No application scheduler or polling replacement is added. AWX is the approved future mechanism for recurring scheduling, but AWX job template and schedule creation are explicitly out of scope for this slice. Deployment manifests and existing environment variables remain unchanged.

## User Stories

1. As an operator, I want the AFK Outcome Consumer to start up and begin Kafka consumption and live event persistence immediately, so that live ingestion is not delayed by any reconciliation setup.
2. As an operator, I want the consumer startup to make no provider API calls, so that startup is fast and does not depend on provider availability.
3. As an operator, I want the consumer startup to create no reconciliation task, so that no implicit polling is introduced at runtime.
4. As an operator, I want the automatic scheduled reconciliation timer wiring removed, so that the consumer no longer self-schedules provider reconciliation.
5. As an operator, I want the reconciliation methods and provider adapters preserved, so that explicit reconciliation remains available without reimplementation.
6. As an operator, I want the cadence/window settings preserved, so that the archival configuration remains intact for future scheduling use.
7. As an operator, I want the AFK Backfill CLI retained, so that I can explicitly converge terminal states on demand.
8. As an operator, I want merged/closed convergence to wait until an explicit backfill run, so that convergence timing is deliberate rather than implicit.
9. As an operator, I want no application scheduler or polling replacement added, so that the consumer does not reintroduce implicit scheduling.
10. As an operator, I want the existing AFK read model to remain supported, so that downstream readers are unaffected by this change.
11. As an operator, I want the manual backfill path to remain supported, so that I retain a deterministic convergence mechanism.
12. As an operator, I want deployment manifests and existing environment variables unchanged, so that no operational configuration churn accompanies this change.

## Implementation Decisions

### 1. AFK Consumer Timer Lifecycle Removal

Remove the automatic scheduled reconciliation timer wiring from the AFK Outcome Consumer lifecycle. The consumer no longer starts a periodic reconciliation timer on startup. The reconciliation methods, provider adapters, and cadence/window settings are retained for archival purposes and are not deleted as part of this feature.

### 2. Consumer Startup Behavior

Consumer startup performs Kafka consumption and live event persistence only. No provider API calls and no immediate reconciliation occur at startup. The consumer does not create a reconciliation task during startup, and no application scheduler or polling replacement is introduced.

### 3. Reconciliation Test Boundary

Tests retain direct reconciliation/backfill tests, remove or revise timer-loop expectations, and add an assertion that startup creates no reconciliation task. This keeps the reconciliation behavior covered while reflecting the removal of the automatic timer.

## Testing Decisions

- Retain direct reconciliation and backfill tests so that explicit convergence behavior remains covered.
- Remove or revise timer-loop expectations that assumed an automatic scheduled reconciliation timer.
- Add an assertion that consumer startup creates no reconciliation task.
- Verify that startup performs Kafka consumption and live event persistence without provider API calls.
- Confirm that the AFK read model and manual backfill remain supported.

## Out of Scope

- AWX job template and schedule creation (AWX is the approved future mechanism for recurring scheduling, but its setup is out of scope now).
- Adding an application scheduler or polling replacement.
- Deleting the reconciliation file/code, provider adapters, or cadence/window settings.
- Changing deployment manifests or existing environment variables.
- Documentation changes (this PRD is for the implementation slice; docs are not changed now).
- Schema changes or new APIs.

## Further Notes

The completed prior PRD was deleted to prevent future requirement pollution. This PRD describes the implementation slice only. The existing AFK read model and manual backfill remain supported. Merged/closed convergence may wait until an explicit backfill run.
