#!/usr/bin/env python3
"""AFK outcome backfill and reconciliation CLI (issue #449).

A stdlib-argparse entry point that pulls a bounded window of engineering
activity from a provider (GitHub or GitLab) through the existing provider
adapters, correlates it against the Gateway's OpenCode sessions via the
:class:`afk_outcomes.correlation.CorrelationEngine`, and persists the
resolved runs via the :class:`afk_outcomes.repository.AsyncpgOutcomeRepository`
— the same domain models, normalization, and engine as the live path, so
reporting stays consistent.

The CLI is a thin orchestration layer: it never duplicates or forks
normalization or correlation logic, and all app-dependent wiring (config,
DB pool, the Gateway ``sessions`` lookup) lives here, keeping
``afk_outcomes/`` pure domain.

Usage::

    python scripts/afk_backfill.py --provider github \\
        --repository owner/repo --since 2026-08-01 --until 2026-08-14 \\
        [--dry-run] [--show-evidence]

Flags:
    --provider       github | gitlab (required)
    --repository     full owner/repo (or group/project) name (required)
    --since/--until  bounded reconciliation window (ISO 8601; defaults to the
                     last 7 days).  The window is the repair primitive:
                     re-applying the pull -> correlate -> persist path over
                     explicit bounds reconciles that window.
    --dry-run        print the full report (change_requests scanned, issues
                     scanned, sessions considered, explicit/high/inferred
                     matches, ambiguous, unmatched, and per-match evidence
                     with --show-evidence) and write nothing.
    --show-evidence  include per-match evidence lines in the report.

Idempotency by construction:
    * the run id is derived deterministically from the session identity
      (:class:`SessionKeyedULID`), so re-running the same window resolves
      the same session to the same ``afk_run_id`` and every write converges
      (enrich-only upserts, ``ON CONFLICT DO NOTHING`` events);
    * before persisting, an existing run is looked up by entity-mapping
      uniqueness (the owning change request's ``afk_run_entities`` row), so
      a run whose change request is already mapped reuses the stored run
      instead of creating a duplicate;
    * duplicate engineering events are conflict-ignored by the repository.

Report counter vocabulary (locked with the domain): ``explicit`` matches are
``explicit_run_id`` correlations; ``high`` matches are correlations at or
above the resolved-role confidence threshold; ``inferred`` matches are
below it; ``ambiguous`` / ``unmatched`` map to the
:class:`afk_outcomes.models.UnresolvedReason` outcomes.

Provider credentials come from the environment via the adapters' injectable
API-client seam: ``GITHUB_TOKEN`` for GitHub, ``GITLAB_TOKEN`` for GitLab
(no token handling or storage is implemented here).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

import asyncpg
import httpx

# Allow running from any location by resolving the repo root relative to this script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from afk_outcomes import (  # noqa: E402
    AFKRun,
    AsyncpgOutcomeRepository,
    Correlation,
    CorrelationEngine,
    EngineeringEntity,
    EntityType,
    Provider,
    ProviderAdapter,
    ResolutionResult,
    RunStatus,
    SessionDescriptor,
    UnresolvedReason,
    make_ulid,
)
from afk_outcomes.correlation import RESOLVED_ROLE_THRESHOLD  # noqa: E402
from afk_outcomes.providers.github import GitHubAdapter  # noqa: E402
from afk_outcomes.providers.gitlab import GitLabAdapter  # noqa: E402
from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger("afk_backfill")

# Report-bucket vocabulary: the correlation method that marks an explicit
# run-id match; everything at/above the resolved-role threshold is "high".
EXPLICIT_METHOD = "explicit_run_id"

DEFAULT_WINDOW_DAYS = 7

# Sessions whose activity window overlaps the backfill window are considered
# candidates; the title (when known) anchors the owning change request.
SESSIONS_WINDOW_SQL = """
    SELECT DISTINCT ON (s.id)
           s.id, s.external_session_id, s.first_message_at, s.last_message_at,
           ctx.title
    FROM sessions s
    LEFT JOIN opencode_session_contexts ctx ON ctx.session_id = s.id
    WHERE s.first_message_at <= $2
      AND s.last_message_at >= $1
    ORDER BY s.id, ctx.id
"""

# Entity-mapping uniqueness: the owning change request's resolved link row
# identifies the run that already claims the change request.
FIND_RUN_BY_ENTITY_SQL = """
    SELECT afk_run_id
    FROM afk_run_entities
    WHERE provider = $1
      AND repository = $2
      AND entity_type = 'change_request'
      AND external_id = $3
      AND role = 'resolved'
    ORDER BY afk_run_id
    LIMIT 1
"""


# ── Deterministic run identity ──────────────────────────────────────────────


class SessionKeyedULID:
    """ULID source whose first ULID is deterministic per session identity.

    Re-running the same window must resolve the same session to the same
    ``afk_run_id`` so every repository write converges.  The run id is keyed
    on the internal session UUID with the session's start time as the ULID
    timestamp; the correlation/unresolved ULIDs that follow increment a
    counter, keeping the whole resolution replay-stable.
    """

    def __init__(self) -> None:
        self._seed = ""
        self._timestamp_ms = 0
        self._counter = 0

    def set_session(self, *, seed: str, timestamp_ms: int) -> None:
        """Begin a new session: subsequent ULIDs derive from ``seed``."""
        self._seed = seed
        self._timestamp_ms = timestamp_ms
        self._counter = 0

    def next_ulid(self) -> str:
        digest = hashlib.sha256(f"{self._seed}:{self._counter}".encode()).digest()
        randomness = int.from_bytes(digest[:10], "big")
        ulid = make_ulid(self._timestamp_ms, randomness)
        self._counter += 1
        return ulid


def _ulid_timestamp(value: datetime | None, fallback: datetime) -> int:
    """Millisecond timestamp for the session-keyed ULID."""
    ts = value if value is not None else fallback
    return int(ts.timestamp() * 1000)


# ── Session seeds ───────────────────────────────────────────────────────────


@dataclass
class SessionSeed:
    """One candidate OpenCode session in the backfill window."""

    session_id: str | None
    external_session_id: str | None
    title: str | None
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def key(self) -> str:
        """The stable identity the run id derives from (always present in DB)."""
        return self.session_id or self.external_session_id or ""

    def to_descriptor(self) -> SessionDescriptor:
        return SessionDescriptor(
            session_id=self.session_id,
            external_session_id=self.external_session_id,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


async def _load_sessions(
    conn: asyncpg.Connection, *, since: datetime, until: datetime
) -> list[SessionSeed]:
    rows = await conn.fetch(SESSIONS_WINDOW_SQL, since, until)
    return [
        SessionSeed(
            session_id=str(row["id"]),
            external_session_id=row["external_session_id"],
            title=row["title"],
            started_at=row["first_message_at"],
            finished_at=row["last_message_at"],
        )
        for row in rows
    ]


# ── Existing-run lookup (entity-mapping uniqueness) ─────────────────────────


def _owning_change_request(run: AFKRun) -> EngineeringEntity | None:
    """The change request the run resolved (role ``resolved``), when one exists."""
    entities = {entity.entity_id: entity for entity in run.entities}
    for link in run.entity_links:
        if link.role == "resolved" and link.entity_id.startswith("change_request:"):
            return entities.get(link.entity_id)
    return None


async def _find_existing_run_id(conn: asyncpg.Connection, run: AFKRun) -> str | None:
    """Return the stored run id that already maps the run's owning change request."""
    owning = _owning_change_request(run)
    if owning is None:
        return None
    external_id = owning.entity_id.partition(":")[2]
    row = await conn.fetchrow(
        FIND_RUN_BY_ENTITY_SQL, run.provider.value, owning.repository, external_id
    )
    return str(row["afk_run_id"]) if row is not None else None


def _remap_run(result: ResolutionResult, afk_run_id: str) -> ResolutionResult:
    """Re-key a resolved run onto an existing run id found by entity mapping."""
    run = result.run.model_copy(update={"afk_run_id": afk_run_id})
    run.correlations = [
        c.model_copy(
            update={
                "afk_run_id": afk_run_id,
                "correlation_id": f"{afk_run_id}:{c.entity_id}",
            }
        )
        for c in result.run.correlations
    ]
    run.entity_links = [
        link.model_copy(update={"afk_run_id": afk_run_id})
        for link in result.run.entity_links
    ]
    run.session_links = [
        link.model_copy(update={"afk_run_id": afk_run_id})
        for link in result.run.session_links
    ]
    unresolved = [
        item.model_copy(update={"afk_run_id": afk_run_id})
        for item in result.unresolved
    ]
    return ResolutionResult(
        run=run, unresolved=unresolved, resolver_version=result.resolver_version
    )


# ── Report ──────────────────────────────────────────────────────────────────


@dataclass
class BackfillReport:
    """The full report printed by the CLI (and asserted by tests)."""

    provider: Provider
    repository: str
    since: datetime
    until: datetime
    dry_run: bool
    change_requests_scanned: int
    issues_scanned: int
    sessions_considered: int
    explicit_matches: int
    high_matches: int
    inferred_matches: int
    ambiguous: int
    unmatched: int
    evidence_lines: list[str] = field(default_factory=list)


def _match_buckets(correlations: Sequence[Correlation]) -> tuple[int, int, int]:
    """Partition correlations into (explicit, high, inferred) match buckets.

    ``explicit`` — bound by an explicit run identifier; ``high`` — at or
    above the resolved-role confidence threshold; ``inferred`` — below it.
    Every correlation lands in exactly one bucket.
    """
    explicit = 0
    high = 0
    inferred = 0
    for correlation in correlations:
        if correlation.method == EXPLICIT_METHOD:
            explicit += 1
        elif correlation.correlation_confidence >= RESOLVED_ROLE_THRESHOLD:
            high += 1
        else:
            inferred += 1
    return explicit, high, inferred


def _evidence_lines(results: Sequence[ResolutionResult]) -> list[str]:
    """Per-match evidence lines: one per correlation and unresolved outcome."""
    lines: list[str] = []
    for result in results:
        for c in sorted(result.run.correlations, key=lambda c: c.entity_id):
            evidence = " ".join(
                f"{e.kind}(source={e.source_entity_id}, detail={e.detail!r},"
                f" weight={e.weight:g})"
                for e in c.evidence
            ) or "(none)"
            lines.append(
                f"match {c.entity_id} method={c.method}"
                f" confidence={c.correlation_confidence:g} evidence=[{evidence}]"
            )
        for item in result.unresolved:
            candidates = ", ".join(item.candidates) or "(none)"
            evidence = " ".join(
                f"{e.kind}(source={e.source_entity_id}, detail={e.detail!r},"
                f" weight={e.weight:g})"
                for e in item.evidence
            ) or "(none)"
            lines.append(
                f"unresolved {item.entity_id} reason={item.reason.value}"
                f" candidates=[{candidates}] evidence=[{evidence}]"
            )
    return lines


def format_report(report: BackfillReport) -> str:
    """Render the full report (dry-run and write runs share the same form)."""
    lines = [
        "AFK outcome backfill report",
        f"provider: {report.provider.value}",
        f"repository: {report.repository}",
        f"window: {report.since.isoformat()} .. {report.until.isoformat()}",
        f"mode: {'dry-run' if report.dry_run else 'write'}",
        f"change_requests scanned: {report.change_requests_scanned}",
        f"issues scanned: {report.issues_scanned}",
        f"sessions considered: {report.sessions_considered}",
        f"explicit matches: {report.explicit_matches}",
        f"high matches: {report.high_matches}",
        f"inferred matches: {report.inferred_matches}",
        f"ambiguous: {report.ambiguous}",
        f"unmatched: {report.unmatched}",
    ]
    if report.evidence_lines:
        lines.append("evidence:")
        lines.extend(f"  {line}" for line in report.evidence_lines)
    if report.dry_run:
        lines.append("dry-run: no rows were written; re-run without --dry-run to persist.")
    return "\n".join(lines)


# ── The windowed backfill orchestration ─────────────────────────────────────


async def run_backfill(
    conn: asyncpg.Connection,
    *,
    adapter: ProviderAdapter,
    repository: str,
    since: datetime,
    until: datetime,
    dry_run: bool = False,
    show_evidence: bool = False,
) -> BackfillReport:
    """Pull a window from ``adapter``, correlate, persist, and count the report.

    The same domain models, provider normalization, and
    :class:`CorrelationEngine` as the live path are reused — this is a thin
    orchestration layer.  In ``dry_run`` mode nothing is written and no
    existing-run lookup is performed; the returned report is identical to
    what a real write would produce for the same window.
    """
    sessions = await _load_sessions(conn, since=since, until=until)
    entities = await adapter.fetch_entities(repository, since=since, until=until)
    events = await adapter.fetch_events(repository, since=since, until=until)

    ulid_source = SessionKeyedULID()
    engine = CorrelationEngine(ulid_source=ulid_source)
    repository_impl = AsyncpgOutcomeRepository(conn)

    descriptors = [seed.to_descriptor() for seed in sessions]
    results: list[ResolutionResult] = []
    for seed in sessions:
        ulid_source.set_session(
            seed=seed.key, timestamp_ms=_ulid_timestamp(seed.started_at, since)
        )
        run_seed = AFKRun(
            afk_run_id="",
            provider=adapter.provider,
            status=RunStatus.COMPLETED,
            title=seed.title,
            started_at=seed.started_at,
            finished_at=seed.finished_at,
        )
        result = await engine.resolve(
            run_seed, entities=entities, events=events, sessions=descriptors
        )
        if not dry_run:
            existing = await _find_existing_run_id(conn, result.run)
            if existing is not None and existing != result.run.afk_run_id:
                result = _remap_run(result, existing)
            await repository_impl.save(result.run)
            await repository_impl.save_unresolved(
                result.run, result.unresolved, repository=repository
            )
        results.append(result)

    explicit, high, inferred = _match_buckets(
        [c for result in results for c in result.run.correlations]
    )
    ambiguous = sum(
        1
        for result in results
        for item in result.unresolved
        if item.reason is UnresolvedReason.AMBIGUOUS
    )
    unmatched = sum(
        1
        for result in results
        for item in result.unresolved
        if item.reason is UnresolvedReason.UNMATCHED
    )

    return BackfillReport(
        provider=adapter.provider,
        repository=repository,
        since=since,
        until=until,
        dry_run=dry_run,
        change_requests_scanned=sum(
            1 for e in entities if e.entity_type is EntityType.CHANGE_REQUEST
        ),
        issues_scanned=sum(1 for e in entities if e.entity_type is EntityType.ISSUE),
        sessions_considered=len(sessions),
        explicit_matches=explicit,
        high_matches=high,
        inferred_matches=inferred,
        ambiguous=ambiguous,
        unmatched=unmatched,
        evidence_lines=_evidence_lines(results) if show_evidence else [],
    )


# ── CLI wiring ──────────────────────────────────────────────────────────────


def _parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 bound (naive values are assumed UTC)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid datetime: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill/reconcile AFK outcomes: pull a window of engineering"
            " activity from a provider, correlate it against Gateway sessions,"
            " and persist resolved runs idempotently."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["github", "gitlab"],
        required=True,
        help="Source provider to pull from.",
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="Full owner/repo (or group/project) name.",
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full report without writing any rows.",
    )
    parser.add_argument(
        "--show-evidence",
        action="store_true",
        help="Include per-match evidence lines in the report.",
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


class _Closable(Protocol):
    async def aclose(self) -> None: ...


class _GitHubHttpApi:
    """A :class:`afk_outcomes.providers.github.GitHubApi` over httpx."""

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


def _build_adapter(provider: str) -> tuple[ProviderAdapter, _Closable]:
    """Build the provider adapter plus its API client (closed by the caller).

    Credentials come from the environment via the adapters' injectable
    API-client seam — no token handling is implemented here.
    """
    if provider == "github":
        github_client = _GitHubHttpApi(os.environ.get("GITHUB_TOKEN", ""))
        return GitHubAdapter(github_client), github_client
    headers: dict[str, str] = {}
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        headers["PRIVATE-TOKEN"] = token
    gitlab_client = httpx.AsyncClient(headers=headers, timeout=30.0)
    return GitLabAdapter(client=gitlab_client), gitlab_client


async def _get_pool() -> asyncpg.Pool:
    """Create a database connection pool from application settings."""
    settings = get_settings()
    return await asyncpg.create_pool(
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
        user=settings.database_user,
        password=settings.database_password,
        min_size=1,
        max_size=2,
    )


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, pull the window, correlate, persist or dry-run."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    pool = await _get_pool()
    adapter, client = _build_adapter(args.provider)

    try:
        async with pool.acquire() as conn:
            report = await run_backfill(
                conn,
                adapter=adapter,
                repository=args.repository,
                since=args.since,
                until=args.until,
                dry_run=args.dry_run,
                show_evidence=args.show_evidence,
            )
            print(format_report(report))
    finally:
        await client.aclose()
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
