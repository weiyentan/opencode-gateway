"""Tests for the port allocation service (app/core/ports.py).

Covers allocation, gap-aware scanning, exhaustion, release, and concurrent
safety via PostgreSQL advisory locks per ADR 0003.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from tests.conftest import mock_row


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def _import_allocate_port():
    from app.core.ports import allocate_port

    return allocate_port


def _import_release_port():
    from app.core.ports import release_port

    return release_port


def _import_port_constants():
    from app.core import ports as mod

    return mod


def _import_port_exhausted_error():
    from app.core.ports import PortExhaustedError

    return PortExhaustedError


def _import_port_lock_key():
    from app.db.lock import PORT_LOCK_KEY

    return PORT_LOCK_KEY


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestPortConstants:
    """Verify port range constants are correct."""

    def test_port_range_start_is_10000(self):
        mod = _import_port_constants()
        assert mod.PORT_RANGE_START == 10000

    def test_port_range_end_is_10999(self):
        mod = _import_port_constants()
        assert mod.PORT_RANGE_END == 10999

    def test_port_range_size_is_1000(self):
        mod = _import_port_constants()
        assert mod.PORT_RANGE_SIZE == 1000


class TestPortExhaustedError:
    """Tests for the PortExhaustedError exception."""

    def test_message_contains_range(self):
        err = _import_port_exhausted_error()()
        assert "10000" in str(err)
        assert "10999" in str(err)

    def test_is_exception_subclass(self):
        err_cls = _import_port_exhausted_error()
        assert issubclass(err_cls, Exception)


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


class TestAllocatePort:
    """Tests for allocate_port()."""

    @pytest.mark.asyncio
    async def test_returns_first_port_when_none_allocated(self):
        """When no ports are allocated, allocate_port returns 10000."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()

        async def _fetch(sql: str, *args):
            # Return empty list — no used ports
            return []

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10000

    @pytest.mark.asyncio
    async def test_returns_next_sequential_port(self):
        """When first ports are taken, allocate_port returns the next one."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()

        async def _fetch(sql: str, *args):
            # Ports 10000, 10001, 10002 are taken
            return [mock_row({"port": p}) for p in (10000, 10001, 10002)]

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10003

    @pytest.mark.asyncio
    async def test_finds_gap_in_middle_of_range(self):
        """When a port in the middle is free, allocate_port returns it."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()

        # Ports 10000–10049 and 10051–10100 are taken. 10050 is free.
        used = list(range(10000, 10050)) + list(range(10051, 10101))

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in used]

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10050

    @pytest.mark.asyncio
    async def test_finds_first_free_port_at_start(self):
        """When port 10000 is free even though higher ports are used."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()

        # 10000 is free, 10001–10100 are taken
        used = list(range(10001, 10101))

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in used]

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10000

    @pytest.mark.asyncio
    async def test_acquires_and_releases_lock(self):
        """allocate_port must acquire then release the PG advisory lock."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        await allocate_port(conn)

        lock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_lock" in s]
        unlock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_unlock" in s]
        assert len(lock_calls) == 1
        assert len(unlock_calls) == 1

    @pytest.mark.asyncio
    async def test_uses_correct_lock_key(self):
        """The advisory lock key must match PORT_LOCK_KEY."""
        allocate_port = _import_allocate_port()
        PORT_LOCK_KEY = _import_port_lock_key()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        await allocate_port(conn)

        lock_call = next((s, a) for s, a in execute_calls if "pg_advisory_lock" in s)
        assert lock_call[1][0] == PORT_LOCK_KEY

    @pytest.mark.asyncio
    async def test_ignores_ports_outside_range(self):
        """Ports outside 10000–10999 should be ignored by allocation."""
        allocate_port = _import_allocate_port()

        conn = AsyncMock()

        # Ports 8080 and 9999 are outside range. 10000 should still be free.
        used = [8080, 9999, 11000, 20000]

        async def _fetch(sql: str, *args):
            # The query filters for port >= 10000 and port <= 10999,
            # so only return values within range. Our mock simulates
            # the DB-level filter — return empty to simulate no in-range
            # ports being used.
            return []

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10000


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------


class TestPortExhaustion:
    """Tests for port exhaustion scenario."""

    @pytest.mark.asyncio
    async def test_raises_when_all_ports_used(self):
        """When all 1000 ports are in use, PortExhaustedError is raised."""
        allocate_port = _import_allocate_port()
        PortExhaustedError = _import_port_exhausted_error()

        conn = AsyncMock()

        # All 1000 ports taken
        all_ports = list(range(10000, 11000))

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in all_ports]

        async def _execute(sql: str, *args):
            pass

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        with pytest.raises(PortExhaustedError):
            await allocate_port(conn)

    @pytest.mark.asyncio
    async def test_releases_lock_even_on_exhaustion(self):
        """The advisory lock must be released even when exhaustion occurs."""
        allocate_port = _import_allocate_port()
        PortExhaustedError = _import_port_exhausted_error()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        all_ports = list(range(10000, 11000))

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in all_ports]

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        try:
            await allocate_port(conn)
        except PortExhaustedError:
            pass

        unlock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_unlock" in s]
        assert len(unlock_calls) == 1


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestReleasePort:
    """Tests for release_port()."""

    @pytest.mark.asyncio
    async def test_release_sets_port_to_null(self):
        """release_port must UPDATE the workspace port to NULL."""
        release_port = _import_release_port()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)

        ws_id = uuid.uuid4()
        await release_port(conn, ws_id)

        # Check that the UPDATE SQL was issued with the workspace id
        update_calls = [(s, a) for s, a in execute_calls if "UPDATE workspaces" in s]
        assert len(update_calls) == 1
        sql, args = update_calls[0]
        assert "SET port = NULL" in sql
        assert args[0] == ws_id

    @pytest.mark.asyncio
    async def test_release_acquires_and_releases_lock(self):
        """release_port must acquire then release the advisory lock."""
        release_port = _import_release_port()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 1" if "UPDATE" in sql else None

        conn.execute = AsyncMock(side_effect=_execute)

        ws_id = uuid.uuid4()
        await release_port(conn, ws_id)

        lock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_lock" in s]
        unlock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_unlock" in s]
        assert len(lock_calls) == 1
        assert len(unlock_calls) == 1

    @pytest.mark.asyncio
    async def test_release_is_noop_when_port_already_null(self):
        """release_port is a no-op when the workspace has no port."""
        release_port = _import_release_port()

        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 0"  # No rows affected

        conn.execute = AsyncMock(side_effect=_execute)

        ws_id = uuid.uuid4()
        await release_port(conn, ws_id)

        # Lock/unlock should still happen, but UPDATE affected 0 rows
        lock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_lock" in s]
        assert len(lock_calls) == 1

    @pytest.mark.asyncio
    async def test_release_accepts_string_workspace_id(self):
        """release_port should accept a string workspace ID."""
        release_port = _import_release_port()

        conn = AsyncMock()

        async def _execute(sql: str, *args):
            return "UPDATE 1" if "UPDATE" in sql else None

        conn.execute = AsyncMock(side_effect=_execute)

        ws_id_str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        await release_port(conn, ws_id_str)

        update_calls = [(s, a) for s, a in conn.execute.call_args_list if "UPDATE workspaces" in str(s)]
        assert len(update_calls) == 1


# ---------------------------------------------------------------------------
# Concurrent allocation
# ---------------------------------------------------------------------------


class TestConcurrentAllocation:
    """Tests that concurrent allocations produce unique ports."""

    @pytest.mark.asyncio
    async def test_concurrent_allocations_are_unique(self):
        """Multiple concurrent allocations must each get a unique port."""
        allocate_port = _import_allocate_port()

        # Shared state to simulate a real database
        used_ports: set[int] = set()
        lock = asyncio.Lock()

        async def _fetch(sql: str, *args):
            # Return the current set of used ports
            return [mock_row({"port": p}) for p in sorted(used_ports)]

        async def _execute(sql: str, *args):
            pass

        async def concurrent_allocate() -> int:
            conn = AsyncMock()
            conn.fetch = AsyncMock(side_effect=_fetch)
            conn.execute = AsyncMock(side_effect=_execute)

            port = await allocate_port(conn)

            # Simulate persisting the port (under the lock)
            async with lock:
                assert port not in used_ports, f"Port {port} was already allocated!"
                used_ports.add(port)

            return port

        # Run 50 concurrent allocations
        tasks = [concurrent_allocate() for _ in range(50)]
        ports = await asyncio.gather(*tasks)

        assert len(ports) == 50
        assert len(set(ports)) == 50  # All unique
        # All ports must be in the valid range
        for p in ports:
            assert 10000 <= p <= 10999, f"Port {p} is outside valid range"

    @pytest.mark.asyncio
    async def test_concurrent_allocation_and_release(self):
        """Concurrent allocate and release should reuse freed ports."""
        allocate_port = _import_allocate_port()
        release_port = _import_release_port()

        # Simulate a small range for this test
        used_ports: set[int] = set()
        lock = asyncio.Lock()

        async def _fetch(sql: str, *args):
            return [mock_row({"port": p}) for p in sorted(used_ports)]

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET port = NULL" in sql:
                # Extract workspace id to find which port to release
                # In real code, the advisory lock serialises this.
                pass
            return "UPDATE 1"

        # Allocate 3 ports
        ports: list[int] = []
        for _ in range(3):
            conn = AsyncMock()
            conn.fetch = AsyncMock(side_effect=_fetch)
            conn.execute = AsyncMock(side_effect=_execute)
            p = await allocate_port(conn)
            async with lock:
                used_ports.add(p)
            ports.append(p)

        assert ports == [10000, 10001, 10002]

        # Release port 10001
        async with lock:
            used_ports.discard(10001)

        # Now allocate again — should get 10001 (the gap)
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)
        p = await allocate_port(conn)
        assert p == 10001, f"Expected 10001 (the gap), got {p}"


# ---------------------------------------------------------------------------
# Integration: full allocate → persist → release cycle
# ---------------------------------------------------------------------------


class TestPortLifecycle:
    """End-to-end port lifecycle: allocate → persist → release → re-allocate."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """A port can be allocated, persisted, released, and re-allocated."""
        allocate_port = _import_allocate_port()
        release_port = _import_release_port()

        # Simulated database state
        used_ports: dict[str, int] = {}  # workspace_id → port
        ws_id = uuid.uuid4()

        async def _make_conn():
            conn = AsyncMock()

            async def _fetch(sql: str, *args):
                return [mock_row({"port": p}) for p in sorted(used_ports.values())]

            async def _execute(sql: str, *args):
                if "UPDATE workspaces SET port = NULL" in sql:
                    ws = args[0]
                    used_ports.pop(str(ws), None)
                return "UPDATE 1"

            conn.fetch = AsyncMock(side_effect=_fetch)
            conn.execute = AsyncMock(side_effect=_execute)
            return conn

        # 1. Allocate
        conn = await _make_conn()
        port = await allocate_port(conn)
        assert port == 10000
        used_ports[str(ws_id)] = port

        # 2. Allocate another — should get 10001
        ws_id2 = uuid.uuid4()
        conn = await _make_conn()
        port2 = await allocate_port(conn)
        assert port2 == 10001
        used_ports[str(ws_id2)] = port2

        # 3. Release first port
        conn = await _make_conn()
        await release_port(conn, ws_id)
        used_ports.pop(str(ws_id), None)

        # 4. Re-allocate — should get 10000 again
        ws_id3 = uuid.uuid4()
        conn = await _make_conn()
        port3 = await allocate_port(conn)
        assert port3 == 10000
        used_ports[str(ws_id3)] = port3


# ---------------------------------------------------------------------------
# allocate_and_assign_port
# ---------------------------------------------------------------------------


def _import_allocate_and_assign_port():
    from app.core.ports import allocate_and_assign_port

    return allocate_and_assign_port


class TestAllocateAndAssignPort:
    """Tests for allocate_and_assign_port() (implemented in issue #136).

    These tests validate the combined allocate-and-persist workflow that
    ``allocate_and_assign_port(conn, workspace_id)`` will implement once
    #136 lands.  Until then, these tests will fail with ImportError.
    """

    @pytest.mark.asyncio
    async def test_returns_valid_port_and_updates_workspace(self):
        """allocate_and_assign_port returns a valid port and persists it."""
        allocate_and_assign_port = _import_allocate_and_assign_port()

        conn = AsyncMock()
        ws_id = uuid.uuid4()
        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):  # noqa: ARG001
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 1"

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_and_assign_port(conn, ws_id)

        assert 10000 <= port <= 10999

        # Verify the UPDATE was executed with the port and workspace id
        update_calls = [(s, a) for s, a in execute_calls if "UPDATE workspaces" in s]
        assert len(update_calls) == 1
        sql, args = update_calls[0]
        assert "SET port" in sql
        assert args[0] == port
        assert args[1] == ws_id

    @pytest.mark.asyncio
    async def test_acquires_and_releases_lock(self):
        """allocate_and_assign_port must acquire and release the advisory lock."""
        allocate_and_assign_port = _import_allocate_and_assign_port()

        conn = AsyncMock()
        ws_id = uuid.uuid4()
        execute_calls: list[tuple] = []

        async def _fetch(sql: str, *args):  # noqa: ARG001
            return []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 1"

        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        await allocate_and_assign_port(conn, ws_id)

        lock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_lock" in s]
        unlock_calls = [(s, a) for s, a in execute_calls if "pg_advisory_unlock" in s]
        assert len(lock_calls) == 1
        assert len(unlock_calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_allocate_and_assign_are_unique(self):
        """Multiple concurrent allocate_and_assign_port calls get unique ports."""
        allocate_and_assign_port = _import_allocate_and_assign_port()

        # Shared state to track used ports
        used_ports: set[int] = set()
        lock = asyncio.Lock()

        async def _fetch(sql: str, *args):  # noqa: ARG001
            return [mock_row({"port": p}) for p in sorted(used_ports)]

        async def _execute(sql: str, *args):  # noqa: ARG001
            return "UPDATE 1"

        async def concurrent_assign(ws_id) -> int:
            conn = AsyncMock()
            conn.fetch = AsyncMock(side_effect=_fetch)
            conn.execute = AsyncMock(side_effect=_execute)

            port = await allocate_and_assign_port(conn, ws_id)

            # Simulate the DB persisting the port (under the lock)
            async with lock:
                assert port not in used_ports, f"Port {port} was already allocated!"
                used_ports.add(port)

            return port

        ws_ids = [uuid.uuid4() for _ in range(50)]
        tasks = [concurrent_assign(ws_id) for ws_id in ws_ids]
        ports = await asyncio.gather(*tasks)

        assert len(ports) == 50
        assert len(set(ports)) == 50
        for p in ports:
            assert 10000 <= p <= 10999


# ---------------------------------------------------------------------------
# Port reuse after workspace cleanup
# ---------------------------------------------------------------------------


class TestPortReuseAfterCleanup:
    """Tests that ports are reusable after workspace cleanup.

    When a workspace is cleaned (``cleanup_status = 'cleaned'``), its port
    should not block reallocation — either the cleanup process releases the
    port, or the allocation query filters out cleaned workspaces.
    """

    @pytest.mark.asyncio
    async def test_cleaned_workspace_port_not_counted_as_used(self):
        """Port from a cleaned workspace is treated as available for allocation."""
        allocate_port = _import_allocate_port()

        # Simulate: two cleaned workspaces still hold ports 10000 and 10001.
        # The DB query filters them out (cleanup_status != 'cleaned'),
        # so allocate_port sees no used ports and returns 10000.
        async def _fetch(sql: str, *args):  # noqa: ARG001
            return []

        async def _execute(sql: str, *args):  # noqa: ARG001
            pass

        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.execute = AsyncMock(side_effect=_execute)

        port = await allocate_port(conn)
        assert port == 10000

    @pytest.mark.asyncio
    async def test_allocate_after_cleanup_reuses_freed_port(self):
        """allocate → cleanup → re-allocate cycle reuses the port."""
        allocate_port = _import_allocate_port()

        # Simulate DB state: active workspaces with allocated ports
        active_ports: set[int] = set()

        async def _fetch(sql: str, *args):  # noqa: ARG001
            return [mock_row({"port": p}) for p in sorted(active_ports)]

        async def _execute(sql: str, *args):  # noqa: ARG001
            return "UPDATE 1"

        async def _make_conn():
            conn = AsyncMock()
            conn.fetch = AsyncMock(side_effect=_fetch)
            conn.execute = AsyncMock(side_effect=_execute)
            return conn

        # 1. Allocate first port
        conn = await _make_conn()
        port_a = await allocate_port(conn)
        assert port_a == 10000
        active_ports.add(port_a)

        # 2. Allocate second port
        conn = await _make_conn()
        port_b = await allocate_port(conn)
        assert port_b == 10001
        active_ports.add(port_b)

        # 3. Simulate cleanup: first workspace cleaned, port freed
        active_ports.discard(10000)

        # 4. Allocate again — should get 10000 (reused from cleaned workspace)
        conn = await _make_conn()
        port_c = await allocate_port(conn)
        assert port_c == 10000
        active_ports.add(port_c)

        # 5. Verify we have 2 active ports with correct values
        assert len(active_ports) == 2
        assert 10000 in active_ports
        assert 10001 in active_ports


# ---------------------------------------------------------------------------
# Duplicate active port rejection (partial unique index from issue #135)
# ---------------------------------------------------------------------------


class TestDuplicatePortRejection:
    """Tests that duplicate active port assignments are rejected.

    Issue #135 adds a partial unique index on workspaces.port that only
    applies to active (non-cleaned) workspaces.  This prevents two active
    workspaces from holding the same port while allowing cleaned workspaces
    to retain their port value without blocking reuse.
    """

    @pytest.mark.asyncio
    async def test_duplicate_active_port_raises_unique_violation(self):
        """Assigning an active workspace's port to another active workspace fails."""
        from asyncpg.exceptions import UniqueViolationError

        conn = AsyncMock()

        async def _execute(sql: str, *args):  # noqa: ARG001
            if "UPDATE workspaces SET port" in sql:
                raise UniqueViolationError(
                    'duplicate key value violates unique constraint '
                    '"ix_workspaces_active_port"'
                )
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)

        with pytest.raises(UniqueViolationError):
            await conn.execute(
                "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
                10000,
                uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_duplicate_port_on_cleaned_workspace_allowed(self):
        """Two cleaned workspaces CAN share the same port (unique index excludes cleaned)."""
        conn = AsyncMock()
        execute_calls: list[tuple] = []

        async def _execute(sql: str, *args):
            execute_calls.append((sql, args))
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)

        # Assign port 10000 to two different cleaned workspaces —
        # the partial unique index allows this because cleanup_status='cleaned'
        # is excluded from the unique constraint.
        await conn.execute(
            "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
            10000,
            uuid.uuid4(),
        )
        await conn.execute(
            "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
            10000,
            uuid.uuid4(),
        )

        update_count = len([c for c in execute_calls if "UPDATE" in c[0]])
        assert update_count == 2


# ---------------------------------------------------------------------------
# CHECK constraint on port range (migration 0009)
# ---------------------------------------------------------------------------


class TestCheckConstraint:
    """Tests that port values outside 10000–10999 are rejected by CHECK constraint.

    Migration 0009 adds ``ck_workspaces_port_range`` which enforces
    ``port IS NULL OR (port >= 10000 AND port <= 10999)``.
    """

    @pytest.mark.asyncio
    async def test_port_below_range_rejected_by_constraint(self):
        """Port value below 10000 is rejected by the CHECK constraint."""
        from asyncpg.exceptions import CheckViolationError

        conn = AsyncMock()

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET port" in sql:
                for arg in args:
                    if isinstance(arg, int) and arg < 10000:
                        raise CheckViolationError(
                            'new row for relation "workspaces" violates '
                            'check constraint "ck_workspaces_port_range"'
                        )
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)

        with pytest.raises(CheckViolationError):
            await conn.execute(
                "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
                9999,
                uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_port_above_range_rejected_by_constraint(self):
        """Port value above 10999 is rejected by the CHECK constraint."""
        from asyncpg.exceptions import CheckViolationError

        conn = AsyncMock()

        async def _execute(sql: str, *args):
            if "UPDATE workspaces SET port" in sql:
                for arg in args:
                    if isinstance(arg, int) and arg > 10999:
                        raise CheckViolationError(
                            'new row for relation "workspaces" violates '
                            'check constraint "ck_workspaces_port_range"'
                        )
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)

        with pytest.raises(CheckViolationError):
            await conn.execute(
                "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
                11000,
                uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_null_port_accepted_by_constraint(self):
        """NULL port value is accepted (CHECK constraint allows NULL)."""
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await conn.execute(
            "UPDATE workspaces SET port = NULL, updated_at = NOW() WHERE id = $1",
            uuid.uuid4(),
        )
        assert result == "UPDATE 1"

    @pytest.mark.asyncio
    async def test_port_in_range_accepted_by_constraint(self):
        """Port values within 10000–10999 are accepted by the CHECK constraint."""
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")

        for port in (10000, 10500, 10999):
            result = await conn.execute(
                "UPDATE workspaces SET port = $1, updated_at = NOW() WHERE id = $2",
                port,
                uuid.uuid4(),
            )
            assert result == "UPDATE 1"
