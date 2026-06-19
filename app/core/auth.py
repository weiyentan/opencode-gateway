"""API key authentication middleware for the OpenCode Gateway.

Validates an ``Authorization: Bearer <api-key>`` header against the
``GATEWAY_API_KEY`` setting using constant-time comparison.

Authentication modes:

* **Production** (``GATEWAY_ENV=production``, default) — API key is
  **required**.  The Gateway refuses to start without one.
* **Development** (``GATEWAY_ENV=development``) — API key is optional;
  requests pass through unauthenticated.
* **Insecure opt-in** (``GATEWAY_ALLOW_INSECURE_AUTH=true``) —
  explicitly allows production to run without an API key.  A loud
  warning is logged at startup.  Not recommended for real deployments.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import get_settings

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

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()

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
