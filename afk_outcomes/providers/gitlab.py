"""GitLab provider adapter for AFK Outcome Observability.

:class:`GitLabAdapter` implements the :class:`afk_outcomes.interfaces.ProviderAdapter`
Protocol for the GitLab provider.  It is a coarse-grained, backfill-friendly
fetch: list a repository's merge requests over a time window, then enrich each
one with its reviews (approvals), commits, and pipeline status using batched
API calls — one call per enrichment dimension per merge request, never one call
per sub-item — and normalise the result into the neutral domain model.

Normalisation rules (the locked vocabulary, shared with the GitHub adapter):

* merge requests normalise to ``change_request`` entities (``entity_id``
  ``change_request:{iid}``) exactly as pull requests do;
* a merged merge request additionally emits a ``merge_event`` entity
  (``entity_id`` ``merge_event:{iid}``) when its ``merged_at`` is set;
* the merge request's ``source_branch`` is carried as the change request's
  ``head_ref`` in the change request event ``payload`` (``EngineeringEntity``
  has no ``head_ref`` field, so the branch rides on the events);
* reviews are ``review`` entities derived from the approvals endpoint;
* commits are ``commit`` entities (``entity_id`` ``commit:{id}``);
* pipelines produce events only (``pipeline.succeeded`` / ``pipeline.failed``)
  — there is no pipeline entity type;
* emitted event types are a subset of the ten locked types:
  ``issue.opened``, ``issue.closed``, ``change_request.opened`` /
  ``.review_requested`` / ``.changes_requested`` / ``.approved`` / ``.merged`` /
  ``.closed``, ``pipeline.failed``, ``pipeline.succeeded``.

Because this adapter's fetch is merge-request-centric it emits the
``change_request.*`` and ``pipeline.*`` event types but never ``issue.*``
(issues are not listed) nor ``change_request.changes_requested`` (GitLab's
approvals endpoint carries no changes-requested signal).  Comments are out of
scope.  Provider event IDs are preserved where GitLab emits them (pipeline
``id``, commit ``id``, approval user ``id``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from afk_outcomes.models import (
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)

DEFAULT_BASE_URL = "https://gitlab.com/api/v4"

# ── State / status normalisation ────────────────────────────────────────────

# GitLab merge request states → canonical entity ``state``.
_MR_STATE_MAP = {
    "opened": "open",
    "merged": "merged",
    "closed": "closed",
    "locked": "closed",
}

# GitLab pipeline statuses that map to one of the two locked pipeline events.
_PIPELINE_EVENT_TYPES = {
    "success": "pipeline.succeeded",
    "failed": "pipeline.failed",
}


def _normalize_mr_state(state: str | None) -> str | None:
    if state is None:
        return None
    return _MR_STATE_MAP.get(state, state)


def _parse_dt(value: object) -> datetime | None:
    """Parse a GitLab ISO 8601 timestamp into an aware :class:`datetime`."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _username(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    return user.get("username")


def _suffixed_event_id(kind: str, iid: int, suffix: str, discriminator: object) -> str:
    """Build a stable event ID, appending a discriminator when one exists."""
    if discriminator is not None:
        return f"{kind}:{iid}:{suffix}:{discriminator}"
    return f"{kind}:{iid}:{suffix}"


class GitLabAdapter:
    """Translate GitLab merge requests, approvals, commits, and pipelines."""

    provider = Provider.GITLAB

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = client if client is not None else httpx.AsyncClient()

    # ── Protocol ────────────────────────────────────────────────────────────

    async def fetch_entities(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEntity]:
        entities, _ = await self._fetch_window(repository, since=since, until=until)
        return entities

    async def fetch_events(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEvent]:
        _, events = await self._fetch_window(repository, since=since, until=until)
        return events

    # ── Fetch + enrich ──────────────────────────────────────────────────────

    async def _fetch_window(
        self,
        repository: str,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> tuple[list[EngineeringEntity], list[EngineeringEvent]]:
        merge_requests = await self._list_merge_requests(repository, since, until)
        entities: list[EngineeringEntity] = []
        events: list[EngineeringEvent] = []
        for mr in merge_requests:
            mr_entities, mr_events = await self._enrich(repository, mr)
            entities.extend(mr_entities)
            events.extend(mr_events)
        events.sort(key=lambda e: (e.occurred_at, e.event_id))
        return entities, events

    async def _list_merge_requests(
        self,
        repository: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}/projects/{self._project_path(repository)}/merge_requests"
        params: dict[str, str] = {"state": "all"}
        if since is not None:
            params["updated_after"] = since.isoformat()
        if until is not None:
            params["updated_before"] = until.isoformat()
        return await self._get_json(url, params=params)

    async def _enrich(
        self, repository: str, mr: dict[str, Any]
    ) -> tuple[list[EngineeringEntity], list[EngineeringEvent]]:
        iid = mr["iid"]
        entities: list[EngineeringEntity] = []
        events: list[EngineeringEvent] = []

        entities.extend(self._change_request_entity(repository, mr))
        events.extend(self._change_request_events(mr))

        approvals = await self._get_json(self._mr_url(repository, iid, "approvals"))
        review_entities, review_events = self._reviews(repository, mr, approvals)
        entities.extend(review_entities)
        events.extend(review_events)

        commits = await self._get_json(self._mr_url(repository, iid, "commits"))
        entities.extend(
            self._commit_entities(repository, commits, owning_change_request_id=str(iid))
        )

        pipelines = await self._get_json(self._mr_url(repository, iid, "pipelines"))
        events.extend(self._pipeline_events(mr, pipelines))

        return entities, events

    async def _get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> Any:
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _project_path(self, repository: str) -> str:
        from urllib.parse import quote

        return quote(repository, safe="")

    def _mr_url(self, repository: str, iid: int, suffix: str) -> str:
        return (
            f"{self._base_url}/projects/{self._project_path(repository)}"
            f"/merge_requests/{iid}/{suffix}"
        )

    # ── Normalisation ───────────────────────────────────────────────────────

    def _change_request_entity(
        self, repository: str, mr: dict[str, Any]
    ) -> list[EngineeringEntity]:
        iid = mr["iid"]
        entities: list[EngineeringEntity] = [
            EngineeringEntity(
                entity_id=f"change_request:{iid}",
                entity_type=EntityType.CHANGE_REQUEST,
                provider=Provider.GITLAB,
                repository=repository,
                number=iid,
                title=mr.get("title"),
                state=_normalize_mr_state(mr.get("state")),
                author=_username(mr.get("author")),
                url=mr.get("web_url"),
                created_at=_parse_dt(mr.get("created_at")),
                updated_at=_parse_dt(mr.get("updated_at")),
            )
        ]
        merged_at = _parse_dt(mr.get("merged_at"))
        if merged_at is not None:
            entities.append(
                EngineeringEntity(
                    entity_id=f"merge_event:{iid}",
                    entity_type=EntityType.MERGE_EVENT,
                    provider=Provider.GITLAB,
                    repository=repository,
                    number=iid,
                    title=f"merge {iid}",
                    state="merged",
                    author=_username(mr.get("merged_by")) or _username(mr.get("author")),
                    created_at=merged_at,
                )
            )
        return entities

    def _change_request_events(self, mr: dict[str, Any]) -> list[EngineeringEvent]:
        iid = mr["iid"]
        author = _username(mr.get("author"))
        head_ref = mr.get("source_branch")
        created_at = _parse_dt(mr.get("created_at"))
        updated_at = _parse_dt(mr.get("updated_at"))
        events: list[EngineeringEvent] = []

        if created_at is not None:
            events.append(
                EngineeringEvent(
                    event_id=f"change_request:{iid}:opened",
                    event_type="change_request.opened",
                    provider=Provider.GITLAB,
                    entity_id=f"change_request:{iid}",
                    occurred_at=created_at,
                    actor=author,
                    payload={"head_ref": head_ref},
                )
            )

        for reviewer in mr.get("reviewers") or []:
            username = reviewer.get("username")
            occurred_at = updated_at or created_at
            if occurred_at is None:
                continue
            events.append(
                EngineeringEvent(
                    event_id=_suffixed_event_id(
                        "change_request", iid, "review_requested", reviewer.get("id")
                    ),
                    event_type="change_request.review_requested",
                    provider=Provider.GITLAB,
                    entity_id=f"change_request:{iid}",
                    occurred_at=occurred_at,
                    actor=username,
                    payload={"head_ref": head_ref, "reviewer": username},
                )
            )

        state = _normalize_mr_state(mr.get("state"))
        merged_at = _parse_dt(mr.get("merged_at"))
        if state == "merged" and merged_at is not None:
            events.append(
                EngineeringEvent(
                    event_id=f"change_request:{iid}:merged",
                    event_type="change_request.merged",
                    provider=Provider.GITLAB,
                    entity_id=f"change_request:{iid}",
                    occurred_at=merged_at,
                    actor=_username(mr.get("merged_by")) or author,
                    payload={
                        "head_ref": head_ref,
                        "merge_commit_sha": mr.get("merge_commit_sha"),
                    },
                )
            )
        elif state == "closed":
            closed_at = _parse_dt(mr.get("closed_at")) or updated_at
            if closed_at is not None:
                events.append(
                    EngineeringEvent(
                        event_id=f"change_request:{iid}:closed",
                        event_type="change_request.closed",
                        provider=Provider.GITLAB,
                        entity_id=f"change_request:{iid}",
                        occurred_at=closed_at,
                        actor=author,
                        payload={"head_ref": head_ref},
                    )
                )

        return events

    def _reviews(
        self,
        repository: str,
        mr: dict[str, Any],
        approvals: dict[str, Any],
    ) -> tuple[list[EngineeringEntity], list[EngineeringEvent]]:
        iid = mr["iid"]
        head_ref = mr.get("source_branch")
        entities: list[EngineeringEntity] = []
        events: list[EngineeringEvent] = []
        for entry in approvals.get("approved_by") or []:
            user = entry.get("user") or {}
            user_id = user.get("id")
            username = user.get("username")
            approved_at = _parse_dt(entry.get("created_at")) or _parse_dt(
                mr.get("updated_at")
            )
            review_id = f"review:{iid}:{user_id}"
            entities.append(
                EngineeringEntity(
                    entity_id=review_id,
                    entity_type=EntityType.REVIEW,
                    provider=Provider.GITLAB,
                    repository=repository,
                    state="approved",
                    title=f"approval by {username}",
                    author=username,
                    created_at=approved_at,
                    owning_change_request_id=str(iid),
                )
            )
            if approved_at is not None:
                events.append(
                    EngineeringEvent(
                        event_id=_suffixed_event_id(
                            "change_request", iid, "approved", user_id
                        ),
                        event_type="change_request.approved",
                        provider=Provider.GITLAB,
                        entity_id=f"change_request:{iid}",
                        occurred_at=approved_at,
                        actor=username,
                        payload={"head_ref": head_ref, "review_id": review_id},
                    )
                )
        return entities, events

    def _commit_entities(
        self,
        repository: str,
        commits: list[dict[str, Any]],
        *,
        owning_change_request_id: str | None = None,
    ) -> list[EngineeringEntity]:
        entities: list[EngineeringEntity] = []
        for commit in commits:
            entities.append(
                EngineeringEntity(
                    entity_id=f"commit:{commit.get('id')}",
                    entity_type=EntityType.COMMIT,
                    provider=Provider.GITLAB,
                    repository=repository,
                    title=commit.get("title"),
                    author=commit.get("author_name"),
                    url=commit.get("web_url"),
                    created_at=_parse_dt(
                        commit.get("authored_date") or commit.get("created_at")
                    ),
                    owning_change_request_id=owning_change_request_id,
                )
            )
        return entities

    def _pipeline_events(
        self, mr: dict[str, Any], pipelines: list[dict[str, Any]]
    ) -> list[EngineeringEvent]:
        iid = mr["iid"]
        events: list[EngineeringEvent] = []
        for pipeline in pipelines:
            status = pipeline.get("status")
            event_type = _PIPELINE_EVENT_TYPES.get(status)
            if event_type is None:
                continue
            occurred_at = _parse_dt(
                pipeline.get("finished_at") or pipeline.get("updated_at")
            )
            if occurred_at is None:
                continue
            events.append(
                EngineeringEvent(
                    event_id=f"pipeline:{pipeline.get('id')}",
                    event_type=event_type,
                    provider=Provider.GITLAB,
                    entity_id=f"change_request:{iid}",
                    occurred_at=occurred_at,
                    actor=None,
                    payload={
                        "pipeline_id": pipeline.get("id"),
                        "status": status,
                        "ref": pipeline.get("ref"),
                    },
                )
            )
        return events
