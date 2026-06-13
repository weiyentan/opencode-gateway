"""API key authentication middleware for the OpenCode Gateway.

Validates an ``Authorization: Bearer <api-key>`` header against the
``GATEWAY_API_KEY`` setting.  When no API key is configured the
middleware is transparent — all requests pass through.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack a valid ``Authorization: Bearer <key>`` header.

    When ``GATEWAY_API_KEY`` is empty the middleware is a no-op so that
    local development does not require an API key.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()

        # No API key configured → skip auth (development mode)
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
        if token != settings.api_key:
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
