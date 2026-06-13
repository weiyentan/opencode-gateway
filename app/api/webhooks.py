"""Webhook API endpoints — register, list, delete webhooks and dispatch callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.db.session import DatabasePool, get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


# ── Request / Response models ───────────────────────────────────────────


class WebhookCreateRequest(BaseModel):
    """Request body for POST /webhooks."""

    url: str = Field(min_length=1, description="Callback URL to POST job events to")
    events: list[str] = Field(
        default_factory=lambda: ["job.completed", "job.failed"],
        description="Event types that trigger this webhook",
    )
    secret: str | None = Field(
        default=None,
        description="HMAC secret for signing payloads; auto-generated if omitted",
    )


class WebhookResponse(BaseModel):
    """Response body for webhook endpoints."""

    id: uuid.UUID
    url: str
    events: list[str]
    created_at: str


# ── Internal helpers ────────────────────────────────────────────────────


def _compute_signature(secret: str, payload: dict) -> str:
    """Compute HMAC-SHA256 hex digest for a webhook payload."""
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256)
    return mac.hexdigest()


async def _fetch_webhook_rows(conn: asyncpg.Connection, event_type: str):
    """Query all webhooks whose *events* array contains *event_type*."""
    return await conn.fetch(
        "SELECT id, url, secret FROM webhooks WHERE $1 = ANY(events)",
        event_type,
    )


# ── Webhook dispatcher (called by jobs.py) ──────────────────────────────


async def dispatch_webhooks(
    pool: DatabasePool,
    job_id: uuid.UUID,
    event_type: str,
    job_payload: dict,
) -> None:
    """Fire all matching webhooks for a job event.

    Designed to run as a background task via :func:`asyncio.create_task`.
    Errors from individual webhook POSTs are logged and swallowed — they
    never propagate to the caller, so slow or failing webhooks do not
    block job completion.
    """
    if pool.pool is None:
        logger.warning("Cannot dispatch webhooks: no database pool")
        return

    try:
        async with pool.pool.acquire() as conn:
            rows = await _fetch_webhook_rows(conn, event_type)
    except Exception:
        logger.exception(
            "Failed to query webhooks for job %s (event: %s)", job_id, event_type
        )
        return

    if not rows:
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        for row in rows:
            webhook_id = row["id"]
            webhook_url = row["url"]
            secret = row["secret"]

            signature = _compute_signature(secret, job_payload)
            headers = {
                "Content-Type": "application/json",
                "X-Signature": signature,
            }

            try:
                resp = await client.post(
                    webhook_url,
                    json=job_payload,
                    headers=headers,
                )
                resp.raise_for_status()
                logger.debug(
                    "Webhook %s delivered to %s (status %s)",
                    webhook_id,
                    webhook_url,
                    resp.status_code,
                )
            except Exception:
                logger.exception(
                    "Webhook %s delivery failed (url=%s, job=%s)",
                    webhook_id,
                    webhook_url,
                    job_id,
                )


# ── CRUD endpoints ──────────────────────────────────────────────────────


@router.post("/webhooks", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreateRequest,
    conn: asyncpg.Connection = Depends(get_session),
) -> WebhookResponse:
    """Register a new webhook callback."""
    webhook_id = uuid.uuid4()
    secret = body.secret or uuid.uuid4().hex

    now = await conn.fetchval("SELECT NOW()")
    await conn.execute(
        "INSERT INTO webhooks (id, url, events, secret, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        webhook_id,
        body.url,
        body.events,
        secret,
        now,
    )

    return WebhookResponse(
        id=webhook_id,
        url=body.url,
        events=body.events,
        created_at=str(now),
    )


@router.get("/webhooks", response_model=list[WebhookResponse])
async def list_webhooks(
    conn: asyncpg.Connection = Depends(get_session),
) -> list[WebhookResponse]:
    """List all registered webhooks."""
    rows = await conn.fetch(
        "SELECT id, url, events, created_at FROM webhooks ORDER BY created_at ASC"
    )
    return [
        WebhookResponse(
            id=row["id"],
            url=row["url"],
            events=row["events"],
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> None:
    """Remove a registered webhook. Returns 404 if not found."""
    result = await conn.execute(
        "DELETE FROM webhooks WHERE id = $1",
        webhook_id,
    )
    # asyncpg execute returns a command tag like "DELETE 0" or "DELETE 1"
    tag = result.split() if isinstance(result, str) else ["DELETE", "0"]
    if tag[-1] == "0":
        raise HTTPException(status_code=404, detail="Webhook not found")
