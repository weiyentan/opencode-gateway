"""Secret redaction helpers for log and event safety.

Provides pattern-based detection of secret-like keys and a recursive
dict redaction function.  Used by executors and the logging formatter
to ensure that values for keys matching TOKEN, PASSWORD, SECRET, KEY,
CREDENTIAL, AUTH (and common variants) are never written to logs or
job events in plaintext.

ADR 0004 compliance: the Gateway never holds infrastructure secrets;
this module protects user-provided environment variables (API tokens,
credentials) that flow through the system to OpenCode sessions.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Secret-like key patterns
# ---------------------------------------------------------------------------

# Sub-strings that, when found in a lower-cased, underscore-stripped key
# name, mark the key as potentially sensitive.
_SECRET_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "token",
        "password",
        "passwd",
        "secret",
        "credential",
        "apikey",
        "api_key",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "authorization",
        "auth",
        "key",
    }
)

# Placeholder string used to replace sensitive values.
REDACTED = "***"


def is_secret_key(key: str) -> bool:
    """Return ``True`` if *key* looks like it holds a secret value.

    The check is case-insensitive and strips underscores so that
    ``GITHUB_TOKEN``, ``github_token``, ``AWS_SECRET_ACCESS_KEY``
    and ``DATABASE_PASSWORD`` are all recognised as secrets.

    >>> is_secret_key("GITHUB_TOKEN")
    True
    >>> is_secret_key("MY_SECRET_KEY")
    True
    >>> is_secret_key("DATABASE_PASSWORD")
    True
    >>> is_secret_key("API_KEY")
    True
    >>> is_secret_key("REPO_URL")
    False
    >>> is_secret_key("BRANCH")
    False
    """
    if not key:
        return False
    # Normalise: lowercase and strip all underscores so that compound
    # names like ``api_key`` match the ``apikey`` substring.
    normalised = key.lower().replace("_", "")
    for s in _SECRET_SUBSTRINGS:
        if s in normalised:
            return True
    return False


def redact_dict(
    data: dict[str, Any],
    *,
    placeholder: str = REDACTED,
) -> dict[str, Any]:
    """Return a shallow copy of *data* with secret-like values replaced.

    Keys are tested with :func:`is_secret_key`.  Nested dictionaries are
    redacted recursively.  All other values (lists, scalars, ``None``)
    are passed through unchanged.

    >>> redact_dict({"REPO_URL": "https://...", "GITHUB_TOKEN": "ghp_abc"})
    {'REPO_URL': 'https://...', 'GITHUB_TOKEN': '***'}

    >>> redact_dict({"nested": {"AWS_SECRET_KEY": "wJalrX..."}})
    {'nested': {'AWS_SECRET_KEY': '***'}}
    """
    if not data:
        return {}
    result: dict[str, Any] = {}
    for k, v in data.items():
        if is_secret_key(k):
            result[k] = placeholder
        elif isinstance(v, dict):
            result[k] = redact_dict(v, placeholder=placeholder)
        else:
            result[k] = v
    return result
