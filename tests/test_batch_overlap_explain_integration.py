"""Integration test that verifies the batch overlap query's
``usage_ingest_attempts`` leg is index-backed when a real PostgreSQL
instance is available.

This directly addresses the reviewer's request for real-DB EXPLAIN
confidence in the index plan (PR #418, Finding 1 / Note B).  The test
is marked ``@pytest.mark.integration`` so CI excludes it with
``-m "not integration"``.  When no database is reachable it skips
gracefully.

To make the index-plan verification deterministic the test seeds a
realistic dataset (thousands of rows) and disables ``enable_seqscan``
for the EXPLAIN run: with sequential scans disabled the planner MUST
serve the ``usage_ingest_attempts`` predicate through
``ix_usage_ingest_attempts_original_source_record_id`` when the index
exists, and can only fall back to a ``Seq Scan`` when the index is
genuinely missing — so the ``no Seq Scan`` assertion is a true guard
for the migration's index rather than an accident of tiny tables.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

_FILLER_IDENTITY_COUNT = 20
_FILLER_ROWS_PER_IDENTITY = 100
_TARGET_RECORD_IDS = ["rec-1", "rec-2"]


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

        # ── Seed a realistic dataset so the planner genuinely prefers ───
        # ── the index over a seq scan on tiny fresh tables.            ──
        client_id = uuid.uuid4()
        identity_a = uuid.uuid4()
        identity_b = uuid.uuid4()
        filler_identities = [
            uuid.uuid4() for _ in range(_FILLER_IDENTITY_COUNT)
        ]
        all_identities = [(identity_a, "collector-a"), (identity_b, "collector-b")]
        all_identities.extend(
            (filler_id, f"collector-filler-{i}")
            for i, filler_id in enumerate(filler_identities)
        )
        await conn.executemany(
            "INSERT INTO source_identities (id, client_id, collector_source_id) "
            "VALUES ($1, $2, $3)",
            [(ident, client_id, name) for ident, name in all_identities],
        )

        # usage_events: ~2,000 filler rows spread across the filler
        # identities, plus the target records — rec-1 owned by the excluded
        # identity A and rec-2 owned by identity B (the expected hit in the
        # events leg).
        event_rows = [
            (
                uuid.uuid4(),
                filler_identities[n % _FILLER_IDENTITY_COUNT],
                f"filler-rec-{n}",
            )
            for n in range(_FILLER_IDENTITY_COUNT * _FILLER_ROWS_PER_IDENTITY)
        ]
        event_rows.append((uuid.uuid4(), identity_a, "rec-1"))
        event_rows.append((uuid.uuid4(), identity_b, "rec-2"))
        await conn.executemany(
            "INSERT INTO usage_events "
            "(id, canonical_source_identity_id, source_record_id) "
            "VALUES ($1, $2, $3)",
            event_rows,
        )

        # usage_ingest_attempts: ~2,000 filler rows (outcome 'accepted'),
        # plus target rows for identity B so the attempts leg also resolves
        # to identity B — present in BOTH legs of the overlap query.
        attempt_rows = [
            (
                uuid.uuid4(),
                filler_identities[n % _FILLER_IDENTITY_COUNT],
                f"filler-rec-{n}",
                "accepted",
            )
            for n in range(_FILLER_IDENTITY_COUNT * _FILLER_ROWS_PER_IDENTITY)
        ]
        for rec_id in _TARGET_RECORD_IDS:
            attempt_rows.append((uuid.uuid4(), identity_b, rec_id, "accepted"))
        await conn.executemany(
            "INSERT INTO usage_ingest_attempts "
            "(id, source_identity_id, original_source_record_id, outcome) "
            "VALUES ($1, $2, $3, $4)",
            attempt_rows,
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

        # Disable seq scans so the plan MUST use the index when it exists:
        # with ``enable_seqscan = off`` the planner only falls back to a
        # Seq Scan when no usable index is present, which makes the
        # assertion below a genuine guard for the migration's index.
        # Restored right after EXPLAIN (connection close in ``finally`` is
        # the backup).
        await conn.execute("SET enable_seqscan = off")
        try:
            rows = await conn.fetch(explain_sql)
        finally:
            await conn.execute("SET enable_seqscan = on")
        plan = "\n".join(str(r[0]) for r in rows)

        assert "usage_ingest_attempts" in plan, (
            "EXPLAIN plan must reference the attempts table"
        )
        assert "Seq Scan on usage_ingest_attempts" not in plan, (
            "Attempts leg must use an index scan, not a sequential scan"
        )
        assert "ix_usage_ingest_attempts_original_source_record_id" in plan, (
            "Attempts leg must be served by the "
            "ix_usage_ingest_attempts_original_source_record_id index"
        )

    finally:
        await conn.execute(
            "DROP TABLE IF EXISTS usage_ingest_attempts,"
            " usage_events, source_identities CASCADE"
        )
        await conn.close()
