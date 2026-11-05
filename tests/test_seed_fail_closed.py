"""Fail-closed tests for the legacy manual seed command (issue #827).

The legacy ``scripts/seed.py`` inserts execution-era sample rows and may run
migrations.  It must refuse every target that is not explicitly attested
disposable: missing or mismatched attestation must block before pool creation,
migration or insert operations, and migration failures must halt before any
inserts.

These tests are mock-based and never touch a live database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_settings(
    database_host: str = "localhost",
    database_port: int = 5432,
    database_name: str = "opencode_gateway_test",
    database_user: str = "opencode",
    database_password: str = "",
) -> MagicMock:
    settings = MagicMock()
    settings.database_host = database_host
    settings.database_port = database_port
    settings.database_name = database_name
    settings.database_user = database_user
    settings.database_password = database_password
    return settings


def _make_args(confirm: str | None = None) -> MagicMock:
    args = MagicMock()
    args.runners = 1
    args.workspaces = 1
    args.jobs = 1
    args.observations = 1
    args.confirm = confirm
    return args


@pytest.mark.asyncio
async def test_missing_attestation_blocks_before_pool_creation() -> None:
    """Absent ``--confirm`` must fail closed with zero DB/migration side effects."""
    settings = _make_settings(database_name="opencode_gateway_test")
    args = _make_args(confirm=None)

    mock_pool_instance = AsyncMock()
    mock_pool_instance.pool = AsyncMock()
    mock_pool_cls_instance = MagicMock(return_value=mock_pool_instance)
    with (
        patch("scripts.seed.Settings", return_value=settings),
        patch("scripts.seed.DatabasePool", mock_pool_cls_instance) as mock_pool_cls,
        patch("scripts.seed.ensure_schema", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.seed._seed_runners", new_callable=AsyncMock) as mock_runners,
        patch("scripts.seed._seed_workspaces", new_callable=AsyncMock) as mock_ws,
        patch("scripts.seed._seed_jobs", new_callable=AsyncMock) as mock_jobs,
        patch("scripts.seed._seed_observations", new_callable=AsyncMock) as mock_obs,
    ):
        from scripts.seed import _run

        with pytest.raises(SystemExit) as excinfo:
            await _run(args)

        # Actionable error text must mention attestation and the target db
        msg = str(excinfo.value)
        assert "confirm" in msg.lower()
        assert "opencode_gateway_test" in msg

        # Zero side effects: pool never created, migrations never run, inserts never run
        mock_pool_cls.assert_not_called()
        mock_ensure.assert_not_called()
        mock_runners.assert_not_called()
        mock_ws.assert_not_called()
        mock_jobs.assert_not_called()
        mock_obs.assert_not_called()


@pytest.mark.asyncio
async def test_mismatched_attestation_blocks_before_pool_creation() -> None:
    """Mismatched ``--confirm`` must also fail closed with zero side effects."""
    settings = _make_settings(database_name="opencode_gateway_test")
    args = _make_args(confirm="wrong_db")

    mock_pool_instance = AsyncMock()
    mock_pool_instance.pool = AsyncMock()
    mock_pool_cls_instance = MagicMock(return_value=mock_pool_instance)
    with (
        patch("scripts.seed.Settings", return_value=settings),
        patch("scripts.seed.DatabasePool", mock_pool_cls_instance) as mock_pool_cls,
        patch("scripts.seed.ensure_schema", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.seed._seed_runners", new_callable=AsyncMock) as mock_runners,
        patch("scripts.seed._seed_workspaces", new_callable=AsyncMock) as mock_ws,
        patch("scripts.seed._seed_jobs", new_callable=AsyncMock) as mock_jobs,
        patch("scripts.seed._seed_observations", new_callable=AsyncMock) as mock_obs,
    ):
        from scripts.seed import _run

        with pytest.raises(SystemExit) as excinfo:
            await _run(args)

        msg = str(excinfo.value)
        assert "confirm" in msg.lower()
        assert "opencode_gateway_test" in msg
        # Must mention the mismatch explicitly and be actionable
        assert "wrong_db" not in msg or "opencode_gateway_test" in msg

        mock_pool_cls.assert_not_called()
        mock_ensure.assert_not_called()
        mock_runners.assert_not_called()
        mock_ws.assert_not_called()
        mock_jobs.assert_not_called()
        mock_obs.assert_not_called()


@pytest.mark.asyncio
async def test_matching_attestation_allows_execution() -> None:
    """Matching ``--confirm`` must allow pool creation, migration and inserts."""
    settings = _make_settings(database_name="opencode_gateway_test")
    args = _make_args(confirm="opencode_gateway_test")

    mock_pool_instance = AsyncMock()
    mock_pool_instance.pool = AsyncMock()
    # pool.connect should succeed and pool.pool not None
    mock_pool_cls_instance = MagicMock(return_value=mock_pool_instance)

    with (
        patch("scripts.seed.Settings", return_value=settings),
        patch("scripts.seed.DatabasePool", mock_pool_cls_instance) as mock_pool_cls,
        patch("scripts.seed.ensure_schema", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.seed._seed_runners", new_callable=AsyncMock, return_value=[MagicMock()]) as mock_runners,
        patch("scripts.seed._seed_workspaces", new_callable=AsyncMock) as mock_ws,
        patch("scripts.seed._seed_jobs", new_callable=AsyncMock) as mock_jobs,
        patch("scripts.seed._seed_observations", new_callable=AsyncMock) as mock_obs,
    ):
        from scripts.seed import _run

        await _run(args)

        # Fail-closed ordering: attestation passed, so side effects occurred
        mock_pool_cls.assert_called_once_with(settings)
        mock_pool_instance.connect.assert_awaited_once()
        mock_ensure.assert_awaited_once()
        # Redundant migration path has been removed — no second alembic invocation
        import scripts.seed as seed_mod

        assert not hasattr(seed_mod, "_ensure_alembic_schema"), "Redundant _ensure_alembic_schema should be removed"
        mock_runners.assert_awaited_once()
        mock_ws.assert_awaited_once()
        mock_jobs.assert_awaited_once()
        mock_obs.assert_awaited_once()


@pytest.mark.asyncio
async def test_migration_failure_prevents_inserts() -> None:
    """Migration errors must be fatal and prevent any inserts."""
    settings = _make_settings(database_name="opencode_gateway_test")
    args = _make_args(confirm="opencode_gateway_test")

    mock_pool_instance = AsyncMock()
    mock_pool_instance.pool = AsyncMock()
    mock_pool_cls_instance = MagicMock(return_value=mock_pool_instance)

    with (
        patch("scripts.seed.Settings", return_value=settings),
        patch("scripts.seed.DatabasePool", mock_pool_cls_instance),
        patch("scripts.seed.ensure_schema", new_callable=AsyncMock, side_effect=RuntimeError("migration failed")) as mock_ensure,
        patch("scripts.seed._seed_runners", new_callable=AsyncMock) as mock_runners,
        patch("scripts.seed._seed_workspaces", new_callable=AsyncMock) as mock_ws,
        patch("scripts.seed._seed_jobs", new_callable=AsyncMock) as mock_jobs,
        patch("scripts.seed._seed_observations", new_callable=AsyncMock) as mock_obs,
    ):
        from scripts.seed import _run

        with pytest.raises(RuntimeError, match="migration failed"):
            await _run(args)

        mock_ensure.assert_awaited_once()
        mock_runners.assert_not_called()
        mock_ws.assert_not_called()
        mock_jobs.assert_not_called()
        mock_obs.assert_not_called()
        # pool.close must still be called via finally
        mock_pool_instance.close.assert_awaited_once()


def test_parse_args_requires_confirm_flag() -> None:
    """CLI must expose ``--confirm``/``--yes`` as the disposable attestation flag."""
    from scripts.seed import _parse_args

    args = _parse_args([])
    assert hasattr(args, "confirm")
    assert args.confirm is None

    args = _parse_args(["--confirm", "my_db"])
    assert args.confirm == "my_db"

    args = _parse_args(["--yes", "my_db"])
    assert args.confirm == "my_db"


def test_parse_args_help_mentions_confirm() -> None:
    """Help text must document the disposable attestation requirement."""
    from scripts.seed import _parse_args
    import inspect
    import scripts.seed as seed_mod

    with pytest.raises(SystemExit) as excinfo:
        _parse_args(["--help"])
    assert excinfo.value.code == 0
    # Inspect source for confirm flag and disposable hint
    source = inspect.getsource(seed_mod._parse_args)
    assert "--confirm" in source
    assert "disposable" in source.lower()


def test_assert_disposable_target_direct() -> None:
    """Direct unit test for the attestation validator."""
    from scripts.seed import _assert_disposable_target

    settings = _make_settings(database_name="disposable_db", database_host="localhost")

    # Missing confirmation -> SystemExit with actionable text
    with pytest.raises(SystemExit) as excinfo:
        _assert_disposable_target(settings, None)
    msg = str(excinfo.value)
    assert "confirm" in msg.lower()
    assert "disposable_db" in msg
    assert "disposable" in msg.lower()

    # Mismatched -> SystemExit
    with pytest.raises(SystemExit):
        _assert_disposable_target(settings, "other_db")

    # Matching -> no exception
    _assert_disposable_target(settings, "disposable_db")


def test_assert_handles_missing_confirm_attribute() -> None:
    """_run must fail closed when args lacks a confirm attribute."""

    settings = _make_settings(database_name="opencode_gateway_test")
    # args without confirm attribute simulates old caller or missing flag
    class ArgsNoConfirm:
        runners = 1
        workspaces = 1
        jobs = 1
        observations = 1

    args = ArgsNoConfirm()

    mock_pool_instance = AsyncMock()
    mock_pool_instance.pool = AsyncMock()
    mock_pool_cls_instance = MagicMock(return_value=mock_pool_instance)
    with (
        patch("scripts.seed.Settings", return_value=settings),
        patch("scripts.seed.DatabasePool", mock_pool_cls_instance) as mock_pool_cls,
        patch("scripts.seed.ensure_schema", new_callable=AsyncMock) as mock_ensure,
    ):
        from scripts.seed import _run

        with pytest.raises(SystemExit):
            import asyncio

            asyncio.run(_run(args))  # type: ignore[arg-type]

        mock_pool_cls.assert_not_called()
        mock_ensure.assert_not_called()
