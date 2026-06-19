"""Tests for webhook registration, listing, deletion, dispatch, and signatures."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.api.webhooks import _compute_signature, build_job_completed_payload
from tests.conftest import create_client, mock_row

# ══════════════════════════════════════════════════════════════════════════
#  Signature computation
# ══════════════════════════════════════════════════════════════════════════


class TestSignature:
    """Tests for HMAC-SHA256 signature computation."""

    def test_compute_signature_is_deterministic(self):
        """Same secret + same payload → same signature."""
        secret = "my-secret-key"
        payload = {"job_id": "abc", "event_type": "job.completed"}

        sig1 = _compute_signature(secret, payload)
        sig2 = _compute_signature(secret, payload)

        assert sig1 == sig2
        assert len(sig1) == 64  # SHA-256 hex digest

    def test_compute_signature_differs_with_different_secret(self):
        """Different secret → different signature."""
        payload = {"job_id": "abc", "event_type": "job.completed"}

        sig1 = _compute_signature("secret-a", payload)
        sig2 = _compute_signature("secret-b", payload)

        assert sig1 != sig2

    def test_compute_signature_differs_with_different_payload(self):
        """Different payload → different signature."""
        secret = "my-secret"

        sig1 = _compute_signature(secret, {"job_id": "a", "event_type": "job.completed"})
        sig2 = _compute_signature(secret, {"job_id": "b", "event_type": "job.completed"})

        assert sig1 != sig2

    def test_compute_signature_is_hex_string(self):
        """Signature is a lowercase hex string of correct length."""
        sig = _compute_signature("key", {"x": 1})
        assert isinstance(sig, str)
        assert sig == sig.lower()
        assert all(c in "0123456789abcdef" for c in sig)
        assert len(sig) == 64


# ══════════════════════════════════════════════════════════════════════════
#  Structured payload construction
# ══════════════════════════════════════════════════════════════════════════


class TestBuildJobCompletedPayload:
    """Tests for build_job_completed_payload — structured payload builder."""

    def test_returns_all_required_fields(self):
        """Payload contains every field specified in the issue-150 schema."""
        import uuid
        from datetime import datetime, timezone

        job_id = uuid.uuid4()
        job_row = {
            "id": job_id,
            "branch_name": "feat/awesome",
            "mr_url": "https://gitlab.com/org/repo/-/merge_requests/42",
            "opencode_session_id": "sess-abc-123",
            "task_summary": "Add awesome feature",
            "workflow_run_id": "wf-run-001",
        }

        payload = build_job_completed_payload(job_row, task_summary="Add awesome feature")

        assert payload["job_id"] == str(job_id)
        assert payload["status"] == "completed"
        assert payload["diff_url"] == f"/jobs/{job_id}/diff"
        assert payload["branch_name"] == "feat/awesome"
        assert payload["mr_url"] == "https://gitlab.com/org/repo/-/merge_requests/42"
        assert payload["session_id"] == "sess-abc-123"
        assert payload["task_summary"] == "Add awesome feature"
        assert payload["failure_reason"] is None
        assert payload["workflow_run_id"] == "wf-run-001"

        # Verify all 9 fields from the schema are present
        assert set(payload.keys()) == {
            "job_id",
            "status",
            "diff_url",
            "branch_name",
            "mr_url",
            "session_id",
            "task_summary",
            "failure_reason",
            "workflow_run_id",
        }

    def test_fields_default_to_none_when_missing(self):
        """When a field is absent from the job row, it should be None."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {"id": job_id, "task_summary": "Minimal row"}

        payload = build_job_completed_payload(job_row)

        assert payload["branch_name"] is None
        assert payload["mr_url"] is None
        assert payload["session_id"] is None
        assert payload["workflow_run_id"] is None
        assert payload["failure_reason"] is None
        assert payload["task_summary"] == "Minimal row"

    def test_diff_url_uses_job_id(self):
        """diff_url is a relative path derived from the job UUID."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {"id": job_id, "task_summary": "Test"}

        payload = build_job_completed_payload(job_row)

        assert payload["diff_url"] == f"/jobs/{job_id}/diff"

    def test_task_summary_fallback_to_row(self):
        """When task_summary is not passed, fall back to job_row."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {"id": job_id, "task_summary": "From row"}

        payload = build_job_completed_payload(job_row)

        assert payload["task_summary"] == "From row"

    def test_task_summary_overrides_row(self):
        """Explicit task_summary parameter overrides job_row value."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {"id": job_id, "task_summary": "From row"}

        payload = build_job_completed_payload(job_row, task_summary="Explicit")

        assert payload["task_summary"] == "Explicit"

    def test_session_id_is_opencode_session_id(self):
        """session_id maps from opencode_session_id column."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {
            "id": job_id,
            "opencode_session_id": "sess-xyz",
            "task_summary": "Test",
        }

        payload = build_job_completed_payload(job_row)

        assert payload["session_id"] == "sess-xyz"

    def test_workflow_run_id_is_optional(self):
        """workflow_run_id can be None when absent."""
        import uuid

        job_id = uuid.uuid4()
        job_row = {"id": job_id, "task_summary": "Test"}

        payload = build_job_completed_payload(job_row)

        assert payload["workflow_run_id"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Webhook CRUD endpoints
# ══════════════════════════════════════════════════════════════════════════


def _make_webhook_client(mock_conn: AsyncMock) -> AsyncClient:
    """Build a test client with webhook and pool dependencies overridden."""
    return create_client(mock_conn)


class TestCreateWebhook:
    """Tests for POST /webhooks."""

    @pytest.mark.asyncio
    async def test_create_webhook_returns_201_with_default_events(self):
        """POST /webhooks with URL only returns 201 with default events."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value="2026-06-14 00:00:00+00")
        mock_conn.execute = AsyncMock(return_value="INSERT 1")

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/webhooks",
                json={"url": "https://example.com/callback"},
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert "id" in data
        assert data["url"] == "https://example.com/callback"
        assert data["events"] == ["job.completed", "job.failed"]
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_create_webhook_with_custom_events(self):
        """POST /webhooks with custom events filter."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value="2026-06-14 00:00:00+00")
        mock_conn.execute = AsyncMock(return_value="INSERT 1")

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/webhooks",
                json={
                    "url": "https://example.com/callback",
                    "events": ["job.completed"],
                },
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["events"] == ["job.completed"]

    @pytest.mark.asyncio
    async def test_create_webhook_with_custom_secret(self):
        """POST /webhooks accepts a custom secret."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value="2026-06-14 00:00:00+00")
        mock_conn.execute = AsyncMock(return_value="INSERT 1")

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/webhooks",
                json={
                    "url": "https://example.com/callback",
                    "secret": "my-custom-secret",
                },
            )

        assert response.status_code == 201

        # Verify the secret was passed to the INSERT
        insert_call = mock_conn.execute.call_args
        # call_args[0] is the tuple of positional args:
        # (sql, id, url, events, secret, created_at)
        args = insert_call[0] if insert_call[0] else insert_call[1]
        assert args[4] == "my-custom-secret"  # noqa: RUF100

    @pytest.mark.asyncio
    async def test_create_webhook_empty_url_returns_422(self):
        """POST /webhooks with empty URL returns 422."""
        mock_conn = AsyncMock()
        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.post(
                "/webhooks",
                json={"url": ""},
            )

        assert response.status_code == 422


class TestListWebhooks:
    """Tests for GET /webhooks."""

    @pytest.mark.asyncio
    async def test_list_webhooks_returns_empty_list(self):
        """GET /webhooks with no webhooks returns empty list."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.get("/webhooks")

        assert response.status_code == 200
        data = response.json()["data"]
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_list_webhooks_returns_registered_webhooks(self):
        """GET /webhooks returns all registered webhooks."""
        webhook_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook_id,
                        "url": "https://example.com/callback",
                        "events": ["job.completed", "job.failed"],
                        "created_at": "2026-06-14 00:00:00+00",
                    }
                )
            ]
        )

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.get("/webhooks")

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["id"] == str(webhook_id)
        assert data[0]["url"] == "https://example.com/callback"
        assert data[0]["events"] == ["job.completed", "job.failed"]


class TestDeleteWebhook:
    """Tests for DELETE /webhooks/{id}."""

    @pytest.mark.asyncio
    async def test_delete_existing_webhook_returns_204(self):
        """DELETE /webhooks/{id} for existing webhook returns 204."""
        webhook_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 1")

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.delete(f"/webhooks/{webhook_id}")

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_nonexistent_webhook_returns_404(self):
        """DELETE /webhooks/{id} for non-existent webhook returns 404."""
        webhook_id = uuid.uuid4()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 0")

        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.delete(f"/webhooks/{webhook_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_invalid_uuid_returns_422(self):
        """DELETE /webhooks/{id} with malformed UUID returns 422."""
        mock_conn = AsyncMock()
        client = _make_webhook_client(mock_conn)

        async with client as c:
            response = await c.delete("/webhooks/not-a-uuid")

        assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════════
#  Webhook dispatch
# ══════════════════════════════════════════════════════════════════════════


class TestDispatchWebhooks:
    """Tests for dispatch_webhooks background task."""

    @pytest.mark.asyncio
    async def test_dispatch_no_matching_webhooks(self):
        """When no webhooks match the event type, no POSTs are made."""
        from app.api.webhooks import dispatch_webhooks

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        payload = {"job_id": str(job_id), "event_type": "job.completed"}

        await dispatch_webhooks(mock_pool, job_id, "job.completed", payload)

        # Should not have made any HTTP calls
        mock_conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_posts_to_matching_webhooks(self):
        """Matching webhooks receive signed POST requests."""
        from app.api.webhooks import _compute_signature, dispatch_webhooks

        webhook_id = uuid.uuid4()
        webhook_url = "https://example.com/webhook"
        secret = "test-secret-123"

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook_id,
                        "url": webhook_url,
                        "secret": secret,
                    }
                )
            ]
        )
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        payload = {"job_id": str(job_id), "event_type": "job.completed", "status": "completed"}

        with patch("app.api.webhooks.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__.return_value = mock_client_instance

            await dispatch_webhooks(mock_pool, job_id, "job.completed", payload)

            # Verify POST was made with correct URL, headers, and payload
            mock_client_instance.post.assert_awaited_once()
            call_args = mock_client_instance.post.call_args
            # call_args is (args, kwargs)
            assert call_args[0][0] == webhook_url

            # Verify X-Signature header
            expected_sig = _compute_signature(secret, payload)
            headers = call_args[1]["headers"]
            assert headers["X-Signature"] == expected_sig
            assert headers["Content-Type"] == "application/json"

            # Verify JSON payload
            assert call_args[1]["json"] == payload

    @pytest.mark.asyncio
    async def test_dispatch_handles_failing_webhook_gracefully(self):
        """When one webhook fails, others still fire and no exception propagates."""
        from app.api.webhooks import dispatch_webhooks

        webhook1_id = uuid.uuid4()
        webhook2_id = uuid.uuid4()

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook1_id,
                        "url": "https://slow.example.com/hook",
                        "secret": "secret-1",
                    }
                ),
                mock_row(
                    {
                        "id": webhook2_id,
                        "url": "https://good.example.com/hook",
                        "secret": "secret-2",
                    }
                ),
            ]
        )
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        payload = {"job_id": str(job_id), "event_type": "job.completed"}

        with patch("app.api.webhooks.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()

            # First webhook fails, second succeeds
            async def _post(url, **kwargs):
                if "slow" in url:
                    raise OSError("Connection refused")
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                return resp

            mock_client_instance.post = AsyncMock(side_effect=_post)
            MockClient.return_value.__aenter__.return_value = mock_client_instance

            # Should not raise — errors are logged and swallowed
            await dispatch_webhooks(mock_pool, job_id, "job.completed", payload)

            # Both webhooks should have been attempted
            assert mock_client_instance.post.await_count == 2

    @pytest.mark.asyncio
    async def test_dispatch_returns_early_when_pool_is_none(self):
        """When pool.pool is None, dispatch exits early without error."""
        from app.api.webhooks import dispatch_webhooks

        mock_pool = MagicMock()
        mock_pool.pool = None

        job_id = uuid.uuid4()

        # Should not raise
        await dispatch_webhooks(
            mock_pool, job_id, "job.completed", {"job_id": str(job_id)}
        )

    @pytest.mark.asyncio
    async def test_dispatch_handles_db_query_failure(self):
        """When the DB query fails, dispatch logs the error and returns."""
        from app.api.webhooks import dispatch_webhooks

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=OSError("DB connection lost"))
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()

        # Should not raise
        await dispatch_webhooks(
            mock_pool, job_id, "job.completed", {"job_id": str(job_id)}
        )

    @pytest.mark.asyncio
    async def test_dispatch_only_fires_matching_event_type(self):
        """Only webhooks whose events array contains the event type are fired."""
        from app.api.webhooks import dispatch_webhooks

        webhook_completed_id = uuid.uuid4()

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        # When querying for "job.completed", only return the completed webhook
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook_completed_id,
                        "url": "https://completed.example.com/hook",
                        "secret": "secret-1",
                    }
                )
            ]
        )
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        payload = {"job_id": str(job_id), "event_type": "job.completed"}

        with patch("app.api.webhooks.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__.return_value = mock_client_instance

            await dispatch_webhooks(mock_pool, job_id, "job.completed", payload)

            # Only one webhook should be fired
            mock_client_instance.post.assert_awaited_once()
            call_args = mock_client_instance.post.call_args
            assert call_args[0][0] == "https://completed.example.com/hook"


# ══════════════════════════════════════════════════════════════════════════
#  Integration: webhook firing triggered from job completion
# ══════════════════════════════════════════════════════════════════════════


class TestWebhookIntegration:
    """Integration tests: webhook dispatch is triggered on job completion/failure."""

    @pytest.mark.asyncio
    async def test_job_completion_triggers_webhook_dispatch(self, mock_conn, mock_executor):
        """When a job completes successfully, dispatch_webhooks is called with structured payload."""
        job_id = uuid.uuid4()

        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Fix a bug",
            "status": "pending",
            "executor_type": "local",
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "completed_at": None,
            "opencode_session_id": "sess-abc-123",
            "diff": None,
            "workspace_name": None,
            "branch_name": None,
            "mr_url": None,
            "workflow_run_id": None,
        }

        # Track the job_id assigned by create_job (uuid.uuid4() inside the handler)
        captured_job_id: uuid.UUID | None = None

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "LEFT JOIN workspaces" in sql:
                    return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
                if "FROM runners WHERE id" in sql:
                    return mock_row({"runner_id": "test-runner"})
                if "FROM runner_observations" in sql:
                    return None
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            nonlocal captured_job_id
            if "INSERT INTO gateway_jobs" in sql and args and isinstance(args[0], uuid.UUID):
                captured_job_id = args[0]
                row_data["id"] = args[0]  # Update the row to match
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        with patch("app.api.jobs.dispatch_webhooks", AsyncMock()) as mock_dispatch:
            client = create_client(mock_conn, mock_executor=mock_executor)

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

            assert response.status_code == 201
            data = response.json()["data"]
            assert data["status"] == "completed"

            # dispatch_webhooks should have been called with job.completed event
            # and the structured payload
            mock_dispatch.assert_called()
            call_args = mock_dispatch.call_args[0]
            # call_args: (pool, job_id, event_type, payload)
            assert call_args[2] == "job.completed"

            # Verify structured payload fields
            payload = call_args[3]
            assert payload["job_id"] == str(captured_job_id)
            assert payload["status"] == "completed"
            assert payload["diff_url"] == f"/jobs/{captured_job_id}/diff"
            assert payload["branch_name"] is None
            assert payload["mr_url"] is None
            assert payload["session_id"] == "sess-abc-123"
            assert payload["task_summary"] == "Fix a bug"
            assert payload["failure_reason"] is None
            assert payload["workflow_run_id"] is None

            # Verify all expected keys are present
            assert set(payload.keys()) == {
                "job_id", "status", "diff_url", "branch_name", "mr_url",
                "session_id", "task_summary", "failure_reason", "workflow_run_id",
            }

    @pytest.mark.asyncio
    async def test_job_failure_triggers_webhook_dispatch(self):
        """When a job fails (executor error), dispatch_webhooks is called."""
        job_id = uuid.uuid4()

        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Fix a bug",
            "status": "pending",
            "executor_type": "local",
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "completed_at": None,
            "opencode_session_id": None,
            "diff": None,
            "workspace_name": None,
        }

        captured_job_id: uuid.UUID | None = None

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "LEFT JOIN workspaces" in sql:
                    return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
                if "FROM runners WHERE id" in sql:
                    return mock_row({"runner_id": "test-runner"})
                if "from runner_observations" in sql:
                    return None
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            nonlocal captured_job_id
            if "INSERT INTO gateway_jobs" in sql and args and isinstance(args[0], uuid.UUID):
                captured_job_id = args[0]
                row_data["id"] = args[0]
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Create a failing executor
        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Workspace creation failed")
        )

        with patch("app.api.jobs.dispatch_webhooks", AsyncMock()) as mock_dispatch:
            client = create_client(mock_conn, mock_executor=failing_executor)

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

            assert response.status_code == 201
            data = response.json()["data"]
            assert data["status"] == "failed"

            # dispatch_webhooks should have been called for failure
            mock_dispatch.assert_called()
            call_args = mock_dispatch.call_args[0]
            assert call_args[2] == "job.failed"
            payload = call_args[3]
            assert payload["job_id"] == str(captured_job_id)
            assert payload["event_type"] == "job.failed"
            assert payload["status"] == "failed"
            assert payload["repo_url"] == "https://github.com/org/repo"
            assert payload["task_summary"] == "Fix a bug"
            assert payload["completed_at"] is None
            assert "error" in payload


# ══════════════════════════════════════════════════════════════════════════
#  Completion callback payload structure
# ══════════════════════════════════════════════════════════════════════════


class TestCompletionCallbackPayload:
    """Tests for the structured webhook completion callback payload."""

    @pytest.mark.asyncio
    async def test_completion_payload_contains_all_required_fields(
        self, mock_conn, mock_executor
    ):
        """When a job completes, the webhook payload contains all expected fields."""
        job_id = uuid.uuid4()

        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Implement feature X",
            "status": "pending",
            "executor_type": "local",
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "completed_at": None,
            "opencode_session_id": None,
            "diff": None,
            "workspace_name": None,
        }

        captured_job_id: uuid.UUID | None = None

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "LEFT JOIN workspaces" in sql:
                    return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
                if "FROM runners WHERE id" in sql:
                    return mock_row({"runner_id": "test-runner"})
                if "FROM runner_observations" in sql:
                    return None
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            nonlocal captured_job_id
            if "INSERT INTO gateway_jobs" in sql and args and isinstance(args[0], uuid.UUID):
                captured_job_id = args[0]
                row_data["id"] = args[0]
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        with patch("app.api.jobs.dispatch_webhooks", AsyncMock()) as mock_dispatch:
            client = create_client(mock_conn, mock_executor=mock_executor)

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Implement feature X",
                    },
                )

            assert response.status_code == 201
            data = response.json()["data"]
            assert data["status"] == "completed"

            mock_dispatch.assert_called()
            call_args = mock_dispatch.call_args[0]

            # Verify event type is job.completed
            assert call_args[2] == "job.completed"

            payload = call_args[3]
            assert isinstance(payload, dict)

            # ── Required fields ───────────────────────────────────────
            # job_id: string UUID of the completed job
            assert "job_id" in payload
            assert payload["job_id"] == str(captured_job_id)
            assert isinstance(payload["job_id"], str)
            uuid.UUID(payload["job_id"])  # validates UUID format

            # event_type: identifies the lifecycle event
            assert payload["event_type"] == "job.completed"

            # status: final job status
            assert payload["status"] == "completed"

            # repo_url: repository the job operated on
            assert payload["repo_url"] == "https://github.com/org/repo"

            # task_summary: human-readable task description
            assert payload["task_summary"] == "Implement feature X"

            # completed_at: ISO-8601 timestamp of completion
            assert "completed_at" in payload
            assert isinstance(payload["completed_at"], str)

            # diff: summary or full diff of the changes
            assert "diff" in payload
            assert isinstance(payload["diff"], str)

    @pytest.mark.asyncio
    async def test_failure_payload_contains_error_field(
        self,
    ):
        """When a job fails, the webhook payload includes error details."""
        job_id = uuid.uuid4()

        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Fix a bug",
            "status": "pending",
            "executor_type": "local",
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "completed_at": None,
            "opencode_session_id": None,
            "diff": None,
            "workspace_name": None,
        }

        captured_job_id: uuid.UUID | None = None

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "LEFT JOIN workspaces" in sql:
                    return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
                if "FROM runners WHERE id" in sql:
                    return mock_row({"runner_id": "test-runner"})
                if "from runner_observations" in sql:
                    return None
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            nonlocal captured_job_id
            if "INSERT INTO gateway_jobs" in sql and args and isinstance(args[0], uuid.UUID):
                captured_job_id = args[0]
                row_data["id"] = args[0]
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'failed'" in sql:
                row_data["status"] = "failed"

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Create a failing executor
        failing_executor = AsyncMock()
        failing_executor.create_workspace = AsyncMock(
            side_effect=RuntimeError("Workspace creation failed")
        )

        with patch("app.api.jobs.dispatch_webhooks", AsyncMock()) as mock_dispatch:
            client = create_client(mock_conn, mock_executor=failing_executor)

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

            assert response.status_code == 201

            mock_dispatch.assert_called()
            call_args = mock_dispatch.call_args[0]

            # Verify event type is job.failed
            assert call_args[2] == "job.failed"

            payload = call_args[3]
            assert isinstance(payload, dict)

            # ── Required fields ───────────────────────────────────────
            assert payload["job_id"] == str(captured_job_id)
            assert payload["event_type"] == "job.failed"
            assert payload["status"] == "failed"
            assert payload["repo_url"] == "https://github.com/org/repo"
            assert payload["task_summary"] == "Fix a bug"

            # completed_at should be None for failed jobs
            assert payload["completed_at"] is None

            # error: descriptive failure message
            assert "error" in payload
            assert isinstance(payload["error"], str)
            assert len(payload["error"]) > 0
            assert "Executor dispatch failed" in payload["error"]
            assert payload["task_summary"] in payload["error"]

    @pytest.mark.asyncio
    async def test_failed_webhook_delivery_does_not_affect_job_status(
        self, mock_conn, mock_executor
    ):
        """Job status remains completed even when webhook delivery fails.

        Webhooks run as an ``asyncio.create_task`` background task — errors
        from the dispatch are logged and swallowed.  The job response must
        still succeed with status ``completed``.
        """
        job_id = uuid.uuid4()

        row_data = {
            "id": job_id,
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Fix a bug",
            "status": "pending",
            "executor_type": "local",
            "created_at": "2026-06-14T00:00:00Z",
            "updated_at": "2026-06-14T00:00:00Z",
            "completed_at": None,
            "opencode_session_id": None,
            "diff": None,
            "workspace_name": None,
        }

        captured_job_id: uuid.UUID | None = None

        async def _fetchrow(sql, *args):
            if "SELECT" in sql.upper():
                if "LEFT JOIN workspaces" in sql:
                    return mock_row({"id": uuid.UUID(int=0), "active_workspaces": 0})
                if "FROM runners WHERE id" in sql:
                    return mock_row({"runner_id": "test-runner"})
                if "FROM runner_observations" in sql:
                    return None
                if "gateway_jobs" in sql:
                    return mock_row(row_data)
                return None
            return None

        async def _execute(sql, *args):
            nonlocal captured_job_id
            if "INSERT INTO gateway_jobs" in sql and args and isinstance(args[0], uuid.UUID):
                captured_job_id = args[0]
                row_data["id"] = args[0]
            if "UPDATE gateway_jobs SET status = 'provisioning_workspace'" in sql:
                row_data["status"] = "provisioning_workspace"
            elif "UPDATE gateway_jobs SET status = 'starting_opencode'" in sql:
                row_data["status"] = "starting_opencode"
            elif "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"

        mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=_execute)

        # Patch dispatch_webhooks with a function that raises an exception
        # when its background task runs.  Since create_task wraps the
        # coroutine, the exception is captured in the task and does not
        # propagate to the request handler.
        async def _failing_dispatch(*_args, **_kwargs):
            raise RuntimeError("Webhook delivery network error")

        with patch("app.api.jobs.dispatch_webhooks", _failing_dispatch):
            client = create_client(mock_conn, mock_executor=mock_executor)

            async with client as c:
                response = await c.post(
                    "/jobs",
                    json={
                        "repo_url": "https://github.com/org/repo",
                        "task_summary": "Fix a bug",
                    },
                )

            # The job must succeed regardless of the webhook failure
            assert response.status_code == 201
            data = response.json()["data"]
            # Status should have progressed through to completed
            assert data["status"] in ("completed", "running")


# ══════════════════════════════════════════════════════════════════════════
#  Completion payload is HMAC-signed
# ══════════════════════════════════════════════════════════════════════════


class TestCompletionPayloadSignature:
    """Tests that the completion callback payload is HMAC-signed."""

    @pytest.mark.asyncio
    async def test_dispatch_sends_completion_payload_with_signature(self):
        """Completion payload is delivered with a valid X-Signature header."""
        from app.api.webhooks import _compute_signature, dispatch_webhooks

        webhook_id = uuid.uuid4()
        webhook_url = "https://hooks.example.com/completion"
        secret = "completion-secret"

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook_id,
                        "url": webhook_url,
                        "secret": secret,
                    }
                )
            ]
        )
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        # Use the same payload shape as the job completion handler in jobs.py
        completion_payload = {
            "job_id": str(job_id),
            "event_type": "job.completed",
            "status": "completed",
            "repo_url": "https://github.com/org/repo",
            "task_summary": "Complete the feature",
            "completed_at": "2026-06-19T12:00:00+00:00",
            "diff": "Job completed: Complete the feature",
        }

        with patch("app.api.webhooks.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__.return_value = mock_client_instance

            await dispatch_webhooks(
                mock_pool, job_id, "job.completed", completion_payload
            )

            # Verify the POST was made with correct headers
            mock_client_instance.post.assert_awaited_once()
            call_args = mock_client_instance.post.call_args
            assert call_args[0][0] == webhook_url

            # Verify X-Signature header matches HMAC of the completion payload
            headers = call_args[1]["headers"]
            assert "X-Signature" in headers
            expected_sig = _compute_signature(secret, completion_payload)
            assert headers["X-Signature"] == expected_sig
            assert headers["Content-Type"] == "application/json"

            # Verify the completion payload was sent as JSON
            assert call_args[1]["json"] == completion_payload


# ══════════════════════════════════════════════════════════════════════════
#  Multiple registered webhooks
# ══════════════════════════════════════════════════════════════════════════


class TestMultipleWebhooks:
    """Tests that multiple registered webhooks all receive the callback."""

    @pytest.mark.asyncio
    async def test_dispatch_fires_to_all_matching_webhooks(self):
        """All matching webhooks receive the same signed payload."""
        from app.api.webhooks import _compute_signature, dispatch_webhooks

        webhook_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        webhook_urls = [
            "https://hooks.example.com/one",
            "https://hooks.example.com/two",
            "https://hooks.example.com/three",
        ]
        secrets = ["secret-1", "secret-2", "secret-3"]

        mock_pool = MagicMock()
        mock_pool.pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row(
                    {
                        "id": webhook_ids[0],
                        "url": webhook_urls[0],
                        "secret": secrets[0],
                    }
                ),
                mock_row(
                    {
                        "id": webhook_ids[1],
                        "url": webhook_urls[1],
                        "secret": secrets[1],
                    }
                ),
                mock_row(
                    {
                        "id": webhook_ids[2],
                        "url": webhook_urls[2],
                        "secret": secrets[2],
                    }
                ),
            ]
        )
        mock_pool.pool.acquire.return_value.__aenter__.return_value = mock_conn

        job_id = uuid.uuid4()
        payload = {
            "job_id": str(job_id),
            "event_type": "job.completed",
            "status": "completed",
        }

        with patch("app.api.webhooks.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__.return_value = mock_client_instance

            await dispatch_webhooks(mock_pool, job_id, "job.completed", payload)

            # All three webhooks should have been called
            assert mock_client_instance.post.await_count == 3

            # Each webhook received the same payload with its own signature
            for i, url in enumerate(webhook_urls):
                call = mock_client_instance.post.await_args_list[i]
                assert call[0][0] == url
                headers = call[1]["headers"]
                assert headers["Content-Type"] == "application/json"
                expected_sig = _compute_signature(secrets[i], payload)
                assert headers["X-Signature"] == expected_sig
                assert call[1]["json"] == payload
