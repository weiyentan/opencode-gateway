"""Tests for the AFK dashboard daily recomputation engine (issue #715).

Each metric category is exercised against a canonical fixture row: the fake
connection dispatches by SQL identity, so a category whose query/filters are
wrong cannot silently return the expected value.  SQL-shape assertions pin
the canonical source table, the event-time bucketing, and the full-replacement
upsert contract.

The tests are pure unit tests (mock asyncpg connection) — no live database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.afk_dashboard_daily import (
    AFK_DASHBOARD_LOCK_CLASS,
    AFK_DASHBOARD_ROLLUP_VERSION,
    CHANGE_REQUEST_SQL,
    COMPUTE_QUERIES,
    EXECUTION_SQL,
    METRIC_COLUMNS,
    RUNS_SQL,
    SESSION_SQL,
    UPSERT_SQL,
    USAGE_SQL,
    acquire_bucket_lock,
    bucket_lock_key,
    compute_bucket_metrics,
    recompute_bucket,
    upsert_bucket,
)

UTC = timezone.utc

_DAY = date(2026, 9, 21)
_PROVIDER = "gitlab"
_REPOSITORY = "cloudnative-pg"


# ---------------------------------------------------------------------------
# Canonical fixtures — one per metric category
# ---------------------------------------------------------------------------


def _category_fixtures() -> dict[str, dict]:
    """Return the canonical fixture row for each per-category query."""
    return {
        RUNS_SQL: {"runs_started": 3},
        CHANGE_REQUEST_SQL: {
            "change_requests_opened": 5,
            "change_requests_merged": 2,
            "change_requests_closed": 1,
        },
        EXECUTION_SQL: {
            "execution_count": 4,
            "successful_execution_count": 3,
            "failed_execution_count": 1,
            "cancelled_execution_count": 0,
        },
        SESSION_SQL: {"session_count": 6},
        USAGE_SQL: {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_read_tokens": 50,
            "cache_write_tokens": 25,
            "estimated_cost_usd": Decimal("1.23"),
        },
    }


def _expected_metrics() -> dict[str, object]:
    expected: dict[str, object] = {}
    for row in _category_fixtures().values():
        expected.update(row)
    return expected


def _expected_values() -> list[object]:
    metrics = _expected_metrics()
    return [metrics[column] for column in METRIC_COLUMNS]


def _mock_conn(*, upsert_row: dict | None = None) -> AsyncMock:
    """Build a mock conn that dispatches per-category queries by SQL identity."""
    fixtures = _category_fixtures()
    conn = AsyncMock()
    conn.lock_args = None
    conn.upsert_args = None
    conn.fetchrow_calls = []

    async def _fetchrow(sql, *args):
        conn.fetchrow_calls.append(sql)
        if sql == UPSERT_SQL:
            conn.upsert_args = args
            if upsert_row is None:
                return None
            return dict(upsert_row)
        return dict(fixtures[sql])

    async def _fetchval(sql, *args):
        conn.lock_args = (sql, args)
        return None

    conn.fetchrow.side_effect = _fetchrow
    conn.fetchval.side_effect = _fetchval
    return conn


# ---------------------------------------------------------------------------
# Recomputation behaviour
# ---------------------------------------------------------------------------


async def test_recompute_returns_persisted_row():
    """The persisted row (RETURNING *) is returned for verification."""
    persisted = {
        "day": _DAY,
        "provider": _PROVIDER,
        "repository": _REPOSITORY,
        **_expected_metrics(),
        "derived_at": datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        "rollup_version": AFK_DASHBOARD_ROLLUP_VERSION,
        "updated_at": datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    }
    conn = _mock_conn(upsert_row=persisted)

    result = await recompute_bucket(conn, _DAY, _PROVIDER, _REPOSITORY)

    assert result == persisted


async def test_recompute_merges_every_metric_category():
    """Every category fixture flows into the returned metric row."""
    conn = _mock_conn()

    metrics = await compute_bucket_metrics(conn, _DAY, _PROVIDER, _REPOSITORY)

    assert metrics == _expected_metrics()


async def test_compute_queries_every_category_once():
    """One query per metric category, no more."""
    conn = _mock_conn()

    await compute_bucket_metrics(conn, _DAY, _PROVIDER, _REPOSITORY)

    assert conn.fetchrow_calls == list(COMPUTE_QUERIES)


async def test_upsert_binds_computed_metrics_in_column_order():
    """The upsert receives the computed metrics in METRIC_COLUMNS order."""
    conn = _mock_conn()

    await recompute_bucket(conn, _DAY, _PROVIDER, _REPOSITORY)

    expected = (_DAY, _PROVIDER, _REPOSITORY, *_expected_values(), AFK_DASHBOARD_ROLLUP_VERSION)
    assert conn.upsert_args == expected


async def test_missing_metric_values_default_to_zero():
    """A NULL category result is written as zero, never omitted."""
    conn = AsyncMock()

    async def _fetchrow(sql, *args):
        if sql == UPSERT_SQL:
            return None
        return None

    conn.fetchrow.side_effect = _fetchrow

    await upsert_bucket(conn, _DAY, _PROVIDER, _REPOSITORY, {})

    # Params: SQL, day, provider, repository, 14 metrics (all zero), rollup_version
    assert list(conn.fetchrow.call_args.args[4:18]) == [0] * len(METRIC_COLUMNS)


async def test_rollup_version_is_configurable():
    """Callers may stamp a different rule version per recompute."""
    conn = _mock_conn()

    await recompute_bucket(conn, _DAY, _PROVIDER, _REPOSITORY, rollup_version="7")

    assert conn.upsert_args[-1] == "7"


# ---------------------------------------------------------------------------
# Metric sourcing + event-time bucketing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected_tokens"),
    [
        (RUNS_SQL, ["afk_runs", "r.started_at", "r.first_seen_at"]),
        (
            CHANGE_REQUEST_SQL,
            [
                "engineering_events",
                "e.entity_type = 'change_request'",
                "e.occurred_at",
                "'change_request.opened'",
                "'change_request.merged'",
                "'change_request.closed'",
            ],
        ),
        (
            EXECUTION_SQL,
            [
                "execution_bindings",
                "b.started_at",
                "b.created_at",
                "'completed'",
                "'failed'",
                "'cancelled'",
            ],
        ),
        (SESSION_SQL, ["afk_run_sessions", "afk_runs"]),
        (
            USAGE_SQL,
            [
                "usage_events",
                "ue.input_tokens",
                "ue.output_tokens",
                "ue.cache_read_tokens",
                "ue.cache_write_tokens",
                "ue.estimated_cost_usd",
                "ue.reported_at",
            ],
        ),
    ],
)
def test_each_metric_sourced_from_canonical_table(sql, expected_tokens):
    for token in expected_tokens:
        assert token in sql, f"expected {token!r} in query"


@pytest.mark.parametrize("sql", COMPUTE_QUERIES)
def test_event_time_bucketed_in_utc(sql):
    assert "AT TIME ZONE 'UTC'" in sql
    assert "::date = $1" in sql


def test_change_request_sql_uses_prefixed_event_types_not_bare_suffixes():
    """FILTER clauses match the stored engineering_events vocabulary.

    ``engineering_events.event_type`` stores the fully-qualified canonical
    type (``change_request.opened`` / ``change_request.merged`` /
    ``change_request.closed``), so a bare ``'opened'`` filter can never
    match a row.
    """
    assert "'change_request.opened'" in CHANGE_REQUEST_SQL
    assert "'change_request.merged'" in CHANGE_REQUEST_SQL
    assert "'change_request.closed'" in CHANGE_REQUEST_SQL
    # The bare suffixes must never be matched on their own.
    assert "'opened'" not in CHANGE_REQUEST_SQL
    assert "'merged'" not in CHANGE_REQUEST_SQL
    assert "'closed'" not in CHANGE_REQUEST_SQL


def test_session_count_excludes_ambiguous_session_attribution():
    """A session mapped to more than one run is excluded, never split.

    Mirrors the USAGE_SQL rule: the count routes through the
    ``unambiguous_sessions`` CTE so a session attributed to more than one
    AFK run contributes to no bucket.
    """
    assert "unambiguous_sessions" in SESSION_SQL
    assert "COUNT(DISTINCT ars.afk_run_id) = 1" in SESSION_SQL
    assert "HAVING" in SESSION_SQL
    # The main query must route through the CTE, not count raw rows.
    assert "JOIN unambiguous_sessions" in SESSION_SQL


def test_upsert_replaces_every_column():
    """Full replacement: DO UPDATE SET col = EXCLUDED.col for every metric."""
    assert "ON CONFLICT (day, provider, repository) DO UPDATE SET" in UPSERT_SQL
    for column in METRIC_COLUMNS:
        assert f"{column} = EXCLUDED.{column}" in UPSERT_SQL
    # No additive increments — that would double-count on retry.
    assert "+ EXCLUDED" not in UPSERT_SQL


def test_upsert_sets_freshness_metadata():
    assert "now()" in UPSERT_SQL
    assert "derived_at = EXCLUDED.derived_at" in UPSERT_SQL
    assert "rollup_version = EXCLUDED.rollup_version" in UPSERT_SQL
    assert "updated_at = EXCLUDED.updated_at" in UPSERT_SQL
    assert "RETURNING *" in UPSERT_SQL


# ---------------------------------------------------------------------------
# Unresolved / ambiguous attribution exclusion
# ---------------------------------------------------------------------------


def test_usage_excludes_null_repository():
    assert "r.repository IS NOT NULL" in USAGE_SQL


def test_usage_excludes_ambiguous_session_attribution():
    """A session mapped to more than one run is excluded, never split."""
    assert "COUNT(DISTINCT ars.afk_run_id) = 1" in USAGE_SQL
    assert "HAVING" in USAGE_SQL


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


def test_bucket_lock_key_is_deterministic():
    assert bucket_lock_key(_DAY, _PROVIDER, _REPOSITORY) == bucket_lock_key(
        _DAY, _PROVIDER, _REPOSITORY
    )


def test_bucket_lock_key_distinguishes_buckets():
    base = bucket_lock_key(_DAY, _PROVIDER, _REPOSITORY)
    assert bucket_lock_key(date(2026, 9, 22), _PROVIDER, _REPOSITORY) != base
    assert bucket_lock_key(_DAY, "github", _REPOSITORY) != base
    assert bucket_lock_key(_DAY, _PROVIDER, "other-repo") != base


def test_bucket_lock_key_uses_lock_namespace():
    lock_class, _ = bucket_lock_key(_DAY, _PROVIDER, _REPOSITORY)
    assert lock_class == AFK_DASHBOARD_LOCK_CLASS


async def test_acquire_bucket_lock_uses_transactional_advisory_lock():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    await acquire_bucket_lock(conn, _DAY, _PROVIDER, _REPOSITORY)

    sql, args = conn.fetchval.call_args.args[0], conn.fetchval.call_args.args[1:]
    assert "pg_advisory_xact_lock" in sql
    assert args == bucket_lock_key(_DAY, _PROVIDER, _REPOSITORY)
