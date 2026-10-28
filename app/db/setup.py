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

Required-relation inventory (issues #825, #832)
-----------------------------------------------
The required set is the identity layer, the canonical usage/ingestion
ledger relations that today's Gateway API and ``POST /ingest`` path touch
on every request, and the AFK reporting/event-consumer relations that the
always-registered AFK API routers and enabled consumer paths require.  A
successful ``alembic upgrade head`` creates all of them; the runtime check
exists because an ``alembic_version`` sitting at head does not prove every
expected relation is still present (a relation can be dropped manually, or
a migration can fail part-way).

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
* AFK reporting read-model (issue #832) — ``afk_runs``, ``afk_run_sessions``,
  ``afk_run_entities``, ``engineering_events``, ``delivery_log`` and
  ``unresolved_correlations`` back the always-registered AFK outcome/run
  APIs, the AFK outcome consumer's facts transaction
  (``delivery_log`` + ``engineering_events``), and the backfill/seeded run
  persistence path.
* AFK run provisioning and execution bindings (issue #832) —
  ``afk_run_delivery_batches`` is written by the provisioning transaction of
  ``POST /api/v1/afk/executions/runs``; ``execution_bindings`` and
  ``recovery_checkpoints`` back the ``/api/v1/afk/executions`` read/write
  surface.
* Reporting deliveries (issue #832) — ``reporting_deliveries`` and
  ``delivery_state_trails`` back the always-registered reporting ingest
  endpoint and the operator-gated reporting read API.

Deliberately excluded (recorded here so the inventory does not drift):

* Optional ingestion projections — ``opencode_source_projects``,
  ``opencode_session_contexts``, ``opencode_project_directories``,
  ``opencode_session_todos``, ``observed_messages``, ``observed_parts``
  and ``observed_tool_calls`` are populated only from optional batch
  arrays.  Per-projection failures are isolated and reported as
  ``projection_rejected``; their absence must never block the usage ledger.
* Derived / rebuildable AFK projections — ``closure_links``,
  ``closure_episodes`` and ``closure_unresolved`` are recomputed from
  immutable ``engineering_events`` facts after the facts transaction
  commits.  The recompute is best-effort and never blocks ingestion, so a
  missing projection degrades the derived closure view rather than an
  enabled write path.
* Derived read-models — ``afk_dashboard_daily``, ``usage_dashboard_daily``
  and ``reporting_resource_aggregates`` are rebuildable summaries refreshed
  out of band; their absence degrades a dashboard, never the facts path.
* ``resource_session_associations`` — the exact resource/session association
  write seam has no active service or API caller yet; the reporting
  session-links read path derives provisional links from
  ``afk_run_sessions``.
* Admin/repair-only relations — ``execution_outcome_corrections`` (operator
  correction CLI audit), ``source_identity_resolutions`` and
  ``alembic_version`` (Alembic migration metadata, not an application
  relation).
* Defunct execution-era relations — tables such as ``jobs``, ``runners`` and
  ``workspaces`` were removed by the observability refactor (issue #207) and
  must never be required again.

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
# identity layer, the canonical usage/ingestion ledger and the active AFK
# reporting/event-consumer relations — see the module docstring for the
# inventory rationale and the deliberately excluded sets.
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
    # ── AFK reporting read-model + consumer facts (issue #832) ────────
    "afk_runs",
    "afk_run_sessions",
    "afk_run_entities",
    "afk_run_delivery_batches",
    "engineering_events",
    "delivery_log",
    "unresolved_correlations",
    # ── AFK execution bindings + recovery checkpoints (issue #832) ────
    "execution_bindings",
    "recovery_checkpoints",
    # ── Reporting ingest/read pipeline (issue #832) ───────────────────
    "reporting_deliveries",
    "delivery_state_trails",
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
    """Verify all required usage/ingestion and AFK reporting relations exist.

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
