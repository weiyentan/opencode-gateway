"""Integration tests for Job CRUD operations against a real Postgres database.

Tests create, read, update, and status transitions for gateway_jobs.
"""

from __future__ import annotations

# ruff: noqa: UP017 — timezone.utc is intentional; env runs Python 3.9
import uuid
from datetime import datetime, timezone

import pytest

from tests.integration.conftest import create_job

pytestmark = pytest.mark.integration


class TestJobCreate:
    """End-to-end job creation and read-back."""

    async def test_create_job_persists_to_database(self, db_conn):
        """A created job should be queryable from gateway_jobs."""
        jid = uuid.uuid4()
        repo_url = "https://github.com/test-org/create-test.git"
        summary = "Create integration test"

        await db_conn.execute(
            "INSERT INTO gateway_jobs (id, repo_url, task_summary, status, "
            "executor_type, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'pending', 'local', $4, $4)",
            jid,
            repo_url,
            summary,
            datetime.now(timezone.utc),
        )

        row = await db_conn.fetchrow(
            "SELECT id, repo_url, task_summary, status, executor_type, "
            "created_at, updated_at, completed_at "
            "FROM gateway_jobs WHERE id = $1",
            jid,
        )
        assert row is not None
        assert row["id"] == jid
        assert row["repo_url"] == repo_url
        assert row["task_summary"] == summary
        assert row["status"] == "pending"
        assert row["executor_type"] == "local"
        assert row["created_at"] is not None
        assert row["updated_at"] is not None
        assert row["completed_at"] is None

    async def test_create_multiple_jobs_yields_distinct_ids(self, db_conn):
        """Each INSERT should produce a unique job ID."""
        jid1 = await create_job(db_conn, task_summary="Job 1")
        jid2 = await create_job(db_conn, task_summary="Job 2")
        jid3 = await create_job(db_conn, task_summary="Job 3")

        assert jid1 != jid2
        assert jid2 != jid3
        assert jid1 != jid3

        count = await db_conn.fetchval("SELECT count(*) FROM gateway_jobs")
        assert count == 3


class TestJobRead:
    """Retrieving jobs from the database."""

    async def test_fetch_existing_job_returns_full_record(self, db_conn):
        """fetchrow by ID returns all expected columns."""
        jid = await create_job(
            db_conn,
            repo_url="https://github.com/test-org/read-test.git",
            task_summary="Read integration test",
        )

        row = await db_conn.fetchrow(
            "SELECT id, repo_url, task_summary, status, executor_type, "
            "created_at, updated_at, completed_at, diff, workspace_name, "
            "opencode_session_id, runner_id, opencode_url, executor_job_id "
            "FROM gateway_jobs WHERE id = $1",
            jid,
        )
        assert row is not None
        assert row["id"] == jid
        assert row["repo_url"] == "https://github.com/test-org/read-test.git"
        assert row["task_summary"] == "Read integration test"
        assert row["status"] == "pending"
        assert row["executor_type"] == "local"
        assert row["diff"] is None
        assert row["workspace_name"] is None

    async def test_fetch_nonexistent_job_returns_none(self, db_conn):
        """fetchrow for a non-existent ID should return None."""
        row = await db_conn.fetchrow(
            "SELECT id FROM gateway_jobs WHERE id = $1",
            uuid.uuid4(),
        )
        assert row is None

    async def test_fetch_with_specific_status(self, db_conn):
        """Can filter jobs by status."""
        await create_job(db_conn, status="pending", task_summary="P1")
        await create_job(db_conn, status="running", task_summary="R1")
        await create_job(db_conn, status="completed", task_summary="C1")
        await create_job(db_conn, status="failed", task_summary="F1")

        pending = await db_conn.fetchval(
            "SELECT count(*) FROM gateway_jobs WHERE status = 'pending'"
        )
        running = await db_conn.fetchval(
            "SELECT count(*) FROM gateway_jobs WHERE status = 'running'"
        )
        completed = await db_conn.fetchval(
            "SELECT count(*) FROM gateway_jobs WHERE status = 'completed'"
        )
        failed = await db_conn.fetchval(
            "SELECT count(*) FROM gateway_jobs WHERE status = 'failed'"
        )

        assert pending == 1
        assert running == 1
        assert completed == 1
        assert failed == 1


class TestJobUpdate:
    """Updating job status and fields."""

    async def test_transition_pending_to_running(self, db_conn):
        """A pending job can be transitioned to running."""
        jid = await create_job(db_conn, status="pending")

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'running', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        row = await db_conn.fetchrow(
            "SELECT status, updated_at FROM gateway_jobs WHERE id = $1", jid
        )
        assert row["status"] == "running"

    async def test_transition_running_to_completed(self, db_conn):
        """A running job can be marked completed with completed_at."""
        jid = await create_job(db_conn, status="running")

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'completed', completed_at = $2, "
            "updated_at = $3, diff = $4 WHERE id = $1",
            jid,
            now,
            now,
            "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new",
        )

        row = await db_conn.fetchrow(
            "SELECT status, completed_at, diff FROM gateway_jobs WHERE id = $1", jid
        )
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["diff"] == "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"

    async def test_transition_to_failed_preserves_fields(self, db_conn):
        """When a job fails, existing fields are preserved."""
        jid = await create_job(db_conn, status="running")

        # Associate a workspace
        await db_conn.execute(
            "UPDATE gateway_jobs SET workspace_name = $2 WHERE id = $1",
            jid,
            str(uuid.uuid4()),
        )

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'failed', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        row = await db_conn.fetchrow(
            "SELECT status, workspace_name FROM gateway_jobs WHERE id = $1", jid
        )
        assert row["status"] == "failed"
        assert row["workspace_name"] is not None  # preserved


class TestJobApprovalFlow:
    """Approval lifecycle integration tests."""

    async def test_approve_transitions_needs_approval_to_running(self, db_conn):
        """Approving a job transitions needs_approval → running and logs approval."""
        jid = await create_job(db_conn, status="needs_approval")

        now = datetime.now(timezone.utc)
        approval_id = uuid.uuid4()

        # Insert approval record
        await db_conn.execute(
            "INSERT INTO approvals "
            "(id, job_id, requested_by, requested_action, approval_type, "
            "approved_by, status, created_at, decided_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            approval_id,
            jid,
            "system",
            "run_job",
            "manual",
            "api",
            "approved",
            now,
            now,
        )

        # Transition the job
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'running', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        # Verify job state
        job_row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", jid
        )
        assert job_row["status"] == "running"

        # Verify approval record
        approval_rows = await db_conn.fetch(
            "SELECT status, approved_by, requested_action FROM approvals "
            "WHERE job_id = $1",
            jid,
        )
        assert len(approval_rows) == 1
        assert approval_rows[0]["status"] == "approved"
        assert approval_rows[0]["approved_by"] == "api"
        assert approval_rows[0]["requested_action"] == "run_job"

    async def test_reject_transitions_needs_approval_to_rejected(self, db_conn):
        """Rejecting a job transitions needs_approval → rejected."""
        jid = await create_job(db_conn, status="needs_approval")

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "INSERT INTO approvals "
            "(id, job_id, requested_by, requested_action, approval_type, "
            "approved_by, status, created_at, decided_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            uuid.uuid4(),
            jid,
            "system",
            "run_job",
            "manual",
            "api",
            "rejected",
            now,
            now,
        )

        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'rejected', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        job_row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", jid
        )
        assert job_row["status"] == "rejected"


class TestJobAbortFlow:
    """Abort lifecycle integration tests."""

    async def test_abort_without_session_transitions_directly(self, db_conn):
        """Aborting a pending job without a session goes directly to aborted."""
        jid = await create_job(db_conn, status="pending")

        now = datetime.now(timezone.utc)
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        # Record abort event
        await db_conn.execute(
            "INSERT INTO job_events "
            "(id, job_id, event_type, actor, details, previous_status, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            jid,
            "aborted",
            "api",
            "Job aborted",
            "pending",
            now,
        )

        job_row = await db_conn.fetchrow(
            "SELECT status FROM gateway_jobs WHERE id = $1", jid
        )
        assert job_row["status"] == "aborted"

        # Verify event was recorded
        event_rows = await db_conn.fetch(
            "SELECT event_type, actor, previous_status FROM job_events "
            "WHERE job_id = $1",
            jid,
        )
        assert len(event_rows) == 1
        assert event_rows[0]["event_type"] == "aborted"
        assert event_rows[0]["actor"] == "api"
        assert event_rows[0]["previous_status"] == "pending"

    async def test_abort_with_session_goes_through_aborting(self, db_conn):
        """Aborting a running job goes through aborting → aborted."""
        jid = await create_job(
            db_conn,
            status="running",
        )
        session_id = "sess-int-001"

        # Set the session
        await db_conn.execute(
            "UPDATE gateway_jobs SET opencode_session_id = $2 WHERE id = $1",
            jid,
            session_id,
        )

        now = datetime.now(timezone.utc)

        # Transition to aborting
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'aborting', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        # Then to aborted (after session abort succeeds)
        await db_conn.execute(
            "UPDATE gateway_jobs SET status = 'aborted', updated_at = $2 "
            "WHERE id = $1",
            jid,
            now,
        )

        row = await db_conn.fetchrow(
            "SELECT status, opencode_session_id FROM gateway_jobs WHERE id = $1",
            jid,
        )
        assert row["status"] == "aborted"
        assert row["opencode_session_id"] == session_id
