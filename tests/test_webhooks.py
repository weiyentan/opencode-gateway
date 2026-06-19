"""Tests for webhook registration, listing, deletion, dispatch, and signatures."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.api.jobs import _get_pool
from app.api.webhooks import _compute_signature
from app.core.factory import create_app
from app.db.session import get_session
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
#  Webhook CRUD endpoints
# ══════════════════════════════════════════════════════════════════════════


def _make_webhook_client(mock_conn: AsyncMock) -> AsyncClient:
    """Build a test client with webhook and pool dependencies overridden."""
    from tests.conftest import _TEST_API_KEY

    app = create_app()
    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[_get_pool] = lambda: mock_pool

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
    )


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
        """When a job completes successfully, dispatch_webhooks is called."""
        job_id = uuid.uuid4()  # This UUID represents what the DB row will return

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
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
            elif "UPDATE gateway_jobs SET status = 'completed'" in sql:
                row_data["status"] = "completed"
                row_data["completed_at"] = "2026-06-14T00:00:00Z"

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

            # dispatch_webhooks should have been called
            mock_dispatch.assert_called()
            call_args = mock_dispatch.call_args[0]
            # call_args: (pool, job_id, event_type, payload)
            assert call_args[2] == "job.completed"
            # The job_id in the payload should match the one assigned by create_job
            assert call_args[3]["job_id"] == str(captured_job_id)
            assert call_args[3]["event_type"] == "job.completed"
            assert call_args[3]["status"] == "completed"

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
            if "UPDATE gateway_jobs SET status = 'running'" in sql:
                row_data["status"] = "running"
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
            assert call_args[3]["job_id"] == str(captured_job_id)
            assert call_args[3]["event_type"] == "job.failed"
            assert call_args[3]["status"] == "failed"
