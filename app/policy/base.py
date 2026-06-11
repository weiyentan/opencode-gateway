"""Pre-flight policy protocol — the pluggable interface for pre-job checks."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PreflightPolicy(Protocol):
    """Protocol for pre-flight check implementations.

    A *PreflightPolicy* is called by the Gateway before it accepts a job.
    The check inspects the runner targeted by the job and returns ``None``
    when the runner is healthy enough to accept work.

    Implementations that detect an unhealthy runner should raise an
    exception (e.g. :class:`PreflightRejection`) — see
    :mod:`app.policy.observation` for the skeleton implementation.
    """

    async def check(self, runner_id: str) -> None:  # pragma: no cover
        """Inspect *runner_id* and return ``None`` if the runner is healthy.

        Parameters
        ----------
        runner_id:
            The identifier of the runner (VM) that will host the job's
            workspace.  The concrete implementation determines what
            operations are performed against this identifier.

        Returns
        -------
        None
            The runner is healthy.  The Gateway may proceed with job
            creation.
        """
        ...
