"""Tests for the GET /health, /live, and /ready endpoints."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app


def _client(app, *, api_key: Optional[str] = None) -> AsyncClient:
    """Return an httpx AsyncClient for *app* with an optional API key header.

    ``api_key=None`` (the default) sends no ``Authorization`` header at all
    — used to exercise the probe auth exemptions.  Pass ``api_key`` to
    authenticate (needed for DB-backed API requests).
    """
    headers = {}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=headers,
    )


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


class TestLiveProbe:
    """Issue #772 — GET /live is a lightweight process-liveness probe.

    It must return 200 while the FastAPI process can serve requests,
    WITHOUT checking PostgreSQL and without API credentials.
    """

    @pytest.mark.asyncio
    async def test_live_returns_200_without_api_key_and_without_pool(self):
        """/live is auth-exempt and succeeds even when app.state.pool is None."""
        app = create_app()
        app.state.pool = None  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/live")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["data"] == {"alive": True}

    @pytest.mark.asyncio
    async def test_live_returns_200_when_pool_attribute_absent(self):
        """/live must not require request.app.state.pool to exist at all
        (no lifespan run, attribute never set)."""
        app = create_app()

        async with _client(app) as client:
            response = await client.get("/live")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_live_returns_200_when_database_broken(self):
        """/live stays 200 even when the pool's acquire raises — liveness
        must never depend on PostgreSQL (kubelet must not restart the pod
        solely because the DB is down)."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(side_effect=OSError("Connection refused"))
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/live")

        assert response.status_code == 200
        # The probe must not have touched the pool at all
        mock_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_returns_200_with_healthy_pool(self):
        """/live also succeeds when the pool is healthy (normal case)."""
        app = create_app()
        mock_pool = AsyncMock()
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/live")

        assert response.status_code == 200
        mock_pool.acquire.assert_not_called()


class TestReadyProbe:
    """Issue #772 — GET /ready reports PostgreSQL usability for Kubernetes.

    Returns 200 (auth-exempt) only when the pool is initialized and a
    bounded connection acquisition succeeds; 503 otherwise.
    """

    @pytest.mark.asyncio
    async def test_ready_returns_200_when_pool_initialized_and_acquire_succeeds(self):
        """Healthy initialized pool (mock) → 200 with an ok envelope."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["data"] == {"ready": True}
        mock_pool.release.assert_awaited_once_with(mock_conn)

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_pool_is_none(self):
        """Missing pool (the postgres-unavailable startup state) → 503."""
        app = create_app()
        app.state.pool = None  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_pool_attribute_absent(self):
        """No pool attribute on app.state at all → 503, never AttributeError."""
        app = create_app()

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_pool_uninitialized(self):
        """DatabasePool registered but inner asyncpg pool never connected
        (``pool.pool is None``) → 503."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_pool.pool = None
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_connection_acquisition_fails(self):
        """Broken pool whose acquire raises → 503 (bounded probe, no hang)."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(side_effect=OSError("Connection refused"))
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_ready_bounded_probe_timeout(self, monkeypatch):
        """A hanging acquisition must yield 503 within the bounded probe
        timeout instead of hanging the request (issue #772)."""
        monkeypatch.setattr(
            "app.api.health.READY_PROBE_TIMEOUT_SECONDS", 0.05
        )
        app = create_app()
        mock_pool = AsyncMock()

        async def _hang() -> None:
            await asyncio.sleep(10)

        mock_pool.acquire = AsyncMock(side_effect=_hang)
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


class TestProbeAuthExemptions:
    """Issue #772 — Kubernetes probes must work without API credentials.

    Exemptions are limited to /health, /live, and /ready; unrelated routes
    keep requiring the Admin API Key.
    """

    @pytest.mark.asyncio
    async def test_live_and_ready_are_auth_exempt(self):
        """With GATEWAY_API_KEY configured (conftest), /live and /ready are
        reachable WITHOUT an Authorization header."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app) as client:  # no Authorization header
            live = await client.get("/live")
            ready = await client.get("/ready")

        assert live.status_code == 200
        assert ready.status_code == 200

    @pytest.mark.asyncio
    async def test_ready_503_still_auth_exempt_not_401(self):
        """An unauthenticated /ready during a DB outage returns the 503
        readiness verdict — never a 401 (kubelet has no credentials)."""
        app = create_app()
        app.state.pool = None  # type: ignore[attr-defined]

        async with _client(app) as client:  # no Authorization header
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["status"] == "error"

    @pytest.mark.asyncio
    async def test_unrelated_routes_still_require_api_key(self):
        """Auth exemptions must not broaden: DB-backed routes without a key
        still get 401."""
        app = create_app()
        app.state.pool = None  # type: ignore[attr-defined]

        async with _client(app) as client:  # no Authorization header
            response = await client.get("/api/v1/afk-outcomes/change-requests")

        assert response.status_code == 401


class TestDbBackedRequestServiceUnavailable:
    """Issue #772 — DB-backed requests fail with a structured 503 envelope
    when the pool is missing, never 'NoneType' AttributeError or a bare 500."""

    @pytest.mark.asyncio
    async def test_change_requests_returns_structured_503_when_pool_missing(self):
        """GET /api/v1/afk-outcomes/change-requests with an authenticated
        request and no pool → structured 503 via the error envelope."""
        app = create_app()
        app.state.pool = None  # type: ignore[attr-defined]

        async with _client(app, api_key="test-api-key") as client:
            response = await client.get("/api/v1/afk-outcomes/change-requests")

        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "NoneType" not in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_change_requests_returns_503_when_pool_uninitialized(self):
        """Registered-but-uninitialized pool (pool.pool is None) also yields
        a structured 503, not a RuntimeError 500."""
        app = create_app()
        mock_pool = AsyncMock()
        mock_pool.pool = None
        app.state.pool = mock_pool  # type: ignore[attr-defined]

        async with _client(app, api_key="test-api-key") as client:
            response = await client.get("/api/v1/afk-outcomes/change-requests")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

def _iso_z(ts: datetime) -> str:
    """Serialize an aware datetime the way the /health response does (Z suffix)."""
    return ts.isoformat().replace("+00:00", "Z")


def _signal_row(
    client_id: str,
    client_name: str,
    *,
    credential_id: Optional[str] = None,
    credential_last_heartbeat=None,
    credential_records: int = 0,
    source_last_seen=None,
):
    """Build an asyncpg-record-like mock row for the client-level collector
    health signal query (issue #750).

    The query returns one row per non-revoked collector credential (carrying
    that credential's heartbeat max and record sum) UNION ALL one row per
    active watched source database (carrying its last_seen_at).
    """
    row = MagicMock()
    row.__getitem__.side_effect = {
        "credential_id": credential_id,
        "client_id": client_id,
        "client_name": client_name,
        "credential_last_heartbeat": credential_last_heartbeat,
        "credential_records": credential_records,
        "source_last_seen": source_last_seen,
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
            _signal_row("cid-1", "remote-collector-ws-a",
                        credential_id="cred-1", credential_last_heartbeat=now),
            _signal_row("cid-2", "awx-execution-bindings",
                        credential_id="cred-2", credential_last_heartbeat=now),
            _signal_row("cid-3", "watcher-dispatcher",
                        credential_id="cred-3", credential_last_heartbeat=now),
            _signal_row("cid-4", "legacy-client",
                        credential_id="cred-4", credential_last_heartbeat=now),
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
    async def test_pure_source_db_signals_from_excluded_clients_are_filtered(self):
        """Source-database signal rows belong to their owning client; rows
        for non-remote-collector clients must not leak into collectors[].
        """
        rows = [
            _signal_row("cid-1", "remote-collector-ws-a"),
            _signal_row("cid-2", "awx-execution-bindings"),
            _signal_row("cid-3", "watcher-dispatcher"),
            _signal_row("cid-4", "legacy-client"),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        names = [entry["client_name"] for entry in collectors]
        assert names == ["remote-collector-ws-a"]


class TestCollectorHealthClientAggregation:
    """Issue #750 — one collectors[] row per remote-collector client.

    The monitored identity is the OpenCode Client, not the credential:
    activity is aggregated across the client's non-revoked Collector
    Credentials and watched source databases.
    """

    @pytest.mark.asyncio
    async def test_multiple_credentials_collapse_to_one_client_row(self):
        """Two credentials of one client produce ONE row whose last_heartbeat
        is the most recent credential heartbeat and whose
        total_records_ingested is the sum across both credentials.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        old = now - timedelta(hours=6)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1",
                        credential_last_heartbeat=old, credential_records=10),
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-2",
                        credential_last_heartbeat=now, credential_records=5),
            _signal_row("cid-b", "remote-collector-ws-b",
                        credential_id="cred-3",
                        credential_last_heartbeat=now, credential_records=3),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        # One row per client, ordered by client_name
        assert [entry["client_name"] for entry in collectors] == [
            "remote-collector-ws-a",
            "remote-collector-ws-b",
        ]
        a = collectors[0]
        assert a["last_heartbeat"] == _iso_z(now)
        assert a["total_records_ingested"] == 15  # 10 + 5, summed per client

    @pytest.mark.asyncio
    async def test_multiple_source_databases_feed_client_liveness(self):
        """The client's watched source databases contribute last_seen_at as a
        liveness signal: the most recent source-database push (from any of
        the client's sources) feeds the client-level last_heartbeat.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        older = now - timedelta(minutes=2)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1",
                        credential_last_heartbeat=older, credential_records=7,
                        source_last_seen=None),
            _signal_row("cid-a", "remote-collector-ws-a",
                        source_last_seen=now),
            _signal_row("cid-a", "remote-collector-ws-a",
                        source_last_seen=now - timedelta(minutes=1)),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        assert [entry["client_name"] for entry in collectors] == [
            "remote-collector-ws-a",
        ]
        a = collectors[0]
        assert a["last_heartbeat"] == _iso_z(now)
        assert a["total_records_ingested"] == 7

    @pytest.mark.asyncio
    async def test_source_only_client_gets_one_row_regardless_of_credential_count(self):
        """A remote-collector client with zero credentials but a watched
        source database still yields exactly one collectors[] row whose
        liveness comes from the source-database signal.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a", source_last_seen=now),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        assert len(collectors) == 1
        a = collectors[0]
        assert a["client_name"] == "remote-collector-ws-a"
        assert a["last_heartbeat"] == _iso_z(now)
        assert a["total_records_ingested"] == 0
        assert a["health"] == "healthy"


class TestCollectorHealthStates:
    """Issue #750 — healthy/stale/unknown semantics at client level."""

    @pytest.mark.asyncio
    async def test_all_three_health_states_preserved_across_clients(self):
        """A recent signal → healthy, an old signal → stale, no signal →
        unknown; the five-minute threshold semantics are unchanged.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1", credential_last_heartbeat=now),
            _signal_row("cid-b", "remote-collector-ws-b",
                        credential_id="cred-2",
                        credential_last_heartbeat=now - timedelta(hours=6)),
            _signal_row("cid-c", "remote-collector-ws-c"),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        health_by_name = {entry["client_name"]: entry for entry in collectors}
        assert health_by_name["remote-collector-ws-a"]["health"] == "healthy"
        assert health_by_name["remote-collector-ws-b"]["health"] == "stale"
        assert health_by_name["remote-collector-ws-c"]["health"] == "unknown"
        assert health_by_name["remote-collector-ws-c"]["last_heartbeat"] is None

    @pytest.mark.asyncio
    async def test_recent_empty_heartbeat_is_healthy_at_zero_records(self):
        """An empty-batch Remote Collector Heartbeat (zero records) is still a
        recent liveness signal: the client is healthy with total_records 0.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1",
                        credential_last_heartbeat=now, credential_records=0),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        assert len(collectors) == 1
        assert collectors[0]["health"] == "healthy"
        assert collectors[0]["total_records_ingested"] == 0
        assert collectors[0]["last_heartbeat"] == _iso_z(now)

    @pytest.mark.asyncio
    async def test_stale_credential_plus_recent_source_signal_is_healthy(self):
        """Aggregation is a MAX across signals: a stale credential heartbeat
        cannot mask a fresh source-database push from another watched source.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1",
                        credential_last_heartbeat=now - timedelta(hours=6),
                        credential_records=4),
            _signal_row("cid-a", "remote-collector-ws-a",
                        source_last_seen=now),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        assert len(collectors) == 1
        assert collectors[0]["health"] == "healthy"
        assert collectors[0]["last_heartbeat"] == _iso_z(now)

    @pytest.mark.asyncio
    async def test_client_row_fields_are_client_level(self):
        """The row identity is the client (client_id / client_name) — no
        credential_id is exposed, and client-level fields remain.
        """
        now = datetime.now(timezone.utc)  # noqa: UP017
        rows = [
            _signal_row("cid-a", "remote-collector-ws-a",
                        credential_id="cred-1", credential_last_heartbeat=now),
        ]
        client = _health_client_with_collector_rows(rows)

        async with client as c:
            response = await c.get("/health")

        collectors = response.json()["data"]["collectors"]
        assert len(collectors) == 1
        entry = collectors[0]
        assert entry["client_id"] == "cid-a"
        assert entry["client_name"] == "remote-collector-ws-a"
        assert "credential_id" not in entry, (
            "credential identity is no longer the monitored identity",
        )
        for field in (
            "client_id",
            "client_name",
            "last_heartbeat",
            "total_records_ingested",
            "health",
        ):
            assert field in entry, f"Collector field {field!r} missing"
