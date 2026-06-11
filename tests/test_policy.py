"""Tests for the pre-flight policy engine."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.policy.base import PreflightPolicy
from app.policy.observation import ObservationBasedPolicy


class TestPreflightPolicyProtocol:
    """Verify the PreflightPolicy protocol / interface contract."""

    def test_is_protocol_class(self) -> None:
        """PreflightPolicy should be a type (runtime-checkable Protocol)."""
        assert isinstance(PreflightPolicy, type)

    def test_check_method_exists(self) -> None:
        """The 'check' method is defined on the protocol."""
        assert hasattr(PreflightPolicy, "check")
        assert callable(PreflightPolicy.check)

    def test_conforming_object_satisfies_protocol(self) -> None:
        """An object with a matching async check() method should pass isinstance."""

        class ConformingPolicy:
            async def check(self, runner_id: str) -> None:
                return None

        obj = ConformingPolicy()
        assert isinstance(obj, PreflightPolicy)

    def test_non_conforming_object_fails_protocol(self) -> None:
        """An object missing 'check' should NOT be an instance of the protocol."""

        class NonConformingPolicy:
            async def other_method(self) -> None:
                return None

        obj = NonConformingPolicy()
        assert not isinstance(obj, PreflightPolicy)

    def test_check_signature_is_async_callable(self) -> None:
        """The protocol's check method is defined as an async function."""
        import inspect

        sig = inspect.signature(PreflightPolicy.check)
        params = list(sig.parameters.keys())
        assert "runner_id" in params
        assert inspect.iscoroutinefunction(PreflightPolicy.check)
        # With from __future__ import annotations, the annotation is a string
        assert sig.return_annotation in (None, "None")


class TestObservationBasedPolicy:
    """Verify ObservationBasedPolicy initialisation and defaults."""

    def test_default_thresholds(self) -> None:
        """Thresholds should come from Settings defaults."""
        policy = ObservationBasedPolicy()
        assert policy.disk_threshold_percent == 80.0
        assert policy.memory_threshold_percent == 85.0
        assert policy.staleness_seconds == 600

    def test_custom_thresholds_via_settings(self) -> None:
        """Custom Settings values should be reflected in the policy."""
        settings = Settings(
            disk_threshold_percent=90.0,
            memory_threshold_percent=70.0,
            staleness_seconds=300,
        )
        policy = ObservationBasedPolicy(settings)
        assert policy.disk_threshold_percent == 90.0
        assert policy.memory_threshold_percent == 70.0
        assert policy.staleness_seconds == 300

    def test_custom_thresholds_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars should flow through Settings into the policy."""
        monkeypatch.setenv("GATEWAY_DISK_THRESHOLD_PERCENT", "95.0")
        monkeypatch.setenv("GATEWAY_MEMORY_THRESHOLD_PERCENT", "60.0")
        monkeypatch.setenv("GATEWAY_STALENESS_SECONDS", "120")

        settings = Settings()
        policy = ObservationBasedPolicy(settings)
        assert policy.disk_threshold_percent == 95.0
        assert policy.memory_threshold_percent == 60.0
        assert policy.staleness_seconds == 120

    def test_check_returns_none(self) -> None:
        """check() must return None — skeleton implementation, no enforcement."""
        policy = ObservationBasedPolicy()
        # We need to run the async method; use asyncio.run_simple or
        # run the async test directly.
        import asyncio

        result = asyncio.run(policy.check("runner-1"))
        assert result is None

    @pytest.mark.asyncio
    async def test_check_returns_none_async(self) -> None:
        """check() must return None when awaited directly (async test)."""
        policy = ObservationBasedPolicy()
        result = await policy.check("runner-1")
        assert result is None

    def test_satisfies_preflight_policy_protocol(self) -> None:
        """ObservationBasedPolicy should satisfy the PreflightPolicy protocol."""
        policy = ObservationBasedPolicy()
        assert isinstance(policy, PreflightPolicy)


class TestConfigIntegration:
    """Verify that Settings fields are correctly declared."""

    def test_settings_has_threshold_fields(self) -> None:
        """Settings must expose the three threshold fields with correct types."""
        settings = Settings()
        assert hasattr(settings, "disk_threshold_percent")
        assert hasattr(settings, "memory_threshold_percent")
        assert hasattr(settings, "staleness_seconds")
        assert isinstance(settings.disk_threshold_percent, float)
        assert isinstance(settings.memory_threshold_percent, float)
        assert isinstance(settings.staleness_seconds, int)

    def test_field_descriptions_are_present(self) -> None:
        """Each threshold field should have a Field() description."""
        from pydantic.fields import FieldInfo

        for attr in (
            "disk_threshold_percent",
            "memory_threshold_percent",
            "staleness_seconds",
        ):
            field_info = Settings.model_fields[attr]
            assert isinstance(field_info, FieldInfo)
            assert field_info.description, (
                f"Field '{attr}' is missing a Field() description"
            )
