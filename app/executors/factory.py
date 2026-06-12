"""Executor factory — FastAPI dependency that returns the active ExecutorPlugin."""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.executors import EXECUTOR_REGISTRY, ExecutorPlugin

logger = logging.getLogger(__name__)


def _create_awx_executor(settings: Settings) -> ExecutorPlugin:
    """Build a fully-wired :class:`AWXExecutorPlugin` from settings.

    Constructs an :class:`AWXApiClient` with the configured base URL,
    token, timeout, and poll interval, then injects it together with
    the three template IDs into ``AWXExecutorPlugin``.

    Raises:
        ValueError: If any required AWX template ID is ``None`` or zero.
    """
    from app.executors.awx import AWXApiClient, AWXExecutorPlugin

    template_ids = {
        "awx_create_workspace_template_id": settings.awx_create_workspace_template_id,
        "awx_opencode_lifecycle_template_id": settings.awx_opencode_lifecycle_template_id,
        "awx_workspace_teardown_template_id": settings.awx_workspace_teardown_template_id,
    }

    missing = [key for key, val in template_ids.items() if val is None or val == 0]
    if missing:
        raise ValueError(
            f"AWX executor is configured but the following template IDs "
            f"are missing or zero: {missing!r}. "
            f"Set the corresponding GATEWAY_AWX_* environment variables."
        )

    client = AWXApiClient(
        base_url=settings.awx_base_url,
        token=settings.awx_token,
        timeout_seconds=settings.awx_timeout_seconds,
        poll_interval_seconds=settings.awx_poll_interval_seconds,
    )

    logger.info(
        "AWX executor: base_url=%s timeout=%ds poll=%ds "
        "templates=(create=%d, lifecycle=%d, teardown=%d)",
        settings.awx_base_url,
        settings.awx_timeout_seconds,
        settings.awx_poll_interval_seconds,
        settings.awx_create_workspace_template_id,
        settings.awx_opencode_lifecycle_template_id,
        settings.awx_workspace_teardown_template_id,
    )

    return AWXExecutorPlugin(
        client=client,
        create_workspace_template_id=settings.awx_create_workspace_template_id,
        opencode_lifecycle_template_id=settings.awx_opencode_lifecycle_template_id,
        workspace_teardown_template_id=settings.awx_workspace_teardown_template_id,
    )


def get_executor() -> ExecutorPlugin:
    """Return the active executor plugin.

    Reads ``executor_type`` from ``Settings`` and looks up the
    corresponding class in ``EXECUTOR_REGISTRY``.  For the ``"awx"``
    executor, constructs a fully-wired instance with AWXApiClient and
    template IDs from settings.

    Used as a FastAPI dependency so tests can override it with
    ``app.dependency_overrides[get_executor]``.

    Raises:
        ValueError: If the configured executor type is not in the registry
            or if required AWX settings are missing.
    """
    settings = Settings()
    executor_type = settings.executor_type
    executor_cls = EXECUTOR_REGISTRY.get(executor_type)
    if executor_cls is None:
        raise ValueError(
            f"Unknown executor type: {executor_type!r}. "
            f"Available types: {list(EXECUTOR_REGISTRY)}"
        )

    if executor_type == "awx":
        return _create_awx_executor(settings)

    return executor_cls()
