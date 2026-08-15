"""Execution-transcript read-only REST API (issue #217, ADR 0016).

Six GET endpoints under the versioned ``/api/v1/execution`` prefix expose
the message/part/tool-call transcript slice stored by ingest:

- ``GET /sessions/{session_id}``           — transcript session header
- ``GET /sessions/{session_id}/children``  — child subagent sessions
- ``GET /sessions/{session_id}/messages``  — a session's messages, chronological
- ``GET /sessions/{session_id}/parts``     — a session's part events, chronological
- ``GET /sessions/{session_id}/timeline``  — unified timeline across parent + descendants
- ``GET /tool-calls``                      — global tool-call query

Transcript list endpoints use keyset (cursor) pagination ordered by
``(source_created_at, id)`` — stable under concurrent ingest.  The header
and children endpoints use the shared offset/limit :class:`PaginatedResponse`.
All responses use the existing ``{status, data, error}`` envelope and are
protected by the global :class:`~app.core.auth.ApiKeyMiddleware`.

The read path follows the ``app/api/usage.py`` / ``afk_outcomes.py``
convention: raw asyncpg via ``Depends(get_session)``, explicit-column
SELECTs, parameterised filters with 400 on invalid values, and the
``_db_timeout``/``_request_timeout`` helpers.

.. note:: Hierarchy sources diverge.  The ``/children`` and header child
   queries resolve parentage from ``sessions.parent_session_id`` (an external
   session-ID string), while the ``/timeline`` CTE and the header's
   ``parent_internal_id`` resolve from ``opencode_session_contexts.parent_session_id``
   (an internal UUID FK to ``sessions.id``), falling back to
   ``observed_messages.parent_external_session_id`` when the context
   projection is missing (ADR 0016 §1).  The two can diverge until
   reconciled, so ``/children`` and ``/timeline`` may return different
   descendant sets.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.pagination import NULL_CURSOR_SENTINEL, decode_cursor, next_cursor
from app.core.config import get_settings
from app.core.schemas.execution import (
    ChildSession,
    CursorPage,
    ObservedMessage,
    ObservedPart,
    ObservedToolCall,
    SessionHeader,
    TimelineEvent,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timeout_operation
from app.db.session import get_session

router = APIRouter(tags=["execution"])

# Default/max keyset page size and default timeline depth bound.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
# None = all generations (no depth cutoff); the recursion cycle guard is the
# safety bound in unbounded mode. Client-supplied values are capped at 200.
_DEFAULT_MAX_DEPTH: int | None = None
_MAX_MAX_DEPTH = 200


# ── Timeout helpers (mirror app/api/usage.py) ────────────────────────────────


@contextlib.asynccontextmanager
async def _db_timeout(event_name: str, db_timeout_seconds: int) -> AsyncIterator[None]:
    """Wrap a database query with the configured per-query timeout budget."""
    async with timeout_operation(event_name, "db", budget_ms=db_timeout_seconds * 1000):
        yield


@contextlib.asynccontextmanager
async def _request_timeout(
    total_request_timeout_seconds: int,
) -> AsyncIterator[None]:
    """Wrap an endpoint handler body with the total request timeout budget."""
    async with timeout_operation(
        "request.total",
        "request",
        budget_ms=total_request_timeout_seconds * 1000,
    ):
        yield


# ── Keyset cursor helpers ─────────────────────────────────────────────────────

# (Moved to app.api.pagination — see NULL_CURSOR_SENTINEL, encode_cursor,
#  decode_cursor, next_cursor there. Imported at the top of this module.)


def _parse_datetime(raw: str | None, param_name: str) -> datetime | None:
    """Parse an ISO-8601 datetime query param, raising 400 on malformed values."""
    if raw is None:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {param_name}: {raw!r} is not a valid ISO-8601 datetime",
        ) from None


def _to_ms(dt: datetime | None) -> int | None:
    """Convert a datetime to a millisecond epoch for source_created_at filters."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _validate_window(start: datetime | None, end: datetime | None) -> None:
    """Raise 400 when an (inverted) window start is after its end."""
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must not be after 'to'",
        )


# ── Row builders ──────────────────────────────────────────────────────────────


def _message_row(row: asyncpg.Record) -> ObservedMessage:
    return ObservedMessage(
        id=row["id"],
        external_message_id=row["external_message_id"],
        external_session_id=row["external_session_id"],
        session_id=row["session_id"],
        parent_external_session_id=row["parent_external_session_id"],
        role=row["role"],
        agent=row["agent"],
        mode=row["mode"],
        cost_usd=row["cost_usd"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        source_created_at=row["source_created_at"],
        source_updated_at=row["source_updated_at"],
        source_created_at_tz=row["source_created_at_tz"],
        source_updated_at_tz=row["source_updated_at_tz"],
        data=json.loads(row["data"]) if row["data"] is not None else None,
    )


def _part_row(row: asyncpg.Record) -> ObservedPart:
    return ObservedPart(
        id=row["id"],
        external_part_id=row["external_part_id"],
        external_message_id=row["external_message_id"],
        external_session_id=row["external_session_id"],
        message_id=row["message_id"],
        session_id=row["session_id"],
        part_type=row["part_type"],
        source_created_at=row["source_created_at"],
        source_updated_at=row["source_updated_at"],
        source_created_at_tz=row["source_created_at_tz"],
        source_updated_at_tz=row["source_updated_at_tz"],
        data=json.loads(row["data"]) if row["data"] is not None else None,
    )


def _tool_call_row(row: asyncpg.Record) -> ObservedToolCall:
    return ObservedToolCall(
        id=row["id"],
        external_part_id=row["external_part_id"],
        external_session_id=row["external_session_id"],
        part_id=row["part_id"],
        message_id=row["message_id"],
        session_id=row["session_id"],
        tool_name=row["tool_name"],
        tool_status=row["tool_status"],
        tool_input=json.loads(row["tool_input"]) if row["tool_input"] is not None else None,
        tool_output=json.loads(row["tool_output"]) if row["tool_output"] is not None else None,
        source_created_at=row["source_created_at"],
        source_created_at_tz=row["source_created_at_tz"],
    )


def _timeline_row(row: asyncpg.Record) -> TimelineEvent:
    return TimelineEvent(
        part_id=row["part_id"],
        session_id=row["session_id"],
        external_session_id=row["external_session_id"],
        agent=row["agent"],
        depth=row["depth"],
        part_type=row["part_type"],
        source_created_at=row["source_created_at"],
        source_created_at_tz=row["source_created_at_tz"],
        data=json.loads(row["data"]) if row["data"] is not None else None,
    )


# ── Fetch helpers ─────────────────────────────────────────────────────────────


async def _fetch_session_header(
    conn: asyncpg.Connection, session_id: UUID, db_timeout_seconds: int
) -> SessionHeader | None:
    """Fetch the transcript session header, or None when the session is absent."""
    async with _db_timeout("execution.header", db_timeout_seconds):
        session = await conn.fetchrow(
            "SELECT id, external_session_id, agent, parent_session_id FROM sessions WHERE id = $1",
            session_id,
        )
    if session is None:
        return None

    external_session_id = session["external_session_id"]

    async with _db_timeout("execution.header.linkage", db_timeout_seconds):
        parent_row = await conn.fetchrow(
            "SELECT parent_session_id FROM opencode_session_contexts WHERE session_id = $1 LIMIT 1",
            session_id,
        )
        child_rows = await conn.fetch(
            "SELECT id, external_session_id, agent FROM sessions WHERE parent_session_id = $1",
            external_session_id,
        )

    async with _db_timeout("execution.header.counts", db_timeout_seconds):
        message_count = await conn.fetchval(
            "SELECT COUNT(*) FROM observed_messages WHERE session_id = $1",
            session_id,
        )
        part_count = await conn.fetchval(
            "SELECT COUNT(*) FROM observed_parts WHERE session_id = $1",
            session_id,
        )
        tool_call_count = await conn.fetchval(
            "SELECT COUNT(*) FROM observed_tool_calls WHERE session_id = $1",
            session_id,
        )
        window = await conn.fetchrow(
            "SELECT MIN(source_created_at_tz) AS first_part_at, "
            "       MAX(source_created_at_tz) AS last_part_at "
            "FROM observed_parts WHERE session_id = $1",
            session_id,
        )

    return SessionHeader(
        id=session["id"],
        external_session_id=external_session_id,
        agent=session["agent"],
        parent_session_id=session["parent_session_id"],
        parent_internal_id=parent_row["parent_session_id"] if parent_row else None,
        child_session_ids=[r["id"] for r in child_rows],
        message_count=message_count or 0,
        part_count=part_count or 0,
        tool_call_count=tool_call_count or 0,
        first_part_at=window["first_part_at"] if window else None,
        last_part_at=window["last_part_at"] if window else None,
    )


async def _fetch_children(
    conn: asyncpg.Connection,
    session_id: UUID,
    limit: int,
    offset: int,
    db_timeout_seconds: int,
) -> PaginatedResponse[ChildSession]:
    """Fetch direct child sessions of a transcript session."""
    async with _db_timeout("execution.children", db_timeout_seconds):
        session = await conn.fetchrow(
            "SELECT external_session_id FROM sessions WHERE id = $1", session_id
        )
        if session is None:
            return PaginatedResponse(items=[], total=0, limit=limit, offset=offset)
        external_session_id = session["external_session_id"]
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM sessions WHERE parent_session_id = $1",
            external_session_id,
        )
        rows = await conn.fetch(
            "SELECT id, external_session_id, agent FROM sessions "
            "WHERE parent_session_id = $1 "
            "ORDER BY first_message_at ASC, id ASC "
            "LIMIT $2 OFFSET $3",
            external_session_id,
            limit,
            offset,
        )
    items = [
        ChildSession(id=r["id"], external_session_id=r["external_session_id"], agent=r["agent"])
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def _fetch_messages(
    conn: asyncpg.Connection,
    session_id: UUID,
    agent: str | None,
    role: str | None,
    from_ms: int | None,
    to_ms: int | None,
    limit: int,
    after_ms: int | None,
    after_id: UUID | None,
    db_timeout_seconds: int,
) -> CursorPage[ObservedMessage]:
    """Fetch a keyset-paginated, chronologically-ordered message stream."""
    params: list[object] = [session_id, NULL_CURSOR_SENTINEL]
    null_sentinel_param = 2
    where: list[str] = ["session_id = $1"]

    if agent is not None:
        params.append(agent)
        where.append(f"agent = ${len(params)}")
    if role is not None:
        params.append(role)
        where.append(f"role = ${len(params)}")
    if from_ms is not None:
        params.append(from_ms)
        where.append(f"source_created_at >= ${len(params)}")
    if to_ms is not None:
        params.append(to_ms)
        where.append(f"source_created_at <= ${len(params)}")
    if after_ms is not None and after_id is not None:
        params.extend([after_ms, after_id])
        where.append(
            f"(COALESCE(source_created_at, ${null_sentinel_param}), id) > "
            f"(${len(params) - 1}, ${len(params)})"
        )

    params.append(limit + 1)
    query = (
        "SELECT id, external_message_id, external_session_id, session_id, "
        "       parent_external_session_id, role, agent, mode, cost_usd, "
        "       input_tokens, output_tokens, source_created_at, source_updated_at, "
        "       source_created_at_tz, source_updated_at_tz, data "
        "FROM observed_messages "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(source_created_at, ${null_sentinel_param}) ASC, id ASC "
        f"LIMIT ${len(params)}"
    )
    async with _db_timeout("execution.messages", db_timeout_seconds):
        rows = await conn.fetch(query, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if has_more and rows:
        cursor = next_cursor(rows[-1]["source_created_at"], str(rows[-1]["id"]))
    return CursorPage(
        items=[_message_row(r) for r in rows],
        next_cursor=cursor,
        has_more=has_more,
    )


async def _fetch_parts(
    conn: asyncpg.Connection,
    session_id: UUID,
    part_type: str | None,
    tool_name: str | None,
    from_ms: int | None,
    to_ms: int | None,
    limit: int,
    after_ms: int | None,
    after_id: UUID | None,
    db_timeout_seconds: int,
) -> CursorPage[ObservedPart]:
    """Fetch a keyset-paginated, chronologically-ordered part event stream."""
    params: list[object] = [session_id, NULL_CURSOR_SENTINEL]
    null_sentinel_param = 2
    where: list[str] = ["session_id = $1"]

    if part_type is not None:
        params.append(part_type)
        where.append(f"part_type = ${len(params)}")
    if tool_name is not None:
        params.append(tool_name)
        where.append(f"data->>'tool' = ${len(params)}")
    if from_ms is not None:
        params.append(from_ms)
        where.append(f"source_created_at >= ${len(params)}")
    if to_ms is not None:
        params.append(to_ms)
        where.append(f"source_created_at <= ${len(params)}")
    if after_ms is not None and after_id is not None:
        params.extend([after_ms, after_id])
        where.append(
            f"(COALESCE(source_created_at, ${null_sentinel_param}), id) > "
            f"(${len(params) - 1}, ${len(params)})"
        )

    params.append(limit + 1)
    query = (
        "SELECT id, external_part_id, external_message_id, external_session_id, "
        "       message_id, session_id, part_type, source_created_at, source_updated_at, "
        "       source_created_at_tz, source_updated_at_tz, data "
        "FROM observed_parts "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(source_created_at, ${null_sentinel_param}) ASC, id ASC "
        f"LIMIT ${len(params)}"
    )
    async with _db_timeout("execution.parts", db_timeout_seconds):
        rows = await conn.fetch(query, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if has_more and rows:
        cursor = next_cursor(rows[-1]["source_created_at"], str(rows[-1]["id"]))
    return CursorPage(
        items=[_part_row(r) for r in rows],
        next_cursor=cursor,
        has_more=has_more,
    )


async def _fetch_tool_calls(
    conn: asyncpg.Connection,
    session_id: UUID | None,
    agent: str | None,
    tool_name: str | None,
    tool_status: str | None,
    from_ms: int | None,
    to_ms: int | None,
    limit: int,
    after_ms: int | None,
    after_id: UUID | None,
    db_timeout_seconds: int,
) -> CursorPage[ObservedToolCall]:
    """Fetch a keyset-paginated, global tool-call stream."""
    params: list[object] = [NULL_CURSOR_SENTINEL]
    null_sentinel_param = 1
    where: list[str] = ["TRUE"]

    if session_id is not None:
        params.append(session_id)
        where.append(f"tc.session_id = ${len(params)}")
    if agent is not None:
        params.append(agent)
        where.append(f"s.agent = ${len(params)}")
    if tool_name is not None:
        params.append(tool_name)
        where.append(f"tc.tool_name = ${len(params)}")
    if tool_status is not None:
        params.append(tool_status)
        where.append(f"tc.tool_status = ${len(params)}")
    if from_ms is not None:
        params.append(from_ms)
        where.append(f"tc.source_created_at >= ${len(params)}")
    if to_ms is not None:
        params.append(to_ms)
        where.append(f"tc.source_created_at <= ${len(params)}")
    if after_ms is not None and after_id is not None:
        params.extend([after_ms, after_id])
        where.append(
            f"(COALESCE(tc.source_created_at, ${null_sentinel_param}), tc.id) > "
            f"(${len(params) - 1}, ${len(params)})"
        )

    params.append(limit + 1)
    query = (
        "SELECT tc.id, tc.external_part_id, tc.external_session_id, tc.part_id, "
        "       tc.message_id, tc.session_id, tc.tool_name, tc.tool_status, "
        "       tc.tool_input, tc.tool_output, tc.source_created_at, "
        "       tc.source_created_at_tz "
        "FROM observed_tool_calls tc "
        "LEFT JOIN sessions s ON s.id = tc.session_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(tc.source_created_at, ${null_sentinel_param}) ASC, tc.id ASC "
        f"LIMIT ${len(params)}"
    )
    async with _db_timeout("execution.tool_calls", db_timeout_seconds):
        rows = await conn.fetch(query, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if has_more and rows:
        cursor = next_cursor(rows[-1]["source_created_at"], str(rows[-1]["id"]))
    return CursorPage(
        items=[_tool_call_row(r) for r in rows],
        next_cursor=cursor,
        has_more=has_more,
    )


async def _fetch_timeline(
    conn: asyncpg.Connection,
    session_id: UUID,
    agent: str | None,
    max_depth: int | None,
    from_ms: int | None,
    to_ms: int | None,
    limit: int,
    after_ms: int | None,
    after_id: UUID | None,
    db_timeout_seconds: int,
) -> CursorPage[TimelineEvent]:
    """Fetch a unified parent+descendant timeline via a recursive CTE.

    Parentage is resolved from ``opencode_session_contexts.parent_session_id``
    with a fallback to ``observed_messages.parent_external_session_id`` (the
    out-of-order context-projection case, ADR 0016 §1), normalized into a
    single ``edges`` CTE so sessions linked via both sources are traversed
    once (the ``UNION`` collapses the duplicate edge).  The recursive step
    carries a visited ``path`` array so a parent-linkage cycle cannot recurse
    forever, and the outer query deduplicates each part by id
    (``DISTINCT ON (p.id)``) preferring the shallowest depth.  ``max_depth``
    of ``None`` walks all generations (bounded only by the cycle guard); an
    explicit value bounds recursion at that depth.
    """
    params: list[object] = [session_id, max_depth, NULL_CURSOR_SENTINEL]
    null_sentinel_param = 3
    where: list[str] = ["TRUE"]

    if agent is not None:
        params.append(agent)
        where.append(f"agent = ${len(params)}")
    if from_ms is not None:
        params.append(from_ms)
        where.append(f"source_created_at >= ${len(params)}")
    if to_ms is not None:
        params.append(to_ms)
        where.append(f"source_created_at <= ${len(params)}")
    if after_ms is not None and after_id is not None:
        params.extend([after_ms, after_id])
        where.append(
            f"(COALESCE(source_created_at, ${null_sentinel_param}), part_id) > "
            f"(${len(params) - 1}, ${len(params)})"
        )

    params.append(limit + 1)
    query = (
        "WITH RECURSIVE edges AS ("
        "  SELECT ctx.session_id AS child_id, ctx.parent_session_id AS parent_id"
        "  FROM opencode_session_contexts ctx"
        "  WHERE ctx.parent_session_id IS NOT NULL"
        "  UNION"
        "  SELECT m.session_id AS child_id, s2.id AS parent_id"
        "  FROM observed_messages m"
        "  JOIN sessions s2"
        "    ON s2.external_session_id = m.parent_external_session_id"
        "   AND s2.source_database_id = m.source_database_id"
        "  WHERE m.parent_external_session_id IS NOT NULL AND m.session_id IS NOT NULL"
        "), descendants AS ("
        "  SELECT id AS session_id, external_session_id, agent, 0 AS depth, ARRAY[id] AS path"
        "  FROM sessions WHERE id = $1"
        "  UNION ALL"
        "  SELECT s.id, s.external_session_id, s.agent, d.depth + 1, d.path || s.id"
        "  FROM edges e"
        "  JOIN descendants d ON e.parent_id = d.session_id"
        "  JOIN sessions s ON s.id = e.child_id"
        "  WHERE (d.depth < $2 OR $2 IS NULL) AND NOT (s.id = ANY(d.path))"
        "), dedup AS ("
        "  SELECT DISTINCT ON (p.id) "
        "         p.id AS part_id, d.session_id, d.agent, d.depth, "
        "         p.external_session_id, p.part_type, p.source_created_at, "
        "         p.source_created_at_tz, p.data "
        "  FROM descendants d "
        "  JOIN observed_parts p ON p.session_id = d.session_id "
        "  ORDER BY p.id, d.depth"
        ") "
        "SELECT * FROM dedup "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(source_created_at, ${null_sentinel_param}) ASC, part_id ASC "
        f"LIMIT ${len(params)}"
    )
    async with _db_timeout("execution.timeline", db_timeout_seconds):
        rows = await conn.fetch(query, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if has_more and rows:
        cursor = next_cursor(rows[-1]["source_created_at"], str(rows[-1]["part_id"]))
    return CursorPage(
        items=[_timeline_row(r) for r in rows],
        next_cursor=cursor,
        has_more=has_more,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/sessions/{session_id}")
async def get_session_header(
    request: Request,
    session_id: UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> SessionHeader:
    """Return a transcript session's identity, linkage, counts, and time window."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        header = await _fetch_session_header(
            conn, session_id, db_timeout_seconds=settings.database_timeout_seconds
        )
    if header is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )
    return header


@router.get("/sessions/{session_id}/children")
async def get_session_children(
    request: Request,
    session_id: UUID,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ChildSession]:
    """Return the direct child subagent sessions of a transcript session."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_children(
            conn,
            session_id,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    request: Request,
    session_id: UUID,
    agent: str | None = Query(default=None),
    role: str | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    after: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    conn: asyncpg.Connection = Depends(get_session),
) -> CursorPage[ObservedMessage]:
    """Return a session's messages, chronologically, keyset-paginated."""
    from_dt = _parse_datetime(from_ts, "from")
    to_dt = _parse_datetime(to_ts, "to")
    _validate_window(from_dt, to_dt)
    after_ms, after_id = (None, None)
    if after is not None:
        after_ms, after_id = decode_cursor(after)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_messages(
            conn,
            session_id,
            agent,
            role,
            _to_ms(from_dt),
            _to_ms(to_dt),
            limit,
            after_ms,
            after_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/sessions/{session_id}/parts")
async def get_session_parts(
    request: Request,
    session_id: UUID,
    part_type: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    after: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    conn: asyncpg.Connection = Depends(get_session),
) -> CursorPage[ObservedPart]:
    """Return a session's part events, chronologically, keyset-paginated."""
    from_dt = _parse_datetime(from_ts, "from")
    to_dt = _parse_datetime(to_ts, "to")
    _validate_window(from_dt, to_dt)
    after_ms, after_id = (None, None)
    if after is not None:
        after_ms, after_id = decode_cursor(after)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_parts(
            conn,
            session_id,
            part_type,
            tool_name,
            _to_ms(from_dt),
            _to_ms(to_dt),
            limit,
            after_ms,
            after_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/sessions/{session_id}/timeline")
async def get_session_timeline(
    request: Request,
    session_id: UUID,
    agent: str | None = Query(default=None),
    max_depth: int | None = Query(default=_DEFAULT_MAX_DEPTH, ge=0, le=_MAX_MAX_DEPTH),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    after: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    conn: asyncpg.Connection = Depends(get_session),
) -> CursorPage[TimelineEvent]:
    """Return a unified timeline across a session and its descendants."""
    from_dt = _parse_datetime(from_ts, "from")
    to_dt = _parse_datetime(to_ts, "to")
    _validate_window(from_dt, to_dt)
    after_ms, after_id = (None, None)
    if after is not None:
        after_ms, after_id = decode_cursor(after)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_timeline(
            conn,
            session_id,
            agent,
            max_depth,
            _to_ms(from_dt),
            _to_ms(to_dt),
            limit,
            after_ms,
            after_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/tool-calls")
async def get_tool_calls(
    request: Request,
    session_id: UUID | None = Query(default=None),
    agent: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    tool_status: str | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    after: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    conn: asyncpg.Connection = Depends(get_session),
) -> CursorPage[ObservedToolCall]:
    """Return a global tool-call stream, queryable separately from usage aggregates."""
    from_dt = _parse_datetime(from_ts, "from")
    to_dt = _parse_datetime(to_ts, "to")
    _validate_window(from_dt, to_dt)
    after_ms, after_id = (None, None)
    if after is not None:
        after_ms, after_id = decode_cursor(after)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_tool_calls(
            conn,
            session_id,
            agent,
            tool_name,
            tool_status,
            _to_ms(from_dt),
            _to_ms(to_dt),
            limit,
            after_ms,
            after_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
