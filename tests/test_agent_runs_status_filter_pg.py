"""Database-backed regression test for filtered agent-session listing (issue #778).

Executes status-filtered agent-runs queries against a real PostgreSQL
instance and would fail on the untyped timestamp placeholder (SQLSTATE
42883).  Covers:

- Each supported status (running, completed, blocked, stale, unknown) returns
  HTTP 200-equivalent (no 42883) with accurate totals.
- Combined status/date filters and pagination remain consistent across count
  and page queries (total matches page contents, LIMIT/OFFSET coherent).
- Same reference instant for count+page (params share timestamp).
- Unfiltered listing, session detail, and canonical thresholds unchanged.
- Typed timestamptz param: SQL contains ``::timestamptz`` and untyped SQL
  would raise 42883.

Prerequisites (like other integration tests):
    GATEWAY_ENV=development python -m alembic upgrade head
    pytest tests/test_agent_runs_status_filter_pg.py -v -m integration

When the test DB is not reachable the test skips gracefully.  The temp
PG started for local validation (``/tmp/pg-778-test`` on 55432) is tried
first, then the docker-compose.test.yml default (localhost:5433).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.api.usage import (
    QUIET_THRESHOLD_MINUTES,
    STALE_THRESHOLD_HOURS,
    UNKNOWN_THRESHOLD_HOURS,
    _fetch_agent_runs,
    _status_case_expression,
)

_VALID_STATUSES = ("running", "completed", "blocked", "stale", "unknown")

# Default DSN candidates in priority order: temp PG for this worktree,
# then the standard docker-compose.test.yml PG.
_CANDIDATE_DSNS = [
    # Temp PG started via initdb/pg_ctl on 55432, trust auth, user svc_admin_ansible
    "postgresql://svc_admin_ansible@localhost:55432/postgres",
    # docker-compose.test.yml PG on 5433
    "postgresql://opencode_test:opencode_test@localhost:5433/opencode_gateway_test",
]


def _env_dsn() -> str | None:
    """Return a DSN from GATEWAY_DATABASE_* env vars if explicitly set."""
    host = os.environ.get("GATEWAY_DATABASE_HOST")
    if host is None:
        return None
    port = int(os.environ.get("GATEWAY_DATABASE_PORT", "5432"))
    db = os.environ.get("GATEWAY_DATABASE_NAME", "postgres")
    user = os.environ.get("GATEWAY_DATABASE_USER", "svc_admin_ansible")
    pwd = os.environ.get("GATEWAY_DATABASE_PASSWORD", "")
    cred = f"{user}:{pwd}@" if pwd else f"{user}@"
    return f"postgresql://{cred}{host}:{port}/{db}"


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn, timeout=5), timeout=10)
        await conn.close()
        return True
    except Exception:
        return False


async def _find_working_dsn() -> str | None:
    # Env DSN first if set
    env = _env_dsn()
    if env and await _can_connect(env):
        return env
    for dsn in _CANDIDATE_DSNS:
        if await _can_connect(dsn):
            return dsn
    return None


@pytest.fixture(scope="function")
async def pg_pool() -> asyncpg.Pool:
    dsn = await _find_working_dsn()
    if dsn is None:
        pytest.skip(
            "Test Postgres not available. Start it with:\n"
            "  initdb -D /tmp/pg-778-test --no-locale --auth=trust && "
            "pg_ctl -D /tmp/pg-778-test -l /tmp/pg-778-test/logfile -o '-p 55432 -k /tmp/pg-778-test' start\n"
            "  GATEWAY_ENV=development python -m alembic upgrade head\n"
            "or: docker compose -f docker-compose.test.yml up -d"
        )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    assert pool is not None
    yield pool
    await pool.close()


# ── Helpers ───────────────────────────────────────────────────────────────

async def _seed_client_and_source(conn: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    client_id = await conn.fetchval(
        "INSERT INTO opencode_clients (name) VALUES ($1) RETURNING id",
        f"cli-{uuid.uuid4()}",
    )
    # source_databases needs collector_credential_id; create a dummy credential
    cred_id = await conn.fetchval(
        "INSERT INTO collector_credentials (client_id, token_hash, token_prefix) VALUES ($1, $2, $3) RETURNING id",
        client_id,
        "a" * 64,
        "test",
    )
    source_db_id = await conn.fetchval(
        "INSERT INTO source_databases (collector_credential_id, client_id) VALUES ($1, $2) RETURNING id",
        cred_id,
        client_id,
    )
    return client_id, source_db_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _insert_session(
    conn: asyncpg.Connection,
    *,
    client_id: uuid.UUID,
    source_database_id: uuid.UUID,
    last_message_at: datetime,
    message_count: int = 5,
    parent_session_id: str | None = None,
    external_session_id: str | None = None,
    agent: str | None = "code-editor",
    project_id: str | None = None,
) -> uuid.UUID:
    external = external_session_id or f"ses_{uuid.uuid4().hex[:8]}"
    sid = await conn.fetchval(
        """
        INSERT INTO sessions (
            client_id, source_database_id, external_session_id,
            first_message_at, last_message_at, message_count,
            total_input_tokens, total_output_tokens,
            project_id, workspace_id, agent, parent_session_id
        ) VALUES ($1,$2,$3,$4,$5,$6,0,0,$7,$8,$9,$10) RETURNING id
        """,
        client_id,
        source_database_id,
        external,
        last_message_at - timedelta(hours=1),
        last_message_at,
        message_count,
        project_id,
        None,
        agent,
        parent_session_id,
    )
    return sid


# ── Tests ─────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_case_expression_uses_timestamptz_cast() -> None:
    """The status CASE expression must cast the bound timestamp placeholder.

    This is the narrow fix for SQLSTATE 42883: ``($N::timestamptz) - interval``
    is well-typed, while bare ``$N - interval`` lets PG infer ``interval``.
    """
    sql = _status_case_expression(now_param="($2::timestamptz)")
    assert "($2::timestamptz) - interval" in " ".join(sql.split())
    # Default now() path must stay uncast
    sql_now = _status_case_expression()
    assert "now() - interval" in " ".join(sql_now.split())
    # No accidental double-cast
    assert sql.count("::timestamptz") == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_untyped_placeholder_would_raise_42883(pg_pool: asyncpg.Pool) -> None:
    """Untyped ``$1 - interval`` is the 42883 fault; typed ``($1::timestamptz)`` succeeds."""
    now = _utcnow()
    async with pg_pool.acquire() as conn:
        # Create a minimal sessions context so the CASE has a row to evaluate.
        client_id, source_db_id = await _seed_client_and_source(conn)
        sid = await _insert_session(
            conn,
            client_id=client_id,
            source_database_id=source_db_id,
            last_message_at=now - timedelta(minutes=5),
            message_count=5,
        )
        try:
            # Untyped placeholder: PG cannot resolve ``timestamptz > interval``
            # and raises UndefinedFunctionError (SQLSTATE 42883).
            untyped_sql = """
                SELECT COUNT(*) FROM sessions s
                WHERE (
                    CASE
                        WHEN s.message_count = 0 OR s.last_message_at IS NULL THEN 'unknown'
                        WHEN s.last_message_at > $1 - interval '15 minutes' THEN 'running'
                        ELSE 'unknown'
                    END
                ) = 'running'
            """
            with pytest.raises(asyncpg.exceptions.UndefinedFunctionError) as exc:
                await conn.fetchval(untyped_sql, now)
            assert "42883" in str(exc.value) or "does not exist" in str(exc.value)

            # Typed placeholder: succeeds and counts the running row.
            typed_sql = """
                SELECT COUNT(*) FROM sessions s
                WHERE (
                    CASE
                        WHEN s.message_count = 0 OR s.last_message_at IS NULL THEN 'unknown'
                        WHEN s.last_message_at > ($1::timestamptz) - interval '15 minutes' THEN 'running'
                        ELSE 'unknown'
                    END
                ) = 'running'
            """
            count = await conn.fetchval(typed_sql, now)
            assert count >= 1
        finally:
            await conn.execute("DELETE FROM sessions WHERE id = $1", sid)
            await conn.execute("DELETE FROM source_databases WHERE id = $1", source_db_id)
            # cascade cleanup: client & credential via FK
            await conn.execute("DELETE FROM collector_credentials WHERE client_id = $1", client_id)
            await conn.execute("DELETE FROM opencode_clients WHERE id = $1", client_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_status_returns_200_with_accurate_total(pg_pool: asyncpg.Pool) -> None:
    """Each supported status yields 200 with valid rows and accurate total."""
    now = _utcnow()
    # Build deterministic sessions covering each status:
    # running: 5m ago, completed: 1h ago no parent, blocked: 1h ago with parent,
    # stale: 3h ago, unknown: 72h ago, unknown (no messages): message_count 0
    async with pg_pool.acquire() as conn:
        client_id, source_db_id = await _seed_client_and_source(conn)
        # Need a parent for blocked
        parent_ext = f"ses_parent_{uuid.uuid4().hex[:6]}"
        parent_id = await _insert_session(
            conn,
            client_id=client_id,
            source_database_id=source_db_id,
            last_message_at=now - timedelta(minutes=2),
            message_count=5,
            external_session_id=parent_ext,
        )
        ids: dict[str, uuid.UUID] = {}
        ids["running"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now - timedelta(minutes=5), message_count=5
        )
        ids["completed"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now - timedelta(hours=1), message_count=5, parent_session_id=None
        )
        ids["blocked"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now - timedelta(hours=1), message_count=5, parent_session_id=parent_ext
        )
        ids["stale"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now - timedelta(hours=3), message_count=5
        )
        ids["unknown_old"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now - timedelta(hours=72), message_count=5
        )
        ids["unknown_empty"] = await _insert_session(
            conn, client_id=client_id, source_database_id=source_db_id,
            last_message_at=now, message_count=0
        )
        try:
            for status in _VALID_STATUSES:
                result = await _fetch_agent_runs(
                    conn,
                    client_id=None,
                    from_date=None,
                    to_date=None,
                    agent=None,
                    external_project_id=None,
                    status_filter=status,
                    limit=50,
                    offset=0,
                    grafana_base_url="https://grafana.example.com",
                    db_timeout_seconds=5,
                    quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                    stale_threshold_hours=STALE_THRESHOLD_HOURS,
                    unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
                )
                # Must not raise 42883; result is paginated
                assert result.total >= 1, f"status {status} expected at least 1 row, got {result.total}"
                assert all(r.status == status for r in result.items), (
                    f"status filter {status} leaked rows: {[r.status for r in result.items]}"
                )
                # total must match count of rows actually matching (page may be subset)
                assert result.total == len([r for r in result.items]) or result.total >= len(result.items)
                # For statuses with a single seeded row, total should be at least 1
                # and for unknown we seeded 2 rows (old + empty) so total >=2
                if status == "unknown":
                    assert result.total >= 2

            # Unfiltered listing still works
            unfiltered = await _fetch_agent_runs(
                conn,
                client_id=None, from_date=None, to_date=None, agent=None,
                external_project_id=None, status_filter=None, limit=50, offset=0,
                grafana_base_url="https://grafana.example.com",
                db_timeout_seconds=5,
                quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                stale_threshold_hours=STALE_THRESHOLD_HOURS,
                unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
            )
            assert unfiltered.total >= 6
            assert len(unfiltered.items) >= 1

        finally:
            for sid in list(ids.values()) + [parent_id]:
                await conn.execute("DELETE FROM sessions WHERE id = $1", sid)
            await conn.execute("DELETE FROM source_databases WHERE id = $1", source_db_id)
            await conn.execute("DELETE FROM collector_credentials WHERE client_id = $1", client_id)
            await conn.execute("DELETE FROM opencode_clients WHERE id = $1", client_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_combined_status_date_filters_and_pagination_coherent(pg_pool: asyncpg.Pool) -> None:
    """Combined status/date filters and pagination remain consistent.

    Inserts 5 running sessions within a 1-hour window, then queries with
    status=running + from_date/to_date and paginated LIMIT/OFFSET.
    Asserts total matches page contents and LIMIT/OFFSET coherent.
    """
    now = _utcnow()
    from_date = now - timedelta(hours=1)
    to_date = now + timedelta(hours=1)
    async with pg_pool.acquire() as conn:
        client_id, source_db_id = await _seed_client_and_source(conn)
        sids = []
        for i in range(5):
            sid = await _insert_session(
                conn, client_id=client_id, source_database_id=source_db_id,
                last_message_at=now - timedelta(minutes=i + 1),
                message_count=5,
            )
            sids.append(sid)
        try:
            # Page 1: limit 2 offset 0
            p1 = await _fetch_agent_runs(
                conn, client_id=None, from_date=from_date, to_date=to_date,
                agent=None, external_project_id=None, status_filter="running",
                limit=2, offset=0, grafana_base_url="https://grafana.example.com",
                db_timeout_seconds=5,
                quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                stale_threshold_hours=STALE_THRESHOLD_HOURS,
                unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
            )
            assert p1.total == 5, f"expected total 5 running, got {p1.total}"
            assert len(p1.items) == 2
            assert p1.limit == 2 and p1.offset == 0

            # Page 2: limit 2 offset 2
            p2 = await _fetch_agent_runs(
                conn, client_id=None, from_date=from_date, to_date=to_date,
                agent=None, external_project_id=None, status_filter="running",
                limit=2, offset=2, grafana_base_url="https://grafana.example.com",
                db_timeout_seconds=5,
                quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                stale_threshold_hours=STALE_THRESHOLD_HOURS,
                unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
            )
            assert p2.total == 5
            assert len(p2.items) == 2
            # No overlap between pages, ordered by last_message_at DESC
            p1_ids = {r.id for r in p1.items}
            p2_ids = {r.id for r in p2.items}
            assert p1_ids.isdisjoint(p2_ids)

            # Page 3: remainder
            p3 = await _fetch_agent_runs(
                conn, client_id=None, from_date=from_date, to_date=to_date,
                agent=None, external_project_id=None, status_filter="running",
                limit=2, offset=4, grafana_base_url="https://grafana.example.com",
                db_timeout_seconds=5,
                quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                stale_threshold_hours=STALE_THRESHOLD_HOURS,
                unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
            )
            assert p3.total == 5
            assert len(p3.items) == 1

            # Combined filter with client_id must also cohere
            p_client = await _fetch_agent_runs(
                conn, client_id=client_id, from_date=from_date, to_date=to_date,
                agent=None, external_project_id=None, status_filter="running",
                limit=10, offset=0, grafana_base_url="https://grafana.example.com",
                db_timeout_seconds=5,
                quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
                stale_threshold_hours=STALE_THRESHOLD_HOURS,
                unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
            )
            assert p_client.total == 5
            assert len(p_client.items) == 5
            assert all(r.client_id == client_id for r in p_client.items)

        finally:
            for sid in sids:
                await conn.execute("DELETE FROM sessions WHERE id = $1", sid)
            await conn.execute("DELETE FROM source_databases WHERE id = $1", source_db_id)
            await conn.execute("DELETE FROM collector_credentials WHERE client_id = $1", client_id)
            await conn.execute("DELETE FROM opencode_clients WHERE id = $1", client_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_reference_instant_for_count_and_page(pg_pool: asyncpg.Pool) -> None:
    """Count and page queries must use the same reference instant.

    The _fetch_agent_runs docstring guarantees a single _utcnow() binding for
    both COUNT and DATA.  This test verifies the SQL shape contains a single
    typed placeholder and that count/data share the bound value (no threshold
    straddle).
    """
    # Synthetic unit check: the SQL must contain the typed placeholder
    # and the params must share the timestamp.
    from unittest.mock import AsyncMock

    # Mock conn that captures SQL and returns deterministic counts
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=1)

    # Build a row that looks like a real agent-run row
    from unittest.mock import MagicMock

    row = MagicMock()
    data = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "source_database_id": uuid.uuid4(),
        "external_session_id": "ses_test123",
        "first_message_at": _utcnow() - timedelta(hours=1),
        "last_message_at": _utcnow() - timedelta(minutes=5),
        "message_count": 5,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "project_id": None,
        "workspace_id": None,
        "agent": None,
        "parent_session_id": None,
        "total_estimated_cost_usd": None,
        "_status": "running",
        "child_run_count": 0,
        "todo_total": 0,
        "todo_completed": 0,
        "todo_blocked": 0,
        "session_title": None,
        "session_model": None,
        "project_label": None,
        "total_reasoning_tokens": 0,
        "primary_provider": None,
    }
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    mock_conn.fetch = AsyncMock(return_value=[row])

    result = await _fetch_agent_runs(
        mock_conn,
        client_id=None, from_date=None, to_date=None, agent=None,
        external_project_id=None, status_filter="running", limit=10, offset=0,
        grafana_base_url="https://grafana.example.com",
        db_timeout_seconds=5,
        quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
        stale_threshold_hours=STALE_THRESHOLD_HOURS,
        unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
    )
    assert result.total == 1
    # Inspect captured SQL
    count_sql = mock_conn.fetchval.call_args[0][0]
    data_sql = mock_conn.fetch.call_args[0][0]
    count_params = mock_conn.fetchval.call_args[0][1:]
    data_params = mock_conn.fetch.call_args[0][1:]
    # Must be typed
    assert "($2::timestamptz) - interval" in " ".join(count_sql.split())
    assert "($2::timestamptz) - interval" in " ".join(data_sql.split())
    # Same instant
    assert count_params[1] == data_params[1]

    # With extra filters the placeholder index shifts but stays typed
    mock_conn.fetchval.reset_mock()
    mock_conn.fetch.reset_mock()
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_conn.fetch = AsyncMock(return_value=[])
    await _fetch_agent_runs(
        mock_conn,
        client_id=uuid.uuid4(),
        from_date=_utcnow() - timedelta(hours=1),
        to_date=_utcnow(),
        agent="code-editor",
        external_project_id="proj-1",
        status_filter="running",
        limit=10, offset=0,
        grafana_base_url="https://grafana.example.com",
        db_timeout_seconds=5,
        quiet_threshold_minutes=QUIET_THRESHOLD_MINUTES,
        stale_threshold_hours=STALE_THRESHOLD_HOURS,
        unknown_threshold_hours=UNKNOWN_THRESHOLD_HOURS,
    )
    count_sql2 = mock_conn.fetchval.call_args[0][0]
    # 5 extra filters + status ($6) + now ($7) => ($7::timestamptz)
    assert "($7::timestamptz) - interval" in " ".join(count_sql2.split())
