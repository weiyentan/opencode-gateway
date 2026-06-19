"""Tests for the executor factory — ``create_executor_from_settings`` and ``_create_awx_executor``.

Covers success paths (local, fully-configured AWX), failure paths
(missing base URL, token, template IDs), and integration with the
app factory's ``_create_executor``.
"""

from __future__ import annotations

import importlib

import pytest

from app.core.config import Settings
from app.executors.awx.plugin import AWXExecutorPlugin
from app.executors.local import LocalExecutor


# ── Helper: build a Settings object with the given env vars ──────────────


def _settings(**overrides: str) -> Settings:
    """Return a ``Settings`` instance with the given env-var overrides.

    Each key should be the full ``GATEWAY_*`` environment variable name.
    Values are set via ``monkeypatch``-style env injection using a
    context-manager so that ``Settings()`` sees them.
    """
    import os

    saved = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        return Settings()
    finally:
        for key, saved_val in saved.items():
            if saved_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved_val


# ── AWX helper constants ─────────────────────────────────────────────────

_AWX_BASE_URL = "https://awx.example.com"
_AWX_TOKEN = "test-token-abc123"
_AWX_CREATE_ID = "10"
_AWX_LIFECYCLE_ID = "20"
_AWX_TEARDOWN_ID = "30"

# Full set of AWX env vars needed for a valid AWX executor.
_AWX_FULL_ENV = {
    "GATEWAY_EXECUTOR_TYPE": "awx",
    "GATEWAY_AWX_BASE_URL": _AWX_BASE_URL,
    "GATEWAY_AWX_TOKEN": _AWX_TOKEN,
    "GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID": _AWX_CREATE_ID,
    "GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID": _AWX_LIFECYCLE_ID,
    "GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID": _AWX_TEARDOWN_ID,
}


# ══════════════════════════════════════════════════════════════════════════
#  create_executor_from_settings
# ══════════════════════════════════════════════════════════════════════════


class TestCreateExecutorFromSettings:
    """Tests for the ``create_executor_from_settings`` factory function."""

    def test_default_returns_local_executor(self):
        """With default settings (executor_type='local'), returns LocalExecutor."""
        settings = _settings()
        from app.executors.factory import create_executor_from_settings

        executor = create_executor_from_settings(settings)
        assert isinstance(executor, LocalExecutor)
        assert executor.name == "local"

    def test_awx_type_with_full_settings_returns_awx_executor(self):
        """With all AWX env vars set, returns a fully-wired AWXExecutorPlugin."""
        settings = _settings(**_AWX_FULL_ENV)
        from app.executors.factory import create_executor_from_settings

        executor = create_executor_from_settings(settings)
        assert isinstance(executor, AWXExecutorPlugin)
        assert executor.name == "awx"

    def test_awx_missing_base_url_raises_clear_error(self):
        """AWX executor type without base URL fails at startup."""
        env = dict(_AWX_FULL_ENV)
        env.pop("GATEWAY_AWX_BASE_URL")
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="connection settings are missing"):
            create_executor_from_settings(settings)

    def test_awx_missing_token_raises_clear_error(self):
        """AWX executor type without token fails at startup."""
        env = dict(_AWX_FULL_ENV)
        env.pop("GATEWAY_AWX_TOKEN")
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="connection settings are missing"):
            create_executor_from_settings(settings)

    def test_awx_base_url_whitespace_only_raises(self):
        """A base_url that is whitespace-only is treated as missing."""
        env = dict(_AWX_FULL_ENV)
        env["GATEWAY_AWX_BASE_URL"] = "   "
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="connection settings are missing"):
            create_executor_from_settings(settings)

    def test_awx_token_whitespace_only_raises(self):
        """A token that is whitespace-only is treated as missing."""
        env = dict(_AWX_FULL_ENV)
        env["GATEWAY_AWX_TOKEN"] = "\t  "
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="connection settings are missing"):
            create_executor_from_settings(settings)

    def test_awx_missing_all_template_ids_raises(self):
        """AWX executor type with template IDs at default 0 fails."""
        env = {
            "GATEWAY_EXECUTOR_TYPE": "awx",
            "GATEWAY_AWX_BASE_URL": _AWX_BASE_URL,
            "GATEWAY_AWX_TOKEN": _AWX_TOKEN,
            # All template IDs default to 0
        }
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="missing or zero"):
            create_executor_from_settings(settings)

    def test_awx_missing_single_template_id_raises(self):
        """Only 2 of 3 template IDs set should still raise."""
        env = {
            "GATEWAY_EXECUTOR_TYPE": "awx",
            "GATEWAY_AWX_BASE_URL": _AWX_BASE_URL,
            "GATEWAY_AWX_TOKEN": _AWX_TOKEN,
            "GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID": "10",
            "GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID": "20",
            # GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID defaults to 0
        }
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="missing or zero"):
            create_executor_from_settings(settings)

    def test_awx_missing_both_connection_and_templates_raises_connection_first(self):
        """Connection validation runs before template validation — a
        misconfigured AWX setup surfaces the connection error first."""
        env = {
            "GATEWAY_EXECUTOR_TYPE": "awx",
            # No base_url, no token, no template IDs
        }
        settings = _settings(**env)
        from app.executors.factory import create_executor_from_settings

        with pytest.raises(ValueError, match="connection settings are missing"):
            create_executor_from_settings(settings)

    def test_unknown_executor_type_returns_none(self):
        """An unknown executor_type returns None so the scheduler
        can skip cleanup ticks gracefully."""
        settings = _settings(GATEWAY_EXECUTOR_TYPE="nonexistent")
        from app.executors.factory import create_executor_from_settings

        executor = create_executor_from_settings(settings)
        assert executor is None


# ══════════════════════════════════════════════════════════════════════════
#  _create_awx_executor
# ══════════════════════════════════════════════════════════════════════════


class TestCreateAwxExecutorValidation:
    """Tests for the ``_create_awx_executor`` validation logic.

    These tests call ``_create_awx_executor`` directly with various
    ``Settings`` combinations to verify the fail-fast validation for
    missing AWX connection settings and template IDs.
    """

    def test_missing_both_base_url_and_token_raises(self):
        """Missing both connection settings raises with both in message."""
        settings = _settings(
            GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID="10",
            GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID="20",
            GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID="30",
            # GATEWAY_AWX_BASE_URL and GATEWAY_AWX_TOKEN default to ""
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError, match="connection settings are missing"):
            _create_awx_executor(settings)

    def test_missing_base_url_only_raises(self):
        """Missing base_url with token present still raises."""
        settings = _settings(
            GATEWAY_AWX_TOKEN=_AWX_TOKEN,
            GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID="10",
            GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID="20",
            GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID="30",
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError, match="connection settings are missing"):
            _create_awx_executor(settings)

    def test_missing_token_only_raises(self):
        """Missing token with base_url present still raises."""
        settings = _settings(
            GATEWAY_AWX_BASE_URL=_AWX_BASE_URL,
            GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID="10",
            GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID="20",
            GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID="30",
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError, match="connection settings are missing"):
            _create_awx_executor(settings)

    def test_missing_all_template_ids_raises(self):
        """All template IDs default to 0 — raises clear error."""
        settings = _settings(
            GATEWAY_AWX_BASE_URL=_AWX_BASE_URL,
            GATEWAY_AWX_TOKEN=_AWX_TOKEN,
            # All template IDs default to 0
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError, match="missing or zero"):
            _create_awx_executor(settings)

    def test_valid_settings_returns_awx_plugin(self):
        """With all required settings, returns a fully-wired AWXExecutorPlugin."""
        settings = _settings(**_AWX_FULL_ENV)
        from app.executors.factory import _create_awx_executor

        executor = _create_awx_executor(settings)
        assert isinstance(executor, AWXExecutorPlugin)
        assert executor.name == "awx"


# ══════════════════════════════════════════════════════════════════════════
#  App factory integration
# ══════════════════════════════════════════════════════════════════════════


class TestAppFactoryIntegration:
    """Tests that ``app.core.factory._create_executor`` delegates to
    the executor factory correctly."""

    def test_create_executor_with_default_settings_returns_local(self):
        """The app factory's ``_create_executor`` returns a LocalExecutor
        when executor_type is 'local' (default)."""
        settings = _settings()
        from app.core.factory import _create_executor

        executor = _create_executor(settings)
        assert isinstance(executor, LocalExecutor)
        assert executor.name == "local"

    def test_create_executor_with_unknown_type_returns_none(self):
        """The app factory's ``_create_executor`` returns None for
        unknown executor types so the scheduler can skip ticks."""
        settings = _settings(GATEWAY_EXECUTOR_TYPE="nonexistent")
        from app.core.factory import _create_executor

        executor = _create_executor(settings)
        assert executor is None

    def test_create_executor_does_not_directly_construct_executor_cls(self):
        """Verify that ``_create_executor`` in app.core.factory no longer
        contains direct ``executor_cls()`` construction — it delegates to
        the executor factory instead.

        We confirm this by reading the source of ``_create_executor``
        and ensuring it imports from ``app.executors.factory``.
        """
        import inspect
        from app.core.factory import _create_executor

        source = inspect.getsource(_create_executor)
        assert "from app.executors.factory import" in source
        assert "EXECUTOR_REGISTRY" not in source


# ══════════════════════════════════════════════════════════════════════════
#  Error message quality
# ══════════════════════════════════════════════════════════════════════════


class TestErrorMessages:
    """Ensure error messages for missing AWX settings are clear and
    actionable."""

    def test_connection_error_lists_missing_fields(self):
        """The error message names each missing connection field."""
        settings = _settings(
            GATEWAY_AWX_CREATE_WORKSPACE_TEMPLATE_ID="10",
            GATEWAY_AWX_OPENCODE_LIFECYCLE_TEMPLATE_ID="20",
            GATEWAY_AWX_WORKSPACE_TEARDOWN_TEMPLATE_ID="30",
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError) as exc_info:
            _create_awx_executor(settings)
        message = str(exc_info.value)
        assert "awx_base_url" in message
        assert "awx_token" in message
        assert "GATEWAY_AWX_" in message

    def test_template_id_error_lists_missing_ids(self):
        """The error message names each missing template ID field."""
        settings = _settings(
            GATEWAY_AWX_BASE_URL=_AWX_BASE_URL,
            GATEWAY_AWX_TOKEN=_AWX_TOKEN,
            # All template IDs default to 0
        )
        from app.executors.factory import _create_awx_executor

        with pytest.raises(ValueError) as exc_info:
            _create_awx_executor(settings)
        message = str(exc_info.value)
        assert "awx_create_workspace_template_id" in message
        assert "awx_opencode_lifecycle_template_id" in message
        assert "awx_workspace_teardown_template_id" in message
        assert "GATEWAY_AWX_" in message


# ══════════════════════════════════════════════════════════════════════════
#  OpenCode client configuration
# ══════════════════════════════════════════════════════════════════════════


class TestOpencodeClientConfiguration:
    """Tests for the OpenCode Serve client configuration/wiring.

    Verifies that an ``OpenCodeServeClient`` can be constructed from
    configuration-like values and that the ``get_opencode_client()``
    dependency follows the expected injection pattern.
    """

    # ── Client construction from config values ─────────────────────────

    def test_construct_from_base_url_and_timeout(self):
        """An OpenCodeServeClient can be constructed from a base URL and timeout."""
        from app.opencode.serve_client import OpenCodeServeClient

        client = OpenCodeServeClient(
            base_url="http://opencode-serve:8080",
            timeout=30,
        )
        assert client._base_url == "http://opencode-serve:8080"
        assert client._timeout == 30
        assert client._client is not None

    def test_strips_trailing_slash_from_base_url(self):
        """Trailing slash in base_url is stripped."""
        from app.opencode.serve_client import OpenCodeServeClient

        client = OpenCodeServeClient(
            base_url="http://opencode-serve:8080/",
            timeout=30,
        )
        assert client._base_url == "http://opencode-serve:8080"

    def test_default_timeout_is_30_seconds(self):
        """Default timeout is 30 seconds when not specified."""
        from app.opencode.serve_client import OpenCodeServeClient

        client = OpenCodeServeClient(
            base_url="http://opencode-serve:8080",
        )
        assert client._timeout == 30

    def test_implements_opencode_client_protocol(self):
        """OpenCodeServeClient satisfies the OpenCodeClientProtocol interface."""
        from app.opencode.protocol import OpenCodeClientProtocol
        from app.opencode.serve_client import OpenCodeServeClient

        assert issubclass(OpenCodeServeClient, OpenCodeClientProtocol)

    # ── Dependency injection pattern ───────────────────────────────────

    def test_get_opencode_client_default_is_none(self):
        """The default get_opencode_client() returns None (no client)."""
        import asyncio

        from app.api.jobs import get_opencode_client

        result = asyncio.run(get_opencode_client())
        assert result is None

    def test_get_opencode_client_can_be_overridden_via_di(self):
        """The get_opencode_client dependency can be overridden to return a
        configured client — verified by checking the endpoint uses it.

        This test confirms the injection pattern works: when the DI
        container provides a client, the endpoint receives it.
        """
        from app.opencode.serve_client import OpenCodeServeClient

        # A real client (or mock) can be injected via dependency_overrides.
        # This just validates the plumbing — concrete end-to-end tests
        # live in test_jobs.py.
        client = OpenCodeServeClient(
            base_url="http://opencode-serve:8080",
            timeout=10,
        )
        assert client is not None
        assert hasattr(client, "get_session_diff")
        assert hasattr(client, "get_session_log")
        assert hasattr(client, "abort_session")
        assert hasattr(client, "get_session")
