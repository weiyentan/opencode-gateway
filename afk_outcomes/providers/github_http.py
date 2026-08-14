"""Shared parsed-JSON GitHub HTTP client for AFK Outcome Observability.

The :class:`afk_outcomes.providers.github.GitHubAdapter` seam expects
``get()`` to return the parsed JSON body (a list or dict), not an
``httpx.Response``.  A raw ``httpx.AsyncClient.get()`` returns
``httpx.Response``, so the adapter's ``_items()`` / ``isinstance(body,
dict)`` checks would silently yield zero entities/events.  This module
provides the single parse-JSON wrapper shared by both the live AFK outcome
consumer (``app.consumer.afk_consumer``) and the backfill CLI
(``scripts.afk_backfill``), so the seam cannot drift between the two call
sites.

The module is pure domain: it imports nothing from the application package
(``app``) — only stdlib + ``httpx``.
"""

from __future__ import annotations

import httpx


class GitHubHttpApi:
    """A :class:`afk_outcomes.providers.github.GitHubApi` over httpx.

    Wraps an ``httpx.AsyncClient`` configured for the GitHub REST API so
    :meth:`get` returns ``response.json()`` (the parsed body), matching the
    ``GitHubApi`` protocol the ``GitHubAdapter`` consumes.  A raw
    ``httpx.AsyncClient.get()`` returns ``httpx.Response``, which would
    silently yield zero entities/events through the adapter — this wrapper
    is the fix for that seam.
    """

    def __init__(self, token: str) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30.0
        )

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        response = await self._client.get(path, params=params or {})
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
