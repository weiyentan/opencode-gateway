"""Database setup — runs Alembic migrations and validates required relations.

This module replaces the old ``app/db/schema.py`` ``ensure_schema()``
approach that executed a static ``schema.sql`` file at startup.  Alembic
is now the single source of truth for the production database schema.

On startup the Gateway:

1. Runs ``alembic upgrade head`` to bring the database to the latest
   migration revision.
2. Checks that every relation in :data:`_REQUIRED_TABLES` exists in the
   database.
3. Fails fast with a clear error message naming the missing relations, so
   an incomplete schema is never published as a ready connection pool.

Required-relation inventory (issue #825)
----------------------------------------
The required set is the identity layer plus the canonical usage/ingestion
ledger relations that today's Gateway API and ``POST /ingest`` path touch
on every request.  A successful ``alembic upgrade head`` creates all of
them; the runtime check exists because an ``alembic_version`` sitting at
head does not prove every expected relation is still present (a relation
can be dropped manually, or a migration can fail part-way).

Included:

* identity — ``opencode_clients`` and ``collector_credentials`` (API-key
  ownership and collector-token validation).
* usage ledger — ``source_databases``, ``sessions``, ``usage_events``,
  ``opencode_usage_records`` and ``observed_models`` (the canonical event
  plus its session, model and legacy raw-record context).
* canonical identity and ingest bookkeeping — ``source_identities``,
  ``source_identity_quarantine``, ``ingest_batches``, ``ingest_audit`` and
  ``usage_ingest_attempts``.
* client/project rollup — ``client_project_rollup`` is maintained inside
  the canonical ingest transaction, so a missing rollup breaks the same
  requests that write ``usage_events``.

Deliberately excluded (recorded here so the inventory does not drift):

* Optional ingestion projections — ``opencode_source_projects``,
  ``opencode_session_contexts``, ``opencode_project_directories``,
  ``opencode_session_todos``, ``observed_messages``, ``observed_parts``
  and ``observed_tool_calls`` are populated only from optional batch
  arrays.  Per-projection failures are isolated and reported as
  ``projection_rejected``; their absence must never block the usage ledger.
* AFK reporting read-model — ``afk_runs``, ``afk_run_entities``,
  ``afk_run_sessions``, ``engineering_events``, ``delivery_log``,
  ``execution_bindings`` and the closure/recovery projections.  They are
  required by the AFK reporting surfaces and are covered by the separate
  AFK outcome coverage slice (#832), which extends this validation from
  usage into the enabled AFK paths.
* Derived read-models — ``afk_dashboard_daily``, ``usage_dashboard_daily``
  and ``reporting_resource_aggregates`` are rebuildable summaries refreshed
  out of band.
* Reporting ingestion — ``reporting_deliveries`` / ``delivery_state_trails``
  belong to the reporting ingest/read pipeline, not the usage ingest path.
* Admin/repair-only relations (e.g. ``source_identity_resolutions``) and
  ``alembic_version`` (Alembic migration metadata, not an application
  relation).

When a new source-of-truth relation becomes part of an active API or
ingestion path, add it here in the same change that introduces its query
and migration.  Optional or derived relations stay out unless their absence
would be reported as a broken required path; list them above with the
reason so the boundary is explicit.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# Relations that MUST exist after migrations have been applied.  This is the
# identity layer plus the canonical usage/ingestion ledger — see the module
# docstring for the inventory rationale and the deliberately excluded sets.
_REQUIRED_TABLES: list[str] = [
    # ── Identity layer ───────────────────────────────────────────────
    "opencode_clients",
    "collector_credentials",
    # ── Canonical usage ledger ───────────────────────────────────────
    "source_databases",
    "sessions",
    "usage_events",
    "opencode_usage_records",
    "observed_models",
    # ── Canonical identity + ingest bookkeeping ──────────────────────
    "source_identities",
    "source_identity_quarantine",
    "ingest_batches",
    "ingest_audit",
    "usage_ingest_attempts",
    # ── Client/project rollup (written inside the ingest transaction) ─
    "client_project_rollup",
]

# Resolved once when the module is loaded.
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_migrations_sync() -> None:
    """Synchronous core — called via ``asyncio.to_thread``."""
    import alembic.command
    import alembic.config

    alembic_cfg = alembic.config.Config(str(_PROJ_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_PROJ_ROOT / "alembic"))
    alembic.command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied successfully.")


async def run_migrations() -> None:
    """Run Alembic migrations to upgrade the database to the latest revision.

    This should be called during the Gateway startup lifespan, after the
    Postgres connection pool has been established.  The function is a
    no-op if Alembic determines the database is already at ``head``.

    Alembic reads database connection parameters from the Gateway
    settings (``GATEWAY_*`` env vars) via ``alembic/env.py``, so no
    additional configuration is needed.
    """
    logger.info("Running Alembic migrations...")
    try:
        await asyncio.to_thread(_run_migrations_sync)
    except Exception:
        logger.exception("Alembic migrations failed.")
        raise


async def check_required_tables(pool: asyncpg.Pool) -> None:
    """Verify all required usage/ingestion relations exist in the database.

    Queries ``information_schema.tables`` for the public schema and
    compares the result against the canonical list of required relations
    (:data:`_REQUIRED_TABLES`).

    Raises:
        RuntimeError: If one or more required relations are missing,
            with a clear, deterministic message naming every missing
            relation and suggesting ``alembic upgrade head`` as a fix.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        existing = {row["table_name"] for row in rows}

    required = set(_REQUIRED_TABLES)
    missing = required - existing

    if missing:
        msg = (
            f"Required database tables are missing: {', '.join(sorted(missing))}. "
            f"Run 'alembic upgrade head' to create them."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    logger.info("All %d required database tables are present.", len(required))
    return None
