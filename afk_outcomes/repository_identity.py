"""Provider-aware repository identity normalization.

This mirrors the canonical identity contract implemented in the FastAPI EDA
Gateway (MR !91): GitHub paths fold to lowercase, GitLab namespace/project
paths retain their case, enterprise hosts remain distinct, and malformed
identities fail closed.  The returned database key intentionally omits the
URL scheme for compatibility with existing OpenCode Gateway records.
"""

from __future__ import annotations

from urllib.parse import urlparse as _urlparse


def _provider_for_host(host: str) -> str | None:
    if host == "github.com" or "github" in host:
        return "github"
    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    return None


def normalize_repository_url(raw: str, provider: str | None = None) -> str | None:
    """Return a canonical repository identity, or ``None`` if malformed.

    ``provider`` may be supplied when the forge host is a custom enterprise
    hostname that does not include ``github`` or ``gitlab`` in its name.
    GitHub repositories must have exactly ``owner/repo``; GitLab supports
    nested groups and requires at least ``namespace/project``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = _urlparse(raw.strip())
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if not host or parsed.query or parsed.fragment:
        return None
    if port not in (None, 80, 443):
        return None

    inferred = _provider_for_host(host)
    resolved_provider = provider.strip().lower() if isinstance(provider, str) else None
    if resolved_provider is not None and resolved_provider not in {"github", "gitlab"}:
        return None
    if inferred is not None and resolved_provider is not None and inferred != resolved_provider:
        return None
    if resolved_provider is None:
        resolved_provider = inferred
    if resolved_provider is None:
        return None

    path = parsed.path.strip("/")
    while path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    if not path:
        return None
    segments = path.split("/")
    if any(not segment for segment in segments):
        return None
    if resolved_provider == "github":
        if len(segments) != 2:
            return None
        path = path.lower()
    elif len(segments) < 2:
        return None

    port_suffix = f":{port}" if port not in (None, 80, 443) else ""
    return f"{host}{port_suffix}/{path}"


def normalize_repository_key(raw: str, provider: str | None = None) -> str | None:
    """Canonicalize a stored Gateway key, which omits the URL scheme.

    Public request inputs still pass through :func:`normalize_repository_url`
    and therefore must be absolute HTTP(S) URLs. This helper exists for
    comparing historical persisted keys such as ``github.com/Owner/Repo``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if "://" not in value:
        segments = value.lstrip("/").split("/")
        if (
            len(segments) == 2
            and "." not in segments[0]
            and provider in {"github", "gitlab"}
        ):
            forge = "github.com" if provider == "github" else "gitlab.com"
            value = f"https://{forge}/{value.lstrip('/')}"
        else:
            value = f"https://{value.lstrip('/')}"
    return normalize_repository_url(value, provider)
