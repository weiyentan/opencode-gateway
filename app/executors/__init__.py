"""Executor plugin interface — abstract base class and type exports.

Contains the abstract ExecutorPlugin base class and re-exports all
Pydantic request/response models. Concrete implementations (AWX, local,
SSH, etc.) are added as subpackages.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.executors.models import (
    CancelJobRequest,
    CancelJobResponse,
    CleanupWorkspaceRequest,
    CleanupWorkspaceResponse,
    CollectStateRequest,
    CollectStateResponse,
    CreateWorkspaceRequest,
    CreateWorkspaceResponse,
    RestartOpencodeRequest,
    RestartOpencodeResponse,
    StartOpencodeRequest,
    StartOpencodeResponse,
    StopOpencodeRequest,
    StopOpencodeResponse,
)


class ExecutorPlugin(ABC):
    """Abstract base class for executor plugins.

    Per ADR 0002, the executor exposes a seven-method async lifecycle
    interface.  Each method accepts and returns typed Pydantic models so
    the Gateway never needs to know backend-specific details.

    .. rubric:: Current active surface

    The Gateway currently calls four of the seven lifecycle methods at
    runtime:

    * :meth:`create_workspace` — provision a workspace directory
    * :meth:`start_opencode`  — start the OpenCode Serve process
    * :meth:`stop_opencode`   — stop the OpenCode Serve process
    * :meth:`cleanup_workspace` — tear down a workspace directory

    .. rubric:: Intentional future surface

    The following three methods are part of the designed seven-method
    lifecycle (ADR 0002) but are not yet invoked by the Gateway at
    runtime.  They exist so that concrete executors can provide them
    uniformly when the Gateway grows call sites that need them:

    * :meth:`restart_opencode` — restart the OpenCode Serve process
    * :meth:`collect_state`    — collect workspace / service state
    * :meth:`cancel_job`       — cancel an in-flight executor job
    """

    name: str

    # -- Active surface ---------------------------------------------------

    @abstractmethod
    async def create_workspace(
        self, request: CreateWorkspaceRequest
    ) -> CreateWorkspaceResponse:
        """Provision a workspace directory on the Runner VM."""
        ...

    @abstractmethod
    async def start_opencode(
        self, request: StartOpencodeRequest
    ) -> StartOpencodeResponse:
        """Start the OpenCode Serve process for a workspace."""
        ...

    @abstractmethod
    async def stop_opencode(
        self, request: StopOpencodeRequest
    ) -> StopOpencodeResponse:
        """Stop the OpenCode Serve process for a workspace."""
        ...

    @abstractmethod
    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        """Tear down a workspace directory."""
        ...

    # -- Intentional future surface ---------------------------------------

    @abstractmethod
    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        """Restart the OpenCode Serve process for a workspace.

        **Future surface.**  Not yet called by the Gateway at runtime;
        included so every executor provides a uniform implementation
        when a call site is added.
        """
        ...

    @abstractmethod
    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        """Collect the current state of a workspace and its service.

        **Future surface.**  Not yet called by the Gateway at runtime;
        included so every executor provides a uniform implementation
        when a call site is added.
        """
        ...

    @abstractmethod
    async def cancel_job(
        self, request: CancelJobRequest
    ) -> CancelJobResponse:
        """Cancel a running job for a workspace.

        **Future surface.**  Not yet called by the Gateway at runtime;
        included so every executor provides a uniform implementation
        when a call site is added.
        """
        ...


# --- Executor plugin registry ------------------------------------------------
# Imported after ExecutorPlugin is defined to avoid circular imports
# (LocalExecutor and AWXExecutorPlugin import ExecutorPlugin from this module).

from app.executors.awx.plugin import AWXExecutorPlugin  # noqa: E402
from app.executors.local import LocalExecutor  # noqa: E402

EXECUTOR_REGISTRY: dict[str, type[ExecutorPlugin]] = {
    "awx": AWXExecutorPlugin,
    "local": LocalExecutor,
}

__all__ = [
    "CancelJobRequest",
    "CancelJobResponse",
    "CleanupWorkspaceRequest",
    "CleanupWorkspaceResponse",
    "CollectStateRequest",
    "CollectStateResponse",
    "CreateWorkspaceRequest",
    "CreateWorkspaceResponse",
    "ExecutorPlugin",
    "RestartOpencodeRequest",
    "RestartOpencodeResponse",
    "StartOpencodeRequest",
    "StartOpencodeResponse",
    "StopOpencodeRequest",
    "StopOpencodeResponse",
]
