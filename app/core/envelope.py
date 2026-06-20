"""Response envelope middleware and exception handlers.

Wraps every successful JSON response in ``{status: "ok", data: ...}``
and configures FastAPI exception handlers to return
``{status: "error", error: {code: "...", message: "..."}}`` on failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status-code → error-code mapping
# ---------------------------------------------------------------------------

_STATUS_TO_ERROR_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    424: "FAILED_DEPENDENCY",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _status_to_code(status: int) -> str:
    """Map an HTTP status code to a stable error code string."""
    return _STATUS_TO_ERROR_CODE.get(status, "HTTP_ERROR")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ResponseEnvelopeMiddleware(BaseHTTPMiddleware):
    """Wrap every JSON response body in a ``{status, data, error}`` envelope.

    Success responses (2xx) are wrapped as ``{status: "ok", data: <body>}``.
    Responses that already carry a ``status`` field (``"ok"`` or
    ``"error"``) are left untouched to avoid double-wrapping.  Non-JSON
    responses and 204 No Content pass through unchanged.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        # 204 No Content and other empty responses pass through
        if response.status_code == 204:
            return response

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        # Read the response body (Starlette >=0.49 uses .body; older
        # versions used .body_iterator — we support both via getattr).
        if hasattr(response, "body_iterator"):
            body = b""
            async for chunk in response.body_iterator:  # type: ignore[union-attr]
                body += chunk
        else:
            body = response.body  # type: ignore[assignment]

        # Only wrap JSON bodies
        try:
            data: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            new_headers = {
                k: v for k, v in dict(response.headers).items()
                if k.lower() != "content-length"
            }
            return Response(
                content=body,
                status_code=response.status_code,
                headers=new_headers,
                media_type=response.media_type,
            )

        # Already wrapped — don't double-wrap.
        # We require BOTH a 'status' field AND either 'data' or 'error'
        # to be present; this prevents false positives on domain models
        # that happen to have a 'status' field (e.g. HealthResponse).
        if (
            isinstance(data, dict)
            and data.get("status") in ("ok", "error")
            and ("data" in data or "error" in data)
        ):
            new_headers = {
                k: v for k, v in dict(response.headers).items()
                if k.lower() != "content-length"
            }
            return Response(
                content=json.dumps(data),
                status_code=response.status_code,
                headers=new_headers,
                media_type=response.media_type,
            )

        # Wrap the original body as data
        if 200 <= response.status_code < 300:
            wrapped: dict[str, Any] = {"status": "ok", "data": data}
        else:
            wrapped = {
                "status": "error",
                "error": {
                    "code": _status_to_code(response.status_code),
                    "message": str(data),
                },
            }

        new_headers = {
            k: v for k, v in dict(response.headers).items()
            if k.lower() != "content-length"
        }
        return Response(
            content=json.dumps(wrapped),
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
        )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Return envelope-formatted error for :class:`HTTPException`.

    Extracts the human-readable message from ``exc.detail`` — whether it
    is a plain string or a structured dict (e.g. :class:`PolicyViolation`).
    """
    code = _status_to_code(exc.status_code)

    if isinstance(exc.detail, dict):
        message: str = exc.detail.get("message", str(exc.detail))
    else:
        message = str(exc.detail)

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "error": {
                "code": code,
                "message": message,
            },
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return envelope-formatted error for Pydantic validation failures (422)."""
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
            },
        },
    )
