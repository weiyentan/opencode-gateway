"""PostgreSQL advisory lock helpers for workspace coordination.

Provides key constants, UUID-to-key conversion, and acquire/release
functions for the advisory locks used by the workspace API and the
background cleanup scheduler.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any

import asyncpg

# ---------------------------------------------------------------------------
# Lock key constants — advisory lock namespaces
# ---------------------------------------------------------------------------

PORT_LOCK_KEY = 47_001
"""Fixed key used to serialise port allocation across concurrent requests."""

CLEANUP_LOCK_CLASS = 47_002
"""High 32 bits of the two-arg per-workspace cleanup lock.

The low 32 bits are derived from the workspace UUID (``int & 0xFFFFFFFF``).
"""


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def uuid_to_lock_key(workspace_id: Any) -> int:
    """Convert a workspace UUID to a positive bigint for advisory locks.

    PostgreSQL advisory locks accept ``bigint`` keys (signed 64-bit on the
    wire).  We derive the key by taking the low 63 bits of the UUID's
    128-bit integer representation so the result is always a non-negative
    bigint.

    Accepts a :class:`uuid.UUID` object or a string representation.
    """
    if isinstance(workspace_id, _uuid.UUID):
        uid = workspace_id
    else:
        uid = _uuid.UUID(str(workspace_id))
    return uid.int & 0x7FFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Cleanup lock acquire / release (per-workspace)
# ---------------------------------------------------------------------------


async def try_acquire_cleanup_lock(
    conn: asyncpg.Connection, workspace_id: _uuid.UUID
) -> bool:
    """Try to acquire a per-workspace cleanup advisory lock.

    Uses ``pg_try_advisory_lock`` so the caller can distinguish between
    "lock already held" and other failures.  Returns ``True`` when the
    lock is acquired, ``False`` when another process holds it.

    The lock key is ``(CLEANUP_LOCK_CLASS, workspace_id.int & 0xFFFFFFFF)``
    — the same key used by :func:`release_cleanup_lock` so that the
    cleanup API endpoint and the background scheduler coordinate correctly.
    """
    locked: bool = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)",
        CLEANUP_LOCK_CLASS,
        workspace_id.int & 0xFFFFFFFF,
    )
    return locked


async def release_cleanup_lock(
    conn: asyncpg.Connection, workspace_id: _uuid.UUID
) -> None:
    """Release the per-workspace cleanup advisory lock."""
    await conn.execute(
        "SELECT pg_advisory_unlock($1, $2)",
        CLEANUP_LOCK_CLASS,
        workspace_id.int & 0xFFFFFFFF,
    )
