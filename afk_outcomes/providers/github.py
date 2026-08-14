"""GitHub provider adapter for AFK Outcome Observability.

Translates real GitHub REST API-shaped payloads into the neutral
:mod:`afk_outcomes.models` domain.  The adapter is coarse-grained and
backfill-friendly:

* one call to list issues in a repository over a time window;
* one call to list change requests (pull requests) in that window;
* batched enrichment per in-window change request — reviews, commits, and
  pipeline (check-run) status, one call per change request per dimension.

The number of API calls is therefore bounded by ``2 + 2N`` for a window of
``N`` change requests (entities) and ``2 + 2N`` for events — linear, never
per-commit or per-reviewer.

Network access is fully injectable: the adapter talks to a
:class:`GitHubApi` client, so tests (and callers) can feed it
GitHub-shaped dicts and assert canonical normalization without touching the
network.

Normalization map (locked vocabulary):

* entities — ``issue``, ``change_request``, ``review``, ``commit``, and a
  ``merge_event`` for change requests with a ``merged_at`` timestamp;
  the change request's source branch is carried as ``head_ref``;
* events — ``issue.opened``, ``issue.closed``, ``change_request.opened``,
  ``change_request.review_requested``, ``change_request.changes_requested``,
  ``change_request.approved``, ``change_request.merged``,
  ``change_request.closed``, ``pipeline.failed``, ``pipeline.succeeded``;
* pipelines are represented by events only (no pipeline entity);
* review comments are out of scope.

Provider event-ID preservation: GitHub emits stable database IDs for
reviews and check-runs, so ``change_request.approved`` /
``change_request.changes_requested`` events carry ``event_id``
``"review:<id>"`` and pipeline events carry ``"check_run:<id>"``.  Where
GitHub emits no stable event ID (opened / review_requested / merged /
closed), a deterministic synthesized ID is used (e.g.
``"change_request:200:opened"``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from afk_outcomes.models import (
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
)

# Locked event-type vocabulary (see afk_outcomes/models.py and CONTEXT.md).
EVENT_ISSUE_OPENED = "issue.opened"
EVENT_ISSUE_CLOSED = "issue.closed"
EVENT_CHANGE_REQUEST_OPENED = "change_request.opened"
EVENT_CHANGE_REQUEST_REVIEW_REQUESTED = "change_request.review_requested"
EVENT_CHANGE_REQUEST_CHANGES_REQUESTED = "change_request.changes_requested"
EVENT_CHANGE_REQUEST_APPROVED = "change_request.approved"
EVENT_CHANGE_REQUEST_MERGED = "change_request.merged"
EVENT_CHANGE_REQUEST_CLOSED = "change_request.closed"
EVENT_PIPELINE_FAILED = "pipeline.failed"
EVENT_PIPELINE_SUCCEEDED = "pipeline.succeeded"

# GitHub review states that map onto canonical change_request events.
_REVIEW_STATE_APPROVED = "approved"
_REVIEW_STATE_CHANGES_REQUESTED = "changes_requested"

# GitHub check-run conclusions that map onto canonical pipeline events.
_CHECK_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "cancelled"}
)


class GitHubApi(Protocol):
    """The seam over the GitHub REST API (injectable for tests and callers)."""

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        """GET a GitHub REST API path; return the parsed JSON body.

        ``path`` is an absolute REST path such as
        ``"/repos/owner/repo/pulls/200/reviews"``.  Implementations are free
        to return a list (list endpoints) or a dict (single-object endpoints).
        """
        ...


def _parse_dt(value: object) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp (``...Z``) into an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso_utc(value: datetime) -> str:
    """Render a datetime as a UTC ISO 8601 string with a ``Z`` suffix."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    else:
        value = value.astimezone(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    return value.isoformat().replace("+00:00", "Z")


def _str(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _int(obj: dict[str, object], key: str) -> int | None:
    value = obj.get(key)
    return value if isinstance(value, int) else None


def _dict(obj: dict[str, object], key: str) -> dict[str, object]:
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _list(obj: dict[str, object], key: str) -> list[dict[str, object]]:
    value = obj.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _items(value: object) -> list[dict[str, object]]:
    """Coerce a list-endpoint body into a list of object dicts."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _login(obj: dict[str, object]) -> str | None:
    return _str(_dict(obj, "user"), "login")


def _in_window(
    value: datetime | None,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Return True when ``value`` falls within the ``[since, until]`` window.

    A missing timestamp is treated as in-window (coarse-grained backfill).
    """
    if value is None:
        return True
    if since is not None and value < since:
        return False
    if until is not None and value > until:
        return False
    return True


class GitHubAdapter:
    """A :class:`afk_outcomes.interfaces.ProviderAdapter` for GitHub."""

    provider = Provider.GITHUB

    def __init__(self, client: GitHubApi, *, per_page: int = 100) -> None:
        self._client = client
        self._per_page = per_page

    # ── ProviderAdapter protocol ─────────────────────────────────────────

    async def fetch_entities(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEntity]:
        """List issues, change requests, and their reviews/commits.

        Enrichment is batched: for ``N`` in-window change requests the total
        API call count is ``2 + 2N`` (issues + pulls + reviews-per-CR +
        commits-per-CR).  Pipeline status is not fetched here because
        pipelines are represented by events only.
        """
        entities: list[EngineeringEntity] = []
        issues = await self._list_issues(repository, since, until)
        entities.extend(self._issue_entities(issues, repository))
        pulls = await self._list_pulls(repository, since, until)
        entities.extend(self._change_request_entities(pulls, repository))
        for pull in pulls:
            number = self._pull_number(pull)
            if number is None:
                continue
            reviews = await self._list_reviews(repository, number)
            entities.extend(
                self._review_entities(reviews, repository, owning_change_request_id=str(number))
            )
            commits = await self._list_commits(repository, number)
            entities.extend(
                self._commit_entities(commits, repository, owning_change_request_id=str(number))
            )
        return entities

    async def fetch_events(
        self,
        repository: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[EngineeringEvent]:
        """Emit issue, change-request, and pipeline events.

        Enrichment is batched: for ``N`` in-window change requests the total
        API call count is ``2 + 2N`` (issues + pulls + reviews-per-CR +
        check-runs-per-CR).
        """
        events: list[EngineeringEvent] = []
        events.extend(self._issue_events(await self._list_issues(repository, since, until)))
        pulls = await self._list_pulls(repository, since, until)
        for pull in pulls:
            number = self._pull_number(pull)
            if number is None:
                continue
            events.extend(self._change_request_events(pull))
            reviews = await self._list_reviews(repository, number)
            events.extend(self._review_events(reviews, number))
            head_sha = _str(_dict(pull, "head"), "sha")
            if head_sha:
                check_runs = await self._list_check_runs(repository, head_sha)
                events.extend(self._pipeline_events(check_runs, number, head_sha))
        return events

    # ── GitHub API calls (each maps to exactly one REST GET) ─────────────

    async def _list_issues(
        self,
        repository: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, object]]:
        params = {"state": "all", "per_page": str(self._per_page)}
        if since is not None:
            params["since"] = _iso_utc(since)
        body = await self._client.get(f"/repos/{repository}/issues", params=params)
        result: list[dict[str, object]] = []
        for item in _items(body):
            # Pull requests are also issues in GitHub's model; skip them here.
            if "pull_request" in item:
                continue
            if _in_window(_parse_dt(item.get("created_at")), since=since, until=until):
                result.append(item)
        return result

    async def _list_pulls(
        self,
        repository: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, object]]:
        params = {"state": "all", "per_page": str(self._per_page)}
        if since is not None:
            params["since"] = _iso_utc(since)
        body = await self._client.get(f"/repos/{repository}/pulls", params=params)
        result: list[dict[str, object]] = []
        for item in _items(body):
            if _in_window(_parse_dt(item.get("created_at")), since=since, until=until):
                result.append(item)
        return result

    async def _list_reviews(self, repository: str, number: int) -> list[dict[str, object]]:
        params = {"per_page": str(self._per_page)}
        body = await self._client.get(
            f"/repos/{repository}/pulls/{number}/reviews", params=params
        )
        return _items(body)

    async def _list_commits(self, repository: str, number: int) -> list[dict[str, object]]:
        params = {"per_page": str(self._per_page)}
        body = await self._client.get(
            f"/repos/{repository}/pulls/{number}/commits", params=params
        )
        return _items(body)

    async def _list_check_runs(self, repository: str, head_sha: str) -> list[dict[str, object]]:
        params = {"per_page": str(self._per_page)}
        body = await self._client.get(
            f"/repos/{repository}/commits/{head_sha}/check-runs", params=params
        )
        value = body if isinstance(body, dict) else {}
        return _list(value, "check_runs")

    # ── Normalization: entities ──────────────────────────────────────────

    def _issue_entities(
        self, issues: list[dict[str, object]], repository: str
    ) -> list[EngineeringEntity]:
        entities: list[EngineeringEntity] = []
        for issue in issues:
            number = _int(issue, "number")
            if number is None:
                continue
            entities.append(
                EngineeringEntity(
                    entity_id=f"issue:{number}",
                    entity_type=EntityType.ISSUE,
                    provider=Provider.GITHUB,
                    repository=repository,
                    number=number,
                    title=_str(issue, "title"),
                    state=_str(issue, "state"),
                    author=_login(issue),
                    url=_str(issue, "html_url"),
                    created_at=_parse_dt(issue.get("created_at")),
                    updated_at=_parse_dt(issue.get("updated_at")),
                )
            )
        return entities

    def _change_request_entities(
        self, pulls: list[dict[str, object]], repository: str
    ) -> list[EngineeringEntity]:
        entities: list[EngineeringEntity] = []
        for pull in pulls:
            number = self._pull_number(pull)
            if number is None:
                continue
            entities.append(
                EngineeringEntity(
                    entity_id=f"change_request:{number}",
                    entity_type=EntityType.CHANGE_REQUEST,
                    provider=Provider.GITHUB,
                    repository=repository,
                    number=number,
                    title=_str(pull, "title"),
                    state=_str(pull, "state"),
                    author=_login(pull),
                    url=_str(pull, "html_url"),
                    head_ref=_str(_dict(pull, "head"), "ref"),
                    created_at=_parse_dt(pull.get("created_at")),
                    updated_at=_parse_dt(pull.get("updated_at")),
                )
            )
            merged_at = _parse_dt(pull.get("merged_at"))
            if merged_at is not None:
                entities.append(
                    EngineeringEntity(
                        entity_id=f"merge_event:{number}",
                        entity_type=EntityType.MERGE_EVENT,
                        provider=Provider.GITHUB,
                        repository=repository,
                        number=number,
                        title=f"merge {number}",
                        state="merged",
                        author=_str(_dict(pull, "merged_by"), "login") or _login(pull),
                        created_at=merged_at,
                    )
                )
        return entities

    def _review_entities(
        self,
        reviews: list[dict[str, object]],
        repository: str,
        *,
        owning_change_request_id: str | None = None,
    ) -> list[EngineeringEntity]:
        entities: list[EngineeringEntity] = []
        for review in reviews:
            review_id = _int(review, "id")
            if review_id is None:
                continue
            state = (_str(review, "state") or "").lower()
            entities.append(
                EngineeringEntity(
                    entity_id=f"review:{review_id}",
                    entity_type=EntityType.REVIEW,
                    provider=Provider.GITHUB,
                    repository=repository,
                    title=f"review {state}" if state else "review",
                    state=state or None,
                    author=_login(review),
                    created_at=_parse_dt(review.get("submitted_at")),
                    owning_change_request_id=owning_change_request_id,
                )
            )
        return entities

    def _commit_entities(
        self,
        commits: list[dict[str, object]],
        repository: str,
        *,
        owning_change_request_id: str | None = None,
    ) -> list[EngineeringEntity]:
        entities: list[EngineeringEntity] = []
        for commit in commits:
            sha = _str(commit, "sha")
            if sha is None:
                continue
            git = _dict(commit, "commit")
            git_author = _dict(git, "author")
            author = _login(commit) or _str(git_author, "name")
            entities.append(
                EngineeringEntity(
                    entity_id=f"commit:{sha}",
                    entity_type=EntityType.COMMIT,
                    provider=Provider.GITHUB,
                    repository=repository,
                    title=_str(git, "message"),
                    author=author,
                    url=_str(commit, "html_url"),
                    created_at=_parse_dt(git_author.get("date")),
                    owning_change_request_id=owning_change_request_id,
                )
            )
        return entities

    # ── Normalization: events ────────────────────────────────────────────

    def _issue_events(self, issues: list[dict[str, object]]) -> list[EngineeringEvent]:
        events: list[EngineeringEvent] = []
        for issue in issues:
            number = _int(issue, "number")
            if number is None:
                continue
            entity_id = f"issue:{number}"
            created_at = _parse_dt(issue.get("created_at"))
            if created_at is not None:
                events.append(
                    self._event(
                        event_id=f"{entity_id}:opened",
                        event_type=EVENT_ISSUE_OPENED,
                        entity_id=entity_id,
                        occurred_at=created_at,
                        actor=_login(issue),
                    )
                )
            closed_at = _parse_dt(issue.get("closed_at"))
            if closed_at is not None:
                events.append(
                    self._event(
                        event_id=f"{entity_id}:closed",
                        event_type=EVENT_ISSUE_CLOSED,
                        entity_id=entity_id,
                        occurred_at=closed_at,
                        actor=_str(_dict(issue, "closed_by"), "login") or _login(issue),
                    )
                )
        return events

    def _change_request_events(
        self, pull: dict[str, object]
    ) -> list[EngineeringEvent]:
        number = self._pull_number(pull)
        if number is None:
            return []
        entity_id = f"change_request:{number}"
        events: list[EngineeringEvent] = []
        created_at = _parse_dt(pull.get("created_at"))
        if created_at is not None:
            events.append(
                self._event(
                    event_id=f"{entity_id}:opened",
                    event_type=EVENT_CHANGE_REQUEST_OPENED,
                    entity_id=entity_id,
                    occurred_at=created_at,
                    actor=_login(pull),
                )
            )

        # Review requests: GitHub exposes "who" (requested_reviewers) but no
        # per-request timestamp on the pull object, so we timestamp the event
        # at the pull's latest update (a coarse, backfill-friendly proxy).
        updated_at = _parse_dt(pull.get("updated_at"))
        requested_at = updated_at or created_at
        for reviewer in _list(pull, "requested_reviewers"):
            # ``requested_reviewers`` entries are bare User objects (with
            # ``login``), not ``{"user": {...}}`` wrappers.
            login = _str(reviewer, "login")
            if login is None or requested_at is None:
                continue
            events.append(
                self._event(
                    event_id=f"{entity_id}:review_requested:{login}",
                    event_type=EVENT_CHANGE_REQUEST_REVIEW_REQUESTED,
                    entity_id=entity_id,
                    occurred_at=requested_at,
                    actor=None,
                    payload={"reviewer": login},
                )
            )

        merged_at = _parse_dt(pull.get("merged_at"))
        if merged_at is not None:
            payload: dict[str, object] = {}
            merge_sha = _str(pull, "merge_commit_sha")
            if merge_sha:
                payload["commit_sha"] = merge_sha
            events.append(
                self._event(
                    event_id=f"{entity_id}:merged",
                    event_type=EVENT_CHANGE_REQUEST_MERGED,
                    entity_id=entity_id,
                    occurred_at=merged_at,
                    actor=_str(_dict(pull, "merged_by"), "login") or _login(pull),
                    payload=payload,
                )
            )
        elif _str(pull, "state") == "closed":
            closed_at = _parse_dt(pull.get("closed_at"))
            if closed_at is not None:
                events.append(
                    self._event(
                        event_id=f"{entity_id}:closed",
                        event_type=EVENT_CHANGE_REQUEST_CLOSED,
                        entity_id=entity_id,
                        occurred_at=closed_at,
                        actor=_login(pull),
                    )
                )
        return events

    def _review_events(
        self, reviews: list[dict[str, object]], number: int
    ) -> list[EngineeringEvent]:
        entity_id = f"change_request:{number}"
        events: list[EngineeringEvent] = []
        for review in reviews:
            state = (_str(review, "state") or "").lower()
            review_id = _int(review, "id")
            if review_id is None:
                continue
            submitted_at = _parse_dt(review.get("submitted_at"))
            if submitted_at is None:
                continue
            if state == _REVIEW_STATE_APPROVED:
                event_type = EVENT_CHANGE_REQUEST_APPROVED
            elif state == _REVIEW_STATE_CHANGES_REQUESTED:
                event_type = EVENT_CHANGE_REQUEST_CHANGES_REQUESTED
            else:
                # COMMENTED / PENDING / DISMISSED are out of scope (comments).
                continue
            events.append(
                self._event(
                    event_id=f"review:{review_id}",
                    event_type=event_type,
                    entity_id=entity_id,
                    occurred_at=submitted_at,
                    actor=_login(review),
                    payload={
                        "review_id": review_id,
                        "commit_id": _str(review, "commit_id"),
                    },
                )
            )
        return events

    def _pipeline_events(
        self, check_runs: list[dict[str, object]], number: int, head_sha: str
    ) -> list[EngineeringEvent]:
        entity_id = f"change_request:{number}"
        events: list[EngineeringEvent] = []
        for run in check_runs:
            check_run_id = _int(run, "id")
            if check_run_id is None:
                continue
            conclusion = (_str(run, "conclusion") or "").lower()
            if conclusion == "success":
                event_type = EVENT_PIPELINE_SUCCEEDED
            elif conclusion in _CHECK_FAILURE_CONCLUSIONS:
                event_type = EVENT_PIPELINE_FAILED
            else:
                # neutral / skipped / stale / pending → no canonical event.
                continue
            completed_at = _parse_dt(run.get("completed_at"))
            if completed_at is None:
                continue
            events.append(
                self._event(
                    event_id=f"check_run:{check_run_id}",
                    event_type=event_type,
                    entity_id=entity_id,
                    occurred_at=completed_at,
                    actor=None,
                    payload={
                        "check_run_id": check_run_id,
                        "name": _str(run, "name"),
                        "conclusion": _str(run, "conclusion"),
                        "head_sha": head_sha,
                    },
                )
            )
        return events

    # ── Helpers ──────────────────────────────────────────────────────────

    def _pull_number(self, pull: dict[str, object]) -> int | None:
        return _int(pull, "number")

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        entity_id: str,
        occurred_at: datetime,
        actor: str | None,
        payload: dict[str, object] | None = None,
    ) -> EngineeringEvent:
        return EngineeringEvent(
            event_id=event_id,
            event_type=event_type,
            provider=Provider.GITHUB,
            entity_id=entity_id,
            occurred_at=occurred_at,
            actor=actor,
            payload=payload or {},
        )
