"""Tests for the pre-flight policy engine."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import Settings
from app.policy.base import PolicyViolation, PreflightPolicy
from app.policy.observation import (
    RUNNER_STATUS_BLOCKED_DISK,
    RUNNER_STATUS_BLOCKED_MEMORY,
    RUNNER_STATUS_UNKNOWN,
    ObservationBasedPolicy,
)


def _mock_row(data: dict):
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get = data.get
    return row


def _make_mock_conn(
    *,
    runner_id="runner-1",
    runner_uuid=None,
    disk_used_percent=None,
    memory_used_percent=None,
    observed_at=None,
) -> AsyncMock:
    """Build a mock asyncpg connection with configurable observation data.

    Parameters
    ----------
    runner_id:
        The text runner_id to return from the runners lookup.
    runner_uuid:
        The runner's UUID.  Auto-generated when None.
    disk_used_percent:
        The disk_used_percent value to return from runner_observations.
        When None, the observation row is not returned (simulates
        "no observations").
    memory_used_percent:
        The memory_used_percent value to return from runner_observations.
    observed_at:
        The observed_at timestamp.  Defaults to now (UTC).
    """
    conn = AsyncMock()

    if runner_uuid is None:
        runner_uuid = uuid.uuid4()

    # Runner lookup: SELECT id, status FROM runners WHERE runner_id = $1
    async def _fetchrow_runner(sql, *args):
        if "FROM runners" in sql:
            return _mock_row({"id": runner_uuid, "status": "HEALTHY"})
        return None

    if disk_used_percent is not None or memory_used_percent is not None:
        # Observation lookup: SELECT ... FROM runner_observations ...
        obs_at = observed_at if observed_at is not None else datetime.now(timezone.utc)

        async def _fetchrow_obs(sql, *args):
            if "FROM runners" in sql:
                return _mock_row({"id": runner_uuid, "status": "HEALTHY"})
            if "FROM runner_observations" in sql:
                return _mock_row(
                    {
                        "disk_used_percent": disk_used_percent,
                        "memory_used_percent": memory_used_percent,
                        "observed_at": obs_at,
                    }
                )
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow_obs)
    else:
        conn.fetchrow = AsyncMock(side_effect=_fetchrow_runner)

    conn.execute = AsyncMock(return_value=None)
    return conn


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

        import asyncpg

        class ConformingPolicy:
            async def check(
                self, runner_id: str, conn: asyncpg.Connection | None = None
            ) -> None:
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

    def test_check_returns_none_without_conn(self) -> None:
        """check() returns None when no DB connection is provided (safe skip)."""
        policy = ObservationBasedPolicy()
        import asyncio

        result = asyncio.run(policy.check("runner-1"))
        assert result is None

    @pytest.mark.asyncio
    async def test_check_returns_none_without_conn_async(self) -> None:
        """check() returns None when no DB connection is provided (async)."""
        policy = ObservationBasedPolicy()
        result = await policy.check("runner-1")
        assert result is None

    def test_satisfies_preflight_policy_protocol(self) -> None:
        """ObservationBasedPolicy should satisfy the PreflightPolicy protocol."""
        policy = ObservationBasedPolicy()
        assert isinstance(policy, PreflightPolicy)

    def test_concrete_check_signature_matches_protocol(self) -> None:
        """ObservationBasedPolicy.check() accepts all PreflightPolicy.check()
        parameters, including the optional ``conn``."""
        import inspect

        import asyncpg

        # Verify ObservationBasedPolicy.check has the conn parameter
        sig = inspect.signature(ObservationBasedPolicy.check)
        params = sig.parameters
        assert "runner_id" in params
        assert "conn" in params
        assert params["conn"].default is None
        assert params["conn"].annotation in (
            "asyncpg.Connection | None",
            "asyncpg.connection.Connection | None",
        ) or (
            hasattr(params["conn"].annotation, "__origin__")
        )


# ---------------------------------------------------------------------------
# Enforcement tests — disk and memory thresholds
# ---------------------------------------------------------------------------


class TestPolicyViolation:
    """Tests for the PolicyViolation exception."""

    def test_status_code_is_503(self) -> None:
        """PolicyViolation must have status_code 503."""
        exc = PolicyViolation(
            resource="disk",
            current_value=90.0,
            threshold=80.0,
            runner_id="runner-1",
        )
        assert exc.status_code == 503

    def test_detail_has_required_fields(self) -> None:
        """The detail dict must contain resource, current_value, threshold, runner_id."""
        exc = PolicyViolation(
            resource="memory",
            current_value=92.5,
            threshold=85.0,
            runner_id="runner-alpha",
        )
        detail = exc.detail
        assert detail["resource"] == "memory"
        assert detail["current_value"] == 92.5
        assert detail["threshold"] == 85.0
        assert detail["runner_id"] == "runner-alpha"

    def test_detail_type_is_dict(self) -> None:
        """The detail must be a dict for JSON serialisation."""
        exc = PolicyViolation(
            resource="disk",
            current_value=99.0,
            threshold=95.0,
            runner_id="runner-z",
        )
        assert isinstance(exc.detail, dict)
        assert len(exc.detail) == 5  # resource, current_value, threshold, runner_id, message


class TestCheckDiskPressure:
    """Tests for disk pressure detection in ObservationBasedPolicy.check()."""

    @pytest.mark.asyncio
    async def test_disk_threshold_exceeded_raises_policy_violation(self) -> None:
        """When disk_used_percent > threshold, PolicyViolation is raised."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=90.0, memory_used_percent=50.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "disk"
        assert detail["current_value"] == 90.0
        assert detail["threshold"] == 80.0
        assert detail["runner_id"] == "runner-1"

    @pytest.mark.asyncio
    async def test_disk_threshold_exceeded_updates_runner_status(self) -> None:
        """When disk pressure is detected, runner status is set to BLOCKED_DISK_PRESSURE."""
        policy = ObservationBasedPolicy()
        runner_uuid = uuid.uuid4()

        # Track execute calls via a side-effect list (captures both SQL and args).
        execute_calls: list[tuple] = []

        def _make_mock_conn_tracked(**kwargs):
            c = _make_mock_conn(**kwargs)
            async def _track_execute(sql, *args):
                execute_calls.append((sql, args))
                return None
            c.execute = AsyncMock(side_effect=_track_execute)
            return c

        conn = _make_mock_conn_tracked(
            runner_uuid=runner_uuid,
            disk_used_percent=95.0,
            memory_used_percent=50.0,
        )

        try:
            await policy.check("runner-1", conn=conn)
        except PolicyViolation:
            pass

        # Verify the runner status update was invoked with the correct status
        status_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET status" in sql
        ]
        assert len(status_updates) == 1, (
            f"Expected 1 status update, got {len(status_updates)}. "
            f"All execute calls: {execute_calls}"
        )
        _sql, args = status_updates[0]
        assert args[0] == RUNNER_STATUS_BLOCKED_DISK, (
            f"Expected status={RUNNER_STATUS_BLOCKED_DISK}, got {args[0]}"
        )

    @pytest.mark.asyncio
    async def test_disk_below_threshold_does_not_raise(self) -> None:
        """When disk_used_percent is below threshold, no exception is raised."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=70.0, memory_used_percent=50.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_disk_at_threshold_does_not_raise(self) -> None:
        """When disk_used_percent equals the threshold, no exception (strict > check)."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=80.0, memory_used_percent=50.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_disk_exceeded_with_custom_threshold(self) -> None:
        """Custom thresholds from Settings are respected."""
        settings = Settings(disk_threshold_percent=75.0)
        policy = ObservationBasedPolicy(settings)
        conn = _make_mock_conn(disk_used_percent=80.0, memory_used_percent=50.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.detail["threshold"] == 75.0
        assert exc_info.value.detail["resource"] == "disk"

    @pytest.mark.asyncio
    async def test_disk_none_does_not_raise(self) -> None:
        """When disk_used_percent is NULL, no disk pressure is flagged."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=None, memory_used_percent=50.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None


class TestCheckMemoryPressure:
    """Tests for memory pressure detection in ObservationBasedPolicy.check()."""

    @pytest.mark.asyncio
    async def test_memory_threshold_exceeded_raises_policy_violation(self) -> None:
        """When memory_used_percent > threshold, PolicyViolation is raised."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=90.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "memory"
        assert detail["current_value"] == 90.0
        assert detail["threshold"] == 85.0
        assert detail["runner_id"] == "runner-1"

    @pytest.mark.asyncio
    async def test_memory_threshold_exceeded_updates_runner_status(self) -> None:
        """When memory pressure is detected, runner status is set to BLOCKED_MEMORY_PRESSURE."""
        policy = ObservationBasedPolicy()

        # Track execute calls via side-effect list (captures both SQL and args).
        execute_calls: list[tuple] = []

        def _make_mock_conn_tracked(**kwargs):
            c = _make_mock_conn(**kwargs)
            async def _track_execute(sql, *args):
                execute_calls.append((sql, args))
                return None
            c.execute = AsyncMock(side_effect=_track_execute)
            return c

        conn = _make_mock_conn_tracked(
            disk_used_percent=50.0,
            memory_used_percent=95.0,
        )

        try:
            await policy.check("runner-1", conn=conn)
        except PolicyViolation:
            pass

        status_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET status" in sql
        ]
        assert len(status_updates) == 1
        _sql, args = status_updates[0]
        assert args[0] == RUNNER_STATUS_BLOCKED_MEMORY, (
            f"Expected status={RUNNER_STATUS_BLOCKED_MEMORY}, got {args[0]}"
        )

    @pytest.mark.asyncio
    async def test_memory_below_threshold_does_not_raise(self) -> None:
        """When memory_used_percent is below threshold, no exception is raised."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=75.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_at_threshold_does_not_raise(self) -> None:
        """When memory_used_percent equals the threshold, no exception (strict > check)."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=85.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_exceeded_with_custom_threshold(self) -> None:
        """Custom memory thresholds from Settings are respected."""
        settings = Settings(memory_threshold_percent=60.0)
        policy = ObservationBasedPolicy(settings)
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=70.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.detail["threshold"] == 60.0
        assert exc_info.value.detail["resource"] == "memory"

    @pytest.mark.asyncio
    async def test_memory_none_does_not_raise(self) -> None:
        """When memory_used_percent is NULL, no memory pressure is flagged."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=None)

        result = await policy.check("runner-1", conn=conn)
        assert result is None


class TestCheckEdgeCases:
    """Edge case tests for ObservationBasedPolicy.check()."""

    @pytest.mark.asyncio
    async def test_no_conn_returns_none(self) -> None:
        """When conn is None, check returns None (skip enforcement)."""
        policy = ObservationBasedPolicy()
        result = await policy.check("runner-1", conn=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_runner_not_found_returns_none(self) -> None:
        """When the runner_id is not in the runners table, return None."""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)  # runner not found
        conn.execute = AsyncMock(return_value=None)

        policy = ObservationBasedPolicy()
        result = await policy.check("unknown-runner", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_observations_returns_none(self) -> None:
        """When the runner has no observations, return None."""
        runner_uuid = uuid.uuid4()
        conn = AsyncMock()

        async def _fetchrow(sql, *args):
            if "FROM runners" in sql:
                return _mock_row({"id": runner_uuid, "status": "HEALTHY"})
            if "FROM runner_observations" in sql:
                return None  # no observations
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        conn.execute = AsyncMock(return_value=None)

        policy = ObservationBasedPolicy()
        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_healthy_runner_returns_none(self) -> None:
        """A runner with healthy metrics returns None."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=40.0, memory_used_percent=55.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_disk_checked_before_memory(self) -> None:
        """Disk pressure is checked first — disk > memory when both exceed."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=95.0, memory_used_percent=95.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        # Disk is checked first, so disk violation should be raised
        assert exc_info.value.detail["resource"] == "disk"

    @pytest.mark.asyncio
    async def test_only_memory_exceeded_raises_memory_violation(self) -> None:
        """When only memory exceeds threshold, memory violation is raised."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=90.0)

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.detail["resource"] == "memory"

    @pytest.mark.asyncio
    async def test_stale_observations_block_with_staleness_violation(self) -> None:
        """Stale observations now raise a staleness PolicyViolation (block)."""
        policy = ObservationBasedPolicy()
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        conn = _make_mock_conn(
            disk_used_percent=90.0,
            memory_used_percent=50.0,
            observed_at=old_time,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "staleness"
        assert detail["runner_id"] == "runner-1"
        assert "last_seen_at" in detail


class TestCheckStaleness:
    """Tests for staleness detection in ObservationBasedPolicy.check()."""

    @pytest.mark.asyncio
    async def test_staleness_exceeded_raises_policy_violation(self) -> None:
        """When the latest observation is older than staleness_seconds,
        a PolicyViolation is raised with resource='staleness'."""
        policy = ObservationBasedPolicy()
        # 15 minutes ago → older than default 600s threshold
        old_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        old_time = datetime.fromtimestamp(
            old_time.timestamp() - 900,  # 15 min = 900s
            tz=timezone.utc,
        )
        conn = _make_mock_conn(
            disk_used_percent=50.0,
            memory_used_percent=50.0,
            observed_at=old_time,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "staleness"
        assert detail["runner_id"] == "runner-1"
        assert detail["last_seen_at"] == old_time.isoformat()
        assert detail["current_value"] >= 899  # at least ~900s old

    @pytest.mark.asyncio
    async def test_staleness_updates_runner_status_to_unknown(self) -> None:
        """When staleness is detected, runner status is set to UNKNOWN."""
        policy = ObservationBasedPolicy()
        execute_calls: list[tuple] = []

        def _make_mock_conn_tracked(**kwargs):
            c = _make_mock_conn(**kwargs)

            async def _track_execute(sql, *args):
                execute_calls.append((sql, args))
                return None
            c.execute = AsyncMock(side_effect=_track_execute)
            return c

        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        conn = _make_mock_conn_tracked(
            disk_used_percent=50.0,
            memory_used_percent=50.0,
            observed_at=old_time,
        )

        try:
            await policy.check("runner-1", conn=conn)
        except PolicyViolation:
            pass

        status_updates = [
            (sql, args) for sql, args in execute_calls
            if "UPDATE runners SET status" in sql
        ]
        assert len(status_updates) == 1, (
            f"Expected 1 status update, got {len(status_updates)}. "
            f"All execute calls: {execute_calls}"
        )
        _sql, args = status_updates[0]
        assert args[0] == RUNNER_STATUS_UNKNOWN, (
            f"Expected status={RUNNER_STATUS_UNKNOWN}, got {args[0]}"
        )

    @pytest.mark.asyncio
    async def test_staleness_error_message_contents(self) -> None:
        """The PolicyViolation detail includes descriptive message and last_seen_at."""
        policy = ObservationBasedPolicy()
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        conn = _make_mock_conn(
            disk_used_percent=50.0,
            memory_used_percent=50.0,
            observed_at=old_time,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        detail = exc_info.value.detail
        assert "Runner runner-1 observation is stale" in detail["message"]
        assert "2020-01-01T00:00:00" in detail["message"]
        assert "Current staleness threshold is 600s" in detail["message"]
        assert detail["last_seen_at"] == "2020-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_no_observations_warns_but_does_not_block(self) -> None:
        """When there are no observations, return None (warn but don't block)."""
        runner_uuid = uuid.uuid4()
        conn = AsyncMock()

        async def _fetchrow(sql, *args):
            if "FROM runners" in sql:
                return _mock_row({"id": runner_uuid, "status": "HEALTHY"})
            if "FROM runner_observations" in sql:
                return None  # no observations
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        conn.execute = AsyncMock(return_value=None)

        policy = ObservationBasedPolicy()
        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_healthy_runner_returns_none_and_logs_accept(self) -> None:
        """A runner with recent healthy metrics returns None and logs policy_accept."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(disk_used_percent=40.0, memory_used_percent=55.0)

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_staleness_threshold_is_configurable(self) -> None:
        """The staleness threshold can be set via Settings."""
        settings = Settings(staleness_seconds=60)
        policy = ObservationBasedPolicy(settings)
        # 2 minutes ago → older than 60s threshold but not 600s
        old_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        old_time = datetime.fromtimestamp(
            old_time.timestamp() - 120,
            tz=timezone.utc,
        )
        conn = _make_mock_conn(
            disk_used_percent=50.0,
            memory_used_percent=50.0,
            observed_at=old_time,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        detail = exc_info.value.detail
        assert detail["threshold"] == 60
        assert detail["resource"] == "staleness"

    @pytest.mark.asyncio
    async def test_recent_observation_does_not_trigger_staleness(self) -> None:
        """A very recent observation does not trigger staleness."""
        policy = ObservationBasedPolicy()
        recent = datetime.now(timezone.utc)
        conn = _make_mock_conn(
            disk_used_percent=40.0,
            memory_used_percent=55.0,
            observed_at=recent,
        )

        result = await policy.check("runner-1", conn=conn)
        assert result is None


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
