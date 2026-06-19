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

    Validates that the AWX base URL, token, and all three template IDs
    are configured before construction — missing values fail fast with
    a clear error so misconfiguration is caught at startup rather than
    at runtime.

    Raises:
        ValueError: If the AWX base URL or token is empty, or if any
            required AWX template ID is ``None`` or zero.
    """
    from app.executors.awx import AWXApiClient, AWXExecutorPlugin

    # ── Validate base URL and token ──────────────────────────────────
    missing_conn: list[str] = []
    if not settings.awx_base_url or not settings.awx_base_url.strip():
        missing_conn.append("awx_base_url")
    if not settings.awx_token or not settings.awx_token.strip():
        missing_conn.append("awx_token")
    if missing_conn:
        raise ValueError(
            f"AWX executor is configured but the following connection "
            f"settings are missing or empty: {missing_conn!r}. "
            f"Set the corresponding GATEWAY_AWX_* environment variables."
        )

    # ── Validate template IDs ───────────────────────────────────────
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


def create_executor_from_settings(settings: Settings) -> ExecutorPlugin | None:
    """Build an executor plugin from settings for app startup.

    This is the single construction path used by the Gateway app
    factory at startup.  It routes to the correct executor
    implementation based on ``settings.executor_type``, using
    :func:`_create_awx_executor` for the ``"awx"`` type (which
    validates all required settings and returns a fully-wired
    instance) and no-arg construction for all other registered types.

    Returns ``None`` when the executor type is unknown so the cleanup
    scheduler can skip ticks gracefully instead of crashing.

    Raises:
        ValueError: If the executor type is ``"awx"`` and required
            AWX settings (base URL, token, template IDs) are missing.
    """
    executor_cls = EXECUTOR_REGISTRY.get(settings.executor_type)
    if executor_cls is None:
        logger.warning(
            "Unknown executor type %r — cleanup scheduler will skip ticks",
            settings.executor_type,
        )
        return None

    if settings.executor_type == "awx":
        return _create_awx_executor(settings)

    return executor_cls()


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
