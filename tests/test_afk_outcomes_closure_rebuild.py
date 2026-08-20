"""Closure projection rebuild CLI + repository tests (issue #539).

Covers the operator-only rebuild operation:

* the repository ``rebuild_closure_projection`` reuses the same pure-domain
  projector (``project_closure_episodes``) and reconcile helpers as the
  incremental recompute, and is deterministic and idempotent;
* optional time bounds (``since``/``until``) filter the processed fact range;
* the CLI requires explicit confirmation before writing a full rebuild;
* the CLI reports the processed event range and the resulting projection
  counts (closure_links, closure_episodes, closure_unresolved);
* argparse validation of ``--since`` / ``--until`` / ``--confirm`` /
  ``--dry-run``.

No schema migration and no automatic reconciliation scheduler are
introduced — this is a CLI/AWX-only operation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from afk_outcomes.models import (
    CLOSURE_RESOLVER_VERSION,
    ClosureEpisodeStatus,
    ClosureLinkKind,
    ClosureLinkState,
    EntityType,
    Provider,
)
from afk_outcomes.repository import AsyncpgOutcomeRepository
from app.core.repository import normalize_repository_url
from scripts.rebuild_closure_projection import (
    RebuildReport,
    _parse_args,
    format_report,
    run_rebuild,
)
from tests.conftest import mock_row

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

REPO = "gitlab.com/cloudnative-pg"
T0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)


def _t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _row(
    *,
    entity_type: str,
    event_type: str,
    external_id: str,
    occurred_at: datetime,
    payload: dict | None = None,
    observed_via: str = "webhook",
) -> dict:
    return {
        "provider": Provider.GITLAB.value,
        "repository": REPO,
        "entity_type": entity_type,
        "external_id": external_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "observed_via": observed_via,
        "payload": payload or {},
    }


def _declares_snapshot(number: str) -> dict:
    return {
        "issue_links": {
            "references": [],
            "declares_closure": [
                {"repository": f"https://gitlab.com/{REPO.removeprefix('gitlab.com/')}", "number": number}
            ],
        }
    }


def _inferred_facts() -> list[dict]:
    """A same-repo CR merged before the issue close → one inferred episode."""
    return [
        _row(
            entity_type="change_request",
            event_type="change_request.opened",
            external_id="6",
            occurred_at=_t(0),
            payload=_declares_snapshot("1"),
        ),
        _row(
            entity_type="change_request",
            event_type="change_request.merged",
            external_id="6",
            occurred_at=_t(10),
        ),
        _row(
            entity_type="issue",
            event_type="issue.closed",
            external_id="1",
            occurred_at=_t(20),
        ),
    ]


def _mock_conn(rows: list[dict]) -> AsyncMock:
    conn = AsyncMock()

    async def _fetch(sql: str, *args):
        if "FROM engineering_events" in sql:
            return [mock_row(row) for row in rows]
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


# ── repository rebuild_closure_projection ────────────────────────────────────


async def test_rebuild_projects_full_closure_projection() -> None:
    """A full rebuild loads the closure-relevant facts, projects them with the
    same pure projector, and reconciles the three projection tables."""
    conn = _mock_conn(_inferred_facts())
    repo = AsyncpgOutcomeRepository(conn)

    result = await repo.rebuild_closure_projection(
        normalize_repository=normalize_repository_url
    )

    assert result.facts_processed == 3
    assert result.event_range_start == _t(0)
    assert result.event_range_end == _t(20)
    assert len(result.projection.episodes) == 1
    episode = result.projection.episodes[0]
    assert episode.status is ClosureEpisodeStatus.INFERRED
    assert episode.change_request_external_id == "6"
    assert episode.resolver_version == CLOSURE_RESOLVER_VERSION
    assert len(result.projection.links) == 1
    assert result.projection.links[0].kind is ClosureLinkKind.DECLARES_CLOSURE
    assert result.projection.links[0].state is ClosureLinkState.ACTIVE
    assert result.projection.unresolved == []

    # the reconcile wrote to all three projection tables
    sqls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("INSERT INTO closure_links" in s for s in sqls)
    assert any("INSERT INTO closure_episodes" in s for s in sqls)


async def test_rebuild_filters_facts_by_time_bounds() -> None:
    """Optional time bounds filter the processed fact range: facts outside the
    window are not projected."""
    facts = _inferred_facts()
    conn = _mock_conn(facts)
    repo = AsyncpgOutcomeRepository(conn)

    # window covers only the CR open+merge, not the issue close
    result = await repo.rebuild_closure_projection(
        since=_t(0),
        until=_t(15),
        normalize_repository=normalize_repository_url,
    )

    assert result.facts_processed == 2
    assert result.event_range_start == _t(0)
    assert result.event_range_end == _t(10)
    # no issue.closed in the window → no closed episode; the open interval
    # with a merged declaration renders awaiting_closure
    assert len(result.projection.episodes) == 1
    assert result.projection.episodes[0].status is ClosureEpisodeStatus.AWAITING_CLOSURE


async def test_rebuild_is_deterministic_and_idempotent() -> None:
    """Repeated full rebuilds converge to an identical projection."""
    conn = _mock_conn(_inferred_facts())
    repo = AsyncpgOutcomeRepository(conn)

    first = await repo.rebuild_closure_projection(
        normalize_repository=normalize_repository_url
    )
    second = await repo.rebuild_closure_projection(
        normalize_repository=normalize_repository_url
    )

    assert first.projection.model_dump(mode="json") == second.projection.model_dump(
        mode="json"
    )
    assert first.facts_processed == second.facts_processed == 3


async def test_rebuild_empty_facts_produces_empty_projection() -> None:
    """No closure-relevant facts → an empty projection, no writes."""
    conn = _mock_conn([])
    repo = AsyncpgOutcomeRepository(conn)

    result = await repo.rebuild_closure_projection(
        normalize_repository=normalize_repository_url
    )

    assert result.facts_processed == 0
    assert result.event_range_start is None
    assert result.event_range_end is None
    assert result.projection.links == []
    assert result.projection.episodes == []
    assert result.projection.unresolved == []
    conn.execute.assert_not_called()


# ── CLI orchestration ────────────────────────────────────────────────────────


def _fake_result(**overrides) -> object:
    from afk_outcomes.repository import ClosureRebuildResult
    from afk_outcomes.models import ClosureProjection

    base = {
        "projection": ClosureProjection(),
        "facts_processed": 3,
        "event_range_start": _t(0),
        "event_range_end": _t(20),
    }
    base.update(overrides)
    return ClosureRebuildResult(**base)


async def test_run_rebuild_requires_confirmation_to_write() -> None:
    """A full rebuild refuses to write without explicit confirmation."""
    conn = _mock_conn([])
    with patch.object(
        AsyncpgOutcomeRepository,
        "rebuild_closure_projection",
        new_callable=AsyncMock,
        return_value=_fake_result(),
    ):
        with pytest.raises(SystemExit):
            await run_rebuild(conn, dry_run=False, confirm=False)


async def test_run_rebuild_dry_run_does_not_require_confirmation() -> None:
    """Dry-run never writes, so it needs no confirmation and issues no writes.

    This exercises the real repository write path (no mocking of
    ``rebuild_closure_projection``): the projection is computed read-only and
    the underlying connection receives no write (``execute``) calls.
    """
    conn = _mock_conn(_inferred_facts())
    report = await run_rebuild(conn, dry_run=True, confirm=False)

    assert report.dry_run is True
    assert report.confirmed is False
    # The real rebuild ran and computed the projection...
    assert report.facts_processed == 3
    assert report.closure_episodes == 1
    # ...but no write ever reached the database.
    conn.execute.assert_not_called()


async def test_run_rebuild_with_confirmation_writes_and_reports() -> None:
    """With --confirm, a full rebuild writes and reports the event range and
    the resulting projection counts."""
    conn = _mock_conn([])
    result = _fake_result(
        facts_processed=3,
        event_range_start=_t(0),
        event_range_end=_t(20),
    )
    with patch.object(
        AsyncpgOutcomeRepository,
        "rebuild_closure_projection",
        new_callable=AsyncMock,
        return_value=result,
    ) as mock_rebuild:
        report = await run_rebuild(conn, dry_run=False, confirm=True)

    assert report.dry_run is False
    assert report.confirmed is True
    mock_rebuild.assert_awaited_once()
    assert report.facts_processed == 3
    assert report.event_range_start == _t(0)
    assert report.event_range_end == _t(20)
    assert report.closure_links == 0
    assert report.closure_episodes == 0
    assert report.closure_unresolved == 0


async def test_run_rebuild_passes_time_bounds_to_repository() -> None:
    """The CLI forwards --since/--until to the repository rebuild."""
    conn = _mock_conn([])
    with patch.object(
        AsyncpgOutcomeRepository,
        "rebuild_closure_projection",
        new_callable=AsyncMock,
        return_value=_fake_result(),
    ) as mock_rebuild:
        await run_rebuild(
            conn,
            since=_t(0),
            until=_t(15),
            dry_run=True,
            confirm=False,
        )

    kwargs = mock_rebuild.call_args.kwargs
    assert kwargs["since"] == _t(0)
    assert kwargs["until"] == _t(15)


# ── report formatting ────────────────────────────────────────────────────────


def test_format_report_renders_range_and_counts() -> None:
    report = RebuildReport(
        since=_t(0),
        until=_t(20),
        dry_run=False,
        confirmed=True,
        facts_processed=3,
        event_range_start=_t(0),
        event_range_end=_t(20),
        closure_links=1,
        closure_episodes=1,
        closure_unresolved=0,
    )

    text = format_report(report)

    assert "closure projection rebuild report" in text
    assert "mode: write" in text
    assert "facts processed: 3" in text
    assert "event range: 2026-08-01T10:00:00+00:00 .. 2026-08-01T10:00:20+00:00" in text
    assert "closure_links: 1" in text
    assert "closure_episodes: 1" in text
    assert "closure_unresolved: 0" in text


def test_format_report_dry_run_notes_no_writes() -> None:
    report = RebuildReport(
        since=None,
        until=None,
        dry_run=True,
        confirmed=False,
        facts_processed=0,
        event_range_start=None,
        event_range_end=None,
        closure_links=0,
        closure_episodes=0,
        closure_unresolved=0,
    )

    text = format_report(report)

    assert "mode: dry-run" in text
    assert "dry-run: no rows were written" in text


# ── argparse ─────────────────────────────────────────────────────────────────


def test_parse_args_accepts_bounds_and_flags() -> None:
    args = _parse_args(
        [
            "--since",
            "2026-08-01T00:00:00Z",
            "--until",
            "2026-08-02",
            "--confirm",
            "--dry-run",
        ]
    )
    assert args.since == datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    assert args.until == datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)
    assert args.confirm is True
    assert args.dry_run is True


def test_parse_args_defaults_to_full_rebuild() -> None:
    args = _parse_args([])
    assert args.since is None
    assert args.until is None
    assert args.confirm is False
    assert args.dry_run is False


def test_parse_args_rejects_since_after_until() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--since", "2026-08-10", "--until", "2026-08-01"])


def test_parse_args_rejects_invalid_datetime() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--since", "not-a-date"])
