# Gateway remains a reporting service

Status: accepted

The Gateway consumes watcher and provider events, persists reporting facts,
maintains rebuildable projections, and exposes read APIs. It is not an AFK
orchestration service and must not dispatch events back to the FastAPI EDA
Gateway or initiate workflow side effects.

An outbox that triggers AFK execution belongs in the producing or orchestration
service. A Gateway-local outbox may be considered only for a concrete
reporting concern, such as reliable projection or reconciliation work, and
must not create a reverse Gateway-to-EDA workflow dependency.
