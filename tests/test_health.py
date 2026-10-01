"""Tests for the GET /health endpoint."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app


@pytest.fixture
def client_no_db():
    """Return an httpx AsyncClient against an app with no database pool."""
    app = create_app()
    app.state.pool = None
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


@pytest.fixture
def client_healthy_db():
    """Return an httpx AsyncClient against an app with a mocked healthy pool."""
    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    app = create_app()
    app.state.pool = mock_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


@pytest.fixture
def client_broken_db():
    """Return an httpx AsyncClient against an app with a pool whose acquire raises."""
    mock_pool = AsyncMock()
    mock_pool.acquire = AsyncMock(side_effect=OSError("Connection refused"))
    app = create_app()
    app.state.pool = mock_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-api-key"},
    )


class TestHealthEndpointBasic:
    """Basic smoke tests for GET /health."""

    @pytest.mark.asyncio
    async def test_returns_200_with_correct_json_structure(self, client_no_db):
        """GET /health should return 200 with status, version, and database fields."""
        async with client_no_db as client:
            response = await client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        data = payload["data"]
        assert "status" in data
        assert "version" in data
        assert "database" in data
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_version_is_non_empty_string(self, client_no_db):
        """The version field should be a non-empty string."""
        async with client_no_db as client:
            response = await client.get("/health")

        payload = response.json()
        version = payload["data"]["version"]
        assert isinstance(version, str)
        assert len(version) > 0


class TestHealthDatabaseConnected:
    """Tests for the database connectivity check — connected case."""

    @pytest.mark.asyncio
    async def test_database_connected_when_pool_healthy(self, client_healthy_db):
        """If the pool is healthy, database should be 'connected'."""
        async with client_healthy_db as client:
            response = await client.get("/health")

        payload = response.json()
        assert payload["data"]["database"] == "connected"


class TestHealthDatabaseDisconnected:
    """Tests for the database connectivity check — disconnected cases."""

    @pytest.mark.asyncio
    async def test_database_disconnected_when_pool_is_none(self, client_no_db):
        """If app.state.pool is None, database should be 'disconnected'."""
        async with client_no_db as client:
            response = await client.get("/health")

        payload = response.json()
        assert payload["data"]["database"] == "disconnected"

    @pytest.mark.asyncio
    async def test_database_disconnected_when_acquire_raises(self, client_broken_db):
        """If pool.acquire() raises, database should be 'disconnected' (no 500 crash)."""
        async with client_broken_db as client:
            response = await client.get("/health")

        # Must still return 200, never 500
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"]["database"] == "disconnected"


def _collector_row(credential_id: str, client_name: str, last_heartbeat, total: int):
    """Build an asyncpg-record-like mock row for the collector-health query."""
    row = MagicMock()
    row.__getitem__.side_effect = {
        "credential_id": credential_id,
        "client_name": client_name,
        "last_heartbeat": last_heartbeat,
        "total_records_ingested": total,
    }.__getitem__
    return row


def _health_client_with_collector_rows(rows):
    """Return an httpx client whose pool serves the given collector rows.

    The first conn.fetch call (collector summary) returns `rows`; the
    second (source-database summary) returns nothing; fetchrow (last
    ingest timestamp) returns None.
    """
    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire = AsyncMock(return_value=mock_conn)
    mock_pool.release = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[rows, []])
    mock_conn.fetchrow = AsyncMock(return_value=None)

    app = create_app()
    app.state.pool = mock_pool
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


class TestCollectorHealthFiltering:
    """The /health collector summary only surfaces remote-collector* clients."""

    @pytest.mark.asyncio
    async def test_excludes_non_remote_collector_clients(self):
        """Integration identities (awx-execution-bindings, watcher-dispatcher)
        and any other non-matching client name are excluded from
        HealthResponse.collectors; only remote-collector* entries remain.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _collector_row("cred-1", "remote-collector-ws-a", now, 10),
            _collector_row("cred-2", "awx-execution-bindings", now, 20),
            _collector_row("cred-3", "watcher-dispatcher", now, 30),
            _collector_row("cred-4", "legacy-client", now, 40),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        assert response.status_code == 200
        collectors = response.json()["data"]["collectors"]
        names = [entry["client_name"] for entry in collectors]
        assert names == ["remote-collector-ws-a"]
        assert "awx-execution-bindings" not in names
        assert "watcher-dispatcher" not in names
        assert "legacy-client" not in names

    @pytest.mark.asyncio
    async def test_includes_remote_collector_clients_with_unchanged_semantics(self):
        """Every client whose name begins with 'remote-collector' is included,
        and the existing per-credential fields and healthy/stale/unknown
        heartbeat semantics are preserved verbatim.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            # Recent heartbeat → healthy
            _collector_row("cred-1", "remote-collector-ws-a", now, 10),
            # Old heartbeat → stale
            _collector_row(
                "cred-2",
                "remote-collector-ws-b",
                now - timedelta(hours=6),  # noqa: UP017
                20,
            ),
            # No heartbeat → unknown
            _collector_row("cred-3", "remote-collector-ws-c", None, 0),
            # Prefix match with suffix → included
            _collector_row("cred-4", "remote-collector", now, 5),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        assert response.status_code == 200
        collectors = response.json()["data"]["collectors"]
        assert [entry["client_name"] for entry in collectors] == [
            "remote-collector-ws-a",
            "remote-collector-ws-b",
            "remote-collector-ws-c",
            "remote-collector",
        ]
        health_by_name = {entry["client_name"]: entry for entry in collectors}
        assert health_by_name["remote-collector-ws-a"]["health"] == "healthy"
        assert health_by_name["remote-collector-ws-b"]["health"] == "stale"
        assert health_by_name["remote-collector-ws-c"]["health"] == "unknown"
        for entry in collectors:
            for field in (
                "credential_id",
                "client_name",
                "last_heartbeat",
                "total_records_ingested",
                "health",
            ):
                assert field in entry, f"Collector field {field!r} missing"
        assert health_by_name["remote-collector-ws-a"]["total_records_ingested"] == 10
        assert health_by_name["remote-collector-ws-a"]["credential_id"] == "cred-1"
        assert health_by_name["remote-collector-ws-a"]["last_heartbeat"] is not None
        assert health_by_name["remote-collector-ws-c"]["last_heartbeat"] is None
