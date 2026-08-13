"""Real historical fixtures: GitHub and GitLab clusters with deliberate noise.

Each provider has a raw payload (shaped like the real provider API) and a
golden canonical output.  A deterministic builder (fixed ULID source, fixed
timestamps) turns the raw payload into an :class:`AFKRun`; the test asserts
the canonical JSON matches the committed golden byte-for-byte and that the
deliberate noise is present but correctly un-correlated.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from afk_outcomes import (
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    Provider,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
    SequenceULID,
    dumps_canonical,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "afk_outcomes"

GITHUB_ULID_MS = 1_786_615_829_000  # 2026-08-13T10:10:29Z
GITLAB_ULID_MS = 1_786_102_200_000  # 2026-08-07T11:30:00Z


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _numbers(text: str) -> set[int]:
    return {int(m.group(1)) for m in re.finditer(r"#(\d+)", text)}


def _resolved_numbers(body: str) -> set[int]:
    numbers: set[int] = set()
    keyword = r"\b(?:resolves|closes|fixes)\s+"
    # Capture a keyword followed by a comma/and/&-separated list of #numbers.
    for m in re.finditer(keyword + r"#?\d+(?:\s*(?:,|and|&)?\s*#?\d+)*", body, re.IGNORECASE):
        numbers.update(int(n) for n in re.findall(r"#?(\d+)", m.group(0)))
    return numbers


# ── Normalisation: raw provider payload → neutral shape ────────────────────


def _normalize_github(raw: dict) -> dict:
    return {
        "provider": Provider.GITHUB,
        "repository": raw["repository"],
        "run": raw["run"],
        "issues": [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "author": i["user"]["login"],
                "created_at": i["created_at"],
                "closed_at": i.get("closed_at"),
                "url": i["html_url"],
            }
            for i in raw["issues"]
        ],
        "change_requests": [
            {
                "number": cr["number"],
                "title": cr["title"],
                "state": cr["state"],
                "author": cr["user"]["login"],
                "created_at": cr["created_at"],
                "merged_at": cr.get("merged_at"),
                "branch": cr["head"]["ref"],
                "base": cr["base"]["ref"],
                "sha": cr["head"]["sha"],
                "body": cr.get("body", ""),
                "url": cr["html_url"],
            }
            for cr in raw["change_requests"]
        ],
        "commits": [
            {
                "sha": c["sha"],
                "message": c["message"],
                "author": c["author"]["name"],
                "date": c["author"]["date"],
            }
            for c in raw["commits"]
        ],
        "reviews": [
            {
                "id": r["id"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
                "author": r["user"]["login"],
                "commit_id": r["commit_id"],
            }
            for r in raw["reviews"]
        ],
        "merge_events": [
            {
                "number": m["change_request_number"],
                "merged_at": m["merged_at"],
                "actor": m["actor"],
                "sha": m["commit_sha"],
            }
            for m in raw["merge_events"]
        ],
    }


def _normalize_gitlab(raw: dict) -> dict:
    return {
        "provider": Provider.GITLAB,
        "repository": raw["repository"],
        "run": raw["run"],
        "issues": [
            {
                "number": i["iid"],
                "title": i["title"],
                "state": i["state"],
                "author": i["author"]["username"],
                "created_at": i["created_at"],
                "closed_at": i.get("closed_at"),
                "url": i["web_url"],
            }
            for i in raw["issues"]
        ],
        "change_requests": [
            {
                "number": cr["iid"],
                "title": cr["title"],
                "state": cr["state"],
                "author": cr["author"]["username"],
                "created_at": cr["created_at"],
                "merged_at": cr.get("merged_at"),
                "branch": cr["source_branch"],
                "base": cr["target_branch"],
                "sha": cr["sha"],
                "body": cr.get("description", ""),
                "url": cr["web_url"],
            }
            for cr in raw["merge_requests"]
        ],
        "commits": [
            {
                "sha": c["id"],
                "message": c["title"],
                "author": c["author_name"],
                "date": c["created_at"],
            }
            for c in raw["commits"]
        ],
        "reviews": [
            {
                "id": r["id"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
                "author": r["author"]["username"],
                "commit_id": r["commit_id"],
            }
            for r in raw["reviews"]
        ],
        "merge_events": [
            {
                "number": m["merge_request_iid"],
                "merged_at": m["merged_at"],
                "actor": m["actor"],
                "sha": m["commit_sha"],
            }
            for m in raw["merge_events"]
        ],
    }


# ── Builder: neutral shape → AFKRun ───────────────────────────────────────


def build_run(neutral: dict, ulid_ms: int) -> AFKRun:
    provider: Provider = neutral["provider"]
    repository: str = neutral["repository"]
    run_meta: dict = neutral["run"]
    ulid = SequenceULID(timestamp_ms=ulid_ms)

    afk_run_id = ulid.next_ulid()

    # The cluster's change_request is the one whose title matches the run's title.
    cluster_cr = next(
        cr for cr in neutral["change_requests"] if cr["title"] == run_meta["title"]
    )
    cluster_number = cluster_cr["number"]
    resolved_numbers = _resolved_numbers(cluster_cr["body"])

    entities: list[EngineeringEntity] = []
    events: list[EngineeringEvent] = []

    def add_event(entity_id: str, event_type: str, occurred_at: str, actor: str | None,
                  payload: dict | None = None) -> None:
        events.append(
            EngineeringEvent(
                event_id=f"{entity_id}:{event_type}",
                event_type=event_type,
                provider=provider,
                entity_id=entity_id,
                occurred_at=_parse_dt(occurred_at),
                actor=actor,
                payload=payload or {},
            )
        )

    # Issues
    for issue in neutral["issues"]:
        entity_id = f"issue:{issue['number']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.ISSUE,
                provider=provider,
                repository=repository,
                number=issue["number"],
                title=issue["title"],
                state=issue["state"],
                author=issue["author"],
                url=issue["url"],
                created_at=_parse_dt(issue["created_at"]),
            )
        )
        add_event(entity_id, "opened", issue["created_at"], issue["author"])
        if issue["closed_at"]:
            add_event(entity_id, "closed", issue["closed_at"], issue["author"])

    # Change requests
    for cr in neutral["change_requests"]:
        entity_id = f"change_request:{cr['number']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.CHANGE_REQUEST,
                provider=provider,
                repository=repository,
                number=cr["number"],
                title=cr["title"],
                state=cr["state"],
                author=cr["author"],
                url=cr["url"],
                created_at=_parse_dt(cr["created_at"]),
            )
        )
        add_event(entity_id, "opened", cr["created_at"], cr["author"])

    # Commits
    for commit in neutral["commits"]:
        entity_id = f"commit:{commit['sha']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.COMMIT,
                provider=provider,
                repository=repository,
                title=commit["message"],
                author=commit["author"],
                created_at=_parse_dt(commit["date"]),
            )
        )
        add_event(
            entity_id,
            "committed",
            commit["date"],
            commit["author"],
            payload={"sha": commit["sha"], "message": commit["message"]},
        )

    # Reviews
    for review in neutral["reviews"]:
        entity_id = f"review:{review['id']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.REVIEW,
                provider=provider,
                repository=repository,
                title=f"review {review['state']}",
                author=review["author"],
                created_at=_parse_dt(review["submitted_at"]),
            )
        )
        add_event(
            entity_id,
            "review_submitted",
            review["submitted_at"],
            review["author"],
            payload={"state": review["state"], "commit_id": review["commit_id"]},
        )

    # Merge events
    for merge in neutral["merge_events"]:
        entity_id = f"merge_event:{merge['number']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.MERGE_EVENT,
                provider=provider,
                repository=repository,
                number=merge["number"],
                title=f"merge {merge['number']}",
                author=merge["actor"],
                created_at=_parse_dt(merge["merged_at"]),
            )
        )
        add_event(
            entity_id,
            "merged",
            merge["merged_at"],
            merge["actor"],
            payload={"commit_sha": merge["sha"]},
        )

    events.sort(key=lambda e: (e.occurred_at, e.event_id))

    # ── Correlations ───────────────────────────────────────────────────────
    correlations: list[Correlation] = []
    entity_links: list[RunEntityLink] = []

    # Cluster change_request — high confidence.
    correlations.append(
        Correlation(
            correlation_id=ulid.next_ulid(),
            afk_run_id=afk_run_id,
            entity_id=f"change_request:{cluster_number}",
            correlation_confidence=1.0,
            method="change_request_merged",
            evidence=[
                CorrelationEvidence(
                    kind="branch_name",
                    source_entity_id=f"change_request:{cluster_number}",
                    detail=f"branch={cluster_cr['branch']}",
                    weight=1.0,
                ),
            ],
        )
    )
    entity_links.append(
        RunEntityLink(
            afk_run_id=afk_run_id,
            entity_id=f"change_request:{cluster_number}",
            role="resolved",
            correlation_confidence=1.0,
        )
    )

    # Resolved issues — high confidence, from explicit resolves/closes refs.
    for number in sorted(resolved_numbers):
        correlations.append(
            Correlation(
                correlation_id=ulid.next_ulid(),
                afk_run_id=afk_run_id,
                entity_id=f"issue:{number}",
                correlation_confidence=1.0,
                method="issue_resolved",
                evidence=[
                    CorrelationEvidence(
                        kind="issue_mention",
                        source_entity_id=f"change_request:{cluster_number}",
                        detail=f"resolves #{number}",
                        weight=1.0,
                    ),
                ],
            )
        )
        entity_links.append(
            RunEntityLink(
                afk_run_id=afk_run_id,
                entity_id=f"issue:{number}",
                role="resolved",
                correlation_confidence=1.0,
            )
        )

    # Mentioned-but-never-worked issues — low confidence (deliberate noise).
    mentioned_numbers = _numbers(cluster_cr["body"]) - resolved_numbers
    for number in sorted(mentioned_numbers):
        correlations.append(
            Correlation(
                correlation_id=ulid.next_ulid(),
                afk_run_id=afk_run_id,
                entity_id=f"issue:{number}",
                correlation_confidence=0.1,
                method="issue_mention",
                evidence=[
                    CorrelationEvidence(
                        kind="issue_mention",
                        source_entity_id=f"change_request:{cluster_number}",
                        detail=f"mentioned #{number}",
                        weight=0.1,
                    ),
                ],
            )
        )
        entity_links.append(
            RunEntityLink(
                afk_run_id=afk_run_id,
                entity_id=f"issue:{number}",
                role="referenced",
                correlation_confidence=0.1,
            )
        )

    # Unrelated change requests — noise (no correlation, link role "noise").
    for cr in neutral["change_requests"]:
        if cr["number"] != cluster_number:
            entity_links.append(
                RunEntityLink(
                    afk_run_id=afk_run_id,
                    entity_id=f"change_request:{cr['number']}",
                    role="noise",
                    correlation_confidence=0.0,
                )
            )

    # Commits referencing a non-cluster number — noise.
    for commit in neutral["commits"]:
        referenced = _numbers(commit["message"])
        known = resolved_numbers | {cluster_number}
        if referenced and not referenced.issubset(known):
            entity_links.append(
                RunEntityLink(
                    afk_run_id=afk_run_id,
                    entity_id=f"commit:{commit['sha']}",
                    role="noise",
                    correlation_confidence=0.0,
                )
            )

    # ── Outcome ────────────────────────────────────────────────────────────
    cluster_merge = next(
        m for m in neutral["merge_events"] if m["number"] == cluster_number
    )
    outcome = EngineeringOutcome(
        status=EngineeringOutcomeStatus.MERGED,
        change_request_ids=[f"change_request:{cluster_number}"],
        resolved_issue_ids=[f"issue:{n}" for n in sorted(resolved_numbers)],
        merge_event_id=f"merge_event:{cluster_number}",
        merged_at=_parse_dt(cluster_merge["merged_at"]),
    )

    session_link = RunSessionLink(
        afk_run_id=afk_run_id,
        session_id=run_meta.get("session_id"),
        external_session_id=run_meta.get("external_session_id"),
        started_at=_parse_dt(run_meta["started_at"]),
        finished_at=_parse_dt(run_meta["finished_at"]),
    )

    return AFKRun(
        afk_run_id=afk_run_id,
        provider=provider,
        status=RunStatus(run_meta["status"]),
        title=run_meta["title"],
        started_at=_parse_dt(run_meta["started_at"]),
        finished_at=_parse_dt(run_meta["finished_at"]),
        entities=entities,
        events=events,
        correlations=correlations,
        outcome=outcome,
        entity_links=entity_links,
        session_links=[session_link],
    )


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _load_golden(path: Path) -> str:
    return path.read_text().rstrip("\n")


def _build_github_run() -> AFKRun:
    raw = _load_json(FIXTURES_DIR / "github" / "raw_payload.json")
    return build_run(_normalize_github(raw), GITHUB_ULID_MS)


def _build_gitlab_run() -> AFKRun:
    raw = _load_json(FIXTURES_DIR / "gitlab" / "raw_payload.json")
    return build_run(_normalize_gitlab(raw), GITLAB_ULID_MS)


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "build", "ulid_ms"),
    [
        ("github", _build_github_run, GITHUB_ULID_MS),
        ("gitlab", _build_gitlab_run, GITLAB_ULID_MS),
    ],
)
def test_golden_canonical_output_matches(provider: str, build, ulid_ms: int) -> None:
    run = build()
    golden = _load_golden(FIXTURES_DIR / provider / "golden_run.json")
    assert dumps_canonical(run) == golden, "canonical output drifted from golden"


def test_github_cluster_represents_issues_437_to_440() -> None:
    run = _build_github_run()
    assert run.outcome is not None
    assert run.outcome.resolved_issue_ids == [
        "issue:437",
        "issue:438",
        "issue:439",
        "issue:440",
    ]
    assert run.outcome.change_request_ids == ["change_request:442"]
    assert run.title == "Develop-Loop: Consolidated run — Implemented issues #437, #438, #439, #440"


def test_github_noise_is_present_but_not_resolved() -> None:
    run = _build_github_run()
    entity_ids = {e.entity_id for e in run.entities}

    # Unrelated change_request #441 exists but is never part of the outcome.
    assert "change_request:441" in entity_ids
    assert "change_request:441" not in (run.outcome.change_request_ids if run.outcome else [])

    # Issue #436 is mentioned but never worked → low confidence, not resolved.
    assert "issue:436" in entity_ids
    assert run.outcome is not None
    assert "issue:436" not in run.outcome.resolved_issue_ids
    mention = [
        c for c in run.correlations
        if c.entity_id == "issue:436" and c.method == "issue_mention"
    ]
    assert len(mention) == 1
    assert mention[0].correlation_confidence == 0.1

    # The commit referencing the wrong issue is marked as noise, not resolved.
    wrong = [
        link for link in run.entity_links
        if link.entity_id.startswith("commit:") and link.role == "noise"
    ]
    assert wrong, "expected a noise commit (wrong-issue reference)"


def test_gitlab_cluster_represents_issues_115_and_116() -> None:
    run = _build_gitlab_run()
    assert run.outcome is not None
    assert run.outcome.resolved_issue_ids == ["issue:115", "issue:116"]
    assert run.outcome.change_request_ids == ["change_request:118"]


def test_fixtures_use_injected_ulid_source() -> None:
    """The same raw payload + fixed ULID source must reproduce identical runs."""
    assert dumps_canonical(_build_github_run()) == dumps_canonical(_build_github_run())
    assert dumps_canonical(_build_gitlab_run()) == dumps_canonical(_build_gitlab_run())


def test_afk_run_id_is_a_26_char_ulid() -> None:
    run = _build_github_run()
    assert len(run.afk_run_id) == 26
