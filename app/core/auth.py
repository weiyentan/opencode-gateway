"""API key authentication middleware and collector token auth for the
OpenCode Gateway.

Two authentication layers:

1. **ApiKeyMiddleware** — Validates every request against the
   ``GATEWAY_API_KEY`` setting (with exempt paths for health checks).
   Used for admin/management endpoints.

2. **require_collector_token** — FastAPI dependency that validates
   collector bearer tokens.  Used by future collector-facing endpoints.
   Tokens are hashed with SHA-256 and checked against the
   ``collector_credentials`` table.  Revoked tokens and tokens for
   inactive clients are rejected with 401.

These two layers are independent — the collector token dependency does
not alter or interact with the API-key middleware.
"""

from __future__ import annotations

import hmac
import logging

import asyncpg
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import get_settings
from app.core.identity import hash_token
from app.db.session import get_session

logger = logging.getLogger(__name__)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack a valid ``Authorization: Bearer <key>`` header.

    * When an API key is configured, every request must carry it.
    * When no API key is configured **and** the environment permits it
      (development mode or explicit insecure opt-in) the middleware is
      transparent.
    * API-key comparison uses ``hmac.compare_digest`` for constant-time
      checking.
    """

    # Paths that bypass API key authentication (e.g., health checks)
    EXEMPT_PATHS: frozenset[str] = frozenset({"/health"})

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()

        # Exempt paths bypass auth — kubelet/Docker health checks don't carry tokens
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        # No API key configured → skip auth (development / insecure mode)
        # The Settings validator guarantees we cannot reach this branch
        # in production mode without an explicit opt-in.
        if not settings.api_key:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Auth rejected: missing or malformed Authorization header "
                "from %s",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Missing or invalid Authorization header",
                    },
                },
            )

        token = auth_header.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, settings.api_key):
            logger.warning(
                "Auth rejected: invalid API key from %s",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Invalid API key",
                    },
                },
            )

        return await call_next(request)


# ── Collector token auth dependency ───────────────────────────────────────


async def require_collector_token(
    request: Request,
    conn: asyncpg.Connection = Depends(get_session),
) -> dict[str, str]:
    """FastAPI dependency — validate a collector bearer token.

    Reads ``Authorization: Bearer <token>``, hashes the token with
    SHA-256, and looks it up in ``collector_credentials``.

    Returns a dict with ``client_id``, ``credential_id``, and
    ``client_name`` for use by downstream handlers (attached to
    ``request.state`` if desired).

    Raises:
        HTTPException(401): If the token is missing, malformed, not
            found, revoked, or belongs to an inactive client.

    .. note::

        ``last_used_at`` is updated as a fire-and-forget task so that
        auth latency is not affected by the write.  If the update fails
        it is logged but never surfaced to the caller.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    raw_token = auth_header.removeprefix("Bearer ").strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
        )

    token_hash = hash_token(raw_token)

    row = await conn.fetchrow(
        """
        SELECT cc.id         AS credential_id,
               cc.revoked_at,
               cc.last_used_at,
               c.id          AS client_id,
               c.name        AS client_name,
               c.is_active   AS client_is_active
        FROM collector_credentials cc
        JOIN opencode_clients c ON c.id = cc.client_id
        WHERE cc.token_hash = $1
        """,
        token_hash,
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if row["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    if not row["client_is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Client is deactivated",
        )

    # Fire-and-forget last_used_at update
    from datetime import datetime, timezone

    async def _touch_last_used() -> None:
        try:
            await conn.execute(
                "UPDATE collector_credentials SET last_used_at = $1 WHERE id = $2",
                datetime.now(timezone.utc),
                row["credential_id"],
            )
        except Exception:
            logger.debug("Failed to update last_used_at for credential %s", row["credential_id"])

    await _touch_last_used()

    # Attach identity to request state for downstream handlers
    request.state.client_id = str(row["client_id"])
    request.state.credential_id = str(row["credential_id"])
    request.state.client_name = row["client_name"]

    return {
        "client_id": str(row["client_id"]),
        "credential_id": str(row["credential_id"]),
        "client_name": row["client_name"],
    }
