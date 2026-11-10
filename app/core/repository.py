"""Application compatibility exports for repository identity helpers.

The implementation lives in :mod:`afk_outcomes.repository_identity` so the
domain package does not depend on the FastAPI application package.
"""

from afk_outcomes.repository_identity import (
    normalize_repository_key,
    normalize_repository_url,
)

__all__ = ["normalize_repository_key", "normalize_repository_url"]
