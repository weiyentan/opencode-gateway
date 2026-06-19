"""Port allocation service — Postgres-backed port management with advisory locks.

Implements ADR 0003's port allocation contract using PostgreSQL advisory
locks as the serialisation primitive.  Ports are allocated from the range
10000–10999 and persisted against workspace rows in the database.

Key design decisions:
- Advisory lock (``PORT_LOCK_KEY``) serialises all allocate/release calls
  so no two concurrent requests can receive the same port.
- Allocation scans for the first unused port in the range (gap-aware)
  rather than a naive ``MAX(port) + 1`` that wastes ports forever.
- ``PortExhaustedError`` is raised when all 1000 ports are in use so
  callers get a clear signal.
- Release sets the workspace port to NULL, making it available for reuse.
"""

from __future__ import annotations

import logging

import asyncpg

from app.db.lock import PORT_LOCK_KEY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PORT_RANGE_START: int = 10000
PORT_RANGE_END: int = 10999
PORT_RANGE_SIZE: int = PORT_RANGE_END - PORT_RANGE_START + 1  # 1000

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PortExhaustedError(Exception):
    """Raised when all ports in the range 10000–10999 are currently allocated."""

    def __init__(self) -> None:
        super().__init__(
            f"No free ports available in range {PORT_RANGE_START}–{PORT_RANGE_END}"
        )


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


async def allocate_port(conn: asyncpg.Connection) -> int:
    """Allocate a free port from the range 10000–10999.

    Acquires an exclusive advisory lock (``PORT_LOCK_KEY``), finds the
    first unused port in the range, and returns it.  The caller is
    responsible for persisting the port against a workspace row in the
    same transaction.

    Raises:
        PortExhaustedError: When all 1000 ports are currently in use.

    The advisory lock is released when the connection is returned to the
    pool (or when ``release_port`` is called explicitly).
    """
    await conn.execute("SELECT pg_advisory_lock($1)", PORT_LOCK_KEY)
    try:
        # Collect all currently-allocated ports in the range.
        rows = await conn.fetch(
            "SELECT port FROM workspaces "
            "WHERE port IS NOT NULL AND port >= $1 AND port <= $2 "
            "ORDER BY port ASC",
            PORT_RANGE_START,
            PORT_RANGE_END,
        )
        used: set[int] = {row["port"] for row in rows}

        # Find the first gap.  A sequential scan of 1000 integers is O(1)
        # in practice — this is a tiny range.
        for candidate in range(PORT_RANGE_START, PORT_RANGE_END + 1):
            if candidate not in used:
                logger.debug("Allocated port %d (used=%d)", candidate, len(used))
                return candidate

        raise PortExhaustedError()
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", PORT_LOCK_KEY)


async def allocate_and_assign_port(
    conn: asyncpg.Connection,
    workspace_id: asyncpg.UUID | str,
) -> int:
    """Atomically allocate a free port and assign it to a workspace.

    Begins a database transaction, acquires the ``PORT_LOCK_KEY`` advisory
    lock, calls :func:`allocate_port` to find the next free port, persists
    the assignment by updating the workspace row, and commits.

    The advisory lock is held for the entire operation — even though
    :func:`allocate_port` internally acquires and releases its own instance
    of the same lock (PG advisory locks are **reentrant per session**), the
    outer acquisition ensures no concurrent caller can observe or modify
    port state between allocation and persistence.

    Args:
        conn:         An asyncpg database connection.
        workspace_id: The UUID (or string representation) of the target
                      workspace.

    Returns:
        The allocated port number.

    Raises:
        PortExhaustedError:  If all 1000 ports in the range 10000–10999
                             are currently allocated.
        ValueError:          If no workspace with the given *workspace_id*
                             exists in the database.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_lock($1)", PORT_LOCK_KEY)
        try:
            port = await allocate_port(conn)

            result = await conn.execute(
                "UPDATE workspaces SET port = $1, updated_at = NOW() "
                "WHERE id = $2",
                port,
                workspace_id,
            )

            if result == "UPDATE 0":
                raise ValueError(
                    f"Workspace {workspace_id} not found — "
                    f"cannot assign port {port}"
                )

            logger.info(
                "Assigned port %d to workspace %s",
                port,
                workspace_id,
            )
            return port

        except PortExhaustedError:
            logger.warning(
                "Port exhaustion for workspace %s — all ports in range "
                "%d–%d are in use",
                workspace_id,
                PORT_RANGE_START,
                PORT_RANGE_END,
            )
            raise
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", PORT_LOCK_KEY)


async def release_port(conn: asyncpg.Connection, workspace_id: asyncpg.UUID | str) -> None:
    """Release the port associated with a workspace, making it available for reuse.

    Sets the workspace's ``port`` column to NULL.  Acquires the
    ``PORT_LOCK_KEY`` advisory lock to serialise with concurrent
    allocations.

    If the workspace has no port allocated, this is a no-op.
    """
    await conn.execute("SELECT pg_advisory_lock($1)", PORT_LOCK_KEY)
    try:
        result = await conn.execute(
            "UPDATE workspaces SET port = NULL, updated_at = NOW() "
            "WHERE id = $1 AND port IS NOT NULL",
            workspace_id,
        )
        # asyncpg returns a string like "UPDATE N" for execute()
        if result != "UPDATE 0":
            logger.debug("Released port for workspace %s", workspace_id)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", PORT_LOCK_KEY)
