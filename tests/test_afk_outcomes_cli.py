"""Unit tests for the AFK outcome backfill CLI (issue #449).

Drives ``scripts/afk_backfill.py`` through its public seams:

* argparse/validation of ``--provider`` / ``--repository`` / ``--since`` /
  ``--until`` / ``--dry-run`` / ``--show-evidence``;
* the windowed ``run_backfill`` orchestration (fake provider API client +
  mocked asyncpg connection) through the real GitHub adapter, correlation
  engine, and repository write semantics;
* deterministic session-keyed run identity (re-runs converge to the same
  ``afk_run_id``) and entity-mapping reuse of existing runs;
* dry-run report counters (explicit/high/inferred matches,
  ambiguous/unmatched) over a known fixture window.

Integration tests against docker-compose Postgres live under
``tests/integration/test_afk_outcomes_cli.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from afk_outcomes import Correlation, Provider
from afk_outcomes.providers.github import GitHubAdapter
from scripts.afk_backfill import (
    EXPLICIT_METHOD,
    BackfillReport,
    PrefetchedWindow,
    SessionKeyedULID,
    _match_buckets,
    _parse_args,
    format_report,
    run_backfill,
)
from tests.conftest import mock_row

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+
REPOSITORY = "weiyentan/opencode-gateway"
SINCE = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 8, 1, 23, 59, 59, tzinfo=UTC)
RUN_START = datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC)
RUN_FINISH = datetime(2026, 8, 1, 10, 30, 0, tzinfo=UTC)
SESSION_ID = "11111111-1111-1111-1111-111111111111"


# ── Fixture payloads (GitHub REST-shaped, adapter-locked vocabulary) ────────


def _issue(number: int, *, created: str, updated: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "open",
        "user": {"login": "alice"},
        "created_at": created,
        "updated_at": updated,
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
    }


def _pull(number: int, *, updated: bool, merged: bool = False) -> dict:
    pull = {
        "number": number,
        "title": "Fix caching bug",
        "state": "closed" if merged else "open",
        "merged": merged,
        "user": {"login": "alice"},
        "created_at": "2026-08-01T08:00:00Z",
        "head": {"ref": "feature/caching", "sha": f"sha{number}"},
        "base": {"ref": "main"},
        "requested_reviewers": [],
        "html_url": f"https://github.com/{REPOSITORY}/pulls/{number}",
    }
    if updated:
        pull["updated_at"] = "2026-08-01T10:00:00Z"
    if merged:
        pull["closed_at"] = "2026-08-01T10:30:00Z"
        pull["merged_at"] = "2026-08-01T10:30:00Z"
        pull["merge_commit_sha"] = f"merge-{number}"
        pull["merged_by"] = {"login": "carol"}
    return pull


def _payloads(*, ambiguous: bool = False) -> dict:
    """Fixture windows over the locked GitHub adapter vocabulary.

    Default window: one owning change request (title matches the session),
    one issue referenced by a commit message, and one unrelated issue that
    only overlaps temporally — exercising the high and inferred buckets.

    ``ambiguous=True``: two change requests share the session's title (no
    updated_at on any entity, so no temporal noise) — the correlation engine
    must surface the tie as an ``ambiguous`` unresolved outcome.
    """
    if ambiguous:
        return {
            "repository": REPOSITORY,
            "issues": [],
            "pulls": [_pull(300, updated=False), _pull(310, updated=False)],
            "reviews": {},
            "commits": {},
            "check_runs": {
                "sha300": {"check_runs": []},
                "sha310": {"check_runs": []},
            },
        }
    return {
        "repository": REPOSITORY,
        "issues": [
            _issue(301, created="2026-08-01T09:00:00Z", updated="2026-08-01T10:00:00Z"),
            _issue(302, created="2026-08-01T08:30:00Z", updated="2026-08-01T09:00:00Z"),
        ],
        "pulls": [_pull(300, updated=True, merged=True)],
        "reviews": {
            "300": [
                {
                    "id": 5001,
                    "user": {"login": "bob"},
                    "state": "APPROVED",
                    "submitted_at": "2026-08-01T09:30:00Z",
                    "commit_id": "c1",
                }
            ]
        },
        "commits": {
            "300": [
                {
                    "sha": "c1",
                    "commit": {
                        "message": "fix #301 caching bug",
                        "author": {"name": "alice", "date": "2026-08-01T09:00:00Z"},
                    },
                }
            ]
        },
        "check_runs": {"sha300": {"check_runs": []}},
    }


class FakeGitHubApi:
    """Serves GitHub REST-shaped fixture payloads by path; records calls."""

    def __init__(self, payloads: dict) -> None:
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> object:
        self.calls.append((path, params or {}))
        repo = self._payloads["repository"]
        if path == f"/repos/{repo}/issues":
            return self._payloads["issues"]
        if path == f"/repos/{repo}/pulls":
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


def _session_row(title: str = "Fix caching bug") -> dict:
    return {
        "id": SESSION_ID,
        "external_session_id": "ses_111111",
        "first_message_at": RUN_START,
        "last_message_at": RUN_FINISH,
        "title": title,
    }


def _mock_conn(sessions: list[dict], *, existing_run_id: str | None = None) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[mock_row(row) for row in sessions])
    conn.fetchrow = AsyncMock(
        return_value=mock_row({"afk_run_id": existing_run_id})
        if existing_run_id is not None
        else None
    )
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


async def _run(
    conn: AsyncMock,
    client: FakeGitHubApi,
    *,
    dry_run: bool = False,
    show_evidence: bool = False,
    since: datetime = SINCE,
    until: datetime = UNTIL,
) -> BackfillReport:
    return await run_backfill(
        conn,
        adapter=GitHubAdapter(client),
        repository=REPOSITORY,
        since=since,
        until=until,
        dry_run=dry_run,
        show_evidence=show_evidence,
    )


def _afk_runs_insert_ids(conn: AsyncMock) -> list[str]:
    return [
        call.args[1]
        for call in conn.execute.call_args_list
        if "INSERT INTO afk_runs" in call.args[0]
    ]


# ── argparse / validation ───────────────────────────────────────────────────


def test_parse_args_requires_provider_and_repository() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])
    with pytest.raises(SystemExit):
        _parse_args(["--provider", "github"])
    with pytest.raises(SystemExit):
        _parse_args(["--repository", "owner/repo"])


def test_parse_args_accepts_window_and_flags() -> None:
    args = _parse_args(
        [
            "--provider",
            "github",
            "--repository",
            "owner/repo",
            "--since",
            "2026-08-01T00:00:00Z",
            "--until",
            "2026-08-02",
            "--dry-run",
            "--show-evidence",
        ]
    )
    assert args.provider == "github"
    assert args.repository == "owner/repo"
    assert args.since == datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    assert args.until == datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)
    assert args.dry_run is True
    assert args.show_evidence is True


def test_parse_args_rejects_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--provider", "bitbucket", "--repository", "owner/repo"])


def test_parse_args_rejects_since_after_until() -> None:
    with pytest.raises(SystemExit):
        _parse_args(
            [
                "--provider",
                "github",
                "--repository",
                "owner/repo",
                "--since",
                "2026-08-10",
                "--until",
                "2026-08-01",
            ]
        )


def test_parse_args_rejects_invalid_datetime() -> None:
    with pytest.raises(SystemExit):
        _parse_args(
            ["--provider", "github", "--repository", "owner/repo", "--since", "not-a-date"]
        )


def test_parse_args_defaults_to_recent_window() -> None:
    args = _parse_args(["--provider", "github", "--repository", "owner/repo"])
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    assert abs(args.until - now) < timedelta(minutes=1)
    assert abs(args.since - (now - timedelta(days=7))) < timedelta(minutes=1)


# ── Deterministic session-keyed run identity ────────────────────────────────


def test_session_keyed_ulid_is_deterministic_per_session() -> None:
    first = SessionKeyedULID()
    first.set_session(seed="ses-1", timestamp_ms=1_786_000_000_000)
    run_id = first.next_ulid()
    correlation_id = first.next_ulid()
    assert run_id != correlation_id

    replay = SessionKeyedULID()
    replay.set_session(seed="ses-1", timestamp_ms=1_786_000_000_000)
    assert replay.next_ulid() == run_id
    assert replay.next_ulid() == correlation_id

    other = SessionKeyedULID()
    other.set_session(seed="ses-2", timestamp_ms=1_786_000_000_000)
    assert other.next_ulid() != run_id


# ── Dry-run: report and no writes ───────────────────────────────────────────


async def test_dry_run_writes_nothing() -> None:
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads()), dry_run=True)

    assert report.dry_run is True
    conn.execute.assert_not_called()  # no insert/update issued
    conn.fetchrow.assert_not_called()  # no existing-run lookup either


async def test_dry_run_report_matches_real_run_report() -> None:
    """Dry-run counts are consistent with what a real write would store."""
    conn = _mock_conn([_session_row()])
    client = FakeGitHubApi(_payloads())

    dry = await _run(conn, client, dry_run=True)
    real = await _run(conn, client, dry_run=False)

    counters = (
        "change_requests_scanned",
        "issues_scanned",
        "sessions_considered",
        "explicit_matches",
        "high_matches",
        "inferred_matches",
        "ambiguous",
        "unmatched",
    )
    for name in counters:
        assert getattr(real, name) == getattr(dry, name), name


# ── Report counters over the fixture window ─────────────────────────────────


async def test_report_counts_resolution_buckets() -> None:
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads()), dry_run=True)

    assert report.provider is Provider.GITHUB
    assert report.repository == REPOSITORY
    # 1 owning CR + 2 issues in the window; 1 session considered.
    assert report.change_requests_scanned == 1
    assert report.issues_scanned == 2
    assert report.sessions_considered == 1
    # high: owning CR (issue_reference 1.0) + issue:301 (commit ref 0.6);
    # inferred: issue:302 (temporal overlap 0.4); nothing ambiguous/unmatched.
    assert report.explicit_matches == 0
    assert report.high_matches == 2
    assert report.inferred_matches == 1
    assert report.ambiguous == 0
    assert report.unmatched == 0


async def test_show_evidence_emits_per_match_evidence_lines() -> None:
    conn = _mock_conn([_session_row()])
    report = await _run(
        conn, FakeGitHubApi(_payloads()), dry_run=True, show_evidence=True
    )

    assert report.evidence_lines
    joined = "\n".join(report.evidence_lines)
    assert "match change_request:300 method=issue_reference confidence=1" in joined
    assert "match issue:301 method=commit_issue_reference confidence=0.6" in joined
    assert "match issue:302 method=temporal_inference confidence=0.4" in joined
    assert "title_match" in joined
    assert "commit_reference" in joined
    assert "temporal_overlap" in joined


async def test_ambiguous_and_unmatched_are_reported() -> None:
    # Two change requests share the session's title → ambiguous tie.
    conn = _mock_conn([_session_row()])
    ambiguous = await _run(
        conn, FakeGitHubApi(_payloads(ambiguous=True)), dry_run=True
    )
    assert ambiguous.ambiguous == 1
    assert ambiguous.unmatched == 0
    assert ambiguous.explicit_matches == 0
    assert ambiguous.high_matches == 0
    assert ambiguous.inferred_matches == 0

    # A session with no matching activity at all → unmatched.
    conn = _mock_conn([_session_row(title="Nothing to do here")])
    unmatched = await _run(
        conn, FakeGitHubApi(_payloads(ambiguous=True)), dry_run=True
    )
    assert unmatched.unmatched == 1
    assert unmatched.ambiguous == 0


# ── Real run: persistence, convergence, reconciliation ──────────────────────


async def test_real_run_persists_resolved_runs_via_repository() -> None:
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads()), dry_run=False)

    assert report.dry_run is False
    run_ids = _afk_runs_insert_ids(conn)
    assert len(run_ids) == 1
    assert len(run_ids[0]) == 26  # ULID

    sqls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("INSERT INTO delivery_log" in s for s in sqls)
    assert any("INSERT INTO engineering_events" in s for s in sqls)
    assert any("INSERT INTO afk_run_entities" in s for s in sqls)
    assert any("INSERT INTO afk_run_sessions" in s for s in sqls)


async def test_real_run_labels_backfill_events_observed_via_backfill() -> None:
    """Backfill-persisted facts must never masquerade as webhook observations.

    ``run_backfill`` is the shared write path for both the operator CLI and
    the consumer reconciliation loop; every engineering_events insert it
    issues must carry ``observed_via = 'backfill'`` (the 12th positional
    parameter, after the SQL string).
    """
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads()), dry_run=False)

    assert report.dry_run is False
    event_calls = [
        call
        for call in conn.execute.call_args_list
        if "INSERT INTO engineering_events" in call.args[0]
    ]
    assert event_calls, "no engineering_events insert issued"
    for call in event_calls:
        assert call.args[11] == "backfill"


def _unresolved_insert_params(conn: AsyncMock) -> list[tuple]:
    """Return (sql, params) for every unresolved_correlations insert issued."""
    return [
        (call.args[0], call.args[1:])
        for call in conn.execute.call_args_list
        if "INSERT INTO unresolved_correlations" in call.args[0]
    ]


async def test_real_run_persists_ambiguous_unresolved_entries() -> None:
    """Ambiguous outcomes are persisted (not just counted) with reason set."""
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads(ambiguous=True)), dry_run=False)

    assert report.ambiguous == 1
    calls = _unresolved_insert_params(conn)
    assert calls, "no unresolved_correlations insert issued"
    args = calls[0][1]
    assert "ambiguous" in args  # method + reason


async def test_real_run_persists_unmatched_unresolved_entries() -> None:
    conn = _mock_conn([_session_row(title="Nothing to do here")])
    report = await _run(conn, FakeGitHubApi(_payloads(ambiguous=True)), dry_run=False)

    assert report.unmatched == 1
    calls = _unresolved_insert_params(conn)
    assert calls, "no unresolved_correlations insert issued"
    assert "unmatched" in calls[0][1]


async def test_dry_run_persists_no_unresolved_entries() -> None:
    conn = _mock_conn([_session_row()])
    report = await _run(conn, FakeGitHubApi(_payloads(ambiguous=True)), dry_run=True)

    assert report.ambiguous == 1
    assert _unresolved_insert_params(conn) == []
    conn.execute.assert_not_called()  # nothing written at all


async def test_rerun_same_window_converges_to_identical_run_ids() -> None:
    """Re-running the same window resolves the same session to the same run."""
    conn = _mock_conn([_session_row()])
    client = FakeGitHubApi(_payloads())

    await _run(conn, client)
    first_ids = _afk_runs_insert_ids(conn)
    conn.execute.reset_mock()

    await _run(conn, client)
    second_ids = _afk_runs_insert_ids(conn)

    assert second_ids == first_ids


async def test_existing_run_found_by_entity_mapping_is_reused() -> None:
    """A resolved run whose owning change request is already mapped reuses it."""
    existing = "01EXISTINGRUN000000000000000"
    conn = _mock_conn([_session_row()], existing_run_id=existing)

    await _run(conn, FakeGitHubApi(_payloads()), dry_run=False)

    run_ids = _afk_runs_insert_ids(conn)
    assert run_ids, "no afk_runs insert issued"
    assert run_ids == [existing], "entity-mapped run id must be reused"


async def test_bounded_window_is_passed_to_provider() -> None:
    conn = _mock_conn([_session_row()])
    client = FakeGitHubApi(_payloads())
    since = datetime(2026, 8, 1, 6, 0, 0, tzinfo=UTC)
    until = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

    await _run(conn, client, since=since, until=until)

    pull_params = [params for path, params in client.calls if path.endswith("/pulls")]
    assert pull_params, "provider pull listing was not called"
    assert pull_params[0]["since"] == "2026-08-01T06:00:00Z"


async def test_run_backfill_uses_prefetched_window_without_refetching() -> None:
    """A pre-fetched window skips the adapter fetch (finding C seam)."""
    conn = _mock_conn([_session_row()])
    client = FakeGitHubApi(_payloads())
    adapter = GitHubAdapter(client)

    entities = await adapter.fetch_entities(REPOSITORY, since=SINCE, until=UNTIL)
    events = await adapter.fetch_events(REPOSITORY, since=SINCE, until=UNTIL)
    calls_before = len(client.calls)

    report = await run_backfill(
        conn,
        adapter=adapter,
        repository=REPOSITORY,
        since=SINCE,
        until=UNTIL,
        dry_run=True,
        prefetched=PrefetchedWindow(entities=entities, events=events),
    )

    assert len(client.calls) == calls_before  # no re-fetch issued
    assert report.change_requests_scanned == 1
    assert report.issues_scanned == 2
    assert report.high_matches == 2
    assert report.inferred_matches == 1


async def test_bounded_window_excluding_activity_reports_unmatched() -> None:
    """Reconciliation over a bounded window re-applies pull→correlate→persist."""
    conn = _mock_conn([_session_row()])
    later = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)
    report = await _run(
        conn,
        FakeGitHubApi(_payloads()),
        since=later,
        until=datetime(2026, 8, 10, 23, 59, 59, tzinfo=UTC),
        dry_run=True,
    )

    assert report.change_requests_scanned == 0
    assert report.issues_scanned == 0
    assert report.high_matches == 0
    assert report.inferred_matches == 0
    assert report.unmatched == 1  # session considered but nothing correlates


async def test_empty_window_reports_zero_everywhere() -> None:
    """A window with no sessions resolves to an all-zero report without error."""
    conn = _mock_conn([])
    report = await _run(conn, FakeGitHubApi(_payloads()), dry_run=True)

    assert report.sessions_considered == 0
    assert report.change_requests_scanned == 1  # entities still scanned
    assert report.explicit_matches == 0
    assert report.high_matches == 0
    assert report.inferred_matches == 0
    assert report.ambiguous == 0
    assert report.unmatched == 0
    conn.execute.assert_not_called()


# ── Reporting primitives ────────────────────────────────────────────────────


def test_match_buckets_partitions_explicit_high_inferred() -> None:
    correlations = [
        Correlation(
            correlation_id="a",
            afk_run_id="r",
            entity_id="issue:1",
            correlation_confidence=1.0,
            method=EXPLICIT_METHOD,
        ),
        Correlation(
            correlation_id="b",
            afk_run_id="r",
            entity_id="change_request:2",
            correlation_confidence=1.0,
            method="issue_reference",
        ),
        Correlation(
            correlation_id="c",
            afk_run_id="r",
            entity_id="issue:3",
            correlation_confidence=0.6,
            method="commit_issue_reference",
        ),
        Correlation(
            correlation_id="d",
            afk_run_id="r",
            entity_id="issue:4",
            correlation_confidence=0.4,
            method="temporal_inference",
        ),
    ]
    assert _match_buckets(correlations) == (1, 2, 1)


def test_format_report_renders_the_full_counter_set() -> None:
    report = BackfillReport(
        provider=Provider.GITHUB,
        repository=REPOSITORY,
        since=SINCE,
        until=UNTIL,
        dry_run=True,
        change_requests_scanned=1,
        issues_scanned=2,
        sessions_considered=1,
        explicit_matches=0,
        high_matches=2,
        inferred_matches=1,
        ambiguous=0,
        unmatched=1,
        evidence_lines=[
            "match issue:1 method=issue_reference confidence=1"
            " evidence=[title_match(source=change_request:2)]"
        ],
    )

    text = format_report(report)

    assert "AFK outcome backfill report" in text
    assert "provider: github" in text
    assert f"repository: {REPOSITORY}" in text
    assert "mode: dry-run" in text
    assert "change_requests scanned: 1" in text
    assert "issues scanned: 2" in text
    assert "sessions considered: 1" in text
    assert "explicit matches: 0" in text
    assert "high matches: 2" in text
    assert "inferred matches: 1" in text
    assert "ambiguous: 0" in text
    assert "unmatched: 1" in text
    assert "evidence:" in text
    assert "dry-run: no rows were written" in text
