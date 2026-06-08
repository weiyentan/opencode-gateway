"""OpenCode Serve client.

Provides an httpx-based client (OpenCodeServeClient) that communicates
with the OpenCode Serve REST API for sessions, task submission, diffs,
and abort, along with custom exception types for error handling.
"""

from app.opencode.serve_client import (
    OpenCodeClientError,
    OpenCodeConnectionError,
    OpenCodeHTTPError,
    OpenCodeServeClient,
    OpenCodeTimeoutError,
)

__all__ = [
    "OpenCodeServeClient",
    "OpenCodeClientError",
    "OpenCodeConnectionError",
    "OpenCodeTimeoutError",
    "OpenCodeHTTPError",
]
