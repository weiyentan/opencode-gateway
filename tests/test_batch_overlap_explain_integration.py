"""Integration test that verifies the batch overlap query's
``usage_ingest_attempts`` leg is index-backed when a real PostgreSQL
instance is available.

This directly addresses the reviewer's request for real-DB EXPLAIN
confidence in the index plan (PR #418, Finding 1 / Note B).  The test
is marked ``@pytest.mark.integration`` so CI excludes it with
``-m "not integration"`` (see .github/workflows/ci.yml).  When no
database is reachable it skips gracefully.

SAFETY: this test NEVER touches the real ``public`` tables.  All
``source_identities`` / ``usage_events`` / ``usage_ingest_attempts``
objects are created inside a UNIQUE per-run scratch schema
(``gateway_explain_<random>``), and cleanup drops only that schema
(``DROP SCHEMA ... CASCADE``).  If the scratch schema cannot be
created (e.g. the connection user lacks CREATE privilege) the test
skips rather than failing — this is an optional integration check.

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
_TARGET_OUTCOMES = ("accepted", "duplicate", "updated")
_SCRATCH_SCHEMA_PREFIX = "gateway_explain_"

# The exact query from check_batch_overlap (app/core/identity.py),
# parameterised with the same $1/$2/$3 shape ($1 client_id, $2
# source_record_ids, $3 canonical_identity_id) so the plan reflects
# production behaviour.
_OVERLAP_QUERY = """EXPLAIN
    SELECT overlapping_identity_id, COUNT(*)::int AS overlap_count
    FROM (
        SELECT ue.canonical_source_identity_id AS overlapping_identity_id,
               ue.source_record_id AS source_record_id
        FROM usage_events ue
        JOIN source_identities si ON si.id = ue.canonical_source_identity_id
        WHERE si.client_id = $1
          AND ue.source_record_id = ANY($2::text[])
          AND ue.canonical_source_identity_id <> $3
          AND ue.canonical_source_identity_id NOT IN (
              SELECT id FROM source_identities
              WHERE canonical_parent_id IS NOT NULL
          )
        UNION
        SELECT a.source_identity_id AS overlapping_identity_id,
               a.original_source_record_id AS source_record_id
        FROM usage_ingest_attempts a
        JOIN source_identities si ON si.id = a.source_identity_id
        WHERE si.client_id = $1
          AND a.original_source_record_id = ANY($2::text[])
          AND a.source_identity_id <> $3
          AND a.source_identity_id NOT IN (
              SELECT id FROM source_identities
              WHERE canonical_parent_id IS NOT NULL
          )
          AND a.outcome IN ('accepted', 'duplicate', 'updated')
    ) shared_records
    GROUP BY overlapping_identity_id
    ORDER BY overlap_count DESC"""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_overlap_attempts_leg_uses_index_scan() -> None:
    """Run EXPLAIN on the full batch overlap query and assert the
    ``usage_ingest_attempts`` leg is served by an index (not a sequential
    scan)."""

    # ── Resolve connection params from the same env vars as Settings ─────────
    kwargs: dict[str, object] = {
        "host": os.environ.get("GATEWAY_DATABASE_HOST", "localhost"),
        "port": os.environ.get("GATEWAY_DATABASE_PORT", "5432"),
        "database": os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway"),
        "user": os.environ.get("GATEWAY_DATABASE_USER", "opencode"),
        "password": os.environ.get("GATEWAY_DATABASE_PASSWORD", ""),
    }

    conn: asyncpg.Connection | None = None
    scratch_schema: str | None = None
    try:
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

        # ── Dedicated scratch schema — NEVER the public tables ──────────
        scratch_schema = f"{_SCRATCH_SCHEMA_PREFIX}{uuid.uuid4().hex[:10]}"
        try:
            await conn.execute(f'CREATE SCHEMA "{scratch_schema}"')
        except Exception:
            pytest.skip(
                "cannot create scratch schema — insufficient privileges"
            )
        # asyncpg is in autocommit by default, so this SET persists for
        # the whole session; every unqualified name below resolves to
        # the scratch schema, leaving the public schema untouched.
        await conn.execute(f'SET search_path TO "{scratch_schema}"')

        # ── Create the three minimal tables in the scratch schema ───────
        # Fresh scratch tables need no IF NOT EXISTS — the schema is
        # unique per run, so the minimal columns are sufficient here.
        await conn.execute(
            """CREATE TABLE source_identities (
                id uuid PRIMARY KEY,
                client_id uuid NOT NULL,
                collector_source_id text NOT NULL,
                canonical_parent_id uuid
            )"""
        )
        await conn.execute(
            """CREATE TABLE usage_events (
                id uuid PRIMARY KEY,
                canonical_source_identity_id uuid NOT NULL,
                source_record_id text NOT NULL
            )"""
        )
        await conn.execute(
            """CREATE TABLE usage_ingest_attempts (
                id uuid PRIMARY KEY,
                source_identity_id uuid NOT NULL,
                original_source_record_id text NOT NULL,
                outcome text NOT NULL
            )"""
        )

        # ── Create indexes CONCURRENTLY (requires autocommit) ───────────
        # Exactly as migrations 0024/0025 do; asyncpg's default autocommit
        # means these are NOT wrapped in a transaction block.
        await conn.execute(
            "CREATE INDEX CONCURRENTLY ix_usage_events_source_record_id "
            "ON usage_events (source_record_id)"
        )
        await conn.execute(
            "CREATE INDEX CONCURRENTLY "
            "ix_usage_ingest_attempts_original_source_record_id "
            "ON usage_ingest_attempts (original_source_record_id)"
        )

        # ── Seed a realistic dataset so the planner genuinely prefers ───
        # ── the index over a seq scan on tiny fresh tables.            ──
        client_id = uuid.uuid4()
        candidate_identity = uuid.uuid4()  # $3 — excluded from evidence
        overlapping_identity = uuid.uuid4()  # owns the expected hits
        filler_identities = [
            uuid.uuid4() for _ in range(_FILLER_IDENTITY_COUNT)
        ]
        all_identities = [
            (candidate_identity, "collector-candidate"),
            (overlapping_identity, "collector-overlapping"),
        ]
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
        # identities, plus the target records — rec-1 owned by the
        # candidate identity (excluded by <> $3) and rec-2 owned by the
        # overlapping identity (the expected hit in the events leg).
        event_rows = [
            (
                uuid.uuid4(),
                filler_identities[n % _FILLER_IDENTITY_COUNT],
                f"filler-rec-{n}",
            )
            for n in range(_FILLER_IDENTITY_COUNT * _FILLER_ROWS_PER_IDENTITY)
        ]
        event_rows.append((uuid.uuid4(), candidate_identity, "rec-1"))
        event_rows.append((uuid.uuid4(), overlapping_identity, "rec-2"))
        await conn.executemany(
            "INSERT INTO usage_events "
            "(id, canonical_source_identity_id, source_record_id) "
            "VALUES ($1, $2, $3)",
            event_rows,
        )

        # usage_ingest_attempts: ~2,000 filler rows cycling through the
        # accounting outcomes ('accepted' / 'duplicate' / 'updated'),
        # plus target rows for BOTH the candidate identity (excluded by
        # <> $3) and the overlapping identity so the attempts leg also
        # resolves to the overlapping identity — present in BOTH legs of
        # the overlap query.
        attempt_rows = [
            (
                uuid.uuid4(),
                filler_identities[n % _FILLER_IDENTITY_COUNT],
                f"filler-rec-{n}",
                _TARGET_OUTCOMES[n % len(_TARGET_OUTCOMES)],
            )
            for n in range(_FILLER_IDENTITY_COUNT * _FILLER_ROWS_PER_IDENTITY)
        ]
        for rec_id in _TARGET_RECORD_IDS:
            attempt_rows.append(
                (uuid.uuid4(), candidate_identity, rec_id, "accepted")
            )
            attempt_rows.append(
                (uuid.uuid4(), overlapping_identity, rec_id, "duplicate")
            )
        await conn.executemany(
            "INSERT INTO usage_ingest_attempts "
            "(id, source_identity_id, original_source_record_id, outcome) "
            "VALUES ($1, $2, $3, $4)",
            attempt_rows,
        )

        # ── EXPLAIN the full batch overlap query ────────────────────────
        # Disable seq scans so the plan MUST use the index when it exists:
        # with ``enable_seqscan = off`` the planner only falls back to a
        # Seq Scan when no usable index is present, which makes the
        # assertion below a genuine guard for the migration's index.
        # Restored right after EXPLAIN (connection close in ``finally`` is
        # the backup).
        await conn.execute("SET enable_seqscan = off")
        try:
            rows = await conn.fetch(
                _OVERLAP_QUERY,
                client_id,
                _TARGET_RECORD_IDS,
                candidate_identity,
            )
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
        assert "usage_events" in plan, (
            "EXPLAIN plan must reference the events table"
        )
        assert "Seq Scan on usage_events" not in plan, (
            "Events leg must use an index scan, not a sequential scan"
        )
        assert "ix_usage_events_source_record_id" in plan, (
            "Events leg must be served by the "
            "ix_usage_events_source_record_id index"
        )

    finally:
        if conn is not None:
            if scratch_schema is not None:
                try:
                    # Drop ONLY the scratch schema — the real public
                    # tables are never touched, so this cleanup is safe
                    # against any migrated database.
                    await conn.execute(
                        f'DROP SCHEMA IF EXISTS "{scratch_schema}" CASCADE'
                    )
                except Exception:
                    # Best-effort cleanup: the connection may already be
                    # broken.  A stray uniquely-named scratch schema is
                    # harmless and is never a production table.
                    pass
            await conn.close()
