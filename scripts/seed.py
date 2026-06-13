#!/usr/bin/env python3
"""Seed script for populating the OpenCode Gateway database with sample data.

Connects using the same ``DatabasePool`` / ``Settings`` infrastructure as the
main Gateway application.  Accepts command-line flags to control the volume of
generated data.  Idempotent — safe to run multiple times.

Usage::

    python scripts/seed.py --runners 2 --workspaces 2 --jobs 3 --observations 5

    # Also accepts short flags
    python scripts/seed.py -r 5 -w 10 -j 15 -o 20

    # Run with --help for a full listing
    python scripts/seed.py --help
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
# Idempotent helpers — each INSERT uses ON CONFLICT DO NOTHING.
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the OpenCode Gateway database with sample data.",
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
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    settings = Settings()

    pool = DatabasePool(settings)
    try:
        await pool.connect()
        if pool.pool is None:
            logger.error("Failed to connect to the database. Check your settings.")
            sys.exit(1)

        # Ensure both schemas exist.
        await ensure_schema(pool.pool)
        await _ensure_alembic_schema(settings)

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


async def _ensure_alembic_schema(settings: Settings) -> None:
    """Run Alembic migrations to create ORM-managed tables (runners, observations)."""
    import subprocess

    env = os.environ.copy()
    env["GATEWAY_DATABASE_HOST"] = settings.database_host
    env["GATEWAY_DATABASE_PORT"] = str(settings.database_port)
    env["GATEWAY_DATABASE_NAME"] = settings.database_name
    env["GATEWAY_DATABASE_USER"] = settings.database_user
    env["GATEWAY_DATABASE_PASSWORD"] = settings.database_password

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=_proj_root,
    )
    if result.returncode != 0:
        logger.warning("Alembic upgrade returned non-zero: %s", result.stderr)
    else:
        logger.info("Alembic migrations applied.")


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
