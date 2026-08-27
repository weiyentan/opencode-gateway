#!/usr/bin/env python3
"""Registry-driven AFK backfill operator (reusable backfill loop).

The single-repository CLI (:mod:`scripts.afk_backfill`) handles one
provider/repository pair.  This operator turns that engine into a reusable
registry-driven loop: it reads a *repository manifest* — a JSON array of
``{"provider": ..., "repository": ...}`` objects or a newline-delimited
``provider repository`` / ``provider,repository`` records file (``#``
comments and blank lines allowed; parsed with stdlib only, no PyYAML
runtime dependency) — validates every provider/repository pair, then runs
the existing backfill engine sequentially per entry and prints one
aggregate report across the whole registry.

Usage::

    python scripts/afk_backfill_registry.py --manifest repos.json \\
        --since 2026-08-01 --until 2026-08-14 --dry-run

Mode is strict: exactly one of ``--dry-run`` / ``--confirm`` must be
supplied.  ``--dry-run`` writes nothing; ``--confirm`` persists resolved
runs through the same idempotent engine as the single-repository CLI.

Loop properties:

* **sequential** — entries are processed one at a time in a deterministic
  canonical order (sorted by provider, then repository), never concurrently;
* **fault-continuing** — a per-entry failure is recorded in the aggregate
  report and the loop continues with the next entry (exit code 1 if any
  entry failed, 0 otherwise);
* **aggregate** — every per-repository counter is summed into one report.

Provider credentials come from the environment (``GITHUB_TOKEN`` /
``GITLAB_TOKEN``) via the existing adapter seam — no token handling or
storage is implemented here.
"""

from __future__ import annotations

import argparse
import asyncio
import asyncpg
import httpx
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from afk_outcomes import Provider  # noqa: E402
from scripts.afk_backfill import (  # noqa: E402
    DEFAULT_WINDOW_DAYS,
    BackfillReport,
    _build_adapter,
    _parse_datetime,
    run_backfill,
)
from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger("afk_backfill_registry")

# Repository names: ``owner/repository`` (GitHub) or ``group/project``
# (GitLab).  Provider adapters receive the full string, so only shape is
# validated here — provider-specific ownership checks happen at fetch time.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")

PROVIDERS = {"github", "gitlab"}


async def _get_pool() -> asyncpg.Pool:
    """Create a production-compatible pool, including CNPG SSL settings."""
    settings = get_settings()
    options = {
        "host": settings.database_host,
        "port": settings.database_port,
        "database": settings.database_name,
        "user": settings.database_user,
        "password": settings.database_password,
        "min_size": 1,
        "max_size": 2,
    }
    if settings.database_ssl:
        options["ssl"] = settings.database_ssl
    return await asyncpg.create_pool(**options)


# ── Manifest model ──────────────────────────────────────────────────────────


class ManifestError(ValueError):
    """Raised when the repository manifest fails validation."""


@dataclass(frozen=True)
class ManifestEntry:
    """One validated provider/repository pair from the manifest."""

    provider: Provider
    repository: str


def _validate_pair(
    provider_raw: str, repository_raw: str, *, line_no: int | None = None
) -> ManifestEntry:
    """Validate one provider/repository pair into a ManifestEntry."""
    location = f"line {line_no}: " if line_no is not None else ""
    provider = provider_raw.strip().lower()
    if provider not in PROVIDERS:
        raise ManifestError(
            f"{location}unknown provider {provider_raw!r} (expected 'github' or 'gitlab')"
        )
    repository = repository_raw.strip()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ManifestError(
            f"{location}invalid repository {repository!r} (expected 'owner/repository')"
        )
    return ManifestEntry(provider=Provider(provider), repository=repository)


def _check_duplicates(entries: Sequence[ManifestEntry]) -> None:
    """Reject manifests that name the same provider/repository pair twice."""
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.provider.value, entry.repository)
        if key in seen:
            raise ManifestError(
                f"duplicate repository entry: {entry.provider.value}:{entry.repository}"
            )
        seen.add(key)


def _parse_json_manifest(text: str) -> list[ManifestEntry]:
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ManifestError("manifest JSON must be a top-level array of entries")
    entries: list[ManifestEntry] = []
    for item in data:
        if not isinstance(item, dict) or not {"provider", "repository"} <= set(item):
            raise ManifestError(
                "manifest entry must be an object with 'provider' and 'repository' keys"
            )
        entries.append(_validate_pair(str(item["provider"]), str(item["repository"])))
    return entries


def _parse_records_manifest(text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f for f in line.replace(",", " ").split() if f]
        if len(fields) != 2:
            raise ManifestError(
                f"line {line_no}: expected '<provider> <owner/repository>' records,"
                f" got {len(fields)} field(s)"
            )
        entries.append(
            _validate_pair(fields[0], fields[1], line_no=line_no)
        )
    return entries


def parse_manifest(text: str) -> list[ManifestEntry]:
    """Parse and validate a repository manifest (JSON array or newline records).

    A JSON array is detected by a leading ``[`` (after whitespace); anything
    else is parsed as newline records.  Returns entries in file order; use
    :func:`ordered_entries` for the canonical processing order.
    """
    if not text.strip():
        raise ManifestError("manifest contains no entries")
    entries = (
        _parse_json_manifest(text)
        if text.lstrip().startswith("[")
        else _parse_records_manifest(text)
    )
    if not entries:
        raise ManifestError("manifest contains no entries")
    _check_duplicates(entries)
    return entries


def ordered_entries(entries: Sequence[ManifestEntry]) -> list[ManifestEntry]:
    """Return entries in canonical deterministic order.

    Sorted by (provider, repository) so the processing order is independent
    of the manifest's file order — the same registry always runs in the same
    order, which keeps the sequential loop and its aggregate report stable.
    """
    return sorted(entries, key=lambda e: (e.provider.value, e.repository))


# ── Aggregate report ────────────────────────────────────────────────────────


@dataclass
class AggregateReport:
    """Summed counters across every per-repository backfill report."""

    since: datetime
    until: datetime
    dry_run: bool
    labels: list[str]
    reports: list[BackfillReport]
    errors: list[str]
    change_requests_scanned: int = 0
    issues_scanned: int = 0
    sessions_considered: int = 0
    explicit_matches: int = 0
    high_matches: int = 0
    inferred_matches: int = 0
    ambiguous: int = 0
    unmatched: int = 0


def aggregate_reports(
    entries: Sequence[ManifestEntry],
    reports: Sequence[BackfillReport],
    errors: Sequence[str],
    *,
    since: datetime,
    until: datetime,
    dry_run: bool,
) -> AggregateReport:
    """Sum per-repository reports (and per-entry errors) into one aggregate."""
    return AggregateReport(
        since=since,
        until=until,
        dry_run=dry_run,
        labels=[f"{e.provider.value}:{e.repository}" for e in entries],
        reports=list(reports),
        errors=list(errors),
        change_requests_scanned=sum(r.change_requests_scanned for r in reports),
        issues_scanned=sum(r.issues_scanned for r in reports),
        sessions_considered=sum(r.sessions_considered for r in reports),
        explicit_matches=sum(r.explicit_matches for r in reports),
        high_matches=sum(r.high_matches for r in reports),
        inferred_matches=sum(r.inferred_matches for r in reports),
        ambiguous=sum(r.ambiguous for r in reports),
        unmatched=sum(r.unmatched for r in reports),
    )


def format_aggregate_report(aggregate: AggregateReport) -> str:
    """Render the aggregate report (dry-run and write runs share the form)."""
    lines = [
        "AFK backfill registry report",
        f"window: {aggregate.since.isoformat()} .. {aggregate.until.isoformat()}",
        f"mode: {'dry-run' if aggregate.dry_run else 'write'}",
        f"repositories: {len(aggregate.labels)} processed"
        f" ({len(aggregate.reports)} ok, {len(aggregate.errors)} failed)",
        f"change_requests scanned: {aggregate.change_requests_scanned}",
        f"issues scanned: {aggregate.issues_scanned}",
        f"sessions considered: {aggregate.sessions_considered}",
        f"explicit matches: {aggregate.explicit_matches}",
        f"high matches: {aggregate.high_matches}",
        f"inferred matches: {aggregate.inferred_matches}",
        f"ambiguous: {aggregate.ambiguous}",
        f"unmatched: {aggregate.unmatched}",
        "per-repository:",
    ]
    for report in aggregate.reports:
        lines.append(
            f"  {report.provider.value} {report.repository}:"
            f" change_requests={report.change_requests_scanned}"
            f" issues={report.issues_scanned}"
            f" sessions={report.sessions_considered}"
            f" explicit={report.explicit_matches}"
            f" high={report.high_matches}"
            f" inferred={report.inferred_matches}"
            f" ambiguous={report.ambiguous}"
            f" unmatched={report.unmatched}"
        )
        if report.evidence_lines:
            lines.append(f"  evidence for {report.provider.value}:{report.repository}:")
            lines.extend(f"    {line}" for line in report.evidence_lines)
    if aggregate.errors:
        lines.append("errors:")
        lines.extend(f"  {error}" for error in aggregate.errors)
    if aggregate.dry_run:
        lines.append("dry-run: no rows were written; re-run with --confirm to persist.")
    return "\n".join(lines)


# ── The sequential registry loop ────────────────────────────────────────────


async def run_registry(
    conn,
    *,
    entries: Sequence[ManifestEntry],
    since: datetime,
    until: datetime,
    dry_run: bool,
    show_evidence: bool = False,
) -> AggregateReport:
    """Run the backfill engine sequentially over every manifest entry.

    Entries are processed one at a time in the given (already deterministic)
    order.  A failing entry is recorded and the loop continues; the returned
    aggregate sums the counters of every successful per-repository report.
    """
    reports: list[BackfillReport] = []
    errors: list[str] = []
    for entry in entries:
        label = f"{entry.provider.value}:{entry.repository}"
        logger.info("backfilling %s (dry_run=%s)", label, dry_run)
        client = None
        started = time.monotonic()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                logger.info(
                    "still processing %s (elapsed_seconds=%d)",
                    label,
                    int(time.monotonic() - started),
                )

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            adapter, client = _build_adapter(entry.provider.value)
            for attempt in range(1, 4):
                try:
                    report = await run_backfill(
                        conn,
                        adapter=adapter,
                        repository=entry.repository,
                        since=since,
                        until=until,
                        dry_run=dry_run,
                        show_evidence=show_evidence,
                    )
                    break
                except httpx.ReadTimeout:
                    if attempt == 3:
                        raise
                    delay = attempt * 10
                    logger.warning(
                        "provider timeout for %s; retrying in %ss (attempt %d/3)",
                        label,
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
            reports.append(report)
            logger.info(
                "completed %s (elapsed_seconds=%d)",
                label,
                int(time.monotonic() - started),
            )
        except Exception as exc:  # noqa: BLE001 - operator loop must keep going
            logger.exception("backfill failed for %s", label)
            errors.append(f"{label}: {exc}")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            if client is not None:
                await client.aclose()
    return aggregate_reports(
        entries, reports, errors, since=since, until=until, dry_run=dry_run
    )


# ── CLI wiring ──────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse operator arguments; exactly one of --dry-run/--confirm required."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the AFK outcome backfill engine sequentially over a repository"
            " manifest (JSON array or newline records) and print an aggregate"
            " report."
        ),
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the repository manifest (JSON array or newline records).",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        help="Process only entries for this provider.",
    )
    parser.add_argument(
        "--repository",
        help="Process only this owner/repository entry.",
    )
    parser.add_argument(
        "--since",
        type=_parse_datetime,
        default=None,
        help=(
            "Window start (ISO 8601; naive values assumed UTC). Defaults to"
            f" {DEFAULT_WINDOW_DAYS} days ago."
        ),
    )
    parser.add_argument(
        "--until",
        type=_parse_datetime,
        default=None,
        help="Window end (ISO 8601; naive values assumed UTC). Defaults to now.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the aggregate report without writing any rows.",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="Persist resolved runs (idempotent upserts) across all entries.",
    )
    parser.add_argument(
        "--show-evidence",
        action="store_true",
        help="Include per-match evidence lines in the per-repository output.",
    )
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    args.since = args.since if args.since is not None else now - timedelta(
        days=DEFAULT_WINDOW_DAYS
    )
    args.until = args.until if args.until is not None else now
    if args.since > args.until:
        parser.error("--since must not be after --until")
    return args


async def main(argv: list[str] | None = None) -> int:
    """Entry point — validate manifest, loop sequentially, print aggregate."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    manifest_text = Path(args.manifest).read_text(encoding="utf-8")
    entries = ordered_entries(parse_manifest(manifest_text))
    if args.provider is not None:
        entries = [entry for entry in entries if entry.provider.value == args.provider]
    if args.repository is not None:
        entries = [entry for entry in entries if entry.repository == args.repository]
    if not entries:
        raise ManifestError("repository filters matched no manifest entries")
    logger.info(
        "registry loaded: %d repositories (mode=%s)",
        len(entries),
        "dry-run" if args.dry_run else "write",
    )

    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            aggregate = await run_registry(
                conn,
                entries=entries,
                since=args.since,
                until=args.until,
                dry_run=args.dry_run,
                show_evidence=args.show_evidence,
            )
            print(format_aggregate_report(aggregate))
    finally:
        await pool.close()

    return 0 if not aggregate.errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
