"""Custom exception hierarchy for the AWX API client.

Mirrors the OpenCodeServeClient error hierarchy with AWX-specific
exceptions for connection, timeout, HTTP, and job errors.
"""

from __future__ import annotations


class AWXClientError(Exception):
    """Base exception for AWX API client errors."""


class AWXConnectionError(AWXClientError):
    """Raised when the client cannot connect to the AWX instance.

    Covers connection-refused, DNS failures, and other transport-layer
    errors caught as :class:`httpx.ConnectError`.
    """


class AWXTimeoutError(AWXClientError):
    """Raised when a request to the AWX instance times out."""


class AWXHTTPError(AWXClientError):
    """Raised when the AWX instance returns a non-2xx response.

    Attributes:
        status_code: The HTTP status code returned by the AWX instance.
    """

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class AWXJobError(AWXClientError):
    """Raised when an AWX job fails, is cancelled, or completes with an error.

    Attributes:
        job_id: The AWX job ID that failed.
    """

    def __init__(self, message: str, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(message)
