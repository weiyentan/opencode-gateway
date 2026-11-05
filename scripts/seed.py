#!/usr/bin/env python3
"""Legacy seed script for execution-era sample data (issue #827 fail-closed).

Populates execution-era tables (``runners``, ``workspaces``, ``gateway_jobs``,
``runner_observations``, ``workspace_observations``,
``opencode_instance_observations``) which are no longer part of the Gateway's
observability domain (see issue #809).  The script is **not** idempotent
despite historical claims: ``_seed_runners`` / ``_seed_workspaces`` /
``_seed_jobs`` generate fresh ``uuid.uuid4()`` per row with
``ON CONFLICT (id) DO NOTHING`` — a new UUID never conflicts on replay, so
re-running inserts fresh rows.  ``_seed_observations`` inserts unconditionally.

Fail-closed safety (issue #827): the command refuses every target that is not
explicitly attested disposable via ``--confirm=<database_name>``.  Attestation
is validated **before** pool creation, migration or any insert; absent or
mismatched confirmation raises ``SystemExit`` with actionable text.  Migration
errors are fatal and halt before inserts; redundant second alembic invocation
has been removed (single ``ensure_schema`` path).

Usage::

    python scripts/seed.py --confirm my_disposable_db --runners 2 --workspaces 2 --jobs 3 --observations 5

    # Also accepts short flags plus --confirm alias --yes
    python scripts/seed.py --confirm opencode_gateway_test -r 5 -w 10 -j 15 -o 20

    # Run with --help for a full listing
    python scripts/seed.py --help

Do not run against production, shared, or Kubernetes databases.  A future
follow-up (issue #810) will retire or replace this tool with deterministic
observability fixtures.
"""

from __future__ import annotations

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9
import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg

# Ensure the app package is importable when run from the project root.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from app.core.config import Settings  # noqa: E402
from app.db.schema import ensure_schema  # noqa: E402
from app.db.session import DatabasePool  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_RUNNERS = 5
DEFAULT_WORKSPACES = 5
DEFAULT_JOBS = 10
DEFAULT_OBSERVATIONS = 20

# Valid status values (matches the Gateway domain)
JOB_STATUSES = [
    "pending",
    "running",
    "completed",
    "failed",
    "needs_approval",
    "rejected",
    "aborted",
]
RUNNER_STATUSES = ["HEALTHY", "UNKNOWN"]
CLEANUP_STATUSES = ["active", "cleaned_up", "pinned"]
EXECUTOR_TYPES = ["local", "awx"]


# ---------------------------------------------------------------------------
# Legacy helpers — NOT idempotent despite ON CONFLICT guards (issue #809/#827).
# Each call generates fresh uuid.uuid4() values; a new UUID never conflicts
# on replay, so re-running inserts fresh rows rather than converging. See
# _assert_disposable_target for the full replay-idempotency analysis.
# ---------------------------------------------------------------------------


async def _seed_runners(pool: asyncpg.Pool, count: int) -> list[uuid.UUID]:
    """Insert *count* runner rows.  Returns their UUIDs."""
    runner_ids: list[uuid.UUID] = []
    async with pool.acquire() as conn:
        for i in range(count):
            rid = uuid.uuid4()
            hostname = f"seed-runner-{i:03d}.example.com"
            await conn.execute(
                "INSERT INTO runners (id, runner_id, hostname, executor_type, "
                "labels, status, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $7) "
                "ON CONFLICT (runner_id) DO NOTHING",
                rid,
                str(rid),
                hostname,
                "local",
                f'{{"env": "seed", "index": {i}}}',
                "HEALTHY",
                datetime.now(timezone.utc),
            )
            runner_ids.append(rid)
    logger.info("Seeded %d runners.", count)
    return runner_ids


async def _seed_workspaces(
    pool: asyncpg.Pool, runner_ids: list[uuid.UUID], count: int
) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    """Insert *count* workspace rows, distributing them across runners."""
    pairs: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    async with pool.acquire() as conn:
        for i in range(count):
            ws_id = uuid.uuid4()
            r_id = runner_ids[i % len(runner_ids)] if runner_ids else None
            name = f"seed-ws-{i:03d}"
            path = f"/data/workspaces/{name}"
            await conn.execute(
                "INSERT INTO workspaces (id, runner_id, workspace_name, path, "
                "repo_url, branch, pinned, cleanup_after, cleanup_status, "
                "created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10) "
                "ON CONFLICT (id) DO NOTHING",
                ws_id,
                r_id,
                name,
                path,
                "https://github.com/example/seed-repo.git",
                "main",
                False,
                datetime.now(timezone.utc) + timedelta(hours=72),
                "active",
                datetime.now(timezone.utc),
            )
            pairs.append((ws_id, r_id))
    logger.info("Seeded %d workspaces.", count)
    return pairs


async def _seed_jobs(pool: asyncpg.Pool, count: int) -> list[uuid.UUID]:
    """Insert *count* job rows in various statuses."""
    job_ids: list[uuid.UUID] = []
    async with pool.acquire() as conn:
        for i in range(count):
            jid = uuid.uuid4()
            status = JOB_STATUSES[i % len(JOB_STATUSES)]
            completed_at = (
                datetime.now(timezone.utc) if status == "completed" else None
            )
            await conn.execute(
                "INSERT INTO gateway_jobs (id, status, repo_url, task_summary, "
                "executor_type, created_at, updated_at, completed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (id) DO NOTHING",
                jid,
                status,
                "https://github.com/example/seed-repo.git",
                f"Seed task #{i:03d}: {status} example",
                "local",
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                completed_at,
            )
            job_ids.append(jid)
    logger.info("Seeded %d jobs.", count)
    return job_ids


async def _seed_observations(
    pool: asyncpg.Pool, runner_ids: list[uuid.UUID], count: int
) -> None:
    """Insert *count* observations across three observation tables."""
    if not runner_ids:
        logger.warning("No runners to attach observations to — skipping.")
        return

    runner_obs_count = max(count // 2, 1)
    ws_obs_count = max(count // 4, 1)
    oc_obs_count = max(count // 4, 1)

    async with pool.acquire() as conn:
        # --- runner_observations ----------------------------------------------
        for i in range(runner_obs_count):
            rid = runner_ids[i % len(runner_ids)]
            await conn.execute(
                "INSERT INTO runner_observations "
                "(id, runner_id, disk_used_percent, memory_used_percent, "
                "load_1m, load_5m, load_15m, observed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                uuid.uuid4(),
                rid,
                30.0 + (i * 5 % 40),  # disk %
                45.0 + (i * 3 % 35),  # memory %
                0.5 + (i * 0.2),      # load
                None,
                None,
                datetime.now(timezone.utc) - timedelta(minutes=i * 10),
            )

        # --- workspace_observations -------------------------------------------
        for i in range(ws_obs_count):
            rid = runner_ids[i % len(runner_ids)]
            await conn.execute(
                "INSERT INTO workspace_observations "
                "(id, runner_id, workspace_name, status, opencode_status, observed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                uuid.uuid4(),
                rid,
                f"seed-ws-{i:03d}",
                "active" if i % 2 == 0 else "idle",
                "running" if i % 3 != 0 else "stopped",
                datetime.now(timezone.utc) - timedelta(minutes=i * 5),
            )

        # --- opencode_instance_observations -----------------------------------
        for i in range(oc_obs_count):
            rid = runner_ids[i % len(runner_ids)]
            await conn.execute(
                "INSERT INTO opencode_instance_observations "
                "(id, runner_id, instance_name, version, status, observed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                uuid.uuid4(),
                rid,
                f"oc-instance-{i:03d}",
                "0.1.0",
                "running",
                datetime.now(timezone.utc) - timedelta(minutes=i * 8),
            )

    logger.info(
        "Seeded %d observations (%d runner, %d workspace, %d opencode).",
        runner_obs_count + ws_obs_count + oc_obs_count,
        runner_obs_count,
        ws_obs_count,
        oc_obs_count,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _assert_disposable_target(settings: Settings, confirm: str | None) -> None:
    """Fail closed unless the target database is explicitly attested disposable.

    The legacy seed command may run migrations and insert execution-era sample
    rows.  It must refuse every target that is not explicitly attested disposable
    via ``--confirm=<database_name>``.  This check runs before any pool creation,
    migration or insert.

    Why random identifiers do not establish replay idempotency (issue #809/#827):
    ``_seed_runners`` / ``_seed_workspaces`` / ``_seed_jobs`` generate a fresh
    ``uuid.uuid4()`` per row and use ``ON CONFLICT (id) DO NOTHING``.  A new UUID
    never conflicts on replay, so re-running the tool inserts fresh sample rows
    rather than converging on a stable fixture set.  ``_seed_observations`` inserts
    new UUIDs unconditionally without any conflict clause.  The ``ON CONFLICT``
    guards therefore mask only accidental collisions, not logical idempotency.

    Args:
        settings: Resolved Gateway settings (database host/name).
        confirm: Value of ``--confirm`` supplied on the CLI.

    Raises:
        SystemExit: When attestation is absent or does not exactly match the
            target database name.  The message is actionable and names the
            expected confirmation value.
    """
    expected = settings.database_name
    if confirm is None or confirm != expected:
        raise SystemExit(
            f"Refusing to seed database '{expected}' at "
            f"{settings.database_host}:{settings.database_port} without explicit "
            f"disposable-database attestation. Re-run with --confirm='{expected}' "
            f"to attest this target is disposable and may be overwritten. "
            f"This command may run migrations and insert execution-era sample rows; "
            f"never use it against production, shared, or Kubernetes databases."
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Legacy seed for execution-era sample data (issue #827). "
            "Requires --confirm=<database_name> to attest the target is "
            "disposable; refuses every non-attested target and fails closed "
            "before pool creation, migration or inserts."
        ),
    )
    parser.add_argument(
        "--runners",
        "-r",
        type=int,
        default=DEFAULT_RUNNERS,
        help=f"Number of runner VMs to create (default: {DEFAULT_RUNNERS})",
    )
    parser.add_argument(
        "--workspaces",
        "-w",
        type=int,
        default=DEFAULT_WORKSPACES,
        help=f"Number of workspaces to create (default: {DEFAULT_WORKSPACES})",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Number of jobs to create (default: {DEFAULT_JOBS})",
    )
    parser.add_argument(
        "--observations",
        "-o",
        type=int,
        default=DEFAULT_OBSERVATIONS,
        help=f"Number of observation records to create (default: {DEFAULT_OBSERVATIONS})",
    )
    parser.add_argument(
        "--confirm",
        "--yes",
        dest="confirm",
        type=str,
        default=None,
        help="Explicit disposable-database attestation: must equal the target "
        "database name (e.g. --confirm='opencode_gateway_test') to confirm the "
        "target is disposable and safe to overwrite. Without this flag the "
        "command refuses to run.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    settings = Settings()
    # Fail closed before any side effect: pool creation, migration or inserts.
    _assert_disposable_target(settings, getattr(args, "confirm", None))

    pool = DatabasePool(settings)
    try:
        await pool.connect()
        if pool.pool is None:
            logger.error("Failed to connect to the database. Check your settings.")
            sys.exit(1)

        # Single migration path: ensure_schema runs Alembic and validates tables.
        # Migration errors are fatal and halt before any inserts (no second
        # alembic invocation, no warning-and-continue).
        await ensure_schema(pool.pool)

        logger.info(
            "Connected to %s:%d/%s as %s.",
            settings.database_host,
            settings.database_port,
            settings.database_name,
            settings.database_user,
        )

        # Populate in dependency order.
        runner_ids = await _seed_runners(pool.pool, args.runners)
        await _seed_workspaces(pool.pool, runner_ids, args.workspaces)
        await _seed_jobs(pool.pool, args.jobs)
        await _seed_observations(pool.pool, runner_ids, args.observations)

        logger.info("Seed complete.")
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    args = _parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
