"""Executor factory — FastAPI dependency that returns the active ExecutorPlugin."""

from __future__ import annotations

from app.executors import ExecutorPlugin
from app.executors.local import LocalExecutor


def get_executor() -> ExecutorPlugin:
    """Return the active executor plugin.

    Used as a FastAPI dependency so tests can override it with
    ``app.dependency_overrides[get_executor]``.
    """
    return LocalExecutor()
