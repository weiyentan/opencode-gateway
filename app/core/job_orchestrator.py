"""Job orchestration — lifecycle logic for creating and aborting jobs.

Extracted from ``app/api/jobs.py`` so that the orchestration can be tested
and reasoned about independently of the HTTP transport layer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from app.core.config import get_settings
from app.core.lifecycle import can_transition
from app.core.models.job import JobStatus
from app.executors import ExecutorPlugin
from app.executors.models import (
    CleanupWorkspaceRequest,
    CreateWorkspaceRequest,
    StartOpencodeRequest,
    StopOpencodeRequest,
)
from app.opencode.protocol import OpenCodeClientProtocol
from app.policy import ObservationBasedPolicy, PolicyViolation

logger = logging.getLogger(__name__)


class JobNotFoundError(Exception):
    """Raised when a job is not found by ID."""


class InvalidJobTransitionError(Exception):
    """Raised when a requested job status transition is not allowed."""


class SessionAbortError(Exception):
    """Raised when the OpenCode session could not be aborted."""


_FETCH_COLS = (
    "id, repo_url, task_summary, status, created_at, updated_at, completed_at, "
    "opencode_session_id, diff, workspace_name"
)


async def _fetch_job(conn: asyncpg.Connection, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Fetch a single job row by ID."""
    return await conn.fetchrow(
        f"SELECT {_FETCH_COLS} FROM gateway_jobs WHERE id = $1",
        job_id,
    )


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
        datetime.now(timezone.utc),
    )


def _resolve_workspace_id(workspace_name: Optional[str]) -> Optional[uuid.UUID]:
    """Parse the *workspace_name* column (stored as a string UUID) back to a UUID."""
    if not workspace_name:
        return None
    try:
        return uuid.UUID(workspace_name)
    except (ValueError, TypeError):
        logger.warning("Invalid workspace_name: %r", workspace_name)
        return None


async def _resolve_runner_id_for_workspace(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
) -> Optional[str]:
    """Resolve a workspace ID to its runner's text identifier.

    Looks up the workspace's ``runner_id`` (UUID FK) in the workspaces
    table, then resolves it to the ``runners.runner_id`` text field.
    Returns ``None`` when the workspace or runner is not found.
    """
    # Get the runner UUID from the workspace row.
    ws_row = await conn.fetchrow(
        "SELECT runner_id FROM workspaces WHERE id = $1",
        workspace_id,
    )
    if ws_row is None or ws_row.get("runner_id") is None:
        logger.debug(
            "No runner_id for workspace %s — skipping policy check",
            workspace_id,
        )
        return None

    runner_uuid = ws_row["runner_id"]

    # Resolve to the text runner_id.
    runner_row = await conn.fetchrow(
        "SELECT runner_id FROM runners WHERE id = $1",
        runner_uuid,
    )
    if runner_row is None:
        logger.warning(
            "Runner UUID %s referenced by workspace %s not found",
            runner_uuid,
            workspace_id,
        )
        return None

    return runner_row["runner_id"]


async def execute_create_job(
    conn: asyncpg.Connection,
    executor: ExecutorPlugin,
    opencode_client: Optional[OpenCodeClientProtocol],
    repo_url: str,
    task_summary: str,
) -> dict:
    """Create and dispatch a job through its lifecycle.

    Returns a dict with the job's final state (id, status, workspace_id, etc.)
    so the API layer can construct the response model.

    Raises :class:`PolicyViolation` when the pre-flight check rejects the job.
    """
    job_id = uuid.uuid4()
    settings = get_settings()

    # 1. Insert the job record in pending state
    await conn.execute(
        "INSERT INTO gateway_jobs (id, repo_url, task_summary, status, executor_type) "
        "VALUES ($1, $2, $3, 'pending', $4)",
        job_id,
        repo_url,
        task_summary,
        executor.name,
    )

    # 2. Validate and perform pending → running transition
    if not can_transition(JobStatus.PENDING, JobStatus.RUNNING):
        logger.error(
            "Lifecycle rejected transition pending→running for job %s", job_id
        )
        raise RuntimeError("Internal state machine error")
    await conn.execute(
        "UPDATE gateway_jobs SET status = 'running', updated_at = $2 WHERE id = $1",
        job_id,
        datetime.now(timezone.utc),
    )

    new_workspace_id = None  # Track for error handling

    try:
        # 3. Create workspace and store its ID (before potentially-failing start)
        ws_response = await executor.create_workspace(
            CreateWorkspaceRequest(
                repo_url=repo_url,
                job_id=job_id,
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

        # 3b. Run pre-flight policy check — resolves workspace → runner and
        # inspects runner observations for disk/memory pressure.
        runner_text_id = await _resolve_runner_id_for_workspace(conn, new_workspace_id)
        if runner_text_id is not None:
            policy = ObservationBasedPolicy(settings)
            await policy.check(runner_text_id, conn=conn)

        # 4. Start OpenCode Serve
        start_response = await executor.start_opencode(
            StartOpencodeRequest(
                workspace_id=new_workspace_id,
                workspace_path=ws_response.workspace_path,
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
        now = datetime.now(timezone.utc)
        diff_summary = f"Job completed: {task_summary}"
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'completed', updated_at = $2, "
            "completed_at = $3, diff = $4 WHERE id = $1",
            job_id,
            now,
            now,
            diff_summary,
        )

        # 6. Set workspace cleanup_after for successful completion
        if new_workspace_id is not None:
            await _set_workspace_cleanup_after(
                conn, new_workspace_id, settings.cleanup_success_retention_hours
            )

        # 7. Fetch and persist the diff (non-blocking — failure does not fail the job)
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
        # Policy rejected the job before dispatch — mark as failed and re-raise
        # the 503 so the caller can receive it.
        logger.warning(
            "Policy check rejected job %s: %s",
            job_id,
            "disk/memory pressure",
        )
        await conn.execute(
            "UPDATE gateway_jobs SET status = 'failed', updated_at = $2 WHERE id = $1",
            job_id,
            datetime.now(timezone.utc),
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
            datetime.now(timezone.utc),
        )

        # Set workspace cleanup_after for failure
        row = await _fetch_job(conn, job_id)
        if row is not None:
            ws_id = _resolve_workspace_id(row.get("workspace_name"))
            if ws_id is not None:
                await _set_workspace_cleanup_after(
                    conn, ws_id, settings.cleanup_failure_retention_hours
                )

    # Return final state
    row = await _fetch_job(conn, job_id)
    return {
        "id": row["id"],
        "repo_url": row["repo_url"],
        "task_summary": row["task_summary"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "opencode_session_id": row.get("opencode_session_id"),
        "diff": row.get("diff"),
    }


async def execute_abort_job(
    conn: asyncpg.Connection,
    executor: ExecutorPlugin,
    opencode_client: Optional[OpenCodeClientProtocol],
    job_id: uuid.UUID,
) -> dict:
    """Abort a running job by cancelling its OpenCode session.

    Returns the job's updated state as a dict.

    Raises:
        JobNotFoundError: The job does not exist.
        InvalidJobTransitionError: The job cannot transition to aborting.
        SessionAbortError: The OpenCode session could not be aborted.
    """
    row = await _fetch_job(conn, job_id)
    if row is None:
        raise JobNotFoundError(f"Job {job_id} not found")

    current_status = JobStatus(row["status"])

    # Allow retry from aborting; otherwise validate via centralised transition table
    if current_status != JobStatus.ABORTING and not can_transition(
        current_status, JobStatus.ABORTING
    ):
        raise InvalidJobTransitionError(
            f"Job is in '{row['status']}' state; "
            f"cannot transition to aborting"
        )

    now = datetime.now(timezone.utc)
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
                datetime.now(timezone.utc),
            )
            aborted = True
        except Exception:
            logger.exception(
                "Failed to abort OpenCode session %s for job %s",
                session_id,
                job_id,
            )
            raise SessionAbortError(
                "OpenCode Serve unreachable; job remains in aborting state"
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
    return {
        "id": row["id"],
        "repo_url": row["repo_url"],
        "task_summary": row["task_summary"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "opencode_session_id": row.get("opencode_session_id"),
        "diff": row.get("diff"),
    }
