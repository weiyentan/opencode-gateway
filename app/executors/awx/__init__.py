"""AWX executor plugin — API client, exception classes, artifact validation,
and executor.

Exports the AWXApiClient, the AWXExecutorPlugin, artifact models, and
the custom exception hierarchy so the executor factory and other modules
can import from a single package.
"""

from __future__ import annotations

from app.executors.awx.artifacts import (
    CollectStateArtifacts,
    CreateWorkspaceArtifacts,
    StartOpencodeArtifacts,
    validate_artifacts,
)
from app.executors.awx.client import AWXApiClient, AWXJobResult, AWXJobSummary
from app.executors.awx.exceptions import (
    AWXArtifactError,
    AWXClientError,
    AWXConnectionError,
    AWXHTTPError,
    AWXJobError,
    AWXTimeoutError,
)
from app.executors.awx.plugin import AWXExecutorPlugin

__all__ = [
    "AWXApiClient",
    "AWXArtifactError",
    "AWXClientError",
    "AWXConnectionError",
    "AWXExecutorPlugin",
    "AWXHTTPError",
    "AWXJobError",
    "AWXJobResult",
    "AWXJobSummary",
    "AWXTimeoutError",
    "CollectStateArtifacts",
    "CreateWorkspaceArtifacts",
    "StartOpencodeArtifacts",
    "validate_artifacts",
]
