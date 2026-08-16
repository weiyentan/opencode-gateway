from __future__ import annotations

import re as _re
from urllib.parse import urlparse as _urlparse

_REPO_URL_RE = _re.compile(r"^https?://", _re.IGNORECASE)


def normalize_repository_url(raw: str) -> str | None:
    """Normalize a repository URL into a deterministic identity string.

    Returns the normalized identity, or ``None`` when the URL is invalid
    (not an absolute HTTP(S) URL, empty, or unparseable).

    .. note::

       The URL scheme (``http`` vs ``https``) is deliberately dropped from
       the normalized form because only HTTPS is used in production.
       ``http://github.com/owner/repo`` and ``https://github.com/owner/repo``
       therefore produce the same identity string.
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not _REPO_URL_RE.match(raw):
        return None

    try:
        parsed = _urlparse(raw)
    except Exception:
        return None

    if not parsed.hostname:
        return None

    # Lowercase the hostname.
    host = parsed.hostname.lower()

    # Strip default ports; preserve non-default ports.
    port = ""
    if parsed.port is not None:
        is_default = (parsed.scheme == "http" and parsed.port == 80) or (
            parsed.scheme == "https" and parsed.port == 443
        )
        if not is_default:
            port = f":{parsed.port}"

    # Build the normalized URL: host + optional non-default port + path.
    path = parsed.path or ""

    # Strip trailing slash.
    path = path.rstrip("/")

    # Strip terminal .git suffix.
    if path.endswith(".git"):
        path = path[:-4]

    # Remove leading slash for the canonical owner/repo form.
    path = path.lstrip("/")

    if not path:
        return None

    return f"{host}{port}/{path}"
