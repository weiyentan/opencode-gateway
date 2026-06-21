"""Job API endpoints — create, retrieve, and monitor coding jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone  # noqa: UP017
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic.config import ConfigDict

from app.api.webhooks import dispatch_webhooks
from app.core.config import get_settings
from app.core.lifecycle import can_transition
from app.core.models.job import JobStatus
from app.core.ports import PortExhaustedError, allocate_and_assign_port
from app.db.session import DatabasePool, get_session
from app.executors import ExecutorPlugin
from app.executors.awx.exceptions import AWXArtifactError
from app.executors.awx.plugin import AWXExecutorPlugin
from app.executors.factory import get_executor
from app.executors.models import (
    CancelJobRequest,
    CleanupWorkspaceRequest,
    CreateWorkspaceRequest,
    StartOpencodeRequest,
    StopOpencodeRequest,
)
from app.opencode.protocol import OpenCodeClientProtocol
from app.policy import ObservationBasedPolicy, PolicyViolation


class PortMismatchError(Exception):
    """Raised when the AWX-executor returns a port that does not match the
    Gateway-allocated port.

    Attributes:
        allocated_port: The port the Gateway assigned via
            :func:`allocate_and_assign_port`.
        returned_port: The port the AWX playbook reported back in
            :class:`StartOpencodeResponse`.
    """

    def __init__(self, allocated_port: int, returned_port: int) -> None:
        self.allocated_port = allocated_port
        self.returned_port = returned_port
        super().__init__(
            f"Port mismatch: allocated {allocated_port}, "
            f"AWX returned {returned_port}"
        )

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
        "runner must exist, have admin_status='online', and "
        "health_status='HEALTHY'.",
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


class JobCompleteRequest(BaseModel):
    """Request body for POST /jobs/{id}/complete.

    Callers provide the ``target_status`` (one of ``awaiting_review``,
    ``completed``, or ``failed``) and optional metadata fields
    (``branch_name``, ``commit_sha``, ``mr_url``, ``diff``) to record
    on the job.

    When transitioning to ``failed``, provide a ``failure_reason``.
    When transitioning to ``completed``, an optional ``summary`` is
    accepted for logging purposes.
    """

    target_status: str = Field(
        description="Target status: awaiting_review, completed, or failed"
    )
    branch_name: str | None = Field(default=None)
    commit_sha: str | None = Field(default=None)
    mr_url: str | None = Field(default=None)
    diff: str | None = Field(default=None)
    summary: str | None = Field(default=None)
    failure_reason: str | None = Field(default=None)


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
    commit_sha: str | None = None
    mr_url: str | None = None
    workflow_run_id: str | None = None
    diff_url: str | None = None
    failure_reason: str | None = None


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
    "opencode_session_id, diff, workspace_name, branch_name, commit_sha, mr_url, "
    "workflow_run_id, failure_reason, executor_job_id"
)


async def _fetch_job(conn: asyncpg.Connection, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Fetch a single job row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM gateway_jobs WHERE id = $1",
        job_id,
    )


async def _check_aborted(conn: asyncpg.Connection, job_id: uuid.UUID) -> bool:
    """Re-read the job status; return True if already ``aborting`` or ``aborted``.

    Call this *before* writing ``failed`` in an exception handler to guard
    against the abort-race (issue #184): a concurrent abort sets the status
    to ``aborted`` while the handler is about to overwrite it with
    ``failed``.  When this function returns ``True`` the caller should skip
    the failed-status update.
    """
    row = await _fetch_job(conn, job_id)
    if row and row["status"] in ("aborting", "aborted"):
        logger.warning(
            "Job %s is already %s — skipping failed status update "
            "(abort race guard)",
            job_id,
            row["status"],
        )
        return True
    return False


async def _fetch_and_check_aborted(
    conn: asyncpg.Connection, job_id: uuid.UUID,
) -> dict | None:
    """Fetch the current job row; return it if status is aborting/aborted.

    Call this after long-awaited lifecycle methods (create_workspace,
    start_opencode) to detect a concurrent abort. When a row is returned
    the caller should stop progressing and return ``_job_response(row)``
    immediately.
    """
    row = await _fetch_job(conn, job_id)
    if row and row["status"] in ("aborting", "aborted"):
        logger.warning(
            "Job %s was %s during lifecycle step — stopping progress",
            job_id, row["status"],
        )
        return row
    return None


def _job_response(request: Request, row: dict) -> JobResponse:
    """Construct a JobResponse from a database row."""
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(row["id"]))),
        failure_reason=row.get("failure_reason"),
    )


async def get_opencode_client() -> OpenCodeClientProtocol | None:
    """Dependency that returns the OpenCode client for diff fetching.

    Creates a real :class:`OpenCodeServeClient` when
    ``GATEWAY_OPENCODE_BASE_URL`` is configured (non-empty).  Returns
    ``None`` when the URL is empty so that callers can check for
    availability.

    Tests override this via ``app.dependency_overrides`` to inject a mock.
    """
    from app.opencode.serve_client import OpenCodeServeClient

    settings = get_settings()
    if not settings.opencode_base_url:
        return None
    return OpenCodeServeClient(base_url=settings.opencode_base_url)


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


async def _record_job_event(
    conn: asyncpg.Connection,
    job_id: uuid.UUID,
    from_status: str,
    to_status: str,
    message: str,
    actor: str = "system",
) -> None:
    """Insert a job lifecycle transition event into the ``job_events`` table.

    Each event captures a state machine transition with the source and
    target statuses, a human-readable message, and a timestamp.

    Parameters
    ----------
    conn:
        An active asyncpg database connection.
    job_id:
        UUID of the job whose status changed.
    from_status:
        The status the job was in before the transition.
    to_status:
        The status the job transitioned into.
    message:
        Human-readable description of the transition (stored as ``details``).
    actor:
        Entity that performed the transition (default ``"system"``).
    """
    event_id = uuid.uuid4()
    now = datetime.now(timezone.utc)  # noqa: UP017
    await conn.execute(
        "INSERT INTO job_events "
        "(id, job_id, event_type, actor, details, previous_status, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        event_id,
        job_id,
        to_status,
        actor,
        message,
        from_status,
        now,
    )


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
    3. If the runner's ``admin_status`` is not ``online`` → raise
       ``HTTPException(400)``.
    4. If the runner's ``health_status`` is not ``HEALTHY`` → raise
       ``HTTPException(400)``.
    5. Otherwise, return the runner's UUID.

    **2. Label-Based Selection (``labels`` provided, ``runner_id`` absent)**
    When the caller provides ``labels`` but no ``runner_id``:

    1. Query all runners where:
       - ``admin_status = 'online'``, AND
       - ``health_status = 'HEALTHY'``, AND
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

    1. Query all runners with ``admin_status = 'online'`` AND
       ``health_status = 'HEALTHY'``.
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
        If the requested runner is not found, its ``admin_status`` is
        not ``online``, its ``health_status`` is not ``HEALTHY``, or no
        matching runner with the required statuses is available.
    """
    if runner_id is not None:
        # --- Mode 1: Explicit pinning ---
        row = await conn.fetchrow(
            "SELECT id, admin_status, health_status FROM runners WHERE id = $1",
            runner_id,
        )
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Runner not found: {runner_id}",
            )
        admin_status = row["admin_status"]
        if admin_status != "online":
            raise HTTPException(
                status_code=400,
                detail=f"Runner admin_status is '{admin_status}', expected 'online': {runner_id}",
            )
        health_status = row["health_status"]
        if health_status != "HEALTHY":
            raise HTTPException(
                status_code=400,
                detail=f"Runner health_status is '{health_status}', expected 'HEALTHY': {runner_id}",
            )
        return runner_id

    # Build the base query for modes 2 and 3
    query = (
        "SELECT r.id, COUNT(w.id) AS active_workspaces "
        "FROM runners r "
        "LEFT JOIN workspaces w ON w.runner_id = r.id "
        "   AND w.cleanup_status = 'active' "
        "WHERE r.admin_status = 'online' AND r.health_status = 'HEALTHY'"
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
                detail=f"No runner with admin_status='online' and health_status='HEALTHY' "
                f"found matching labels: {labels}",
            )
        raise HTTPException(
            status_code=400,
            detail="No runners with admin_status='online' and health_status='HEALTHY' available",
        )
    return row["id"]  # type: ignore[no-any-return]


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    request: Request,
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

    new_workspace_id = None
    # Track current status for event recording in failure handlers
    current_status = "pending"

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

        # 2. Transition: pending → provisioning_workspace
        if not can_transition(JobStatus.PENDING, JobStatus.PROVISIONING_WORKSPACE):
            logger.error(
                "Lifecycle rejected transition pending→provisioning_workspace for job %s",
                job_id,
            )
            raise HTTPException(status_code=500, detail="Internal state machine error")
        now = datetime.now(timezone.utc)  # noqa: UP017
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'provisioning_workspace', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )
        await _record_job_event(
            conn, job_id, "pending", "provisioning_workspace",
            "Provisioning workspace on runner",
        )
        current_status = "provisioning_workspace"

        # 3. Create workspace on the selected runner.
        # Build the on_awx_job_launched callback that persists the
        # executor_job_id immediately after the AWX job is launched
        # (before wait_for_job), so that cross-process cancellation
        # can target the currently-active AWX job (issue #190).
        _on_launch_cb = None
        if isinstance(executor, AWXExecutorPlugin):
            async def _persist_cb(gw_job_id: UUID, awx_job_id: int) -> None:
                await conn.execute(
                    "UPDATE gateway_jobs SET executor_job_id = $2 WHERE id = $1",
                    gw_job_id,
                    str(awx_job_id),
                )
            _on_launch_cb = _persist_cb

        ws_req = CreateWorkspaceRequest(
            repo_url=str(body.repo_url),
            job_id=job_id,
            runner_id=runner_text_id,
            env_vars=body.env_vars,
        )
        if isinstance(executor, AWXExecutorPlugin):
            ws_response = await executor.create_workspace(
                ws_req, on_awx_job_launched=_on_launch_cb,
            )
        else:
            ws_response = await executor.create_workspace(ws_req)
        new_workspace_id = ws_response.workspace_id

        # Check for concurrent abort — if the job was aborted during
        # create_workspace, return immediately without progressing.
        if aborted := await _fetch_and_check_aborted(conn, job_id):
            # Persist workspace_name so it's available for reference / cleanup
            # even though the job won't progress to running.
            await conn.execute(
                "UPDATE gateway_jobs SET workspace_name = $2 WHERE id = $1",
                job_id, str(new_workspace_id),
            )

            # Clean up the newly-known workspace best-effort.
            try:
                await _set_workspace_cleanup_after(
                    conn,
                    new_workspace_id,
                    settings.cleanup_failure_retention_hours,
                )
                await executor.cleanup_workspace(
                    CleanupWorkspaceRequest(
                        workspace_id=new_workspace_id,
                        gateway_job_id=job_id,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to clean up workspace for aborted job %s (workspace %s)",
                    job_id,
                    new_workspace_id,
                )

            row = await _fetch_job(conn, job_id)
            return _job_response(request, row)

        # Store the workspace ID immediately so it is available even if
        # start_opencode fails and the job is marked "failed".
        await conn.execute(
            "UPDATE gateway_jobs SET workspace_name = $2 WHERE id = $1",
            job_id,
            str(new_workspace_id),
        )

        # 4. Allocate a port and persist it atomically against the workspace (ADR 0003).
        # allocate_and_assign_port acquires an advisory lock, selects a free port,
        # and updates the workspace row in a single transaction, eliminating the
        # race window that existed with the old two-step allocate + UPDATE pattern.
        try:
            allocated_port = await allocate_and_assign_port(conn, new_workspace_id)
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
        logger.info(
            "Allocated port %d for job %s (workspace %s)",
            allocated_port,
            job_id,
            new_workspace_id,
        )

        # 5. Transition: provisioning_workspace → starting_opencode
        if not can_transition(JobStatus.PROVISIONING_WORKSPACE, JobStatus.STARTING_OPENCODE):
            logger.error(
                "Lifecycle rejected transition provisioning_workspace→starting_opencode for job %s",
                job_id,
            )
            raise HTTPException(status_code=500, detail="Internal state machine error")
        now = datetime.now(timezone.utc)  # noqa: UP017
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'starting_opencode', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )
        await _record_job_event(
            conn, job_id, "provisioning_workspace", "starting_opencode",
            "Starting OpenCode Serve on workspace",
        )
        current_status = "starting_opencode"

        # 6. Start OpenCode Serve.
        # Persist executor_job_id via callback *before* wait_for_job
        # so the DB contains the active AWX job ID while the job is
        # still in-flight (issue #190).
        start_req = StartOpencodeRequest(
            workspace_id=new_workspace_id,
            workspace_path=ws_response.workspace_path,
            port=allocated_port,
            gateway_job_id=job_id,
            env_vars=body.env_vars,
        )
        if isinstance(executor, AWXExecutorPlugin):
            start_response = await executor.start_opencode(
                start_req, on_awx_job_launched=_on_launch_cb,
            )
        else:
            start_response = await executor.start_opencode(start_req)
        session_id = str(start_response.session_id)

        # Check for concurrent abort after start_opencode.
        if aborted := await _fetch_and_check_aborted(conn, job_id):
            return _job_response(request, aborted)

        # Store the session ID so it is available for diff retrieval
        await conn.execute(
            "UPDATE gateway_jobs SET opencode_session_id = $2 WHERE id = $1",
            job_id,
            session_id,
        )

        # 6a. Validate that the AWX-returned port matches the allocated port.
        # If the playbook overrides the port, health checks and cleanup would
        # target the wrong session.  Fail the job immediately on mismatch.
        if start_response.port != allocated_port:
            raise PortMismatchError(
                allocated_port=allocated_port,
                returned_port=start_response.port,
            )

        # 7. Transition: starting_opencode → running
        if not can_transition(JobStatus.STARTING_OPENCODE, JobStatus.RUNNING):
            logger.error(
                "Lifecycle rejected transition starting_opencode→running for job %s",
                job_id,
            )
            raise HTTPException(status_code=500, detail="Internal state machine error")
        now = datetime.now(timezone.utc)  # noqa: UP017
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
            job_id,
            now,
        )
        await _record_job_event(
            conn, job_id, "starting_opencode", "running",
            "Job is now running",
        )
        current_status = "running"

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
        # --- abort race guard (issue #184) ---
        if await _check_aborted(conn, job_id):
            # Status already set to aborting/aborted by a concurrent abort;
            # do not overwrite with failed.  Skip the rest of the handler
            # and let the normal path return the aborted job.
            pass
        else:
            logger.warning(
                "Policy check rejected job %s before workspace creation",
                job_id,
            )
            now = datetime.now(timezone.utc)  # noqa: UP017
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'failed', updated_at = $2, "
                "failure_reason = $3 WHERE id = $1",
                job_id,
                now,
                "Policy check rejected: disk/memory pressure",
            )
            await _record_job_event(
                conn, job_id, current_status, "failed",
                "Policy check rejected: disk/memory pressure",
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

    except AWXArtifactError as exc:
        # Artifact validation failure — the AWX job completed but returned
        # malformed or missing data.  Store a descriptive failure reason.
        # --- abort race guard (issue #184) ---
        if await _check_aborted(conn, job_id):
            # Status already set to aborting/aborted by a concurrent abort;
            # do not overwrite with failed.
            pass
        else:
            details_parts = [f"AWX artifact validation failed: {exc}"]
            if exc.template_name:
                details_parts.append(f"template={exc.template_name}")
            if exc.missing_fields:
                details_parts.append(f"missing_fields={exc.missing_fields}")
            if exc.invalid_fields:
                details_parts.append(f"invalid_fields={exc.invalid_fields}")
            error_detail = "; ".join(details_parts)

            logger.error("Executor artifact error for job %s: %s", job_id, error_detail)
            now = datetime.now(timezone.utc)  # noqa: UP017
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'failed', updated_at = $2, "
                "failure_reason = $3 WHERE id = $1",
                job_id,
                now,
                error_detail,
            )
            await _record_job_event(
                conn, job_id, current_status, "failed",
                error_detail,
            )
            
            # Record the artifact failure as a job event
            event_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO job_events "
                "(id, job_id, event_type, actor, details, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                event_id,
                job_id,
                "artifact_error",
                "gateway",
                error_detail,
                now,
            )

            # Fire webhooks asynchronously with the error detail
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
                        "error": error_detail,
                    },
                )
            )

            # Set workspace cleanup_after for failure (if a workspace was created)
            row = await _fetch_job(conn, job_id)
            if row is not None:
                ws_id = _resolve_workspace_id(row.get("workspace_name"))
                if ws_id is not None:
                    await _set_workspace_cleanup_after(
                        conn, ws_id, settings.cleanup_failure_retention_hours
                    )

    except PortMismatchError as exc:
        # Port mismatch — the AWX playbook returned a port different from
        # the one the Gateway allocated.  Fail the job, record the mismatch
        # event, and clean up the workspace.
        # --- abort race guard (issue #184) ---
        if await _check_aborted(conn, job_id):
            # Status already set to aborting/aborted by a concurrent abort;
            # do not overwrite with failed.
            pass
        else:
            error_detail = (
                f"Port mismatch: allocated port {exc.allocated_port}, "
                f"AWX returned port {exc.returned_port}"
            )
            logger.error("Port mismatch for job %s: %s", job_id, error_detail)
            now = datetime.now(timezone.utc)  # noqa: UP017
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'failed', updated_at = $2, "
                "failure_reason = $3 WHERE id = $1",
                job_id,
                now,
                error_detail,
            )
            await _record_job_event(
                conn, job_id, current_status, "failed",
                error_detail,
            )

            # Record the port_mismatch event (separate from the status transition)
            event_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO job_events "
                "(id, job_id, event_type, actor, details, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                event_id,
                job_id,
                "port_mismatch",
                "gateway",
                error_detail,
                now,
            )

            # Fire webhooks asynchronously
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
                        "error": error_detail,
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

            # Clean up the workspace (best-effort)
            try:
                await executor.cleanup_workspace(
                    CleanupWorkspaceRequest(workspace_id=new_workspace_id)
                )
            except Exception:
                logger.exception(
                    "Failed to clean up workspace for port-mismatch job %s "
                    "(workspace %s)",
                    job_id,
                    new_workspace_id,
                )

    except Exception:
        logger.exception("Executor dispatch failed for job %s", job_id)
        # --- abort race guard (issue #184) ---
        if await _check_aborted(conn, job_id):
            # Status already set to aborting/aborted by a concurrent abort;
            # do not overwrite with failed.
            pass
        else:
            now = datetime.now(timezone.utc)  # noqa: UP017
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'failed', updated_at = $2, "
                "failure_reason = $3 WHERE id = $1",
                job_id,
                now,
                f"Executor dispatch failed: {body.task_summary}",
            )
            await _record_job_event(
                conn, job_id, current_status, "failed",
                f"Executor dispatch failed: {body.task_summary}",
            )
            
            # Record the failure as a job event
            event_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO job_events "
                "(id, job_id, event_type, actor, details, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                event_id,
                job_id,
                "executor_error",
                "gateway",
                f"Executor dispatch failed: {body.task_summary}",
                now,
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
                        "error": f"Executor dispatch failed: {body.task_summary}",
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


@router.post("/jobs/{job_id}/approve", response_model=JobResponse)
async def approve_job(
    job_id: uuid.UUID,
    request: Request,
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
    await _record_job_event(
        conn, job_id, "needs_approval", "running",
        "Job approved by api",
        actor="api",
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


@router.post("/jobs/{job_id}/reject", response_model=JobResponse)
async def reject_job(
    job_id: uuid.UUID,
    request: Request,
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
    await _record_job_event(
        conn, job_id, "needs_approval", "rejected",
        "Job rejected by api",
        actor="api",
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


@router.post("/jobs/{job_id}/complete", response_model=JobResponse)
async def complete_job(
    job_id: uuid.UUID,
    body: JobCompleteRequest,
    request: Request,
    conn: asyncpg.Connection = Depends(get_session),
    pool: DatabasePool = Depends(_get_pool),
) -> JobResponse:
    """Transition a job to a terminal or review state and store result metadata.

    Accepts metadata (``branch_name``, ``commit_sha``, ``mr_url``, ``diff``)
    to record on the job at completion time.

    **Allowed transitions:**

    * ``running`` → ``awaiting_review`` — move to review gate
    * ``running`` → ``completed`` — immediate completion (backward compatible)
    * ``running`` → ``failed`` — mark as failed
    * ``awaiting_review`` → ``completed`` — approve reviewed work
    * ``awaiting_review`` → ``failed`` — reject reviewed work

    Other transitions (e.g. ``pending`` → ``completed``) are rejected
    with 409.
    """
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    current_status = row["status"]
    target_status = body.target_status

    # Validate the target status is one of the expected completion states
    valid_targets = {"awaiting_review", "completed", "failed"}
    if target_status not in valid_targets:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid target_status '{target_status}'. "
                f"Must be one of: {', '.join(sorted(valid_targets))}"
            ),
        )

    # Validate the transition against the centralised state machine
    try:
        source = JobStatus(current_status)
        target = JobStatus(target_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job status: {current_status}")

    if not can_transition(source, target):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot transition job from '{current_status}' "
                f"to '{target_status}'"
            ),
        )

    now = datetime.now(timezone.utc)  # noqa: UP017

    # Build the UPDATE with optional metadata fields
    is_terminal = target_status in ("completed", "failed")
    update_fields: list[str] = [
        "status = $2",
        "updated_at = $3",
    ]
    update_values: list = [
        job_id,
        target_status,
        now,
    ]
    param_index = 4  # $4 onwards

    # Optional metadata fields
    metadata_fields = {
        "branch_name": body.branch_name,
        "commit_sha": body.commit_sha,
        "mr_url": body.mr_url,
        "diff": body.diff,
    }
    for field_name, field_value in metadata_fields.items():
        if field_value is not None:
            update_fields.append(f"{field_name} = ${param_index}")
            update_values.append(field_value)
            param_index += 1

    if is_terminal:
        update_fields.append(f"completed_at = ${param_index}")
        update_values.append(now)
        param_index += 1

    if target_status == "failed" and body.failure_reason is not None:
        update_fields.append(f"failure_reason = ${param_index}")
        update_values.append(body.failure_reason)
        param_index += 1

    # Record the completion event
    event_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO job_events "
        "(id, job_id, event_type, actor, details, previous_status, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        event_id,
        job_id,
        target_status,
        "api",
        f"Job completed via /complete endpoint (target={target_status})",
        current_status,
        now,
    )

    # Execute the update
    sql = (
        "UPDATE gateway_jobs SET "
        + ", ".join(update_fields)
        + f" WHERE id = $1"
    )
    await conn.execute(sql, *update_values)

    logger.info(
        "Job %s transitioned %s → %s via /complete",
        job_id,
        current_status,
        target_status,
    )

    # Fire webhooks asynchronously — non-blocking
    asyncio.create_task(
        dispatch_webhooks(
            pool,
            job_id,
            f"job.{target_status}",
            {
                "job_id": str(job_id),
                "event_type": f"job.{target_status}",
                "status": target_status,
                "current_status": current_status,
            },
        )
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


@router.post("/jobs/{job_id}/abort", response_model=JobResponse)
async def abort_job(
    job_id: uuid.UUID,
    request: Request,
    conn: asyncpg.Connection = Depends(get_session),
    executor: ExecutorPlugin = Depends(get_executor),
    opencode_client: OpenCodeClientProtocol | None = Depends(get_opencode_client),
) -> JobResponse:
    """Abort a job by cancelling its OpenCode session and marking it aborted.

    If the job has an active OpenCode session the endpoint transitions the job
    to ``aborting``, calls :meth:`OpenCodeClientProtocol.abort_session`, and on
    success marks the job ``aborted``.  When the session is unreachable the
    failure is logged at WARNING level and the job transitions to ``aborted``
    anyway (best-effort); executor cleanup (cancel_job, stop_opencode,
    cleanup_workspace) follows.

    Jobs without a session (e.g. still *pending*) are immediately marked
    ``aborted``.  Jobs already in ``aborted`` state return 200 idempotently.
    """
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    current_status = JobStatus(row["status"])

    # Idempotent — already aborted, return as-is
    if current_status == JobStatus.ABORTED:
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
            commit_sha=row.get("commit_sha"),
            mr_url=row.get("mr_url"),
            workflow_run_id=row.get("workflow_run_id"),
            diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
            failure_reason=row.get("failure_reason"),
        )

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
        except Exception as exc:
            logger.warning(
                "OpenCode session %s for job %s did not abort cleanly: %s",
                session_id,
                job_id,
                exc,
            )
            await conn.execute(
                "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 WHERE id = $1",
                job_id,
                datetime.now(timezone.utc),  # noqa: UP017
            )
            aborted = True
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
    parsed_ws_id = None
    if workspace_name:
        try:
            parsed_ws_id = uuid.UUID(workspace_name)
        except (ValueError, TypeError):
            logger.warning("Invalid workspace_name for job %s: %r", job_id, workspace_name)

    executor_job_id_raw = row.get("executor_job_id")
    executor_job_id: int | None = None
    if executor_job_id_raw is not None:
        try:
            executor_job_id = int(executor_job_id_raw)
        except (ValueError, TypeError):
            logger.warning("Invalid executor_job_id for job %s: %r", job_id, executor_job_id_raw)

    # Cancel in-flight AWX job if we have any way to reach it.
    if parsed_ws_id is not None or executor_job_id is not None:
        cancel_req = CancelJobRequest(
            workspace_id=parsed_ws_id,
            executor_job_id=executor_job_id,
        )
        try:
            await executor.cancel_job(cancel_req)
        except Exception:
            logger.exception("Failed to cancel job for workspace %s (job %s)", parsed_ws_id, job_id)

    # stop_opencode and cleanup_workspace still require a known workspace.
    if parsed_ws_id is not None:
        try:
            await executor.stop_opencode(
                StopOpencodeRequest(
                    workspace_id=parsed_ws_id,
                    gateway_job_id=job_id,
                )
            )
        except Exception:
            logger.exception("Failed to stop OpenCode Serve for job %s (workspace %s)", job_id, parsed_ws_id)
        try:
            await executor.cleanup_workspace(
                CleanupWorkspaceRequest(
                    workspace_id=parsed_ws_id,
                    gateway_job_id=job_id,
                )
            )
        except Exception:
            logger.exception("Failed to clean up workspace for job %s (workspace %s)", job_id, parsed_ws_id)

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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    request: Request,
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
        commit_sha=row.get("commit_sha"),
        mr_url=row.get("mr_url"),
        workflow_run_id=row.get("workflow_run_id"),
        diff_url=str(request.url_for("get_job_diff", job_id=str(job_id))),
        failure_reason=row.get("failure_reason"),
    )


class JobListResponse(BaseModel):
    """Response body for the GET /jobs listing endpoint."""

    items: list[JobResponse]
    total: int
    limit: int
    offset: int


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    request: Request,
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
            commit_sha=row.get("commit_sha"),
            mr_url=row.get("mr_url"),
            workflow_run_id=row.get("workflow_run_id"),
            diff_url=str(request.url_for("get_job_diff", job_id=str(row["id"]))),
            failure_reason=row.get("failure_reason"),
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
    opencode_client: OpenCodeClientProtocol | None = Depends(get_opencode_client),
) -> JSONResponse:
    """Return the diff for a completed job by fetching it from OpenCode Serve.

    Fetches the diff from the OpenCode Serve instance via the stored
    ``opencode_session_id``.  Returns 404 when the job or its session is
    not found, and 503 when the OpenCode Serve instance is unreachable.
    """
    row = await conn.fetchrow(
        "SELECT id, status, opencode_session_id FROM gateway_jobs WHERE id = $1",
        job_id,
    )

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    session_id: str | None = row.get("opencode_session_id")
    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No session associated with this job",
        )

    if opencode_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenCode Serve client not available",
        )

    try:
        diff_response = await opencode_client.get_session_diff(session_id)
    except Exception:
        logger.exception(
            "Failed to fetch diff for job %s (session %s)",
            job_id,
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail="OpenCode Serve unreachable",
        )

    return JSONResponse(
        status_code=200,
        content={
            "job_id": str(row["id"]),
            "diff": diff_response.diff,
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
            status_code=404,
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
