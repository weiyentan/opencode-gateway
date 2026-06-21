# ADR 0002: Executor Plugin Interface Design

## Status

Accepted

## Context

The Gateway needs to delegate infrastructure actions (create workspace, start/stop opencode, collect state, clean up) to external systems. The default MVP executor is AWX, but others (SSH, local shell, Kubernetes Jobs) should be possible later.

The interface must be generic enough to support multiple backends but specific enough to be useful.

## Decision

Use a seven-method async interface with typed Pydantic request/response models:

```python
class ExecutorPlugin:
    name: str
    async def create_workspace(self, request): ...
    async def start_opencode(self, request): ...
    async def stop_opencode(self, request): ...
    async def restart_opencode(self, request): ...
    async def collect_state(self, request): ...
    async def cleanup_workspace(self, request): ...
    async def cancel_job(self, request): ...
```

Exclude `provision_runner` from the base interface — runner provisioning is an infrastructure bootstrap concern, not a per-job lifecycle action.

## Rationale

- Seven methods cover the full workspace/service lifecycle without over-abstracting
- Generic action names (not AWX-specific terms like `launch_job_template`) keep the interface backend-agnostic
- Typed Pydantic models provide validation and documentation at the boundary
- Leaving `provision_runner` out keeps the per-job interface focused and avoids mixing bootstrap concerns with operational ones

## Refinement (issue #70)

The seven-method interface was inspected to identify which methods are
actually called at runtime by the Gateway.  Four methods are actively
invoked:

* ``create_workspace``
* ``start_opencode``
* ``stop_opencode``
* ``cleanup_workspace``

The remaining three — ``restart_opencode``, ``collect_state``, and
``cancel_job`` — are retained as **intentional future surface**:

* ``restart_opencode`` is a natural extension of the lifecycle (the
  operator may want to bounce the service without a full teardown).
* ``collect_state`` supports a future monitoring / telemetry loop that
  would poll workspace health without joining it to a create/cleanup
  cycle.
* ``cancel_job`` provides a generic cancellation mechanism for running
  jobs (e.g. stopping an AWX workflow), ready for when a call site is
  added in the Gateway.

Both are annotated as future surface in the source and remain required
by the ABC so every executor provides a uniform implementation when
Gateway call sites are added.

Similarly, the ``OpenCodeClientProtocol`` has seven methods but only
two — ``get_session_diff`` and ``abort_session`` — are called at
runtime.  The remaining five (``health``, ``list_sessions``,
``get_session``, ``create_session``, ``delete_session``) are
documented as future surface.

## Consequences

Positive:
- The Gateway never calls infrastructure-specific APIs directly
- Adding a new executor type only requires implementing the interface
- Pydantic models serve as living documentation
- Unused methods are documented as future surface, reducing cognitive
  load for readers who only need to understand the active subset

Negative:
- Some executor-specific capabilities may not fit the generic interface
- Future executor types may need additional methods (can be added later)

## Alternatives Considered

**Richer interface with provision_runner**: Would mix bootstrap lifecycle with per-job operations, creating unclear responsibility boundaries.

**Dict-based parameters**: Would sacrifice type safety and self-documentation.

**Command pattern with a single execute(action, params) method**: Would push action-specific logic into the Gateway rather than the executor.
