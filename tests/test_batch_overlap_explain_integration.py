"""Integration test that verifies the batch overlap query's
``usage_ingest_attempts`` leg is index-backed when a real PostgreSQL
instance is available.

This directly addresses the reviewer's request for real-DB EXPLAIN
confidence in the index plan (PR #418, Finding 1 / Note B).  The test
is marked ``@pytest.mark.integration`` so CI excludes it with
``-m "not integration"``.  When no database is reachable it skips
gracefully.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_overlap_attempts_leg_uses_index_scan() -> None:
    """Run EXPLAIN on the full batch overlap query and assert the
    ``usage_ingest_attempts`` leg is served by an index (not a sequential
    scan)."""

    # ── Resolve connection params from the same env vars as Settings ─────────
    kwargs: dict[str, object] = {
        "host": os.environ.get("GATEWAY_DATABASE_HOST", "localhost"),
        "port": int(os.environ.get("GATEWAY_DATABASE_PORT", "5432")),
        "database": os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway"),
        "user": os.environ.get("GATEWAY_DATABASE_USER", "opencode"),
        "password": os.environ.get("GATEWAY_DATABASE_PASSWORD", ""),
    }

    conn: asyncpg.Connection
    try:
        conn = await asyncpg.connect(
            host=str(kwargs["host"]),
            port=int(kwargs["port"]),  # type: ignore[arg-type]
            database=str(kwargs["database"]),
            user=str(kwargs["user"]),
            password=str(kwargs["password"]),
        )
    except Exception:
        pytest.skip("requires a real PostgreSQL instance")

    try:
        await conn.execute("SET search_path TO public")

        # ── Create tables ───────────────────────────────────────────────
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS source_identities (
                id uuid PRIMARY KEY,
                client_id uuid NOT NULL,
                collector_source_id text NOT NULL,
                canonical_parent_id uuid
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_events (
                id uuid PRIMARY KEY,
                canonical_source_identity_id uuid NOT NULL,
                source_record_id text NOT NULL
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_ingest_attempts (
                id uuid PRIMARY KEY,
                source_identity_id uuid NOT NULL,
                original_source_record_id text NOT NULL,
                outcome text NOT NULL
            )"""
        )

        # ── Create indexes CONCURRENTLY (requires autocommit) ───────────
        await conn.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_usage_events_source_record_id "
            "ON usage_events (source_record_id)"
        )
        await conn.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_usage_ingest_attempts_original_source_record_id "
            "ON usage_ingest_attempts (original_source_record_id)"
        )

        # ── Insert test data ────────────────────────────────────────────
        client_id = uuid.uuid4()
        identity_a = uuid.uuid4()
        identity_b = uuid.uuid4()

        await conn.execute(
            "INSERT INTO source_identities (id, client_id, collector_source_id) "
            "VALUES ($1, $2, $3)",
            identity_a, client_id, "collector-a",
        )
        await conn.execute(
            "INSERT INTO source_identities (id, client_id, collector_source_id) "
            "VALUES ($1, $2, $3)",
            identity_b, client_id, "collector-b",
        )

        # usage_events row for identity A
        await conn.execute(
            "INSERT INTO usage_events "
            "(id, canonical_source_identity_id, source_record_id) "
            "VALUES ($1, $2, $3)",
            uuid.uuid4(), identity_a, "rec-1",
        )
        # usage_ingest_attempts row for identity B with outcome accepted
        await conn.execute(
            "INSERT INTO usage_ingest_attempts "
            "(id, source_identity_id, original_source_record_id, outcome) "
            "VALUES ($1, $2, $3, $4)",
            uuid.uuid4(), identity_b, "rec-2", "accepted",
        )

        # ── EXPLAIN the full batch overlap query ────────────────────────
        explain_sql = f"""EXPLAIN
            SELECT overlapping_identity_id, COUNT(*)::int AS overlap_count
            FROM (
                SELECT ue.canonical_source_identity_id AS overlapping_identity_id,
                       ue.source_record_id AS source_record_id
                FROM usage_events ue
                JOIN source_identities si ON si.id = ue.canonical_source_identity_id
                WHERE si.client_id = '{client_id}'
                  AND ue.source_record_id = ANY(ARRAY['rec-1','rec-2']::text[])
                  AND ue.canonical_source_identity_id <> '{identity_a}'
                  AND ue.canonical_source_identity_id NOT IN (
                      SELECT id FROM source_identities
                      WHERE canonical_parent_id IS NOT NULL
                  )
                UNION
                SELECT a.source_identity_id AS overlapping_identity_id,
                       a.original_source_record_id AS source_record_id
                FROM usage_ingest_attempts a
                JOIN source_identities si ON si.id = a.source_identity_id
                WHERE si.client_id = '{client_id}'
                  AND a.original_source_record_id = ANY(ARRAY['rec-1','rec-2']::text[])
                  AND a.source_identity_id <> '{identity_a}'
                  AND a.source_identity_id NOT IN (
                      SELECT id FROM source_identities
                      WHERE canonical_parent_id IS NOT NULL
                  )
                  AND a.outcome IN ('accepted', 'duplicate', 'updated')
            ) shared_records
            GROUP BY overlapping_identity_id
            ORDER BY overlap_count DESC"""

        rows = await conn.fetch(explain_sql)
        plan = "\n".join(str(r[0]) for r in rows)

        assert "usage_ingest_attempts" in plan, (
            "EXPLAIN plan must reference the attempts table"
        )
        assert "Seq Scan on usage_ingest_attempts" not in plan, (
            "Attempts leg must use an index scan, not a sequential scan"
        )

    finally:
        await conn.execute(
            "DROP TABLE IF EXISTS usage_ingest_attempts,"
            " usage_events, source_identities CASCADE"
        )
        await conn.close()
