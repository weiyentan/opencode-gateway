# ADR 0019 — Operator-invoked AFK terminal-state convergence (no in-process scheduler)

## Status

Accepted

## Context

The AFK Outcome Consumer (``app/consumer/afk_consumer.py``) ingests the
external provider-events topic (``afk.events``) and writes canonical
Engineering Events to Postgres. The topic does not carry terminal states
(merged/closed), so the consumer originally ran a **scheduled bounded-window
reconciliation loop** reusing the backfill engine (``scripts.afk_backfill``)
on a config-driven cadence (``GATEWAY_AFK_OUTCOMES_RECONCILE_CADENCE_SECONDS``
/ ``GATEWAY_AFK_OUTCOMES_RECONCILE_WINDOW_SECONDS``) to converge those states.

This scheduling was anticipated in earlier decisions: ADR 0017's consequences
described "scheduled reconciliation as the ultimate self-heal", and the AFK
Outcome Observability PRD planned "live Kafka ingestion (hybrid with scheduled
reconciliation)". The Gateway is observability-only (post-#207 refactor: no
executor, no job scheduler), so embedding a polling loop in a long-running
consumer reintroduces scheduler-like behavior the service deliberately removed.

## Decision

- **Remove automatic scheduled AFK reconciliation polling from the AFK Outcome
  Consumer.** The consumer ingests live events only; it no longer runs a
  bounded-window reconciliation loop on a cadence.
- **Retain ``scripts/afk_backfill.py`` as the explicit, operator-invoked
  reconciliation path.** Terminal merged/closed convergence happens only when an
  operator runs the backfill CLI over a bounded window.
- **Do not add an application scheduler or replacement polling loop** anywhere
  in the Gateway.
- **If recurring scheduling is later needed, schedule the operator workflow
  externally** (e.g. via AWX), keeping the Gateway free of in-process
  schedulers.
- **Consequence of the above:** terminal merged/closed convergence may wait
  until an explicit backfill run; there is no automatic self-heal for terminal
  states.

## Consequences

- The long-running consumer no longer embeds scheduler behavior; it stays a
  pure event-ingestion path consistent with the observability-only identity.
- Terminal-state freshness depends on operator discipline (or an external
  scheduler such as AWX), not on an in-process loop.
- The backfill engine remains the single write path for reconciliation, so
  operator-invoked runs and live ingest still converge idempotently (ADR 0017,
  ADR 0018).
- The config-driven reconciliation cadence/window settings are no longer used
  by the consumer; recurring convergence is an external scheduling concern.

## Alternatives Considered

**Keep the in-consumer scheduled reconciliation loop.** Rejected: it embeds a
scheduler in a long-running service that is deliberately observability-only,
and it reintroduces the polling behavior the post-#207 refactor removed.

**Add a separate application scheduler (e.g. APScheduler) to run backfill.**
Rejected: it adds an in-process scheduler and polling loop, which this decision
explicitly avoids; external scheduling (AWX) already exists for operator
workflows.

**Rely on the live topic alone with no convergence path.** Rejected: terminal
states (merged/closed) are not carried by the topic, so without the explicit
backfill path they would never converge; the operator-invoked backfill retains
a convergence mechanism without an automatic scheduler.
