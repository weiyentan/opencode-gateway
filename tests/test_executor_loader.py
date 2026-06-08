import pytest
from app.executors import EXECUTOR_REGISTRY, ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.local import LocalExecutor


def test_local_executor_is_registered():
    """LocalExecutor should be registered with name 'local'."""
    assert EXECUTOR_REGISTRY["local"] is LocalExecutor


def test_get_executor_defaults_to_local():
    """get_executor() with no args should return a LocalExecutor instance."""
    executor = get_executor()
    assert isinstance(executor, LocalExecutor)


def test_get_executor_unknown_type_raises(monkeypatch):
    """get_executor() should raise ValueError for an unknown executor type."""
    monkeypatch.setenv("GATEWAY_EXECUTOR_TYPE", "nonexistent_executor")

    # Settings may be cached — reload the factory so it picks up the new env value.
    import importlib
    import app.executors.factory
    importlib.reload(app.executors.factory)
    from app.executors.factory import get_executor as _get_executor

    with pytest.raises(ValueError, match="Unknown executor type"):
        _get_executor()


def test_executor_registry_is_dict():
    """EXECUTOR_REGISTRY should be a dict containing the 'local' key."""
    assert isinstance(EXECUTOR_REGISTRY, dict)
    assert "local" in EXECUTOR_REGISTRY


def test_local_executor_is_concrete_subclass():
    """LocalExecutor must be a concrete subclass of ExecutorPlugin (regression guard)."""
    assert issubclass(LocalExecutor, ExecutorPlugin)
