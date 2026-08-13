"""GitHub provider adapter tests: normalization, batching, vocabulary.

Feeds the :class:`afk_outcomes.providers.github.GitHubAdapter` real
GitHub REST API-shaped payloads (loaded from
``tests/fixtures/afk_outcomes/github/rest_api_payloads.json``) through an
injectable fake client, and asserts canonical normalization, the locked
vocabulary, bounded API call counts, and provider event-ID preservation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from afk_outcomes.models import EntityType, Provider
from afk_outcomes.providers.github import GitHubAdapter

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "afk_outcomes"
REPOSITORY = "weiyentan/opencode-gateway"

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

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


def _load_payloads() -> dict:
    return json.loads((FIXTURES_DIR / "github" / "rest_api_payloads.json").read_text())


class RecordingGitHubApi:
    """A fake GitHub client: serves fixture payloads by path, records calls."""

    def __init__(self, payloads: dict) -> None:
        self._payloads = payloads
        self.calls: list[str] = []
        self.params_log: list[dict[str, str]] = []

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        self.calls.append(path)
        self.params_log.append(params or {})
        repo = re.escape(self._payloads["repository"])
        if path == f"/repos/{self._payloads['repository']}/issues":
            return self._payloads["issues"]
        if path == f"/repos/{self._payloads['repository']}/pulls":
            return self._payloads["pulls"]
        match = re.fullmatch(rf"/repos/{repo}/pulls/(\d+)/reviews", path)
        if match:
            return self._payloads["reviews"].get(match.group(1), [])
        match = re.fullmatch(rf"/repos/{repo}/pulls/(\d+)/commits", path)
        if match:
            return self._payloads["commits"].get(match.group(1), [])
        match = re.fullmatch(rf"/repos/{repo}/commits/([^/]+)/check-runs", path)
        if match:
            return self._payloads["check_runs"].get(match.group(1), {"check_runs": []})
        raise AssertionError(f"unexpected GitHub API path: {path}")


def _adapter() -> tuple[GitHubAdapter, RecordingGitHubApi]:
    client = RecordingGitHubApi(_load_payloads())
    return GitHubAdapter(client), client


# ── Entities ───────────────────────────────────────────────────────────────


async def test_fetch_entities_normalizes_issue_and_change_request() -> None:
    adapter, _ = _adapter()
    entities = await adapter.fetch_entities(REPOSITORY)
    by_id = {e.entity_id: e for e in entities}

    issue = by_id["issue:100"]
    assert issue.entity_type is EntityType.ISSUE
    assert issue.provider is Provider.GITHUB
    assert issue.number == 100
    assert issue.title == "Add session context detail panel"
    assert issue.state == "open"
    assert issue.author == "alice"

    cr = by_id["change_request:200"]
    assert cr.entity_type is EntityType.CHANGE_REQUEST
    assert cr.number == 200
    assert cr.state == "closed"
    assert cr.author == "alice"
    # Branch is carried as the change_request head_ref.
    assert cr.head_ref == "feature/gateway-adapter"


async def test_fetch_entities_includes_reviews_and_commits() -> None:
    adapter, _ = _adapter()
    entities = await adapter.fetch_entities(REPOSITORY)
    by_id = {e.entity_id: e for e in entities}

    review = by_id["review:1001"]
    assert review.entity_type is EntityType.REVIEW
    assert review.state == "approved"
    assert review.author == "bob"

    commit = by_id["commit:commit-a"]
    assert commit.entity_type is EntityType.COMMIT
    assert commit.title == "feat: implement GitHub provider adapter"
    assert commit.author == "alice"


async def test_entity_types_match_locked_vocabulary() -> None:
    adapter, _ = _adapter()
    entities = await adapter.fetch_entities(REPOSITORY)
    emitted = {e.entity_type for e in entities}
    assert emitted == {
        EntityType.ISSUE,
        EntityType.CHANGE_REQUEST,
        EntityType.REVIEW,
        EntityType.COMMIT,
    }


# ── Events ────────────────────────────────────────────────────────────────


async def test_fetch_events_emits_all_locked_event_types_and_nothing_else() -> None:
    adapter, _ = _adapter()
    events = await adapter.fetch_events(REPOSITORY)
    emitted = {e.event_type for e in events}
    assert emitted == LOCKED_EVENT_TYPES


async def test_provider_event_ids_preserved() -> None:
    adapter, _ = _adapter()
    events = await adapter.fetch_events(REPOSITORY)
    by_type: dict[str, object] = {}
    for event in events:
        by_type.setdefault(event.event_type, event)

    # GitHub emits stable database IDs for reviews and check-runs.
    assert getattr(by_type["change_request.approved"], "event_id") == "review:1001"
    assert getattr(by_type["change_request.changes_requested"], "event_id") == "review:1002"
    assert getattr(by_type["pipeline.succeeded"], "event_id") == "check_run:2001"
    assert getattr(by_type["pipeline.failed"], "event_id") == "check_run:2002"


async def test_merged_vs_closed_change_request_events() -> None:
    adapter, _ = _adapter()
    events = await adapter.fetch_events(REPOSITORY)

    merged_pr = [e for e in events if e.entity_id == "change_request:200"]
    closed_pr = [e for e in events if e.entity_id == "change_request:201"]

    assert any(e.event_type == "change_request.merged" for e in merged_pr)
    assert not any(e.event_type == "change_request.closed" for e in merged_pr)
    assert any(e.event_type == "change_request.closed" for e in closed_pr)
    assert not any(e.event_type == "change_request.merged" for e in closed_pr)


async def test_issue_closed_event_actor_is_closed_by() -> None:
    adapter, _ = _adapter()
    events = await adapter.fetch_events(REPOSITORY)
    closed = next(e for e in events if e.event_type == "issue.closed")
    assert closed.entity_id == "issue:101"
    assert closed.actor == "bob"


async def test_review_requested_event_carries_reviewer() -> None:
    adapter, _ = _adapter()
    events = await adapter.fetch_events(REPOSITORY)
    requested = [e for e in events if e.event_type == "change_request.review_requested"]
    assert len(requested) == 1
    assert requested[0].entity_id == "change_request:200"
    assert requested[0].payload == {"reviewer": "carol"}


# ── Batching ──────────────────────────────────────────────────────────────


async def test_fetch_entities_enrichment_is_batched() -> None:
    adapter, client = _adapter()
    await adapter.fetch_entities(REPOSITORY)

    num_crs = 2
    # issues + pulls + reviews-per-CR + commits-per-CR = 2 + 2N.
    assert len(client.calls) == 2 * num_crs + 2
    assert client.calls.count(f"/repos/{REPOSITORY}/issues") == 1
    assert client.calls.count(f"/repos/{REPOSITORY}/pulls") == 1


async def test_fetch_events_enrichment_is_batched() -> None:
    adapter, client = _adapter()
    await adapter.fetch_events(REPOSITORY)

    num_crs = 2
    # issues + pulls + reviews-per-CR + check-runs-per-CR = 2 + 2N.
    assert len(client.calls) == 2 * num_crs + 2


# ── Time window ───────────────────────────────────────────────────────────


async def test_fetch_entities_respects_time_window() -> None:
    adapter, _ = _adapter()
    since = datetime(2026, 8, 11, 0, 0, 0, tzinfo=UTC)
    until = datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC)
    entities = await adapter.fetch_entities(REPOSITORY, since=since, until=until)

    ids = {e.entity_id for e in entities}
    # Issues #100/#101 and change_request #200 predate the window; #201 is in it.
    assert ids == {"change_request:201", "commit:commit-c"}


async def test_fetch_events_respects_time_window() -> None:
    adapter, _ = _adapter()
    since = datetime(2026, 8, 11, 0, 0, 0, tzinfo=UTC)
    events = await adapter.fetch_events(REPOSITORY, since=since)

    emitted = {e.event_type for e in events}
    # change_request:201 (in window) yields opened + closed only.
    assert "change_request.opened" in emitted
    assert "change_request.closed" in emitted
    assert "change_request.merged" not in emitted
    assert "pipeline.succeeded" not in emitted


# ── Protocol conformance ──────────────────────────────────────────────────


async def test_adapter_exposes_provider_identity() -> None:
    adapter, _ = _adapter()
    assert adapter.provider is Provider.GITHUB
