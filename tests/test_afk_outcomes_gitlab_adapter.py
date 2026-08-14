"""GitLab provider adapter tests: normalization, vocabulary lock, and batching.

Feeds realistic GitLab REST API-shaped payloads (merge request list, approvals,
commits, pipelines) into :class:`afk_outcomes.providers.gitlab.GitLabAdapter`
and asserts the canonical ``EngineeringEntity`` / ``EngineeringEvent`` output
matches the locked AFK vocabulary:

* merge requests normalise to ``change_request`` exactly as pull requests do;
* the MR ``source_branch`` is carried as the change request ``head_ref``;
* reviews and commits are entities; pipelines are events only;
* emitted event types are a subset of the ten locked types;
* provider event IDs (pipeline ``id``, commit ``id``, approval user ``id``) are
  preserved where GitLab emits them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from afk_outcomes import EntityType, Provider
from afk_outcomes.models import EngineeringEntity, EngineeringEvent
from afk_outcomes.providers.gitlab import GitLabAdapter

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "afk_outcomes" / "gitlab_api"
REPOSITORY = "group/project"
BASE_URL = "https://gitlab.example.com/api/v4"

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+; keep importable on 3.9

# The ten locked event types (issue #446 / #447 locked vocabulary).
LOCKED_EVENT_TYPES = {
    "issue.opened",
    "issue.closed",
    "change_request.opened",
    "change_request.review_requested",
    "change_request.changes_requested",
    "change_request.approved",
    "change_request.merged",
    "change_request.closed",
    "pipeline.failed",
    "pipeline.succeeded",
}

# The locked entity vocabulary (issue #446 / #447 locked vocabulary).
LOCKED_ENTITY_TYPES = {"issue", "change_request", "review", "commit", "merge_event"}


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None

    @property
    def status_code(self) -> int:
        return 200


class FakeGitLabClient:
    """Minimal call-counting HTTP client backed by a fixture bundle.

    Routes the four GitLab endpoints the adapter uses and records every call
    (URL + params) so tests can assert a bounded API call count.
    """

    def __init__(self, payloads: dict[str, Any]) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> FakeResponse:
        self.calls.append((url, params))
        iid = _extract_iid(url)
        if url.endswith("/merge_requests"):
            return FakeResponse(self._payloads["merge_requests"])
        if "/approvals" in url:
            return FakeResponse(self._payloads["approvals"][str(iid)])
        if "/commits" in url:
            return FakeResponse(self._payloads["commits"][str(iid)])
        if "/pipelines" in url:
            return FakeResponse(self._payloads["pipelines"][str(iid)])
        raise AssertionError(f"unexpected URL in fake client: {url}")


def _extract_iid(url: str) -> int | None:
    match = re.search(r"/merge_requests/(\d+)", url)
    return int(match.group(1)) if match else None


def _load_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "payloads.json").read_text(encoding="utf-8"))


@pytest.fixture
def payloads() -> dict[str, Any]:
    return _load_fixture()


@pytest.fixture
def client(payloads: dict[str, Any]) -> FakeGitLabClient:
    return FakeGitLabClient(payloads)


@pytest.fixture
def adapter(client: FakeGitLabClient) -> GitLabAdapter:
    return GitLabAdapter(base_url=BASE_URL, client=client)


async def _fetch_both(
    adapter: GitLabAdapter, *, since: datetime | None = None, until: datetime | None = None
) -> tuple[list[EngineeringEntity], list[EngineeringEvent]]:
    entities = await adapter.fetch_entities(REPOSITORY, since=since, until=until)
    events = await adapter.fetch_events(REPOSITORY, since=since, until=until)
    return entities, events


# ── Protocol conformance ────────────────────────────────────────────────────


def test_provider_attribute_is_gitlab(adapter: GitLabAdapter) -> None:
    assert adapter.provider is Provider.GITLAB
    assert adapter.provider == "gitlab"


def test_adapter_exposes_protocol_methods(adapter: GitLabAdapter) -> None:
    import inspect

    for name in ("fetch_entities", "fetch_events"):
        method = getattr(adapter, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


# ── Normalization: merge requests → change_request ─────────────────────────


async def test_merge_requests_normalize_to_change_request_entities(
    adapter: GitLabAdapter,
) -> None:
    entities, _ = await _fetch_both(adapter)
    crs = [e for e in entities if e.entity_type is EntityType.CHANGE_REQUEST]
    by_number = {e.number: e for e in crs}

    assert set(by_number) == {117, 118}
    cr = by_number[118]
    assert cr.entity_id == "change_request:118"
    assert cr.provider is Provider.GITLAB
    assert cr.repository == REPOSITORY
    assert cr.title == "Develop-Loop: Consolidated run \u2014 Implemented issues #115, #116"
    assert cr.state == "merged"
    assert cr.author == "wyautomation"
    assert cr.url == "https://gitlab.example.com/group/project/-/merge_requests/118"
    assert cr.created_at == _parse_dt("2026-08-07T09:00:00Z")


async def test_entity_types_match_locked_vocabulary(adapter: GitLabAdapter) -> None:
    entities, _ = await _fetch_both(adapter)
    emitted = {e.entity_type.value for e in entities}
    assert emitted <= LOCKED_ENTITY_TYPES
    assert emitted == {"change_request", "review", "commit", "merge_event"}


async def test_merged_mr_emits_merge_event_entity(adapter: GitLabAdapter) -> None:
    entities, _ = await _fetch_both(adapter)
    merge_events = {e.number: e for e in entities if e.entity_type is EntityType.MERGE_EVENT}
    assert set(merge_events) == {117, 118}

    merged = merge_events[118]
    assert merged.entity_id == "merge_event:118"
    assert merged.provider is Provider.GITLAB
    assert merged.state == "merged"
    assert merged.author == "wyautomation"
    assert merged.created_at == _parse_dt("2026-08-07T11:30:00Z")


async def test_source_branch_is_carried_as_head_ref(adapter: GitLabAdapter) -> None:
    _, events = await _fetch_both(adapter)
    opened_118 = next(
        e
        for e in events
        if e.event_type == "change_request.opened"
        and e.entity_id == "change_request:118"
    )
    assert opened_118.payload["head_ref"] == "ai/feat/issues-115-116"

    opened_117 = next(
        e
        for e in events
        if e.event_type == "change_request.opened"
        and e.entity_id == "change_request:117"
    )
    assert opened_117.payload["head_ref"] == "chore/dependency-lockfile"


async def test_reviews_normalize_to_review_entities(adapter: GitLabAdapter) -> None:
    entities, _ = await _fetch_both(adapter)
    reviews = [e for e in entities if e.entity_type is EntityType.REVIEW]
    assert len(reviews) == 1
    review = reviews[0]
    assert review.entity_id == "review:118:13"
    assert review.state == "approved"
    assert review.author == "gatekeeper-bot"
    assert review.provider is Provider.GITLAB


async def test_commits_normalize_to_commit_entities(adapter: GitLabAdapter) -> None:
    entities, _ = await _fetch_both(adapter)
    commits = {e.entity_id: e for e in entities if e.entity_type is EntityType.COMMIT}
    assert commits.keys() == {
        "commit:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
        "commit:2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
        "commit:3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
    }
    commit = commits["commit:2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c"]
    assert commit.title == "feat: add session context detail panel (#115)"
    assert commit.author == "wyautomation"


# ── Events: change_request lifecycle + pipelines ───────────────────────────


async def test_event_types_match_locked_vocabulary(adapter: GitLabAdapter) -> None:
    _, events = await _fetch_both(adapter)
    emitted = {e.event_type for e in events}
    assert emitted <= LOCKED_EVENT_TYPES
    # A merged MR with a reviewer, an approval, and failed+succeeded pipelines
    # exercises the change_request and pipeline event types.
    assert "change_request.opened" in emitted
    assert "change_request.merged" in emitted
    assert "change_request.review_requested" in emitted
    assert "change_request.approved" in emitted
    assert "pipeline.failed" in emitted
    assert "pipeline.succeeded" in emitted


async def test_merged_mr_emits_merged_event(adapter: GitLabAdapter) -> None:
    _, events = await _fetch_both(adapter)
    merged = [
        e for e in events
        if e.event_type == "change_request.merged" and e.entity_id == "change_request:118"
    ]
    assert len(merged) == 1
    assert merged[0].occurred_at == _parse_dt("2026-08-07T11:30:00Z")


async def test_pipelines_are_events_only(adapter: GitLabAdapter) -> None:
    entities, events = await _fetch_both(adapter)
    # No pipeline entity type exists in the locked vocabulary.
    assert all(e.entity_type is not None for e in entities)
    pipeline_events = [e for e in events if e.event_type.startswith("pipeline.")]
    assert {e.event_type for e in pipeline_events} == {"pipeline.failed", "pipeline.succeeded"}

    # The failed-then-succeeded pipeline pair on MR 118 both surface.
    mr118 = [e for e in pipeline_events if e.entity_id == "change_request:118"]
    assert len(mr118) == 2


async def test_provider_event_ids_preserved(adapter: GitLabAdapter) -> None:
    _, events = await _fetch_both(adapter)
    pipeline_ids = {
        e.event_id for e in events if e.event_type.startswith("pipeline.")
    }
    assert pipeline_ids == {"pipeline:5511", "pipeline:5520", "pipeline:5521"}

    succeeded = next(
        e
        for e in events
        if e.event_type == "pipeline.succeeded"
        and e.payload.get("pipeline_id") == 5521
    )
    assert succeeded.payload["pipeline_id"] == 5521


# ── Window filtering ────────────────────────────────────────────────────────


async def test_window_bounds_map_to_gitlab_query_params(
    adapter: GitLabAdapter, client: FakeGitLabClient
) -> None:
    since = datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC)
    until = datetime(2026, 8, 8, 0, 0, 0, tzinfo=UTC)
    await adapter.fetch_entities(REPOSITORY, since=since, until=until)

    list_calls = [c for c in client.calls if c[0].endswith("/merge_requests")]
    assert len(list_calls) == 1
    url, params = list_calls[0]
    assert url == f"{BASE_URL}/projects/group%2Fproject/merge_requests"
    assert params == {
        "state": "all",
        "updated_after": "2026-08-07T00:00:00+00:00",
        "updated_before": "2026-08-08T00:00:00+00:00",
    }


# ── Batching: bounded API call count ────────────────────────────────────────


async def test_batched_enrichment_bounds_api_calls() -> None:
    """Enrichment is one call per dimension per MR, not one call per sub-item.

    A window of 5 merge requests — even with many commits, approvals, and
    pipelines each — must cost exactly ``1 (list) + 5 * 3 (enrichment)`` calls.
    """
    mr_count = 5
    payloads = _build_large_bundle(mr_count)
    client = FakeGitLabClient(payloads)
    adapter = GitLabAdapter(base_url=BASE_URL, client=client)

    await adapter.fetch_events(REPOSITORY)

    expected_calls = 1 + mr_count * 3
    assert len(client.calls) == expected_calls, (
        f"expected {expected_calls} API calls for {mr_count} MRs, got {len(client.calls)}"
    )

    # The calls are the list call plus, per MR, approvals + commits + pipelines.
    by_endpoint: dict[str, int] = {}
    for url, _params in client.calls:
        suffix = _endpoint_suffix(url)
        by_endpoint[suffix] = by_endpoint.get(suffix, 0) + 1
    assert by_endpoint["list"] == 1
    assert by_endpoint["approvals"] == mr_count
    assert by_endpoint["commits"] == mr_count
    assert by_endpoint["pipelines"] == mr_count


def _endpoint_suffix(url: str) -> str:
    if url.endswith("/merge_requests"):
        return "list"
    if "/approvals" in url:
        return "approvals"
    if "/commits" in url:
        return "commits"
    if "/pipelines" in url:
        return "pipelines"
    raise AssertionError(f"unknown endpoint: {url}")


def _build_large_bundle(mr_count: int) -> dict[str, Any]:
    merge_requests: list[dict[str, Any]] = []
    approvals: dict[str, Any] = {}
    commits: dict[str, Any] = {}
    pipelines: dict[str, Any] = {}
    for n in range(mr_count):
        iid = 1000 + n
        merge_requests.append(
            {
                "id": 80000 + n,
                "iid": iid,
                "title": f"feature {n}",
                "state": "merged",
                "created_at": "2026-08-07T08:00:00Z",
                "updated_at": "2026-08-07T09:00:00Z",
                "merged_at": "2026-08-07T09:00:00Z",
                "source_branch": f"feature/{n}",
                "target_branch": "main",
                "sha": f"{n:040x}",
                "merge_commit_sha": f"{n:040x}",
                "author": {"id": 7, "username": "wyautomation"},
                "reviewers": [],
                "web_url": f"https://gitlab.example.com/group/project/-/merge_requests/{iid}",
            }
        )
        approvals[str(iid)] = {
            "approved": True,
            "approved_by": [
                {
                    "user": {"id": 100 + a, "username": f"reviewer-{a}"},
                    "created_at": "2026-08-07T08:30:00Z",
                }
                for a in range(5)
            ],
        }
        commits[str(iid)] = [
            {
                "id": f"{n:020x}{c:020x}",
                "title": f"commit {c}",
                "author_name": "wyautomation",
                "authored_date": "2026-08-07T08:10:00Z",
            }
            for c in range(10)
        ]
        pipelines[str(iid)] = [
            {
                "id": 50000 + n * 100 + p,
                "status": "success",
                "ref": f"feature/{n}",
                "finished_at": "2026-08-07T08:50:00Z",
            }
            for p in range(8)
        ]
    return {
        "merge_requests": merge_requests,
        "approvals": approvals,
        "commits": commits,
        "pipelines": pipelines,
    }
