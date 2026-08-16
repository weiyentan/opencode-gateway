"""AFK outcomes read-only REST API (issue #452).

Four GET endpoints under the versioned namespace expose the AFK outcome
read-model stored by ``afk_outcomes.repository.AsyncpgOutcomeRepository``:

- ``GET /runs``        — list runs, filterable by repository, window
  (started/finished/seen bounds), status, outcome, and origin; paginated.
- ``GET /runs/{id}``   — the full chain for one run (aggregate, outcome,
  engineering entities grouped by type, sessions, agents, usage/cost).
- ``GET /entities``    — engineering entities with their run links, correlation
  provenance, and superseded state.
- ``GET /correlations`` — unresolved correlations with method/confidence/
  evidence/resolver_version and provisional markers.

All responses use the existing ``{status, data, error}`` envelope and are
protected by the global :class:`~app.core.auth.ApiKeyMiddleware`.  This router
is strictly read-only — backfill remains CLI-only (#449).

The read path maps 1:1 to stored columns and follows the ``app/api/usage.py``
convention: raw asyncpg via ``Depends(get_session)``, explicit-column SELECTs,
parameterised filters with 400 on invalid enum/date values, and the
``_db_timeout``/``_request_timeout`` helpers.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from afk_outcomes.models import (
    CorrelationEvidence,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    Provider,
    RunStatus,
    UnresolvedReason,
)
from app.core.config import get_settings
from app.core.schemas.afk import (
    EntityLink,
    EntityRow,
    RunDetail,
    RunSummary,
    SessionLink,
    UnresolvedCorrelationRow,
    UsageAggregate,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

router = APIRouter(tags=["afk-outcomes"])

# ── Valid enum filter values (locked domain vocabulary) ──────────────────────

_VALID_STATUS = frozenset(m.value for m in RunStatus)
_VALID_OUTCOME = frozenset(m.value for m in EngineeringOutcomeStatus)
_VALID_ORIGIN = frozenset(m.value for m in Provider)
_VALID_REASON = frozenset(m.value for m in UnresolvedReason)

# Entity-type → detail response field name grouping.
_ENTITY_TYPE_FIELDS = {
    "issue": "issues",
    "change_request": "change_requests",
    "review": "reviews",
    "commit": "commits",
    "merge_event": "merge_events",
}

# A derived link is provisional (inferred) when its role is not a definitive
# "resolved" — i.e. "referenced" (sub-threshold confidence) or "noise".
_RESOLVED_ROLE = "resolved"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_datetime(raw: str | None, param_name: str) -> datetime | None:
    """Parse an ISO-8601 datetime query param, raising 400 on malformed values.

    Handles the ``Z`` suffix (Python 3.9's ``datetime.fromisoformat`` does not)
    by normalising it to ``+00:00``.
    """
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


def _require_enum_value(raw: str | None, valid: frozenset[str], param_name: str) -> None:
    """Raise 400 when *raw* is not a member of *valid*."""
    if raw is not None and raw not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid {param_name}: {raw!r}. "
                f"Valid values: {', '.join(sorted(valid))}"
            ),
        )


def _validate_window(
    start: datetime | None, end: datetime | None, param_name: str
) -> None:
    """Raise 400 when an (inverted) window start is after its end."""
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} must not be after {param_name.replace('from', 'to')}",
        )


def _parse_evidence(raw: object) -> list[CorrelationEvidence]:
    """Parse a JSONB evidence array into :class:`CorrelationEvidence` items."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    return [CorrelationEvidence.model_validate(item) for item in raw]


def _build_run_filters(
    repository: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    finished_from: datetime | None,
    finished_to: datetime | None,
    seen_from: datetime | None,
    seen_to: datetime | None,
    status_filter: str | None,
    outcome_filter: str | None,
    origin_filter: str | None,
) -> tuple[str, list[object]]:
    """Build a WHERE clause and parameter list for the list-runs query."""
    params: list[object] = []
    filters: list[str] = ["TRUE"]

    if repository is not None:
        filters.append(
            "EXISTS (SELECT 1 FROM afk_run_entities re "
            f"WHERE re.afk_run_id = r.afk_run_id AND re.repository = ${len(params) + 1})"
        )
        params.append(repository)

    for column, value, op in (
        ("started_at", started_from, ">="),
        ("started_at", started_to, "<="),
        ("finished_at", finished_from, ">="),
        ("finished_at", finished_to, "<="),
        ("last_seen_at", seen_from, ">="),
        ("last_seen_at", seen_to, "<="),
    ):
        if value is None:
            continue
        filters.append(f"r.{column} {op} ${len(params) + 1}")
        params.append(value)

    if status_filter is not None:
        filters.append(f"r.status = ${len(params) + 1}")
        params.append(status_filter)
    if outcome_filter is not None:
        filters.append(f"r.outcome_status = ${len(params) + 1}")
        params.append(outcome_filter)
    if origin_filter is not None:
        filters.append(f"r.provider = ${len(params) + 1}")
        params.append(origin_filter)

    return " AND ".join(filters), params


def _entity_link(row: asyncpg.Record) -> EntityLink:
    """Build an :class:`EntityLink` from an ``afk_run_entities`` row."""
    role = row["role"]
    return EntityLink(
        entity_id=f"{row['entity_type']}:{row['external_id']}",
        entity_type=row["entity_type"],
        external_id=row["external_id"],
        provider=row["provider"],
        repository=row["repository"],
        role=role,
        correlation_method=row["correlation_method"],
        correlation_confidence=row["correlation_confidence"],
        evidence=_parse_evidence(row["evidence"]),
        resolver_version=row["resolver_version"],
        owning_change_request_id=row["owning_change_request_id"],
        correlation_source=row["correlation_source"],
        provisional=role != _RESOLVED_ROLE,
    )


# ── Query helpers ────────────────────────────────────────────────────────────


async def _fetch_runs(
    conn: asyncpg.Connection,
    repository: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    finished_from: datetime | None,
    finished_to: datetime | None,
    seen_from: datetime | None,
    seen_to: datetime | None,
    status_filter: str | None,
    outcome_filter: str | None,
    origin_filter: str | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[RunSummary]:
    """Execute count + data queries for the list-runs endpoint."""
    where_clause, params = _build_run_filters(
        repository,
        started_from,
        started_to,
        finished_from,
        finished_to,
        seen_from,
        seen_to,
        status_filter,
        outcome_filter,
        origin_filter,
    )

    count_sql = f"SELECT COUNT(*) FROM afk_runs r WHERE {where_clause}"
    async with timed_operation("db.query.afk.runs.count", "db"):
        async with _db_timeout("db.query.afk.runs.count", db_timeout_seconds):
            total = await conn.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT r.afk_run_id, r.provider, r.status, r.title, r.started_at,
               r.finished_at, r.outcome_status, r.first_seen_at, r.last_seen_at
        FROM afk_runs r
        WHERE {where_clause}
        ORDER BY r.last_seen_at DESC
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.afk.runs.data", "db"):
        async with _db_timeout("db.query.afk.runs.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [
        RunSummary(
            afk_run_id=r["afk_run_id"],
            provider=r["provider"],
            status=r["status"],
            title=r["title"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            outcome_status=r["outcome_status"],
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_run_detail(
    conn: asyncpg.Connection,
    afk_run_id: str,
    *,
    db_timeout_seconds: int,
) -> RunDetail | None:
    """Compose the full chain for one run (3 queries, no N+1)."""
    async with timed_operation("db.query.afk.run.detail.run", "db"):
        async with _db_timeout("db.query.afk.run.detail.run", db_timeout_seconds):
            run_row = await conn.fetchrow(
                """
                SELECT afk_run_id, provider, status, title, started_at, finished_at,
                       outcome_status, outcome, first_seen_at, last_seen_at
                FROM afk_runs
                WHERE afk_run_id = $1
                """,
                afk_run_id,
            )
    if run_row is None:
        return None

    async with timed_operation("db.query.afk.run.detail.entities", "db"):
        async with _db_timeout("db.query.afk.run.detail.entities", db_timeout_seconds):
            entity_rows = await conn.fetch(
                """
                SELECT provider, repository, entity_type, external_id, role,
                       correlation_method, correlation_confidence, evidence,
                       resolver_version, owning_change_request_id,
                       correlation_source
                FROM afk_run_entities
                WHERE afk_run_id = $1 AND superseded_at IS NULL
                ORDER BY entity_type, external_id
                """,
                afk_run_id,
            )

    async with timed_operation("db.query.afk.run.detail.sessions", "db"):
        async with _db_timeout("db.query.afk.run.detail.sessions", db_timeout_seconds):
            session_rows = await conn.fetch(
                """
                SELECT ars.session_id, ars.external_session_id, ars.started_at,
                       ars.finished_at, s.agent, s.total_input_tokens,
                       s.total_output_tokens, s.total_cache_read_tokens,
                       s.total_cache_write_tokens, s.total_estimated_cost_usd,
                       s.message_count
                FROM afk_run_sessions ars
                LEFT JOIN sessions s ON s.id = ars.session_id
                WHERE ars.afk_run_id = $1
                ORDER BY ars.started_at NULLS LAST
                """,
                afk_run_id,
            )

    detail = RunDetail(
        run=RunSummary(
            afk_run_id=run_row["afk_run_id"],
            provider=run_row["provider"],
            status=run_row["status"],
            title=run_row["title"],
            started_at=run_row["started_at"],
            finished_at=run_row["finished_at"],
            outcome_status=run_row["outcome_status"],
            first_seen_at=run_row["first_seen_at"],
            last_seen_at=run_row["last_seen_at"],
        ),
        outcome=(
            EngineeringOutcome.model_validate(run_row["outcome"])
            if run_row["outcome"] is not None
            else None
        ),
    )

    grouped: dict[str, list[EntityLink]] = {}
    for row in entity_rows:
        link = _entity_link(row)
        field = _ENTITY_TYPE_FIELDS.get(row["entity_type"])
        if field is None:
            continue
        grouped.setdefault(field, []).append(link)
    detail.issues = grouped.get("issues", [])
    detail.change_requests = grouped.get("change_requests", [])
    detail.reviews = grouped.get("reviews", [])
    detail.commits = grouped.get("commits", [])
    detail.merge_events = grouped.get("merge_events", [])

    sessions: list[SessionLink] = []
    agents: set[str] = set()
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "message_count": 0,
        "session_count": 0,
    }
    cost: Decimal | None = None
    for row in session_rows:
        sessions.append(
            SessionLink(
                session_id=str(row["session_id"]) if row["session_id"] else None,
                external_session_id=row["external_session_id"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                inferred=True,
                agent=row["agent"],
                message_count=row["message_count"] or 0,
                total_input_tokens=row["total_input_tokens"] or 0,
                total_output_tokens=row["total_output_tokens"] or 0,
                total_cache_read_tokens=row["total_cache_read_tokens"] or 0,
                total_cache_write_tokens=row["total_cache_write_tokens"] or 0,
                total_estimated_cost_usd=row["total_estimated_cost_usd"],
            )
        )
        if row["agent"]:
            agents.add(row["agent"])
        totals["input"] += row["total_input_tokens"] or 0
        totals["output"] += row["total_output_tokens"] or 0
        totals["cache_read"] += row["total_cache_read_tokens"] or 0
        totals["cache_write"] += row["total_cache_write_tokens"] or 0
        totals["message_count"] += row["message_count"] or 0
        totals["session_count"] += 1
        if row["total_estimated_cost_usd"] is not None:
            cost = (cost or Decimal("0")) + row["total_estimated_cost_usd"]

    detail.sessions = sessions
    detail.agents = sorted(agents)
    detail.usage = UsageAggregate(
        active_tokens=totals["input"] + totals["output"],
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        estimated_cost_usd=cost,
        message_count=totals["message_count"],
        session_count=totals["session_count"],
    )
    return detail


async def _fetch_entities(
    conn: asyncpg.Connection,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[EntityRow]:
    """Execute count + data queries for the list-entities endpoint."""
    async with timed_operation("db.query.afk.entities.count", "db"):
        async with _db_timeout("db.query.afk.entities.count", db_timeout_seconds):
            total = await conn.fetchval("SELECT COUNT(*) FROM afk_run_entities")

    data_sql = """
        SELECT afk_run_id, provider, repository, entity_type, external_id, role,
               correlation_method, correlation_confidence, evidence,
               resolver_version, owning_change_request_id, correlation_source,
               superseded_at
        FROM afk_run_entities
        ORDER BY entity_type, external_id, afk_run_id
        LIMIT $1 OFFSET $2
    """
    async with timed_operation("db.query.afk.entities.data", "db"):
        async with _db_timeout("db.query.afk.entities.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, limit, offset)

    items = [
        EntityRow(
            entity_id=f"{r['entity_type']}:{r['external_id']}",
            entity_type=r["entity_type"],
            external_id=r["external_id"],
            provider=r["provider"],
            repository=r["repository"],
            afk_run_id=r["afk_run_id"],
            role=r["role"],
            correlation_method=r["correlation_method"],
            correlation_confidence=r["correlation_confidence"],
            evidence=_parse_evidence(r["evidence"]),
            resolver_version=r["resolver_version"],
            owning_change_request_id=r["owning_change_request_id"],
            correlation_source=r["correlation_source"],
            superseded_at=r["superseded_at"],
            provisional=r["role"] != _RESOLVED_ROLE,
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_correlations(
    conn: asyncpg.Connection,
    limit: int,
    offset: int,
    *,
    reason: str | None,
    db_timeout_seconds: int,
) -> PaginatedResponse[UnresolvedCorrelationRow]:
    """Execute count + data queries for the list-correlations endpoint."""
    params: list[object] = []
    where = "TRUE"
    if reason is not None:
        where = f"reason = ${len(params) + 1}"
        params.append(reason)

    async with timed_operation("db.query.afk.correlations.count", "db"):
        async with _db_timeout("db.query.afk.correlations.count", db_timeout_seconds):
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM unresolved_correlations WHERE {where}", *params
            )

    data_sql = f"""
        SELECT provider, repository, entity_type, external_id, afk_run_id, method,
               reason, correlation_confidence, candidates, evidence, resolver_version,
               created_at
        FROM unresolved_correlations
        WHERE {where}
        ORDER BY created_at DESC, entity_type, external_id
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.afk.correlations.data", "db"):
        async with _db_timeout("db.query.afk.correlations.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [
        UnresolvedCorrelationRow(
            entity_id=f"{r['entity_type']}:{r['external_id']}",
            entity_type=r["entity_type"],
            external_id=r["external_id"],
            provider=r["provider"],
            repository=r["repository"],
            afk_run_id=r["afk_run_id"],
            method=r["method"],
            reason=r["reason"],
            correlation_confidence=r["correlation_confidence"],
            candidates=r["candidates"] or [],
            evidence=_parse_evidence(r["evidence"]),
            resolver_version=r["resolver_version"],
            created_at=r["created_at"],
            provisional=True,
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/runs")
async def list_runs(
    request: Request,
    repository: str | None = Query(default=None),
    started_from: str | None = Query(default=None),
    started_to: str | None = Query(default=None),
    finished_from: str | None = Query(default=None),
    finished_to: str | None = Query(default=None),
    seen_from: str | None = Query(default=None),
    seen_to: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    outcome: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[RunSummary]:
    """List AFK runs, filterable by repository, window, status, outcome, origin."""
    _require_enum_value(status_filter, _VALID_STATUS, "status")
    _require_enum_value(outcome, _VALID_OUTCOME, "outcome")
    _require_enum_value(origin, _VALID_ORIGIN, "origin")

    started_from_dt = _parse_datetime(started_from, "started_from")
    started_to_dt = _parse_datetime(started_to, "started_to")
    finished_from_dt = _parse_datetime(finished_from, "finished_from")
    finished_to_dt = _parse_datetime(finished_to, "finished_to")
    seen_from_dt = _parse_datetime(seen_from, "seen_from")
    seen_to_dt = _parse_datetime(seen_to, "seen_to")

    _validate_window(started_from_dt, started_to_dt, "started_from")
    _validate_window(finished_from_dt, finished_to_dt, "finished_from")
    _validate_window(seen_from_dt, seen_to_dt, "seen_from")

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_runs(
            conn,
            repository,
            started_from_dt,
            started_to_dt,
            finished_from_dt,
            finished_to_dt,
            seen_from_dt,
            seen_to_dt,
            status_filter,
            outcome,
            origin,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/runs/{afk_run_id}")
async def get_run_detail(
    request: Request,
    afk_run_id: str,
    conn: asyncpg.Connection = Depends(get_session),
) -> RunDetail:
    """Return the full chain for one AFK run, with per-link provenance."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        detail = await _fetch_run_detail(
            conn,
            afk_run_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AFK run not found: {afk_run_id}",
        )
    return detail


@router.get("/entities")
async def list_entities(
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[EntityRow]:
    """List engineering entities with their run links, provenance, superseded state."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_entities(
            conn,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/correlations")
async def list_correlations(
    request: Request,
    reason: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[UnresolvedCorrelationRow]:
    """List unresolved correlations (low-confidence links + ambiguous/unmatched)."""
    _require_enum_value(reason, _VALID_REASON, "reason")
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_correlations(
            conn,
            limit,
            offset,
            reason=reason,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
