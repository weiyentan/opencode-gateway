"""Integration smoke test — supervised DB reconnect against a real Postgres
(issue #773): unready -> reconnecting -> ready -> normal API, without restart.

The Gateway connects to the real PostgreSQL **through a controlled TCP
partition proxy**.  Flipping the proxy in and out of partition mode is a
real, wire-level interruption of PostgreSQL connectivity (established
connections are killed, new connections are refused) — the documented
"controlled interruption / failover" smoke path for both startup and
runtime outages.

Prerequisites
-------------
Start the standalone test Postgres container before running::

    docker compose -f docker-compose.test.yml up -d
    pytest tests/integration/test_db_reconnect_smoke.py -v

Connection parameters come from the standard Gateway environment variables
(defaulting to the ``docker-compose.test.yml`` service, port 5433).  Both
tests skip cleanly when no database is reachable.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import patch

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.db.schema import ensure_schema as _real_ensure_schema

_DEFAULT_HOST = os.environ.get("GATEWAY_DATABASE_HOST", "localhost")
_DEFAULT_PORT = int(os.environ.get("GATEWAY_DATABASE_PORT", "5433"))
_DEFAULT_DB = os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test")
_DEFAULT_USER = os.environ.get("GATEWAY_DATABASE_USER", "opencode_test")
_DEFAULT_PASSWORD = os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test")

PROBE_TIMEOUT = 60.0  # overall budget for a readiness transition


class PartitionProxy:
    """A TCP proxy in front of the real Postgres with a partition switch.

    * partition ON  — established upstream connections are killed and new
      connections are refused (accepted then closed): a wire-level outage.
    * partition OFF — bytes are pumped bidirectionally: normal connectivity.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._server: asyncio.AbstractServer | None = None
        self._client_pumps: set[asyncio.Task] = set()
        self.port: int = 0
        self.partitioned = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self._server.sockets is not None
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        for task in list(self._client_pumps):
            task.cancel()
        if self._client_pumps:
            await asyncio.gather(*self._client_pumps, return_exceptions=True)

    async def set_partitioned(self, on: bool) -> None:
        """Flip the partition switch.

        Turning it ON kills every proxied upstream connection (failover
        drop); turning it OFF restores connectivity for new connections.
        """
        self.partitioned = on
        if on:
            for task in list(self._client_pumps):
                task.cancel()
            if self._client_pumps:
                await asyncio.gather(*self._client_pumps, return_exceptions=True)
            self._client_pumps.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Accept one Gateway connection; forward to Postgres unless partitioned."""
        if self.partitioned:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(self._upstream_host, self._upstream_port),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError):  # noqa: UP041 — py39 compat
            writer.close()
            return

        def _make_pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> asyncio.Task:
            async def pump() -> None:
                try:
                    while True:
                        data = await src.read(65536)
                        if not data:
                            break
                        dst.write(data)
                        await dst.drain()
                except (ConnectionError, OSError, asyncio.IncompleteReadError):
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        pass

            return asyncio.create_task(pump())

        pump_tasks = {
            _make_pump(reader, upstream_writer),
            _make_pump(upstream_reader, writer),
        }
        self._client_pumps.update(pump_tasks)
        try:
            done, pending = await asyncio.wait(
                pump_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.done():
                    task.cancel()
        finally:
            self._client_pumps.difference_update(pump_tasks)


def _dsn(host: str, port: int) -> str:
    return (
        f"postgresql://{_DEFAULT_USER}:{_DEFAULT_PASSWORD}"
        f"@{host}:{port}/{_DEFAULT_DB}"
    )


async def _db_reachable() -> bool:
    """Bounded connectivity check against the real Postgres target."""
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=_dsn(_DEFAULT_HOST, _DEFAULT_PORT), timeout=5),
            timeout=10.0,
        )
        await conn.close()
        return True
    except Exception:
        return False


def _unauth_client(app: object) -> AsyncClient:
    """Probe client — /live and /ready are auth-exempt; DB-backed routes are
    authenticated (with the process Admin API Key) so the 503 envelope (not
    401) is exercised."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={
            "Authorization": "Bearer "
            + os.environ.get("GATEWAY_API_KEY", "test-api-key")
        },
    )


async def _get(client: AsyncClient, path: str):
    return await client.get(path)


@contextmanager
def _real_schema_initialization():
    """Restore the real ensure_schema for the app under test.

    The unit-test conftest autouse fixture patches
    ``app.db.session.ensure_schema`` to a no-op for ALL tests under
    ``tests/`` including integration — but this smoke test must exercise
    the real startup schema initialization (Alembic) exactly like
    production.
    """
    with patch("app.db.session.ensure_schema", new=_real_ensure_schema):
        yield


async def _wait_for_ready(client: AsyncClient, expected: int, budget: float = PROBE_TIMEOUT) -> None:
    """Poll /ready until it returns *expected*, failing on budget exhaustion."""
    deadline = asyncio.get_event_loop().time() + budget
    last_status = -1
    while asyncio.get_event_loop().time() < deadline:
        response = await _get(client, "/ready")
        last_status = response.status_code
        if response.status_code == expected:
            return
        await asyncio.sleep(0.4)
    raise AssertionError(
        f"/ready did not reach {expected} within {budget:.0f}s (last: {last_status})"
    )


def _configure_app_env(monkeypatch, proxy_port: int, *, fast_reconnect: bool) -> None:
    """Point the Gateway settings at the proxy with small reconnect knobs."""
    monkeypatch.setenv("GATEWAY_DATABASE_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_DATABASE_PORT", str(proxy_port))
    monkeypatch.setenv("GATEWAY_DATABASE_NAME", _DEFAULT_DB)
    monkeypatch.setenv("GATEWAY_DATABASE_USER", _DEFAULT_USER)
    monkeypatch.setenv("GATEWAY_DATABASE_PASSWORD", _DEFAULT_PASSWORD)
    if fast_reconnect:
        monkeypatch.setenv("GATEWAY_RECONNECT_INITIAL_BACKOFF_SECONDS", "0.2")
        monkeypatch.setenv("GATEWAY_RECONNECT_MAX_BACKOFF_SECONDS", "1.0")
        monkeypatch.setenv("GATEWAY_RECONNECT_TIMEOUT_SECONDS", "2.0")
        monkeypatch.setenv("GATEWAY_RECONNECT_JITTER_RATIO", "0")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_outage_then_auto_recovery(monkeypatch):
    """Postgres unreachable at startup (via partition proxy): the API starts,
    /live is 200, /ready is 503, DB-backed routes return 503; once the
    partition is lifted the Gateway reconnects on its own — /ready 200,
    /health connected, change-requests normal."""
    if not await _db_reachable():
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )

    from app.core.factory import create_app

    proxy = PartitionProxy(_DEFAULT_HOST, _DEFAULT_PORT)
    await proxy.start()
    try:
        await proxy.set_partitioned(True)
        _configure_app_env(monkeypatch, proxy.port, fast_reconnect=True)

        with _real_schema_initialization():
            app = create_app(configure_logging=False)
            async with app.router.lifespan_context(app):
                async with _unauth_client(app) as client:
                    # Startup outage: process alive, not ready, DB-backed 503.
                    live = await _get(client, "/live")
                    assert live.status_code == 200
                    assert live.json()["data"] == {"alive": True}

                    ready = await _get(client, "/ready")
                    assert ready.status_code == 503
                    assert ready.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

                    health = await _get(client, "/health")
                    assert health.status_code == 200
                    assert health.json()["data"]["database"] == "disconnected"

                    change_requests = await _get(client, "/api/v1/afk-outcomes/change-requests")
                    assert change_requests.status_code == 503
                    assert change_requests.json()["status"] == "error"

                    # Connectivity restored — the Gateway must recover on its own.
                    await proxy.set_partitioned(False)
                    await _wait_for_ready(client, expected=200)

                    health = await _get(client, "/health")
                    assert health.json()["data"]["database"] == "connected"

                    change_requests = await _get(client, "/api/v1/afk-outcomes/change-requests")
                    assert change_requests.status_code == 200
                    assert change_requests.json()["status"] == "ok"
    finally:
        await proxy.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_outage_then_auto_recovery(monkeypatch):
    """Postgres becomes unreachable after a healthy startup (simulated
    failover): requests fail gracefully with 503, /ready flips to 503, and
    automatic recovery restores readiness and the normal API."""
    if not await _db_reachable():
        pytest.skip(
            "Test Postgres database not available.  Start it with:\n"
            "  docker compose -f docker-compose.test.yml up -d"
        )

    from app.core.factory import create_app

    proxy = PartitionProxy(_DEFAULT_HOST, _DEFAULT_PORT)
    await proxy.start()
    try:
        _configure_app_env(monkeypatch, proxy.port, fast_reconnect=True)

        with _real_schema_initialization():
            app = create_app(configure_logging=False)
            async with app.router.lifespan_context(app):
                async with _unauth_client(app) as client:
                    # Healthy startup through the proxy.
                    await _wait_for_ready(client, expected=200)
                    change_requests = await _get(client, "/api/v1/afk-outcomes/change-requests")
                    assert change_requests.status_code == 200

                    # Simulated failover: kill the connectivity at the wire level.
                    await proxy.set_partitioned(True)
                    await _wait_for_ready(client, expected=503)

                    change_requests = await _get(client, "/api/v1/afk-outcomes/change-requests")
                    assert change_requests.status_code == 503
                    assert change_requests.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

                    # Failover resolved — automatic recovery restores readiness.
                    await proxy.set_partitioned(False)
                    await _wait_for_ready(client, expected=200)
                    assert (await _get(client, "/live")).status_code == 200

                    change_requests = await _get(client, "/api/v1/afk-outcomes/change-requests")
                    assert change_requests.status_code == 200
                    assert change_requests.json()["status"] == "ok"
    finally:
        await proxy.stop()