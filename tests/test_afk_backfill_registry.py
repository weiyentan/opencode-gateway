"""Unit tests for the registry-driven AFK backfill operator loop.

The operator (``scripts/afk_backfill_registry.py``) turns the single-repository
backfill engine into a reusable, manifest-driven loop.  These tests cover the
four behaviors that keep the loop safe and predictable:

* manifest validation (JSON array or newline records; no PyYAML dependency),
* deterministic ordering (canonical sort; independent of file order),
* strict mode validation (exactly one of ``--dry-run`` / ``--confirm``),
* aggregate counter summation across the per-repository reports.
"""

from datetime import UTC, datetime

import pytest

from afk_outcomes import Provider
from scripts.afk_backfill import BackfillReport
from scripts.afk_backfill_registry import (
    ManifestEntry,
    ManifestError,
    aggregate_reports,
    format_aggregate_report,
    ordered_entries,
    parse_args,
    parse_manifest,
)

SINCE = datetime(2026, 8, 1, tzinfo=UTC)
UNTIL = datetime(2026, 8, 14, tzinfo=UTC)


def make_report(
    repository: str,
    *,
    change_requests: int = 0,
    issues: int = 0,
    sessions: int = 0,
    explicit: int = 0,
    high: int = 0,
    inferred: int = 0,
    ambiguous: int = 0,
    unmatched: int = 0,
) -> BackfillReport:
    """Build a per-repository BackfillReport with explicit counters."""
    return BackfillReport(
        provider=Provider("github"),
        repository=repository,
        since=SINCE,
        until=UNTIL,
        dry_run=True,
        change_requests_scanned=change_requests,
        issues_scanned=issues,
        sessions_considered=sessions,
        explicit_matches=explicit,
        high_matches=high,
        inferred_matches=inferred,
        ambiguous=ambiguous,
        unmatched=unmatched,
    )


# ── Manifest validation ─────────────────────────────────────────────────────


class TestManifestValidation:
    """parse_manifest accepts JSON arrays and newline records, and rejects
    anything that is not a clean provider/repository pair."""

    def test_json_array_parses_entries(self):
        text = (
            '[{"provider": "github", "repository": "owner/repo"},'
            ' {"provider": "gitlab", "repository": "group/project"}]'
        )
        entries = parse_manifest(text)
        assert entries == [
            ManifestEntry(provider=Provider("github"), repository="owner/repo"),
            ManifestEntry(provider=Provider("gitlab"), repository="group/project"),
        ]

    def test_newline_records_parse(self):
        text = "github owner/repo\ngitlab,group/project\n"
        entries = parse_manifest(text)
        assert [e.repository for e in entries] == ["owner/repo", "group/project"]
        assert [e.provider.value for e in entries] == ["github", "gitlab"]

    def test_comments_and_blank_lines_are_skipped(self):
        text = (
            "# the screenshot-selected repositories\n\n  \n"
            "github owner/repo\n# trailing comment\n"
        )
        entries = parse_manifest(text)
        assert len(entries) == 1
        assert entries[0].repository == "owner/repo"

    def test_invalid_provider_is_rejected(self):
        with pytest.raises(ManifestError, match="provider"):
            parse_manifest('[{"provider": "gitlabx", "repository": "owner/repo"}]')

    def test_repository_without_slash_is_rejected(self):
        with pytest.raises(ManifestError, match="owner/repository"):
            parse_manifest("github owneronly\n")

    def test_repository_with_spaces_is_rejected(self):
        with pytest.raises(ManifestError, match="owner/repository"):
            parse_manifest("github owner/rep o\n")

    def test_empty_manifest_is_rejected(self):
        with pytest.raises(ManifestError, match="no entries"):
            parse_manifest("[]")

    def test_blank_text_is_rejected(self):
        with pytest.raises(ManifestError, match="no entries"):
            parse_manifest("\n# nothing here\n")

    def test_malformed_json_is_rejected(self):
        with pytest.raises(ManifestError, match="manifest"):
            parse_manifest('[{"provider": "github", "repository": ')

    def test_duplicate_pair_is_rejected(self):
        text = (
            '[{"provider": "github", "repository": "owner/repo"},'
            ' {"provider": "github", "repository": "owner/repo"}]'
        )
        with pytest.raises(ManifestError, match="duplicate"):
            parse_manifest(text)

    def test_newline_record_with_too_many_fields_is_rejected(self):
        with pytest.raises(ManifestError, match="line 1"):
            parse_manifest("github owner/repo extra\n")


# ── Deterministic ordering ──────────────────────────────────────────────────


class TestDeterministicOrdering:
    """ordered_entries returns a canonical order independent of file order."""

    def test_entries_are_sorted_by_provider_then_repository(self):
        entries = [
            ManifestEntry(provider=Provider("gitlab"), repository="z-group/z-project"),
            ManifestEntry(provider=Provider("github"), repository="b-owner/b-repo"),
            ManifestEntry(provider=Provider("github"), repository="a-owner/a-repo"),
        ]
        ordered = ordered_entries(entries)
        assert [(e.provider.value, e.repository) for e in ordered] == [
            ("github", "a-owner/a-repo"),
            ("github", "b-owner/b-repo"),
            ("gitlab", "z-group/z-project"),
        ]

    def test_order_is_stable_across_input_shuffles(self):
        entries = [
            ManifestEntry(provider=Provider("github"), repository="owner/beta"),
            ManifestEntry(provider=Provider("github"), repository="owner/alpha"),
            ManifestEntry(provider=Provider("gitlab"), repository="group/gamma"),
        ]
        reversed_entries = list(reversed(entries))
        assert ordered_entries(entries) == ordered_entries(reversed_entries)

    def test_full_manifest_round_trip_is_ordered(self):
        text = (
            "gitlab z-group/z-project\n"
            "github b-owner/b-repo\n"
            "github a-owner/a-repo\n"
        )
        ordered = ordered_entries(parse_manifest(text))
        assert [e.repository for e in ordered] == [
            "a-owner/a-repo",
            "b-owner/b-repo",
            "z-group/z-project",
        ]


# ── Mode validation ─────────────────────────────────────────────────────────


class TestModeValidation:
    """The operator accepts exactly one of --dry-run / --confirm."""

    def test_dry_run_mode_parses(self):
        args = parse_args(["--manifest", "repos.json", "--dry-run"])
        assert args.dry_run is True
        assert args.confirm is False

    def test_confirm_mode_parses(self):
        args = parse_args(["--manifest", "repos.json", "--confirm"])
        assert args.confirm is True
        assert args.dry_run is False

    def test_both_modes_are_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--manifest", "repos.json", "--dry-run", "--confirm"])

    def test_neither_mode_is_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--manifest", "repos.json"])

    def test_since_after_until_is_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(
                [
                    "--manifest",
                    "repos.json",
                    "--dry-run",
                    "--since",
                    "2026-08-14",
                    "--until",
                    "2026-08-01",
                ]
            )

    def test_since_and_until_bounds_parse(self):
        args = parse_args(
            [
                "--manifest",
                "repos.json",
                "--dry-run",
                "--since",
                "2026-08-01",
                "--until",
                "2026-08-14",
            ]
        )
        assert args.since == SINCE
        assert args.until == UNTIL


# ── Aggregate counters ──────────────────────────────────────────────────────


class TestAggregateCounters:
    """aggregate_reports sums every per-repository counter and tracks failures."""

    def test_aggregate_sums_all_counters(self):
        reports = [
            make_report(
                "owner/one",
                change_requests=3,
                issues=2,
                sessions=5,
                explicit=1,
                high=2,
                inferred=3,
                ambiguous=4,
                unmatched=5,
            ),
            make_report(
                "owner/two",
                change_requests=7,
                issues=8,
                sessions=15,
                explicit=6,
                high=4,
                inferred=2,
                ambiguous=1,
                unmatched=0,
            ),
        ]
        entries = [
            ManifestEntry(provider=Provider("github"), repository="owner/one"),
            ManifestEntry(provider=Provider("github"), repository="owner/two"),
        ]
        aggregate = aggregate_reports(
            entries, reports, [], since=SINCE, until=UNTIL, dry_run=True
        )
        assert aggregate.change_requests_scanned == 10
        assert aggregate.issues_scanned == 10
        assert aggregate.sessions_considered == 20
        assert aggregate.explicit_matches == 7
        assert aggregate.high_matches == 6
        assert aggregate.inferred_matches == 5
        assert aggregate.ambiguous == 5
        assert aggregate.unmatched == 5

    def test_aggregate_tracks_ok_and_failed_entries(self):
        reports = [make_report("owner/one", change_requests=1)]
        entries = [
            ManifestEntry(provider=Provider("github"), repository="owner/one"),
            ManifestEntry(provider=Provider("gitlab"), repository="group/two"),
        ]
        aggregate = aggregate_reports(
            entries,
            reports,
            ["gitlab:group/two: 401 Unauthorized"],
            since=SINCE,
            until=UNTIL,
            dry_run=True,
        )
        assert aggregate.labels == ["github:owner/one", "gitlab:group/two"]
        assert len(aggregate.reports) == 1
        assert aggregate.errors == ["gitlab:group/two: 401 Unauthorized"]

    def test_format_includes_mode_totals_and_per_repository_lines(self):
        reports = [
            make_report("owner/one", change_requests=2, sessions=3, explicit=1),
            make_report("owner/two", issues=4, ambiguous=1),
        ]
        entries = [
            ManifestEntry(provider=Provider("github"), repository="owner/one"),
            ManifestEntry(provider=Provider("github"), repository="owner/two"),
        ]
        aggregate = aggregate_reports(
            entries, reports, [], since=SINCE, until=UNTIL, dry_run=True
        )
        rendered = format_aggregate_report(aggregate)
        assert "mode: dry-run" in rendered
        assert "change_requests scanned: 2" in rendered
        assert "issues scanned: 4" in rendered
        assert "sessions considered: 3" in rendered
        assert "explicit matches: 1" in rendered
        assert "ambiguous: 1" in rendered
        assert "github owner/one" in rendered
        assert "github owner/two" in rendered
        assert "re-run with --confirm to persist" in rendered

    def test_format_confirm_mode_has_no_dry_run_note(self):
        aggregate = aggregate_reports(
            [], [], [], since=SINCE, until=UNTIL, dry_run=False
        )
        rendered = format_aggregate_report(aggregate)
        assert "mode: write" in rendered
        assert "dry-run" not in rendered
