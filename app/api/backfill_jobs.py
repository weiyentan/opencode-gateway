"""API-triggered AFK backfill endpoints.

- ``POST /jobs``               — submit a bounded backfill for one provider/
  repository.  Write jobs (``dry_run: false``) are queued durably and return
  ``202`` + job id; dry-runs (the default) execute synchronously and return
  the report without writing outcome rows.
- ``GET /jobs``                — list submitted jobs (paginated, optional
  status filter).
- ``GET /jobs/{job_id}``       — job status: queued, running, completed,
  failed, or cancelled, with audit metadata and (on completion) the reused
  backfill report counters.
- ``POST /jobs/{job_id}/cancel`` — cancel a queued job.  Running jobs are
  never forcibly interrupted in v1; cancelling anything but a queued job
  returns 409.

Authentication: every route under this router requires the dedicated
``GATEWAY_BACKFILL_API_KEY`` supplied in the ``X-Backfill-Key`` header.
Configuring that key enables the endpoints; without it they return 503.
Provider credentials are never accepted in request data (``extra="forbid"``
rejects them) — tokens remain server-side environment secrets consumed by the
reused ``scripts.afk_backfill`` adapter wiring.

The dry-run path executes the same ``scripts.afk_backfill.run_backfill``
orchestration as the CLI (no forked correlation/persistence logic) and holds
the pooled connection for the duration of the provider fetches, so it is not
wrapped in the generic request-timeout budget.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.backfill.jobs import (
    ALL_STATUSES,
    BackfillJobStore,
    bounded_evidence,
)
from app.core.config import get_settings
from app.core.schemas.backfill import (
    BackfillJobResponse,
    BackfillReportResponse,
    BackfillRequest,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation
from app.db.session import get_session
from scripts.afk_backfill import BackfillReport, _build_adapter, run_backfill

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backfill"])

BACKFILL_KEY_HEADER = "X-Backfill-Key"


# ── Dedicated authentication ────────────────────────────────────────────────


async def require_backfill_key(request: Request) -> dict[str, str]:
    """FastAPI dependency — enforce the dedicated ``GATEWAY_BACKFILL_API_KEY``.

    * Key not configured → 503: the backfill endpoints are disabled until the
      operator sets ``GATEWAY_BACKFILL_API_KEY``.
    * Key configured → the ``X-Backfill-Key`` header must match
      (constant-time comparison) or the request is rejected with 401.

    Returns the configured key label for the job audit fields — the key value
    itself is never stored, logged, or returned.
    """
    settings = get_settings()
    if not settings.backfill_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The backfill API is disabled — set GATEWAY_BACKFILL_API_KEY "
                "to enable it."
            ),
        )
    provided = request.headers.get(BACKFILL_KEY_HEADER, "")
    if not provided or not hmac.compare_digest(provided, settings.backfill_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing backfill API key",
        )
    return {"key_label": settings.backfill_api_key_label}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _validate_max_window(body: BackfillRequest, max_window_days: int) -> None:
    """Reject windows longer than the configured maximum (400)."""
    window_days = (body.until - body.from_) / timedelta(days=1)
    if window_days > max_window_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Requested window ({window_days:.2f} days) exceeds the "
                f"maximum of {max_window_days} days"
            ),
        )


def _report_response(report: BackfillReport) -> BackfillReportResponse:
    """Re-serialize the reused :class:`BackfillReport` vocabulary unchanged."""
    return BackfillReportResponse(
        provider=report.provider.value,
        repository=report.repository,
        since=report.since,
        until=report.until,
        dry_run=report.dry_run,
        change_requests_scanned=report.change_requests_scanned,
        issues_scanned=report.issues_scanned,
        sessions_considered=report.sessions_considered,
        explicit_matches=report.explicit_matches,
        high_matches=report.high_matches,
        inferred_matches=report.inferred_matches,
        ambiguous=report.ambiguous,
        unmatched=report.unmatched,
        evidence_lines=report.evidence_lines,
    )


def _job_response(row: dict[str, Any]) -> BackfillJobResponse:
    """Map a stored job row onto the response model, attaching report counters."""
    job = BackfillJobResponse.from_row(row)
    if row["change_requests_scanned"] is not None:
        job.report = BackfillReportResponse(
            provider=row["provider"],
            repository=row["repository"],
            since=row["window_from"],
            until=row["window_until"],
            dry_run=row["dry_run"],
            change_requests_scanned=row["change_requests_scanned"],
            issues_scanned=row["issues_scanned"],
            sessions_considered=row["sessions_considered"],
            explicit_matches=row["explicit_matches"],
            high_matches=row["high_matches"],
            inferred_matches=row["inferred_matches"],
            ambiguous=row["ambiguous"],
            unmatched=row["unmatched"],
            evidence_lines=row["evidence"] or [],
        )
    return job


async def _execute_dry_run(
    conn: asyncpg.Connection, body: BackfillRequest
) -> BackfillReport:
    """Run the existing backfill orchestration synchronously (dry-run only)."""
    adapter, client = _build_adapter(body.provider)
    try:
        return await run_backfill(
            conn,
            adapter=adapter,
            repository=body.repository,
            since=body.from_,
            until=body.until,
            dry_run=True,
            show_evidence=body.show_evidence,
        )
    finally:
        await client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/jobs")
async def submit_backfill_job(
    body: BackfillRequest,
    request: Request,
    auth: dict[str, str] = Depends(require_backfill_key),
    conn: asyncpg.Connection = Depends(get_session),
) -> JSONResponse:
    """Submit a bounded backfill for one provider/repository.

    ``dry_run: true`` (default) executes synchronously and returns the full
    report; a completed job row records the audit metadata.  ``dry_run:
    false`` enqueues a durable job and returns ``202`` with its id.
    """
    settings = get_settings()
    _validate_max_window(body, settings.backfill_max_window_days)

    if body.dry_run:
        async with timed_operation("backfill.dry_run.run", "external"):
            report = await _execute_dry_run(conn, body)
        evidence = (
            bounded_evidence(
                report.evidence_lines,
                max_lines=settings.backfill_max_evidence_lines,
            )
            if body.show_evidence
            else None
        )
        async with timed_operation("db.query.backfill.insert", "db"):
            async with conn.transaction():
                row = await BackfillJobStore(conn).create(
                    provider=body.provider,
                    repository=body.repository,
                    window_from=body.from_,
                    window_until=body.until,
                    dry_run=True,
                    show_evidence=body.show_evidence,
                    requested_by=auth["key_label"],
                )
                completed = await BackfillJobStore(conn).complete(
                    row["id"], report=report, evidence=evidence
                )
        logger.info(
            "backfill.dry_run.completed",
            extra={
                "job_id": str(completed["id"]),
                "provider": completed["provider"],
                "repository": completed["repository"],
                "change_requests_scanned": report.change_requests_scanned,
                "issues_scanned": report.issues_scanned,
                "sessions_considered": report.sessions_considered,
                "explicit_matches": report.explicit_matches,
                "high_matches": report.high_matches,
                "inferred_matches": report.inferred_matches,
                "ambiguous": report.ambiguous,
                "unmatched": report.unmatched,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_job_response(completed).model_dump(mode="json"),
        )

    async with timed_operation("db.query.backfill.insert", "db"):
        row = await BackfillJobStore(conn).create(
            provider=body.provider,
            repository=body.repository,
            window_from=body.from_,
            window_until=body.until,
            dry_run=False,
            show_evidence=body.show_evidence,
            requested_by=auth["key_label"],
        )
    logger.info(
        "backfill.job.queued",
        extra={
            "job_id": str(row["id"]),
            "provider": row["provider"],
            "repository": row["repository"],
            "requested_by": row["requested_by"],
        },
    )
    location = f"/api/v1/backfill/jobs/{row['id']}"
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=_job_response(row).model_dump(mode="json"),
        headers={"Location": location},
    )


@router.get("/jobs")
async def list_backfill_jobs(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth: dict[str, str] = Depends(require_backfill_key),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[BackfillJobResponse]:
    """List submitted backfill jobs, newest first, optionally filtered by status."""
    if status_filter is not None and status_filter not in ALL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid status: {status_filter!r}. "
                f"Valid values: {', '.join(sorted(ALL_STATUSES))}"
            ),
        )
    async with timed_operation("db.query.backfill.list", "db"):
        total, rows = await BackfillJobStore(conn).list_jobs(
            status_filter=status_filter, limit=limit, offset=offset
        )
    return PaginatedResponse(
        items=[_job_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}")
async def get_backfill_job(
    job_id: uuid.UUID,
    request: Request,
    auth: dict[str, str] = Depends(require_backfill_key),
    conn: asyncpg.Connection = Depends(get_session),
) -> BackfillJobResponse:
    """Return one job's status, audit metadata, and report counters."""
    async with timed_operation("db.query.backfill.get", "db"):
        row = await BackfillJobStore(conn).get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backfill job not found: {job_id}",
        )
    return _job_response(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_backfill_job(
    job_id: uuid.UUID,
    request: Request,
    auth: dict[str, str] = Depends(require_backfill_key),
    conn: asyncpg.Connection = Depends(get_session),
) -> BackfillJobResponse:
    """Cancel a queued job.  Running/finished jobs are not interrupted (409)."""
    async with timed_operation("db.query.backfill.get", "db"):
        current = await BackfillJobStore(conn).get(job_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backfill job not found: {job_id}",
        )
    async with timed_operation("db.query.backfill.cancel", "db"):
        cancelled = await BackfillJobStore(conn).cancel(job_id)
    if cancelled is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only queued jobs can be cancelled "
                f"(current status: {current['status']})"
            ),
        )
    logger.info("backfill.job.cancelled", extra={"job_id": str(job_id)})
    return _job_response(cancelled)
