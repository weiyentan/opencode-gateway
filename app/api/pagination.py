"""Keyset (cursor) pagination helpers for the execution-transcript list endpoints.

The transcript endpoints in :mod:`app.api.execution` paginate with an opaque
keyset cursor that encodes the last row's ``(source_created_at, id)`` pair, so
the ``after=`` re-issue is stable under concurrent ingest.  The cursor is a
URL-safe base64 string of the colon-joined form
``base64("<source_created_at_ms>:<row_id>")``:

- it is **opaque to consumers** — clients pass it back verbatim and never parse
  it;
- it encodes the last row's ``(source_created_at, id)`` only, matching the
  ``ORDER BY (COALESCE(source_created_at, $sentinel), id)`` ordering;
- the earlier ADR 0016 sketch of a ``base64(sort_key=…&id=…)`` key=value form
  was superseded by this colon-joined encoding (ADR 0016 is amended
  accordingly).

This module lives in the API layer because :func:`decode_cursor` raises
:class:`fastapi.HTTPException` (400) on malformed input.
"""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

from fastapi import HTTPException, status

# Sentinel for NULL ``source_created_at`` in keyset ordering/cursors.  No real
# millisecond-epoch timestamp can reach 2**62, so NULL timestamps sort last and
# encode/decode cleanly without a separate NULL branch in the cursor.
NULL_CURSOR_SENTINEL = 2**62


def encode_cursor(source_created_at: int, row_id: str) -> str:
    """Encode ``(source_created_at, id)`` into an opaque URL-safe cursor."""
    raw = f"{source_created_at}:{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[int, UUID]:
    """Decode a cursor into ``(source_created_at_ms, row_id)``, 400 on garbage."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ms_s, row_id = raw.split(":", 1)
        return int(ms_s), UUID(row_id)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cursor: {cursor!r}",
        ) from exc


def next_cursor(source_created_at: int | None, row_id: str) -> str:
    """Encode the keyset cursor for the last row, NULL-safe on the timestamp.

    A ``None`` timestamp encodes as :data:`NULL_CURSOR_SENTINEL`, matching the
    ``COALESCE`` ordering so NULL rows sort last and the cursor can always
    advance past a full page that ends on a NULL timestamp.
    """
    ts = source_created_at if source_created_at is not None else NULL_CURSOR_SENTINEL
    return encode_cursor(ts, row_id)
