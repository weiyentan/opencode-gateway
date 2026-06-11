"""Executor factory — FastAPI dependency that returns the active ExecutorPlugin."""

from __future__ import annotations

from app.core.config import Settings
from app.executors import EXECUTOR_REGISTRY, ExecutorPlugin


def get_executor() -> ExecutorPlugin:
    """Return the active executor plugin.

    Reads ``executor_type`` from ``Settings`` and looks up the
    corresponding class in ``EXECUTOR_REGISTRY``.  Used as a FastAPI
    dependency so tests can override it with
    ``app.dependency_overrides[get_executor]``.

    Raises:
        ValueError: If the configured executor type is not in the registry.
    """
    settings = Settings()
    executor_cls = EXECUTOR_REGISTRY.get(settings.executor_type)
    if executor_cls is None:
        raise ValueError(
            f"Unknown executor type: {settings.executor_type!r}. "
            f"Available types: {list(EXECUTOR_REGISTRY)}"
        )
    return executor_cls()
