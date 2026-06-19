"""Job API endpoints — create, retrieve, and monitor coding jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone  # noqa: UP017

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic.config import ConfigDict

from app.api.webhooks import dispatch_webhooks
from app.core.config import get_settings
from app.core.lifecycle import can_transition
from app.core.models.job import JobStatus
from app.core.ports import PortExhaustedError, allocate_port
from app.db.session import DatabasePool, get_session
from app.executors import ExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import (
    CleanupWorkspaceRequest,
    CreateWorkspaceRequest,
    StartOpencodeRequest,
    StopOpencodeRequest,
)
from app.opencode.protocol import OpenCodeClientProtocol
from app.policy import ObservationBasedPolicy, PolicyViolation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


async def _get_pool(request: Request) -> DatabasePool:
    """FastAPI dependency that returns the database pool for background tasks."""
    return request.app.state.pool  # type: ignore[return-value]


class JobCreateRequest(BaseModel):
    """Request body for POST /jobs.

    Callers may optionally pin the job to a specific runner (``runner_id``)
    or request a runner that matches a set of label constraints
    (``labels``).  When neither is provided the Gateway auto-selects the
    best available runner via :func:`select_runner`.

    ``env_vars`` are environment variables to pass to the OpenCode session.
    """

    repo_url: HttpUrl = Field(description="URL of the repository to work on")
    task_summary: str = Field(min_length=1, description="Summary of the task to perform")
    runner_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the runner to pin this job to.  When set, the "
        "runner must exist and be HEALTHY.",
    )
    labels: list[str] | None = Field(
        default=None,
        description="Labels used to filter eligible runners.  Each label "
        "must exist as a key in the runner's labels JSONB column. "
        "Ignored when ``runner_id`` is provided.",
    )
    env_vars: dict[str, str] = {}
    workflow_run_id: str | None = Field(
        default=None,
        description="Correlation ID that allows Paperclip to correlate this job "
        "to a workflow run.",
    )

    @field_validator("workflow_run_id", mode="before")
    @classmethod
    def _normalize_workflow_run_id(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class JobResponse(BaseModel):
    """Response body for job endpoints.

    ``branch_name`` and ``mr_url`` are write-once fields set externally
    (e.g. by Paperclip or AWX job templates) when the coding session
    completes.  They remain ``None`` until populated via a webhook or
    update endpoint.
    """

    id: uuid.UUID
    repo_url: str
    task_summary: str
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    opencode_session_id: str | None = None
    diff: str | None = None
    branch_name: str | None = None
    mr_url: str | None = None
    workflow_run_id: str | None = None


class JobEvent(BaseModel):
    """A single approval/rejection event for a job."""

    event_type: str
    timestamp: datetime
    actor: str
    details: str
    previous_status: str | None = None

    model_config = ConfigDict(from_attributes=True)


_FETCH_COLS = (
    "id, repo_url, task_summary, status, created_at, updated_at, completed_at, "
    "opencode_session_id, diff, workspace_name, branch_name, mr_url, workflow_run_id"
)


async def _fetch_job(conn: asyncpg.Connection, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Fetch a single job row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM gateway_jobs WHERE id = $1",
        job_id,
    )


async def get_opencode_client() -> OpenCodeClientProtocol | None:
    """Dependency that returns the OpenCode client for diff fetching.

    Returns None by default; tests override this via
    ``app.dependency_overrides`` to inject a mock.
    """
    return None


async def _set_workspace_cleanup_after(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
    retention_hours: int,
) -> None:
    """Set cleanup_after on a workspace based on its created_at + retention.

    The workspace row must already exist in the database (created by the
    executor or workspace lifecycle).  When the row does not exist the
    UPDATE is a silent no-op.
    """
    await conn.execute(
        "UPDATE workspaces SET cleanup_after = created_at + $2::interval, "
        "updated_at = $3 WHERE id = $1",
        workspace_id,
        timedelta(hours=retention_hours),
        datetime.now(timezone.utc),  # noqa: UP017
    )


def _resolve_workspace_id(workspace_name: str | None) -> uuid.UUID | None:
    """Parse the *workspace_name* column (stored as a string UUID) back to a UUID."""
    if not workspace_name:
        return None
    try:
        return uuid.UUID(workspace_name)
    except (ValueError, TypeError):
        logger.warning("Invalid workspace_name: %r", workspace_name)
        return None


async def select_runner(
    conn: asyncpg.Connection,
    runner_id: uuid.UUID | None = None,
    labels: list[str] | None = None,
) -> uuid.UUID:
    """Select an appropriate runner for a new job.

    **Runner Selection Algorithm**

    This function implements three selection modes in priority order:

    **1. Explicit Pinning (``runner_id`` provided)**
    When the caller specifies a ``runner_id``:

    1. Look up the runner by its UUID primary key in the ``runners`` table.
    2. If the runner does not exist → raise ``HTTPException(400)``.
    3. If the runner's ``status`` is not ``HEALTHY`` → raise
       ``HTTPException(400)``.
    4. Otherwise, return the runner's UUID.

    **2. Label-Based Selection (``labels`` provided, ``runner_id`` absent)**
    When the caller provides ``labels`` but no ``runner_id``:

    1. Query all runners where:
       - ``status = 'HEALTHY'``, AND
       - the ``labels`` JSONB column contains every requested label as a
         key (checked via PostgreSQL ``?&`` operator).
    2. Among the candidates, count each runner's active workspaces
       (``workspaces`` rows where ``cleanup_status = 'active'``).
    3. Select the runner with the **fewest** active workspaces (load
       balancing).
    4. Break ties by picking the runner with the lowest UUID
       (deterministic tie-break).
    5. If no runner matches → raise ``HTTPException(400)``.

    **3. Automatic Selection (neither ``runner_id`` nor ``labels``)**
    When the caller provides no constraints:

    1. Query all runners with ``status = 'HEALTHY'``.
    2. Count each runner's active workspaces.
    3. Select the runner with the **fewest** active workspaces.
    4. Break ties by lowest UUID.
    5. If no healthy runner exists → raise ``HTTPException(400)``.

    **What counts as an active workspace?**
    Workspaces where ``cleanup_status = 'active'`` count as active load
    on a runner.  Workspaces that have been cleaned (``cleanup_status =
    'cleaned'``) or are otherwise non-active are excluded from the
    tally.

    Parameters
    ----------
    conn:
        An active asyncpg database connection.
    runner_id:
        Explicit runner UUID to pin to.  When set, *labels* is ignored.
    labels:
        List of label keys that a runner's ``labels`` JSONB column must
        contain.  Only used when *runner_id* is ``None``.

    Returns
    -------
    uuid.UUID
        The UUID of the selected runner.

    Raises
    ------
    HTTPException(400)
        If the requested runner is not found, is unhealthy, or no
        matching healthy runner is available.
    """
    if runner_id is not None:
        # --- Mode 1: Explicit pinning ---
        row = await conn.fetchrow(
            "SELECT id, status FROM runners WHERE id = $1",
            runner_id,
        )
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Runner not found: {runner_id}",
            )
        if row["status"] != "HEALTHY":
            raise HTTPException(
                status_code=400,
                detail=f"Runner is not healthy (status={row['status']}): {runner_id}",
            )
        return runner_id

    # Build the base query for modes 2 and 3
    query = (
        "SELECT r.id, COUNT(w.id) AS active_workspaces "
        "FROM runners r "
        "LEFT JOIN workspaces w ON w.runner_id = r.id "
        "   AND w.cleanup_status = 'active' "
        "WHERE r.status = 'HEALTHY'"
    )
    query_args: list[list[str]] = []

    if labels:
        # --- Mode 2: Label-based selection ---
        # PostgreSQL ?& operator checks that all keys exist in the JSONB object.
        query += " AND r.labels IS NOT NULL AND r.labels ?& $1"
        query_args.append(labels)

    query += " GROUP BY r.id ORDER BY active_workspaces ASC, r.id ASC LIMIT 1"

    row = await conn.fetchrow(query, *query_args)
    if row is None:
        if labels:
            raise HTTPException(
                status_code=400,
                detail=f"No healthy runner found matching labels: {labels}",
            )
        raise HTTPException(
            status_code=400,
            detail="No healthy runners available",
        )
    return row["id"]  # type: ignore[no-any-return]


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
    pool: DatabasePool = Depends(_get_pool),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: OpenCodeClientProtocol | None = Depends(get_opencode_client),
) -> JobResponse:
    """Create a new job, dispatch via executor, and return the final state."""
    job_id = uuid.uuid4()
    settings = get_settings()

    # 0. Select (or validate) the target runner
    resolved_runner_id = await select_runner(
        conn,
        runner_id=body.runner_id,
        labels=body.labels,
    )

    # 1. Insert the job record in pending state
    await conn.execute(
        "INSERT INTO gateway_jobs "
        "(id, repo_url, task_summary, status, executor_type, runner_id, env_vars, workflow_run_id) "
        "VALUES ($1, $2, $3, 'pending', $4, $5, $6::jsonb, $7)",
        job_id,
        str(body.repo_url),
        body.task_summary,
        executor.name,
        resolved_runner_id,
        json.dumps(body.env_vars) if body.env_vars else "{}",
        body.workflow_run_id,
    )

    # 2. Validate and perform pending → running transition
    if not can_transition(JobStatus.PENDING, JobStatus.RUNNING):
        logger.error(
            "Lifecycle rejected transition pending→running for job %s", job_id
        )
        raise HTTPException(status_code=500, detail="Internal state machine error")
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        datetime.now(timezone.utc),  # noqa: UP017
    )

    new_workspace_id = None

    try:
        # Resolve the selected runner's text ID for pre-flight policy check.
        # This ensures the policy inspects the same runner that select_runner()
        # chose for the job.
        runner_row = await conn.fetchrow(
            "SELECT runner_id FROM runners WHERE id = $1",
            resolved_runner_id,
        )
        runner_text_id = runner_row["runner_id"] if runner_row else None

        if runner_text_id is not None:
            policy = ObservationBasedPolicy(settings)
            await policy.check(runner_text_id, conn=conn)

        # 4. Create workspace on the selected runner (only after policy passes)
        ws_response = await executor.create_workspace(
            CreateWorkspaceRequest(
                repo_url=str(body.repo_url),
                job_id=job_id,
                runner_id=runner_text_id,
                env_vars=body.env_vars,
            )
        )
        new_workspace_id = ws_response.workspace_id

        # Store the workspace ID immediately so it is available even if
        # start_opencode fails and the job is marked "failed".
        await conn.execute(
            "UPDATE gateway_jobs SET workspace_name = $2 WHERE id = $1",
            job_id,
            str(new_workspace_id),
        )

        # 4a. Allocate a port and persist it against the workspace (ADR 0003).
        try:
            allocated_port = await allocate_port(conn)
        except PortExhaustedError:
            logger.error(
                "Port exhaustion — no free ports in range 10000–10999 "
                "for job %s (workspace %s)",
                job_id,
                new_workspace_id,
            )
            raise HTTPException(
                status_code=503,
                detail="No available ports — all 1000 ports in range 10000–10999 are in use",
            )

        await conn.execute(
            "UPDATE workspaces SET port = $2, updated_at = $3 WHERE id = $1",
            new_workspace_id,
            allocated_port,
            datetime.now(timezone.utc),  # noqa: UP017
        )
        logger.info(
            "Allocated port %d for job %s (workspace %s)",
            allocated_port,
            job_id,
            new_workspace_id,
        )

        # 5. Start OpenCode Serve
        start_response = await executor.start_opencode(
            StartOpencodeRequest(
                workspace_id=new_workspace_id,
                workspace_path=ws_response.workspace_path,
                port=allocated_port,
                env_vars=body.env_vars,
            )
        )
        session_id = str(start_response.session_id)

        # Store the session ID so it is available for diff retrieval
        await conn.execute(
            "UPDATE gateway_jobs SET opencode_session_id = $2 WHERE id = $1",
            job_id,
            session_id,
        )

        # 5. Mark completed
        now = datetime.now(timezone.utc)  # noqa: UP017
        diff_summary = f"Job completed: {body.task_summary}"
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'completed', updated_at = $2, "
            "completed_at = $3, diff = $4 WHERE id = $1",
            job_id,
            now,
            now,
            diff_summary,
        )

        # Fire webhooks asynchronously — non-blocking
        asyncio.create_task(
            dispatch_webhooks(
                pool,
                job_id,
                "job.completed",
                {
                    "job_id": str(job_id),
                    "event_type": "job.completed",
                    "status": "completed",
                    "repo_url": str(body.repo_url),
                    "task_summary": body.task_summary,
                    "completed_at": now.isoformat(),
                    "diff": diff_summary,
                },
            )
        )

        # 6. Set workspace cleanup_after for successful completion
        if new_workspace_id is not None:
            await _set_workspace_cleanup_after(
                conn, new_workspace_id, settings.cleanup_success_retention_hours
            )

        # 8. Fetch and persist the diff (non-blocking — failure does not fail the job)
        if opencode_client is not None:
            try:
                diff_response = await opencode_client.get_session_diff(session_id)
                if diff_response and diff_response.diff:
                    await conn.execute(
                        "UPDATE gateway_jobs SET diff = $2 WHERE id = $1",
                        job_id,
                        diff_response.diff,
                    )
                    logger.info(
                        "Diff fetched and persisted for job %s (session %s)",
                        job_id,
                        session_id,
                    )
            except Exception:
                logger.warning(
                    "Failed to fetch diff for job %s (session %s)",
                    job_id,
                    session_id,
                    exc_info=True,
                )

    except PolicyViolation:
        # Policy rejected the job BEFORE any infrastructure action was taken.
        # Mark as failed and re-raise the 503 — no workspace to clean up.
        logger.warning(
            "Policy check rejected job %s before workspace creation",
            job_id,
        )
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'failed', updated_at = $2 WHERE id = $1",
            job_id,
            datetime.now(timezone.utc),  # noqa: UP017
        )
        # Fire webhooks asynchronously for policy-rejected jobs
        asyncio.create_task(
            dispatch_webhooks(
                pool,
                job_id,
                "job.failed",
                {
                    "job_id": str(job_id),
                    "event_type": "job.failed",
                    "status": "failed",
                    "repo_url": str(body.repo_url),
                    "task_summary": body.task_summary,
                    "completed_at": None,
                    "error": "Policy rejected: disk/memory pressure",
                },
            )
        )

        if new_workspace_id is not None:
            await _set_workspace_cleanup_after(
                conn, new_workspace_id, settings.cleanup_failure_retention_hours
            )
            try:
                await executor.cleanup_workspace(
                    CleanupWorkspaceRequest(workspace_id=new_workspace_id)
                )
            except Exception:
                logger.exception(
                    "Failed to clean up workspace for policy-rejected job %s "
                    "(workspace %s)",
                    job_id,
                    new_workspace_id,
                )
        raise

    except Exception:
        logger.exception("Executor dispatch failed for job %s", job_id)
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'failed', updated_at = $2 WHERE id = $1",
            job_id,
            datetime.now(timezone.utc),  # noqa: UP017
        )

        # Fire webhooks asynchronously for failed jobs
        asyncio.create_task(
            dispatch_webhooks(
                pool,
                job_id,
                "job.failed",
                {
                    "job_id": str(job_id),
                    "event_type": "job.failed",
                    "status": "failed",
                    "repo_url": str(body.repo_url),
                    "task_summary": body.task_summary,
                    "completed_at": None,
                },
            )
        )

        # Set workspace cleanup_after for failure
        row = await _fetch_job(conn, job_id)
        if row is not None:
            ws_id = _resolve_workspace_id(row.get("workspace_name"))
            if ws_id is not None:
                await _set_workspace_cleanup_after(
                    conn, ws_id, settings.cleanup_failure_retention_hours
                )

    # 9. Return final state
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
        branch_name=row.get("branch_name"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
    )


@router.post("/jobs/{job_id}/approve", response_model=JobResponse)
async def approve_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Approve a job that is waiting for approval, transitioning it to running."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "needs_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Job is in '{row['status']}' state, expected 'needs_approval'",
        )

    # Validate the transition against the centralised rule set (defence-in-depth)
    if not can_transition(JobStatus.NEEDS_APPROVAL, JobStatus.RUNNING):
        logger.error(
            "Lifecycle rejected transition needs_approval→running for job %s", job_id
        )
        raise HTTPException(status_code=500, detail="Internal state machine error")

    now = datetime.now(timezone.utc)  # noqa: UP017

    # Record approval
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals "
        "(id, job_id, requested_by, requested_action, approval_type, approved_by, "
        "status, created_at, decided_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        approval_id,
        job_id,
        "system",
        "run_job",
        "manual",
        "api",
        "approved",
        now,
        now,
    )

    # Transition job to running
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        now,
    )

    # Return updated job
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
        branch_name=row.get("branch_name"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
    )


@router.post("/jobs/{job_id}/reject", response_model=JobResponse)
async def reject_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Reject a job that is waiting for approval, transitioning it to rejected."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] != "needs_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Job is in '{row['status']}' state, expected 'needs_approval'",
        )

    # Validate the transition against the centralised rule set (defence-in-depth)
    if not can_transition(JobStatus.NEEDS_APPROVAL, JobStatus.REJECTED):
        logger.error(
            "Lifecycle rejected transition needs_approval→rejected for job %s", job_id
        )
        raise HTTPException(status_code=500, detail="Internal state machine error")

    now = datetime.now(timezone.utc)  # noqa: UP017

    # Record rejection
    approval_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO approvals "
        "(id, job_id, requested_by, requested_action, approval_type, approved_by, "
        "status, created_at, decided_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        approval_id,
        job_id,
        "system",
        "run_job",
        "manual",
        "api",
        "rejected",
        now,
        now,
    )

    # Transition job to rejected
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'rejected', updated_at = $2 WHERE id = $1",
        job_id,
        now,
    )

    # Return updated job
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
        branch_name=row.get("branch_name"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
    )


@router.post("/jobs/{job_id}/abort", response_model=JobResponse)
async def abort_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: OpenCodeClientProtocol | None = Depends(get_opencode_client),
) -> JobResponse:
    """Abort a job by cancelling its OpenCode session and marking it aborted.

    If the job has an active OpenCode session the endpoint transitions the job
    to ``aborting``, calls :meth:`OpenCodeClientProtocol.abort_session`, and on
    success marks the job ``aborted``.  When the session is unreachable the job
    stays in ``aborting`` and the caller receives a 503.

    Jobs without a session (e.g. still *pending*) are immediately marked
    ``aborted``.
    """
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    current_status = JobStatus(row["status"])

    # Allow retry from aborting; otherwise validate via centralised transition table
    if current_status != JobStatus.ABORTING and not can_transition(
        current_status, JobStatus.ABORTING
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in '{row['status']}' state; "
                f"cannot transition to aborting"
            ),
        )

    now = datetime.now(timezone.utc)  # noqa: UP017
    session_id = row.get("opencode_session_id")
    previous_status = row["status"]  # capture before transition

    # Transition to aborting unless already in that state (retry)
    if current_status != JobStatus.ABORTING:
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'aborting', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )

    aborted = False

    # If the job has an active session, cancel it via the OpenCode client
    if session_id and opencode_client is not None:
        try:
            await opencode_client.abort_session(session_id)
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 WHERE id = $1",
                job_id,
                datetime.now(timezone.utc),  # noqa: UP017
            )
            aborted = True
        except Exception:
            logger.exception(
                "Failed to abort OpenCode session %s for job %s",
                session_id,
                job_id,
            )
            raise HTTPException(
                status_code=503,
                detail="OpenCode Serve unreachable; job remains in aborting state",
            )
    else:
        # No session to cancel — mark aborted immediately
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )
        aborted = True

    # When the job reached aborted state, set workspace cleanup_after
    if aborted:
        settings = get_settings()
        ws_id = _resolve_workspace_id(row.get("workspace_name"))
        if ws_id is not None:
            await _set_workspace_cleanup_after(
                conn, ws_id, settings.cleanup_failure_retention_hours
            )

    # Executor cleanup --- best effort, do not block the abort response
    workspace_name = row.get("workspace_name")
    if workspace_name:
        try:
            parsed_id = uuid.UUID(workspace_name)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid workspace_name for job %s: %r", job_id, workspace_name
            )
        else:
            try:
                await executor.stop_opencode(
                    StopOpencodeRequest(workspace_id=parsed_id)
                )
            except Exception:
                logger.exception(
                    "Failed to stop OpenCode Serve for job %s (workspace %s)",
                    job_id,
                    parsed_id,
                )
            try:
                await executor.cleanup_workspace(
                    CleanupWorkspaceRequest(workspace_id=parsed_id)
                )
            except Exception:
                logger.exception(
                    "Failed to clean up workspace for job %s (workspace %s)",
                    job_id,
                    parsed_id,
                )

    # Record abort event
    event_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO job_events "
        "(id, job_id, event_type, actor, details, previous_status, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        event_id,
        job_id,
        "aborted",
        "api",
        "Job aborted",
        previous_status,
        now,
    )

    # Return updated job
    row = await _fetch_job(conn, job_id)
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
        branch_name=row.get("branch_name"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JobResponse:
    """Retrieve a job by its ID."""
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(
        id=row["id"],
        repo_url=row["repo_url"],
        task_summary=row["task_summary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        opencode_session_id=row.get("opencode_session_id"),
        diff=row.get("diff"),
        branch_name=row.get("branch_name"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
    )


class JobListResponse(BaseModel):
    """Response body for the GET /jobs listing endpoint."""

    items: list[JobResponse]
    total: int
    limit: int
    offset: int


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: JobStatus | None = None,
    runner_id: uuid.UUID | None = None,
    workflow_run_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> JobListResponse:
    """Return a paginated list of job summaries ordered by created_at DESC.

    Supports optional query filters: ``status``, ``runner_id``,
    ``workflow_run_id``, ``limit``, and ``offset``.

    Returns a ``JobListResponse`` containing the matching ``items``,
    the ``total`` count (ignoring pagination), and the requested
    ``limit`` / ``offset`` for cursor tracking.
    """
    if workflow_run_id is not None and workflow_run_id.strip() == "":
        workflow_run_id = None

    # Build WHERE clauses dynamically
    where_clauses: list[str] = []
    params: list = []
    param_index = 1

    if status is not None:
        where_clauses.append(f"status = ${param_index}")
        params.append(status.value)
        param_index += 1

    if runner_id is not None:
        where_clauses.append(f"runner_id = ${param_index}")
        params.append(runner_id)
        param_index += 1

    if workflow_run_id is not None:
        where_clauses.append(f"workflow_run_id = ${param_index}")
        params.append(workflow_run_id)
        param_index += 1

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # Count total matching rows (ignoring pagination)
    count_sql = f"SELECT COUNT(*) FROM gateway_jobs {where_sql}"
    total_row = await conn.fetchrow(count_sql, *params)
    total = total_row["count"] if total_row else 0  # type: ignore[index]

    # Fetch paginated results
    query = (
        f"SELECT {_FETCH_COLS} FROM gateway_jobs "
        f"{where_sql} ORDER BY created_at DESC "
        f"LIMIT ${param_index} OFFSET ${param_index + 1}"
    )
    params.append(limit)
    params.append(offset)

    rows = await conn.fetch(query, *params)

    items = [
        JobResponse(
            id=row["id"],
            repo_url=row["repo_url"],
            task_summary=row["task_summary"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            opencode_session_id=row.get("opencode_session_id"),
            diff=row.get("diff"),
            branch_name=row.get("branch_name"),
            mr_url=row.get("mr_url"),
            workflow_run_id=row.get("workflow_run_id"),
        )
        for row in rows
    ]

    return JobListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}/events", response_model=list[JobEvent])
async def get_job_events(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> list[JobEvent]:
    """Return approval/rejection events for a job.

    Queries the approvals table and maps each record to an event dict
    with event_type, timestamp, actor, and details.  Returns an empty
    list when the job has no events, or 404 if the job does not exist.
    """
    # Verify the job exists first
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Query the approvals table for this job
    approval_records = await conn.fetch(
        "SELECT status, approved_by, requested_by, requested_action, created_at "
        "FROM approvals WHERE job_id = $1 ORDER BY created_at ASC",
        job_id,
    )

    # Query the job_events table for this job
    event_records = await conn.fetch(
        "SELECT event_type, actor, details, created_at, previous_status "
        "FROM job_events WHERE job_id = $1 ORDER BY created_at ASC",
        job_id,
    )

    events: list[JobEvent] = []
    for rec in approval_records:
        events.append(
            JobEvent(
                event_type=rec["status"],
                timestamp=rec["created_at"],
                actor=rec["approved_by"] if rec["approved_by"] else rec["requested_by"],
                details=rec["requested_action"],
                previous_status=None,
            )
        )
    for rec in event_records:
        events.append(
            JobEvent(
                event_type=rec["event_type"],
                timestamp=rec["created_at"],
                actor=rec["actor"],
                details=rec["details"],
                previous_status=rec["previous_status"],
            )
        )

    # Sort combined events by timestamp ascending
    events.sort(key=lambda e: e.timestamp)

    return events


@router.get("/jobs/{job_id}/diff")
async def get_job_diff(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> JSONResponse:
    """Return the stored diff for a completed job.

    Returns the diff payload on success (200), a conflict response when the
    job is still running (409), or 404 when the job does not exist or has no
    diff available.
    """
    row = await conn.fetchrow(
        "SELECT id, status, diff FROM gateway_jobs WHERE id = $1",
        job_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if row["status"] == "running":
        return JSONResponse(
            status_code=409,
            content={
                "job_id": str(job_id),
                "diff": None,
                "status": "running",
            },
        )

    if row["diff"] is None:
        raise HTTPException(status_code=404, detail="Diff not available")

    return JSONResponse(
        status_code=200,
        content={
            "job_id": str(row["id"]),
            "diff": row["diff"],
        },
    )


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
    opencode_client: OpenCodeClientProtocol | None = Depends(get_opencode_client),
) -> JSONResponse:
    """Return the full log output for a job by proxying to its OpenCode session.

    Retrieves the session log from the OpenCode Serve API.  Returns 404
    when the job does not exist, 409 when the job has no associated session,
    and 424 when the session exists but has not started yet (status is
    still pending/created/queued).
    """
    # 1. Look up the job
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # 2. Check for an associated session
    session_id: str | None = row.get("opencode_session_id")
    if not session_id:
        raise HTTPException(
            status_code=409,
            detail="No session associated with this job",
        )

    # 3. Require an OpenCode client to fetch logs
    if opencode_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenCode Serve client not available",
        )

    # 4. Get session info to check status
    try:
        session_info = await opencode_client.get_session(session_id)
    except Exception:
        logger.exception(
            "Failed to get session info for job %s (session %s)",
            job_id,
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail="OpenCode Serve unreachable",
        )

    # 5. Check whether the session has started (424 when still pending)
    if session_info.status in ("pending", "created", "queued"):
        raise HTTPException(
            status_code=424,
            detail=f"Session has not started yet (status: {session_info.status})",
        )

    # 6. Fetch the session log
    try:
        log_response = await opencode_client.get_session_log(session_id)
    except Exception:
        logger.exception(
            "Failed to get session log for job %s (session %s)",
            job_id,
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to retrieve session logs",
        )

    return JSONResponse(
        status_code=200,
        content={
            "job_id": str(job_id),
            "session_id": session_id,
            "log": log_response.log,
        },
    )
