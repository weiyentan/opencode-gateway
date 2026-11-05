"""Non-writing tests for the fail-closed disposable-Postgres guard (issue #826).

Every refusal path is exercised without opening a real connection: the
``asyncpg.connect`` seam is replaced by a spy that fails the test if an
unapproved DSN reaches it.  The positive path uses a fake connection that only
accepts the read-only probes the guard is allowed to issue.
"""

from __future__ import annotations

import pytest

from tests.integration import _disposable_postgres as guard


@pytest.fixture(autouse=True)
def _attested_development_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(guard.DISPOSABLE_ATTESTATION_ENV, "1")
    monkeypatch.setenv("GATEWAY_ENV", "development")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)


@pytest.fixture
def approved_target() -> guard.DatabaseTarget:
    return guard.DatabaseTarget(
        host="localhost",
        port=guard.DISPOSABLE_DATABASE_PORT,
        name=guard.DISPOSABLE_DATABASE_NAME,
        user=guard.DISPOSABLE_DATABASE_USER,
        password="test",
    )


def _forbid_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _connect(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "verify_disposable_target connected for an unapproved target"
        )

    monkeypatch.setattr(guard.asyncpg, "connect", _connect)


class _FakeConnection:
    """Only the read-only probes the disposable preflight may issue."""

    def __init__(
        self,
        *,
        current_database: str = guard.DISPOSABLE_DATABASE_NAME,
        server_version_num: str = "160004",
    ) -> None:
        self.current_database = current_database
        self.server_version_num = server_version_num
        self.queries: list[str] = []
        self.closed = False

    async def fetchval(self, query: str) -> str:
        self.queries.append(query)
        if query == "SELECT current_database()":
            return self.current_database
        if query == "SHOW server_version_num":
            return self.server_version_num
        raise AssertionError(f"unexpected (possibly destructive) probe: {query}")

    async def close(self) -> None:
        self.closed = True


def _use_fake_connection(
    monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection
) -> None:
    async def _connect(*args: object, **kwargs: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(guard.asyncpg, "connect", _connect)


async def test_non_disposable_host_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_connect(monkeypatch)
    target = guard.DatabaseTarget(
        host="db.production.example.com",
        port=guard.DISPOSABLE_DATABASE_PORT,
        name=guard.DISPOSABLE_DATABASE_NAME,
        user=guard.DISPOSABLE_DATABASE_USER,
        password="test",
    )

    with pytest.raises(guard.DisposableDatabaseError, match="approved loopback host"):
        await guard.verify_disposable_target(target)


async def test_shared_port_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=5432,
        name=approved_target.name,
        user=approved_target.user,
        password=approved_target.password,
    )

    with pytest.raises(guard.DisposableDatabaseError, match="approved disposable service port"):
        await guard.verify_disposable_target(target)


async def test_non_dedicated_database_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=approved_target.port,
        name="opencode_gateway_test",
        user=approved_target.user,
        password=approved_target.password,
    )

    with pytest.raises(guard.DisposableDatabaseError, match="approved disposable databases"):
        await guard.verify_disposable_target(target)


async def test_benchmark_database_is_approved(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=approved_target.port,
        name=guard.BENCHMARK_DATABASE_NAME,
        user=approved_target.user,
        password=approved_target.password,
    )
    connection = _FakeConnection(current_database=guard.BENCHMARK_DATABASE_NAME)
    _use_fake_connection(monkeypatch, connection)

    resolved = await guard.verify_disposable_target(target)

    assert resolved == target
    assert connection.queries == ["SELECT current_database()", "SHOW server_version_num"]
    assert connection.closed is True


async def test_non_dedicated_credentials_are_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=approved_target.port,
        name=approved_target.name,
        user="postgres",
        password=approved_target.password,
    )

    with pytest.raises(guard.DisposableDatabaseError, match="dedicated disposable-service user"):
        await guard.verify_disposable_target(target)


async def test_empty_credentials_are_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=approved_target.port,
        name=approved_target.name,
        user=approved_target.user,
        password="",
    )

    with pytest.raises(guard.DisposableDatabaseError, match="password is empty"):
        await guard.verify_disposable_target(target)


async def test_missing_attestation_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    monkeypatch.delenv(guard.DISPOSABLE_ATTESTATION_ENV)

    with pytest.raises(guard.DisposableDatabaseError, match="GATEWAY_TEST_DISPOSABLE_DB=1"):
        await guard.verify_disposable_target(approved_target)


async def test_production_environment_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    monkeypatch.setenv("GATEWAY_ENV", "production")

    with pytest.raises(guard.DisposableDatabaseError, match="GATEWAY_ENV="):
        await guard.verify_disposable_target(approved_target)


async def test_kubernetes_process_is_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    _forbid_connect(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    with pytest.raises(guard.DisposableDatabaseError, match="inside Kubernetes"):
        await guard.verify_disposable_target(approved_target)


async def test_unreachable_database_raises_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    async def _connect(*args: object, **kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(guard.asyncpg, "connect", _connect)

    with pytest.raises(guard.DisposableDatabaseError, match="is unreachable"):
        await guard.verify_disposable_target(approved_target)


async def test_wrong_postgres_major_is_refused(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    connection = _FakeConnection(server_version_num="150004")
    _use_fake_connection(monkeypatch, connection)

    with pytest.raises(guard.DisposableDatabaseError, match="requires PostgreSQL 16"):
        await guard.verify_disposable_target(approved_target)

    assert connection.closed is True


async def test_connected_database_mismatch_is_refused(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    connection = _FakeConnection(current_database="some_other_database")
    _use_fake_connection(monkeypatch, connection)

    with pytest.raises(guard.DisposableDatabaseError, match="connected to"):
        await guard.verify_disposable_target(approved_target)

    assert connection.closed is True


async def test_approved_target_passes_with_read_only_probes(
    monkeypatch: pytest.MonkeyPatch, approved_target: guard.DatabaseTarget
) -> None:
    connection = _FakeConnection()
    _use_fake_connection(monkeypatch, connection)

    resolved = await guard.verify_disposable_target(approved_target)

    assert resolved == approved_target
    assert connection.queries == ["SELECT current_database()", "SHOW server_version_num"]
    assert connection.closed is True


def test_describe_never_includes_the_password(
    approved_target: guard.DatabaseTarget,
) -> None:
    secret = "s3cret-not-the-database-name"
    target = guard.DatabaseTarget(
        host=approved_target.host,
        port=approved_target.port,
        name=approved_target.name,
        user=approved_target.user,
        password=secret,
    )

    assert secret not in target.describe()
    assert target.describe() == "localhost:5433/opencode_gateway_disposable_test"
