"""Executor plugin interface — abstract base class and type exports.

Contains the abstract ExecutorPlugin base class and re-exports all
Pydantic request/response models. Concrete implementations (AWX, local,
SSH, etc.) are added as subpackages.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.executors.models import (
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

    Per ADR 0002, every executor must implement these six async lifecycle
    methods.  Each method accepts and returns typed Pydantic models so
    the Gateway never needs to know backend-specific details.
    """

    name: str

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
    async def restart_opencode(
        self, request: RestartOpencodeRequest
    ) -> RestartOpencodeResponse:
        """Restart the OpenCode Serve process for a workspace."""
        ...

    @abstractmethod
    async def collect_state(
        self, request: CollectStateRequest
    ) -> CollectStateResponse:
        """Collect the current state of a workspace and its service."""
        ...

    @abstractmethod
    async def cleanup_workspace(
        self, request: CleanupWorkspaceRequest
    ) -> CleanupWorkspaceResponse:
        """Tear down a workspace directory."""
        ...


__all__ = [
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
