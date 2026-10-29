"""Integration concurrency tests for the provisional AFK run lifecycle (issue #589).

Runs against an isolated, disposable PostgreSQL 16 database and verifies the
actual database-enforced guarantees of ``/api/v1/afk/executions/runs``:

* Concurrent identical provisioning of the same
  ``(provider, host, source_event_id)`` key — exactly one 201 Created and
  one 200 OK (idempotent replay), exactly one ``afk_runs`` row for the key.
* Concurrent conflicting provisioning (same key, different repository) —
  exactly one 201 Created and one 409 Conflict; the stored row remains
  exactly the winner's payload (never mutated, never blended).
* Concurrent binding of the same change request to two separate lifecycles
  — exactly one 200 OK and one 409 Conflict, enforcing the 1:1
  lifecycle<->change-request invariant backed by the partial unique index
  ``uq_afk_runs_change_request_identity`` (migration 0039).

Prerequisites
-------------
This suite migrates the schema and can drop every table in the ``public``
schema, so it refuses to run unless the target is positively verified as a
disposable PostgreSQL 16 database.  Start one outside Kubernetes before
running::

    docker run --rm -d --name opencode-gateway-test-db \\
        -e POSTGRES_DB=opencode_gateway_test \\
        -e POSTGRES_USER=opencode_test \\
        -e POSTGRES_PASSWORD=opencode_test \\
        -p 5433:5432 postgres:16-alpine

    GATEWAY_ENV=development GATEWAY_TEST_DISPOSABLE_DB=1 \\
        pytest tests/integration/test_afk_lifecycle_concurrency.py -v -m integration

    docker stop opencode-gateway-test-db

``GATEWAY_TEST_DISPOSABLE_DB=1`` is the operator's explicit attestation that
the database is disposable; without it the safety preflight fails closed.
CI runs this suite against a dedicated ``postgres:16`` service container.

Fixture layout notes
--------------------
* **Migrations are module-scoped** via the shared ``_verified_disposable_database``
  and ``_migrated_schema`` fixtures in ``tests/integration/conftest.py``: the
  upgrade runs in a worker thread where alembic's ``env.py`` can call
  ``asyncio.run()`` safely, and teardown drops every public table.
* **``db_pool`` is function-scoped**: pytest-asyncio 0.26 runs every async
  test in a fresh event loop, and an asyncpg pool is bound to the loop that
  created it — a module-scoped pool would be used across loops.  Each test
  gets its own pool, and teardown truncates ``afk_runs`` and removes the
  seeded auth rows so every test starts from a clean slate.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi import Request

from app.core.identity import hash_token
from tests.integration._disposable_postgres import target_dsn as _dsn

_API_KEY = "test-api-key"

# The dedicated AWX execution-binding client name — matches the constant in
# app.api.afk_executions (issue #550).
_AWX_CLIENT_NAME = "awx-execution-bindings"

_LIFECYCLE_PROVISION_PATH = "/api/v1/afk/executions/runs"


@pytest.fixture
async def db_pool(_migrated_schema: None) -> asyncpg.Pool:
    """Function-scoped pool on the migrated test database.

    Teardown truncates ``afk_runs`` and removes the seeded auth rows so
    every test starts from a clean slate.
    """
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=5)
    assert pool is not None

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE afk_runs CASCADE")
        await conn.execute(
            "DELETE FROM collector_credentials WHERE client_id IN"
            " (SELECT id FROM opencode_clients WHERE name = $1)",
            _AWX_CLIENT_NAME,
        )
        await conn.execute(
            "DELETE FROM opencode_clients WHERE name = $1",
            _AWX_CLIENT_NAME,
        )
    await pool.close()


# ── Builders ─────────────────────────────────────────────────────────────────


async def _seed_afk_client(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the dedicated AWX execution-binding client + collector credential.

    Returns (client_id, credential_id).  The credential hash matches the
    ``_API_KEY`` bearer token so both layers of auth pass.
    """
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        _AWX_CLIENT_NAME,
    )
    credential_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix)"
        " VALUES ($1, $2, $3) RETURNING id",
        client_id,
        hash_token(_API_KEY),
        "test-api",
    )
    return client_id, credential_id


def _build_app(db_pool: asyncpg.Pool) -> object:
    """Build a FastAPI app connected to the real integration DB pool.

    Sends the bearer token matching both the API-key middleware and the
    seeded collector credential.

    ``Request`` is imported at module level (not inside this function): the
    module uses ``from __future__ import annotations``, so the override's
    ``request: Request`` annotation is a string that FastAPI resolves
    against this module's globals — without a module-level import it would
    be misread as a query parameter.
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.factory import create_app
    from app.db.session import get_session

    os.environ.setdefault("GATEWAY_ENV", "development")

    app = create_app(configure_logging=False)

    async def _override_get_session(request: Request):
        conn = await db_pool.acquire()
        try:
            yield conn
        finally:
            await db_pool.release(conn)

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )


def _provision_payload(**overrides) -> dict:
    """Build a ``POST /api/v1/afk/executions/runs`` provisioning payload."""
    payload = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": "eda-concurrent-provision",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Concurrent provisioning",
    }
    payload.update(overrides)
    return payload


def _binding_payload(**overrides) -> dict:
    """Build a ``POST /runs/{afk_run_id}/change-request`` binding payload."""
    payload = {
        "provider": "gitlab",
        "repository": "https://gitlab.com/cloudnative-pg/cloudnative-pg",
        "external_id": "6",
    }
    payload.update(overrides)
    return payload


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_provision_same_key_same_payload(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent identical provisioning: one 201, one 200, exactly one row.

    Two concurrent POSTs with the same ``(provider, host, source_event_id)``
    and identical payload race on the INSERT behind the partial unique index
    ``uq_afk_runs_provisioning_key``.  Exactly one inserts (201 Created);
    the loser re-reads the winner's row and resolves to an idempotent replay
    (200 OK).  Both responses carry the same ``afk_run_id``, and the
    database holds exactly one row for the key.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload = _provision_payload(source_event_id=source_event_id)

    async with client as c:
        results = await asyncio.gather(
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload),
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted) and one 200 (idempotent replay).
        assert status_codes == [200, 201], f"Expected [200, 201], got {status_codes}"

        run_ids: set[str] = set()
        for r in results:
            body = r.json()
            assert body["status"] == "ok"
            assert body["data"]["source_event_id"] == source_event_id
            run_ids.add(body["data"]["afk_run_id"])
        assert len(run_ids) == 1, "Both responses must reference the same lifecycle"

    # Verify exactly one row exists in afk_runs for the provisioning key.
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert count == 1, f"Expected 1 row for the key, got {count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_provision_same_key_different_payload(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent conflicting provisioning: one 201, one 409, no mutation.

    Two concurrent POSTs share the provisioning key but disagree on
    ``repository``.  Exactly one inserts (201 Created); the loser detects
    the payload mismatch against the winner's row (409 Conflict).  The
    stored row must remain exactly the winner's payload — never a blend.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    source_event_id = f"eda-{uuid.uuid4().hex[:16]}"
    payload_a = _provision_payload(
        source_event_id=source_event_id,
        repository="https://github.com/acme/proj",
    )
    payload_b = _provision_payload(
        source_event_id=source_event_id,
        repository="https://github.com/acme/other",
    )

    async with client as c:
        results = await asyncio.gather(
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload_a),
            c.post(_LIFECYCLE_PROVISION_PATH, json=payload_b),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 201 (inserted) and one 409 (conflict rejection).
        assert status_codes == [201, 409], f"Expected [201, 409], got {status_codes}"

        winner = next(r for r in results if r.status_code == 201)
        winner_repository = winner.json()["data"]["repository"]
        assert winner_repository in (
            "github.com/acme/proj",
            "github.com/acme/other",
        )

        loser = next(r for r in results if r.status_code == 409)
        loser_body = loser.json()
        assert loser_body["status"] == "error"
        assert loser_body["error"]["code"] == "CONFLICT"

    # Verify exactly one row, unmutated — the stored repository must match
    # the 201 winner's payload exactly (never the conflicting one).
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert count == 1, f"Expected 1 row for the key, got {count}"

        row = await conn.fetchrow(
            "SELECT repository, trigger_type, title FROM afk_runs"
            " WHERE provider = $1 AND host = $2 AND source_event_id = $3",
            "github",
            "awx-01.internal",
            source_event_id,
        )
        assert row is not None
        assert row["repository"] == winner_repository
        assert row["trigger_type"] == "eda"
        assert row["title"] == "Concurrent provisioning"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_bind_same_change_request(
    db_pool: asyncpg.Pool,
) -> None:
    """Concurrent binding of one change request to two lifecycles: one 200, one 409.

    Two lifecycles are provisioned first (sequential, different
    ``source_event_id``s).  The same change-request identity is then bound
    to both concurrently.  The 1:1 lifecycle<->change-request invariant
    (partial unique index ``uq_afk_runs_change_request_identity``) admits
    exactly one winner (200 OK); the loser resolves to 409 Conflict.
    Exactly one lifecycle carries the bound change request in the database.
    """
    async with db_pool.acquire() as conn:
        await _seed_afk_client(conn)

    client = _build_app(db_pool)
    run_ids: list[str] = []

    async with client as c:
        for _ in range(2):
            payload = _provision_payload(source_event_id=f"eda-{uuid.uuid4().hex[:16]}")
            resp = await c.post(_LIFECYCLE_PROVISION_PATH, json=payload)
            assert resp.status_code == 201, resp.text
            run_ids.append(resp.json()["data"]["afk_run_id"])

        bind_payload = _binding_payload()
        results = await asyncio.gather(
            c.post(
                f"{_LIFECYCLE_PROVISION_PATH}/{run_ids[0]}/change-request",
                json=bind_payload,
            ),
            c.post(
                f"{_LIFECYCLE_PROVISION_PATH}/{run_ids[1]}/change-request",
                json=bind_payload,
            ),
        )
        status_codes = sorted(r.status_code for r in results)
        # Exactly one 200 (bound) and one 409 (the 1:1 invariant held).
        assert status_codes == [200, 409], f"Expected [200, 409], got {status_codes}"

        for r in results:
            body = r.json()
            if r.status_code == 409:
                assert body["status"] == "error"
                assert body["error"]["code"] == "CONFLICT"
            else:
                assert body["status"] == "ok"
                bound = body["data"]["change_request"]
                assert bound is not None
                assert bound["resource_type"] == "change_request"
                assert bound["resource_number"] == "6"

    # Verify database consistency: exactly one lifecycle owns the change
    # request; the other remains unbound.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT afk_run_id, change_request_provider,"
            " change_request_repository, change_request_external_id"
            " FROM afk_runs WHERE afk_run_id = ANY($1::text[])",
            run_ids,
        )
        assert len(rows) == 2
        bound_rows = [r for r in rows if r["change_request_provider"] is not None]
        unbound_rows = [r for r in rows if r["change_request_provider"] is None]
        assert len(bound_rows) == 1, "Exactly one lifecycle must own the change request"
        assert len(unbound_rows) == 1
        assert (
            bound_rows[0]["change_request_provider"],
            bound_rows[0]["change_request_repository"],
            bound_rows[0]["change_request_external_id"],
        ) == ("gitlab", "gitlab.com/cloudnative-pg/cloudnative-pg", "6")
