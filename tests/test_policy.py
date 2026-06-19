"""Tests for the pre-flight policy engine."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import Settings
from app.policy.base import (
    Alert,
    AlertHandler,
    LoggingAlertHandler,
    PolicyViolation,
    PreflightPolicy,
)
from app.policy.observation import (
    RUNNER_STATUS_BLOCKED_DISK,
    RUNNER_STATUS_BLOCKED_MEMORY,
    RUNNER_STATUS_UNKNOWN,
    ObservationBasedPolicy,
)
from tests.conftest import mock_row


def _make_mock_conn(
    *,
    runner_id="runner-1",
    runner_uuid=None,
    runner_status="HEALTHY",
    admin_status=None,
    health_status=None,
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
    runner_status:
        The runner's DB status (e.g. "HEALTHY", "offline", "maintenance").
    admin_status:
        The runner's admin_status value. Defaults to None.
    health_status:
        The runner's health_status value. Defaults to None.
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

    def _make_runner_row():
        return mock_row({
            "id": runner_uuid,
            "status": runner_status,
            "admin_status": admin_status,
            "health_status": health_status,
        })

    # Runner lookup: SELECT id, admin_status, health_status, status FROM runners
    async def _fetchrow_runner(sql, *args):
        if "FROM runners" in sql:
            return _make_runner_row()
        return None

    if disk_used_percent is not None or memory_used_percent is not None:
        # Observation lookup: SELECT ... FROM runner_observations ...
        obs_at = observed_at if observed_at is not None else datetime.now(timezone.utc)

        async def _fetchrow_obs(sql, *args):
            if "FROM runners" in sql:
                return _make_runner_row()
            if "FROM runner_observations" in sql:
                return mock_row(
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
            if "UPDATE runners SET health_status" in sql
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
            if "UPDATE runners SET health_status" in sql
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
                return mock_row({"id": runner_uuid, "status": "HEALTHY"})
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
            if "UPDATE runners SET health_status" in sql
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
                return mock_row({"id": runner_uuid, "status": "HEALTHY"})
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


class TestManualStatusPolicy:
    """Tests for manual (operator-set) status handling in ObservationBasedPolicy."""

    @pytest.mark.asyncio
    async def test_offline_runner_rejects_with_policy_violation(self) -> None:
        """When admin_status is 'offline', check() raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="offline",
            admin_status="offline",
            disk_used_percent=50.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "manual_status"
        assert detail["runner_id"] == "runner-1"
        assert "offline" in detail["message"]

    @pytest.mark.asyncio
    async def test_maintenance_runner_rejects_with_policy_violation(self) -> None:
        """When admin_status is 'maintenance', check() raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="maintenance",
            admin_status="maintenance",
            disk_used_percent=50.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "manual_status"
        assert detail["runner_id"] == "runner-1"
        assert "maintenance" in detail["message"]

    @pytest.mark.asyncio
    async def test_online_runner_with_disk_pressure_raises_violation(
        self,
    ) -> None:
        """When admin_status is 'online', fresh observation checks still run.
        Disk pressure raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="online",
            admin_status="online",
            disk_used_percent=95.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "disk"
        assert detail["runner_id"] == "runner-1"

    @pytest.mark.asyncio
    async def test_online_runner_with_memory_pressure_raises_violation(
        self,
    ) -> None:
        """When admin_status is 'online', memory pressure raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="online",
            admin_status="online",
            disk_used_percent=50.0,
            memory_used_percent=92.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "memory"
        assert detail["runner_id"] == "runner-1"

    @pytest.mark.asyncio
    async def test_online_runner_with_staleness_raises_violation(
        self,
    ) -> None:
        """When admin_status is 'online', staleness raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        conn = _make_mock_conn(
            runner_status="online",
            admin_status="online",
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

    @pytest.mark.asyncio
    async def test_online_runner_with_healthy_metrics_passes(
        self,
    ) -> None:
        """When admin_status is 'online' and all metrics are healthy, check()
        returns None."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="online",
            admin_status="online",
            disk_used_percent=40.0,
            memory_used_percent=55.0,
        )

        result = await policy.check("runner-1", conn=conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_offline_runner_blocks_even_with_healthy_metrics(self) -> None:
        """An offline runner is blocked even when disk/memory are healthy."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="offline",
            admin_status="offline",
            disk_used_percent=10.0,
            memory_used_percent=20.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.detail["resource"] == "manual_status"

    @pytest.mark.asyncio
    async def test_maintenance_runner_blocks_even_with_healthy_metrics(self) -> None:
        """A maintenance runner is blocked even when disk/memory are healthy."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="maintenance",
            admin_status="maintenance",
            disk_used_percent=10.0,
            memory_used_percent=20.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.detail["resource"] == "manual_status"

    @pytest.mark.asyncio
    async def test_healthy_runner_still_checked_normally(self) -> None:
        """A runner with a system status (HEALTHY) still goes through normal observation checks."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="HEALTHY",
            health_status="HEALTHY",
            disk_used_percent=90.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        # Should be a disk violation, not a manual_status one
        assert exc_info.value.detail["resource"] == "disk"


class TestHealthStatusPolicy:
    """Tests for health_status enforcement in ObservationBasedPolicy."""

    @pytest.mark.asyncio
    async def test_unhealthy_health_status_rejects_when_admin_not_set(
        self,
    ) -> None:
        """When admin_status is None and health_status is unhealthy
        (BLOCKED_DISK_PRESSURE), check() raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="BLOCKED_DISK_PRESSURE",
            admin_status=None,
            health_status="BLOCKED_DISK_PRESSURE",
            disk_used_percent=50.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "health_status"
        assert detail["runner_id"] == "runner-1"
        assert "unhealthy health_status" in detail["message"]

    @pytest.mark.asyncio
    async def test_unhealthy_health_status_blocked_memory_rejects(
        self,
    ) -> None:
        """When admin_status is None and health_status is
        BLOCKED_MEMORY_PRESSURE, check() raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="BLOCKED_MEMORY_PRESSURE",
            admin_status=None,
            health_status="BLOCKED_MEMORY_PRESSURE",
            disk_used_percent=50.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "health_status"
        assert "BLOCKED_MEMORY_PRESSURE" in detail["message"]

    @pytest.mark.asyncio
    async def test_unhealthy_health_status_unknown_rejects(
        self,
    ) -> None:
        """When admin_status is None and health_status is UNKNOWN,
        check() raises PolicyViolation."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="UNKNOWN",
            admin_status=None,
            health_status="UNKNOWN",
            disk_used_percent=50.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["resource"] == "health_status"
        assert "UNKNOWN" in detail["message"]

    @pytest.mark.asyncio
    async def test_healthy_health_status_proceeds_to_observation_checks(
        self,
    ) -> None:
        """When admin_status is None and health_status is HEALTHY,
        normal observation checks run (disk pressure here)."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="HEALTHY",
            admin_status=None,
            health_status="HEALTHY",
            disk_used_percent=95.0,
            memory_used_percent=50.0,
        )

        with pytest.raises(PolicyViolation) as exc_info:
            await policy.check("runner-1", conn=conn)

        # Falls through health_status guard to disk pressure check
        assert exc_info.value.detail["resource"] == "disk"

    @pytest.mark.asyncio
    async def test_online_runner_bypasses_health_status_guard(
        self,
    ) -> None:
        """When admin_status is 'online', the health_status guard is
        skipped — fresh observations are always checked."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="online",
            admin_status="online",
            health_status="BLOCKED_DISK_PRESSURE",  # unhealthy but ignored
            disk_used_percent=40.0,  # fresh data is healthy
            memory_used_percent=55.0,
        )

        result = await policy.check("runner-1", conn=conn)
        # Falls through to fresh observation checks — metrics are healthy
        assert result is None

    @pytest.mark.asyncio
    async def test_null_health_status_proceeds_to_observation_checks(
        self,
    ) -> None:
        """When health_status is NULL, no health_status block is applied."""
        policy = ObservationBasedPolicy()
        conn = _make_mock_conn(
            runner_status="HEALTHY",
            admin_status=None,
            health_status=None,
            disk_used_percent=40.0,
            memory_used_percent=55.0,
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


# ══════════════════════════════════════════════════════════════════════════
# Alert model tests
# ══════════════════════════════════════════════════════════════════════════


class TestAlert:
    """Tests for the Alert dataclass and alert infrastructure."""

    def test_alert_has_required_fields(self) -> None:
        """Alert must carry runner_id, metric, current_value, threshold, level."""
        alert = Alert(
            runner_id="runner-1",
            metric="disk",
            current_value=95.0,
            threshold=80.0,
        )
        assert alert.runner_id == "runner-1"
        assert alert.metric == "disk"
        assert alert.current_value == 95.0
        assert alert.threshold == 80.0
        assert alert.level == "WARNING"

    def test_alert_default_level_is_warning(self) -> None:
        """Level defaults to WARNING when not explicitly set."""
        alert = Alert(
            runner_id="runner-x",
            metric="memory",
            current_value=90.0,
            threshold=85.0,
        )
        assert alert.level == "WARNING"

    def test_alert_custom_level(self) -> None:
        """Level can be overridden for higher-severity conditions."""
        alert = Alert(
            runner_id="runner-critical",
            metric="disk",
            current_value=99.0,
            threshold=80.0,
            level="CRITICAL",
        )
        assert alert.level == "CRITICAL"

    def test_alert_is_dataclass(self) -> None:
        """Alert should be a dataclass for easy construction and comparison."""
        a1 = Alert(runner_id="r1", metric="disk", current_value=90.0, threshold=80.0)
        a2 = Alert(runner_id="r1", metric="disk", current_value=90.0, threshold=80.0)
        assert a1 == a2

    def test_logging_alert_handler_is_callable(self) -> None:
        """LoggingAlertHandler should be an async callable."""
        handler = LoggingAlertHandler()
        assert callable(handler)

    @pytest.mark.asyncio
    async def test_logging_alert_handler_logs_at_warning(self, caplog) -> None:
        """LoggingAlertHandler should emit a WARNING-level structured log."""
        import logging

        caplog.set_level(logging.WARNING)
        handler = LoggingAlertHandler()
        alert = Alert(
            runner_id="runner-abc",
            metric="disk",
            current_value=88.0,
            threshold=80.0,
        )
        await handler(alert)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "WARNING"
        assert "policy_alert" in record.message
        assert "runner_id=runner-abc" in record.message
        assert "metric=disk" in record.message
        assert "current_value=88.0" in record.message
        assert "threshold=80" in record.message
        assert "level=WARNING" in record.message

    def test_alert_handler_protocol_is_type(self) -> None:
        """AlertHandler should be a Protocol (type)."""
        assert isinstance(AlertHandler, type)

    def test_conforming_async_callable_satisfies_alert_handler(self) -> None:
        """An async callable matching the signature should satisfy AlertHandler."""

        class CustomHandler:
            async def __call__(self, alert: Alert) -> None:
                pass

        handler = CustomHandler()
        assert isinstance(handler, AlertHandler)


# ══════════════════════════════════════════════════════════════════════════
# Alert emission integration tests
# ══════════════════════════════════════════════════════════════════════════


class TestAlertEmission:
    """Tests for structured alert emission from ObservationBasedPolicy.check()."""

    @pytest.mark.asyncio
    async def test_disk_pressure_emits_alert_with_correct_fields(self) -> None:
        """When disk exceeds threshold, an alert is emitted with correct fields."""
        captured_alerts: list[Alert] = []

        async def _capture(alert: Alert) -> None:
            captured_alerts.append(alert)

        policy = ObservationBasedPolicy(alert_handlers=[_capture])
        conn = _make_mock_conn(disk_used_percent=90.0, memory_used_percent=50.0)

        try:
            await policy.check("runner-disk-alert", conn=conn)
        except PolicyViolation:
            pass

        assert len(captured_alerts) == 1
        alert = captured_alerts[0]
        assert alert.runner_id == "runner-disk-alert"
        assert alert.metric == "disk"
        assert alert.current_value == 90.0
        assert alert.threshold == 80.0
        assert alert.level == "WARNING"

    @pytest.mark.asyncio
    async def test_memory_pressure_emits_alert_with_correct_fields(self) -> None:
        """When memory exceeds threshold, an alert is emitted with correct fields."""
        captured_alerts: list[Alert] = []

        async def _capture(alert: Alert) -> None:
            captured_alerts.append(alert)

        policy = ObservationBasedPolicy(alert_handlers=[_capture])
        conn = _make_mock_conn(disk_used_percent=50.0, memory_used_percent=92.0)

        try:
            await policy.check("runner-mem-alert", conn=conn)
        except PolicyViolation:
            pass

        assert len(captured_alerts) == 1
        alert = captured_alerts[0]
        assert alert.runner_id == "runner-mem-alert"
        assert alert.metric == "memory"
        assert alert.current_value == 92.0
        assert alert.threshold == 85.0
        assert alert.level == "WARNING"

    @pytest.mark.asyncio
    async def test_healthy_runner_emits_no_alert(self) -> None:
        """When runner is healthy, no alert is emitted."""
        captured_alerts: list[Alert] = []

        async def _capture(alert: Alert) -> None:
            captured_alerts.append(alert)

        policy = ObservationBasedPolicy(alert_handlers=[_capture])
        conn = _make_mock_conn(disk_used_percent=40.0, memory_used_percent=55.0)

        result = await policy.check("runner-healthy", conn=conn)
        assert result is None
        assert len(captured_alerts) == 0

    @pytest.mark.asyncio
    async def test_multiple_alert_handlers_all_called(self) -> None:
        """When multiple alert handlers are registered, all are called."""
        captured_by: list[str] = []

        async def _handler_a(alert: Alert) -> None:
            captured_by.append("a")

        async def _handler_b(alert: Alert) -> None:
            captured_by.append("b")

        policy = ObservationBasedPolicy(alert_handlers=[_handler_a, _handler_b])
        conn = _make_mock_conn(disk_used_percent=95.0, memory_used_percent=50.0)

        try:
            await policy.check("runner-multi", conn=conn)
        except PolicyViolation:
            pass

        assert "a" in captured_by
        assert "b" in captured_by
        assert len(captured_by) == 2

    @pytest.mark.asyncio
    async def test_alert_handler_failure_does_not_block_others(self) -> None:
        """If one alert handler raises, subsequent handlers still execute."""
        captured_by: list[str] = []

        async def _failing_handler(alert: Alert) -> None:
            raise RuntimeError("Simulated handler failure")

        async def _good_handler(alert: Alert) -> None:
            captured_by.append("good")

        policy = ObservationBasedPolicy(alert_handlers=[_failing_handler, _good_handler])
        conn = _make_mock_conn(disk_used_percent=95.0, memory_used_percent=50.0)

        try:
            await policy.check("runner-failover", conn=conn)
        except PolicyViolation:
            pass

        assert "good" in captured_by

    @pytest.mark.asyncio
    async def test_default_alert_handler_is_logging_handler(self) -> None:
        """When no alert_handlers specified, LoggingAlertHandler is used by default."""
        policy = ObservationBasedPolicy()
        assert len(policy._alert_handlers) == 1
        assert isinstance(policy._alert_handlers[0], LoggingAlertHandler)

    @pytest.mark.asyncio
    async def test_disk_pressure_with_custom_threshold_emits_correct_threshold(
        self,
    ) -> None:
        """Alert threshold matches the custom Settings threshold."""
        captured_alerts: list[Alert] = []

        async def _capture(alert: Alert) -> None:
            captured_alerts.append(alert)

        settings = Settings(disk_threshold_percent=75.0)
        policy = ObservationBasedPolicy(settings=settings, alert_handlers=[_capture])
        conn = _make_mock_conn(disk_used_percent=80.0, memory_used_percent=50.0)

        try:
            await policy.check("runner-custom", conn=conn)
        except PolicyViolation:
            pass

        assert len(captured_alerts) == 1
        assert captured_alerts[0].threshold == 75.0
        assert captured_alerts[0].current_value == 80.0

    @pytest.mark.asyncio
    async def test_alert_handler_receives_alert_arg_type_annotation(self) -> None:
        """AlertHandler protocol check verifies the annotation is Alert."""

        class TypedHandler:
            async def __call__(self, alert: Alert) -> None:
                pass

        handler = TypedHandler()
        assert isinstance(handler, AlertHandler)
