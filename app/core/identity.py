"""Token generation and hashing utilities for collector credentials."""

from __future__ import annotations

import hashlib
import secrets


def generate_collector_token() -> tuple[str, str, str]:
    """Generate a new collector bearer token.

    Returns a 3-tuple of ``(raw_token, token_hash, token_prefix)``:

    * **raw_token** — 64-char URL-safe string (``secrets.token_urlsafe(48)``).
      This is the value returned to the caller **once** and then discarded.
    * **token_hash** — SHA-256 hex digest of the raw token.  Stored in the
      ``collector_credentials.token_hash`` column for lookup.
    * **token_prefix** — First 8 characters of the raw token.  Stored for
      human identification in admin UIs.
    """
    raw = secrets.token_urlsafe(48)  # 48 bytes → 64 URL-safe chars
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:8]
    return raw, hashed, prefix


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of *raw_token*.

    Convenience for the auth middleware path — avoids needing to know
    the exact hashing algorithm at every call site.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()
