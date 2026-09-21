#!/usr/bin/env python3
"""Nightly ``afk_dashboard_daily`` parity verification (issue #718).

Compares the Gateway's derived ``afk_dashboard_daily`` rollup against the
canonical source tables the recomputation engine
(``app.core.afk_dashboard_daily``) projects from, over a configurable recent
window, and reports every disagreement by ``(day, provider, repository)`` with
a per-metric breakdown.  It is strictly **read-only** — it never inserts,
updates, deletes, or otherwise mutates a rollup or a canonical row.  Its SQL is
SELECT-only by construction.

Two comparisons run:

* **AFK dashboard daily rollup** — ``afk_dashboard_daily`` vs the canonical
  sources the recompute engine uses, per ``(day, provider, repository)`` (issue
  #714/#715).  Every one of the fourteen additive metrics is recomputed from
  its source:

  ====================================  ==========================  =====================================
  Metric                                Canonical table             Event-time column
  ====================================  ==========================  =====================================
  ``runs_started``                      ``afk_runs``                ``COALESCE(started_at, first_seen_at)``
  ``change_requests_opened``            ``engineering_events``      ``occurred_at``
  ``change_requests_merged``            ``engineering_events``      ``occurred_at``
  ``change_requests_closed``            ``engineering_events``      ``occurred_at``
  ``execution_count``                   ``execution_bindings``      ``COALESCE(started_at, created_at)``
  ``successful_execution_count``        ``execution_bindings``      ``COALESCE(started_at, created_at)``
  ``failed_execution_count``            ``execution_bindings``      ``COALESCE(started_at, created_at)``
  ``cancelled_execution_count``         ``execution_bindings``      ``COALESCE(started_at, created_at)``
  ``session_count``                     ``afk_run_sessions``        ``COALESCE(started_at, first_seen_at)``
  ``input_tokens``                      ``usage_events``            ``reported_at``
  ``output_tokens``                     ``usage_events``            ``reported_at``
  ``cache_read_tokens``                 ``usage_events``            ``reported_at``
  ``cache_write_tokens``                ``usage_events``            ``reported_at``
  ``estimated_cost_usd``                ``usage_events``            ``reported_at``
  ====================================  ==========================  =====================================

  Sessions whose internal ``session_id`` maps to more than one AFK run are
  ambiguous and are excluded (``HAVING COUNT(DISTINCT afk_run_id) = 1``), never
  split or arbitrarily attributed.  A rollup row with no backing canonical
  group (stale) and a canonical group with no rollup row (missing) both count
  as mismatches.
* **Reporting aggregate** — ``reporting_resource_aggregates`` vs the canonical
  ``reporting_deliveries`` rows per **stable resource identity**
  ``(provider, repository_url, resource_type, resource_number)`` (ADR 0018).
  The current aggregate's forward-advanced ``last_delivery_id`` /
  ``last_occurred_at`` pointer must resolve to the resource's newest delivery;
  a resource with window deliveries but no aggregate (or an aggregate with no
  backing delivery in the window) is a mismatch.  The canonical delivery count
  for the identity is reported as context.

Exit code is ``0`` when every rollup matches its canonical source and non-zero
when any mismatch is found, so a Kubernetes CronJob can alert on the job status.
There is no ``--fix`` mode: verification reports only; correcting a rollup
remains the job of the recompute tooling.

Usage:
    python scripts/verify_afk_dashboard_daily.py [--window-days N]
    python scripts/verify_afk_dashboard_daily.py --from-date 2026-09-01 --to-date 2026-09-15
    python scripts/verify_afk_dashboard_daily.py --window-days 3 --json

Flags:
    --window-days N   Verify the last N days including today (default: 7).
    --from-date DATE  Explicit inclusive window start (YYYY-MM-DD); requires
                      --to-date.  Overrides --window-days.
    --to-date DATE    Explicit inclusive window end (YYYY-MM-DD).
    --json            Emit the report as JSON on stdout (for CronJob alerting).
    --dry-run         Accepted for CronJob symmetry; always read-only and logs
                      an explicit no-write confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import asyncpg

# Allow running from any location by resolving the repo root relative to this script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.core.reporting_aggregates import (  # noqa: E402
    resource_identity_from_payload,
)

# Mirror of ``app.core.afk_dashboard_daily.METRIC_COLUMNS`` used until the
# #715 recompute engine is merged onto this branch.  When the engine module is
# importable its canonical vocabulary wins, so the verifier and the writer can
# never drift.
_FALLBACK_AFK_DASHBOARD_METRIC_COLUMNS: tuple[str, ...] = (
    "runs_started",
    "change_requests_opened",
    "change_requests_merged",
    "change_requests_closed",
    "execution_count",
    "successful_execution_count",
    "failed_execution_count",
    "cancelled_execution_count",
    "session_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
)
try:  # pragma: no cover - the #715 engine may not be merged on this branch yet.
    from app.core.afk_dashboard_daily import (  # noqa: E402
        METRIC_COLUMNS as AFK_DASHBOARD_METRIC_COLUMNS,
    )
except ImportError:  # pragma: no cover - engine absent, use the mirror above.
    AFK_DASHBOARD_METRIC_COLUMNS = _FALLBACK_AFK_DASHBOARD_METRIC_COLUMNS

logger = logging.getLogger("verify_afk_dashboard_daily")

SOURCE_AFK_DASHBOARD_DAILY = "afk_dashboard_daily"
SOURCE_REPORTING_AGGREGATES = "reporting_resource_aggregates"
# Backward-compatible alias for the historical source label.
SOURCE_USAGE_ROLLUP = SOURCE_AFK_DASHBOARD_DAILY

# ---------------------------------------------------------------------------
# SQL (SELECT-only)
#
# The canonical side recomputes every metric of the ``afk_dashboard_daily``
# rollup from the same source tables the recompute engine
# (``app.core.afk_dashboard_daily``) reads, grouped by
# ``(day, provider, repository)`` for the whole window in one round trip.
# A FULL OUTER JOIN against ``afk_dashboard_daily`` then flags three
# disagreement shapes — a rollup row whose metrics differ, a rollup row with no
# matching canonical group (stale), and a canonical group with no rollup row
# (missing).  Both sides are restricted to the verification window by the UTC
# calendar day.
# ---------------------------------------------------------------------------

_AFK_ROLLUP_COLUMNS_SQL = ",\n           ".join(
    f"d.{column} AS rollup_{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)
_AFK_CANONICAL_COLUMNS_SQL = ",\n           ".join(
    f"c.{column} AS canonical_{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)
_AFK_MISMATCH_PREDICATE = "\n         OR ".join(
    f"d.{column} != c.{column}" for column in AFK_DASHBOARD_METRIC_COLUMNS
)

AFK_DASHBOARD_DAILY_MISMATCH_SQL = f"""
    SELECT COALESCE(d.day, c.day) AS day,
           COALESCE(d.provider, c.provider) AS provider,
           COALESCE(d.repository, c.repository) AS repository,
           {_AFK_ROLLUP_COLUMNS_SQL},
           {_AFK_CANONICAL_COLUMNS_SQL}
    FROM afk_dashboard_daily d
    FULL OUTER JOIN (
        WITH runs_agg AS (
            SELECT (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider,
                   r.repository,
                   COUNT(*)::int AS runs_started
            FROM afk_runs r
            WHERE r.repository IS NOT NULL
              AND (COALESCE(r.started_at, r.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        cr_agg AS (
            SELECT (e.occurred_at AT TIME ZONE 'UTC')::date AS day,
                   e.provider,
                   e.repository,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.opened')::int
                       AS change_requests_opened,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.merged')::int
                       AS change_requests_merged,
                   COUNT(*) FILTER (WHERE e.event_type = 'change_request.closed')::int
                       AS change_requests_closed
            FROM engineering_events e
            WHERE e.entity_type = 'change_request'
              AND e.repository IS NOT NULL
              AND (e.occurred_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
            GROUP BY day, e.provider, e.repository
        ),
        exec_agg AS (
            SELECT (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   b.provider,
                   b.repository_url AS repository,
                   COUNT(*)::int AS execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'completed')::int
                       AS successful_execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'failed')::int
                       AS failed_execution_count,
                   COUNT(*) FILTER (WHERE b.outcome = 'cancelled')::int
                       AS cancelled_execution_count
            FROM execution_bindings b
            WHERE b.repository_url IS NOT NULL
              AND (COALESCE(b.started_at, b.created_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, b.provider, b.repository_url
        ),
        unambiguous AS (
            SELECT ars.session_id, MIN(ars.afk_run_id) AS afk_run_id
            FROM afk_run_sessions ars
            WHERE ars.session_id IS NOT NULL
            GROUP BY ars.session_id
            HAVING COUNT(DISTINCT ars.afk_run_id) = 1
        ),
        sess_agg AS (
            SELECT (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                       AS day,
                   r.provider,
                   r.repository,
                   COUNT(*)::int AS session_count
            FROM afk_run_sessions ars
            JOIN unambiguous u ON u.session_id = ars.session_id
            JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
            WHERE r.repository IS NOT NULL
              AND (COALESCE(ars.started_at, ars.first_seen_at) AT TIME ZONE 'UTC')::date
                  BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        usage_agg AS (
            SELECT (ue.reported_at AT TIME ZONE 'UTC')::date AS day,
                   r.provider,
                   r.repository,
                   COALESCE(SUM(ue.input_tokens), 0)::int AS input_tokens,
                   COALESCE(SUM(ue.output_tokens), 0)::int AS output_tokens,
                   COALESCE(SUM(ue.cache_read_tokens), 0)::int AS cache_read_tokens,
                   COALESCE(SUM(ue.cache_write_tokens), 0)::int AS cache_write_tokens,
                   COALESCE(SUM(ue.estimated_cost_usd), 0) AS estimated_cost_usd
            FROM usage_events ue
            JOIN unambiguous u ON u.session_id = ue.session_id
            JOIN afk_runs r ON r.afk_run_id = u.afk_run_id
            WHERE r.repository IS NOT NULL
              AND (ue.reported_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
            GROUP BY day, r.provider, r.repository
        ),
        canonical AS (
            SELECT COALESCE(r.day, cr.day, ex.day, se.day, ug.day) AS day,
                   COALESCE(r.provider, cr.provider, ex.provider, se.provider,
                            ug.provider) AS provider,
                   COALESCE(r.repository, cr.repository, ex.repository,
                            se.repository, ug.repository) AS repository,
                   COALESCE(r.runs_started, 0)::int AS runs_started,
                   COALESCE(cr.change_requests_opened, 0)::int
                       AS change_requests_opened,
                   COALESCE(cr.change_requests_merged, 0)::int
                       AS change_requests_merged,
                   COALESCE(cr.change_requests_closed, 0)::int
                       AS change_requests_closed,
                   COALESCE(ex.execution_count, 0)::int AS execution_count,
                   COALESCE(ex.successful_execution_count, 0)::int
                       AS successful_execution_count,
                   COALESCE(ex.failed_execution_count, 0)::int
                       AS failed_execution_count,
                   COALESCE(ex.cancelled_execution_count, 0)::int
                       AS cancelled_execution_count,
                   COALESCE(se.session_count, 0)::int AS session_count,
                   COALESCE(ug.input_tokens, 0)::int AS input_tokens,
                   COALESCE(ug.output_tokens, 0)::int AS output_tokens,
                   COALESCE(ug.cache_read_tokens, 0)::int AS cache_read_tokens,
                   COALESCE(ug.cache_write_tokens, 0)::int AS cache_write_tokens,
                   COALESCE(ug.estimated_cost_usd, 0) AS estimated_cost_usd
            FROM runs_agg r
            FULL OUTER JOIN cr_agg cr
              ON cr.day = r.day
             AND cr.provider = r.provider
             AND cr.repository = r.repository
            FULL OUTER JOIN exec_agg ex
              ON ex.day = COALESCE(r.day, cr.day)
             AND ex.provider = COALESCE(r.provider, cr.provider)
             AND ex.repository = COALESCE(r.repository, cr.repository)
            FULL OUTER JOIN sess_agg se
              ON se.day = COALESCE(r.day, cr.day, ex.day)
             AND se.provider = COALESCE(r.provider, cr.provider, ex.provider)
             AND se.repository = COALESCE(r.repository, cr.repository,
                                          ex.repository)
            FULL OUTER JOIN usage_agg ug
              ON ug.day = COALESCE(r.day, cr.day, ex.day, se.day)
             AND ug.provider = COALESCE(r.provider, cr.provider, ex.provider,
                                        se.provider)
             AND ug.repository = COALESCE(r.repository, cr.repository,
                                          ex.repository, se.repository)
        )
        SELECT * FROM canonical
    ) c
      ON d.day = c.day
     AND d.provider = c.provider
     AND d.repository = c.repository
    WHERE COALESCE(d.day, c.day) BETWEEN $1 AND $2
      AND ( d.day IS NULL
         OR c.day IS NULL
         OR {_AFK_MISMATCH_PREDICATE} )
    ORDER BY day, provider, repository
"""

# Backward-compatible alias: the comparison now targets ``afk_dashboard_daily``
# but the module's historical constant name is retained for callers/tests.
USAGE_ROLLUP_MISMATCH_SQL = AFK_DASHBOARD_DAILY_MISMATCH_SQL

# Reporting side: the aggregate table is the *current* state (one row per
# stable resource identity), so it is read in full and window-guarded in
# Python; the canonical delivery rows are windowed by their UTC day.
REPORTING_AGGREGATES_SQL = """
    SELECT provider,
           repository_url,
           resource_type,
           resource_number,
           last_occurred_at,
           last_delivery_id
    FROM reporting_resource_aggregates
    ORDER BY provider, repository_url, resource_type, resource_number
"""

REPORTING_DELIVERIES_SQL = """
    SELECT provider,
           delivery_id,
           occurred_at,
           payload
    FROM reporting_deliveries
    WHERE (occurred_at AT TIME ZONE 'UTC')::date BETWEEN $1 AND $2
    ORDER BY provider, occurred_at, delivery_id
"""


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationWindow:
    """An inclusive calendar-day verification window."""

    from_date: date
    to_date: date

    @property
    def day_count(self) -> int:
        """Number of days in the inclusive window."""
        return (self.to_date - self.from_date).days + 1

    def contains(self, day: date) -> bool:
        """Whether ``day`` falls inside the inclusive window."""
        return self.from_date <= day <= self.to_date


def _today_utc() -> date:
    """The current UTC calendar day (matches the rollup day bucketing)."""
    return datetime.now(timezone.utc).date()


def parse_window(
    *,
    days: int = 7,
    from_date: date | None = None,
    to_date: date | None = None,
    today: date | None = None,
) -> VerificationWindow:
    """Resolve the verification window.

    An explicit ``from_date``/``to_date`` pair (both required) wins over
    ``days``; otherwise the window is the last ``days`` UTC calendar days
    including ``today`` (default: today in UTC).
    """
    if (from_date is None) != (to_date is None):
        raise ValueError("--from-date and --to-date must be supplied together")
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise ValueError("--from-date must not be after --to-date")
        return VerificationWindow(from_date=from_date, to_date=to_date)

    if days < 1:
        raise ValueError(f"--window-days must be a positive integer, got {days}")
    end = today if today is not None else _today_utc()
    start = end - timedelta(days=days - 1)
    return VerificationWindow(from_date=start, to_date=end)


# ---------------------------------------------------------------------------
# Mismatch model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDiff:
    """One differing metric: the rollup value, the canonical value, and delta."""

    rollup: Any
    canonical: Any
    delta: Any


@dataclass(frozen=True)
class Mismatch:
    """One bucket whose rollup disagrees with its canonical source."""

    source: str
    day: date | None
    provider: str | None
    repository: str | None
    resource_type: str | None
    resource_number: str | None
    fields: dict[str, FieldDiff] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def group_key(self) -> tuple[date | None, str | None, str | None]:
        """The ``(day, provider, repository)`` grouping key for the report."""
        return (self.day, self.provider, self.repository)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable representation of the mismatch."""
        return {
            "source": self.source,
            "day": self.day.isoformat() if self.day else None,
            "provider": self.provider,
            "repository": self.repository,
            "resource_type": self.resource_type,
            "resource_number": self.resource_number,
            "fields": {
                name: {
                    "rollup": diff.rollup,
                    "canonical": diff.canonical,
                    "delta": diff.delta,
                }
                for name, diff in self.fields.items()
            },
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# Pure comparison helpers
# ---------------------------------------------------------------------------


def _get(row: Any, key: str) -> Any:
    """Read ``key`` from a mapping or an asyncpg Record, defaulting to None."""
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


def _delta(canonical: Any, rollup: Any) -> Any:
    """Return ``canonical - rollup`` when both sides are present, else None."""
    if canonical is None or rollup is None:
        return None
    try:
        return canonical - rollup
    except TypeError:
        return None


def _as_utc_day(value: datetime) -> date:
    """The UTC calendar day of a timezone-aware (or naive) datetime."""
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(timezone.utc).date()


def compare_afk_dashboard_daily_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mismatch]:
    """Map SQL mismatch rows (already filtered) into grouped mismatch records.

    Every row returned by :data:`AFK_DASHBOARD_DAILY_MISMATCH_SQL` is a genuine
    disagreement, keyed per ``(day, provider, repository)``.  Each of the
    fourteen additive :data:`AFK_DASHBOARD_METRIC_COLUMNS` is compared, so the
    breakdown names every metric — the differing ones carry a non-zero delta,
    the matching ones a zero delta.
    """
    mismatches: list[Mismatch] = []
    for row in rows:
        fields = {
            name: FieldDiff(
                rollup=_get(row, f"rollup_{name}"),
                canonical=_get(row, f"canonical_{name}"),
                delta=_delta(_get(row, f"canonical_{name}"), _get(row, f"rollup_{name}")),
            )
            for name in AFK_DASHBOARD_METRIC_COLUMNS
        }
        mismatched_metrics = [
            name
            for name, diff in fields.items()
            if diff.rollup != diff.canonical
        ]
        mismatches.append(
            Mismatch(
                source=SOURCE_AFK_DASHBOARD_DAILY,
                day=_get(row, "day"),
                provider=_get(row, "provider"),
                repository=_get(row, "repository"),
                resource_type=None,
                resource_number=None,
                fields=fields,
                context={"mismatched_metrics": mismatched_metrics},
            )
        )
    return mismatches


# Backward-compatible alias for the historical helper name.
compare_usage_rollup_rows = compare_afk_dashboard_daily_rows


def canonical_latest_by_resource(
    delivery_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str, str], tuple[datetime, str]], dict[tuple[str, str, str, str], int]]:
    """Derive the canonical newest delivery per stable resource identity.

    Returns ``(latest, counts)`` where ``latest`` maps the identity tuple
    ``(provider, repository_url, resource_type, resource_number)`` to its
    ``(occurred_at, delivery_id)`` and ``counts`` maps the identity to the
    number of deliveries observed.  Ordering mirrors the ingest-time
    forward-only advance: maximum ``occurred_at`` wins, lowest ``delivery_id``
    breaks a tie (ADR 0018).  Deliveries whose payload carries no usable
    ``resource`` object are skipped — the ingest path skips them too, so no
    aggregate is expected for them.
    """
    latest: dict[tuple[str, str, str, str], tuple[datetime, str]] = {}
    counts: dict[tuple[str, str, str, str], int] = {}

    for row in delivery_rows:
        identity = resource_identity_from_payload(
            _get(row, "payload"), provider=_get(row, "provider") or "",
        )
        if identity is None:
            continue
        key = (
            identity.provider,
            identity.repository_url,
            identity.resource_type,
            identity.resource_number,
        )
        counts[key] = counts.get(key, 0) + 1

        occurred_at = _get(row, "occurred_at")
        delivery_id = _get(row, "delivery_id")
        if occurred_at is None or delivery_id is None:
            continue
        current = latest.get(key)
        if (
            current is None
            or occurred_at > current[0]
            or (occurred_at == current[0] and delivery_id < current[1])
        ):
            latest[key] = (occurred_at, delivery_id)

    return latest, counts


def compare_reporting_aggregates(
    aggregate_rows: Sequence[Mapping[str, Any]],
    delivery_rows: Sequence[Mapping[str, Any]],
    window: VerificationWindow,
) -> list[Mismatch]:
    """Compare current aggregates against the canonical deliveries.

    ``aggregate_rows`` is the full current-aggregate table (one row per stable
    resource identity); ``delivery_rows`` holds the canonical deliveries in the
    verification window.  A mismatch is one of:

    * **aggregate_missing** — a resource with window deliveries has no
      aggregate row.
    * **pointer_mismatch** — the aggregate's ``last_delivery_id`` /
      ``last_occurred_at`` pointer does not match the resource's newest
      delivery in the window.
    * **no_backing_delivery** — an aggregate whose ``last_occurred_at`` lies
      inside the window has no matching delivery in the window.

    An aggregate that advanced *past* the window end is not a window mismatch:
    the historical window simply does not cover it.
    """
    latest, counts = canonical_latest_by_resource(delivery_rows)

    aggregates: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for row in aggregate_rows:
        key = (
            _get(row, "provider"),
            _get(row, "repository_url"),
            _get(row, "resource_type"),
            _get(row, "resource_number"),
        )
        aggregates[key] = row

    mismatches: list[Mismatch] = []

    # ── Identities observed in the window must have a matching pointer ──
    for key, (canonical_occurred_at, canonical_delivery_id) in latest.items():
        provider, repository_url, resource_type, resource_number = key
        canonical_day = _as_utc_day(canonical_occurred_at)
        aggregate = aggregates.get(key)

        if aggregate is None:
            mismatches.append(
                Mismatch(
                    source=SOURCE_REPORTING_AGGREGATES,
                    day=canonical_day,
                    provider=provider,
                    repository=repository_url,
                    resource_type=resource_type,
                    resource_number=resource_number,
                    fields={
                        "last_delivery_id": FieldDiff(
                            rollup=None, canonical=canonical_delivery_id, delta=None,
                        ),
                        "last_occurred_at": FieldDiff(
                            rollup=None,
                            canonical=canonical_occurred_at,
                            delta=None,
                        ),
                    },
                    context={
                        "reason": "aggregate_missing",
                        "delivery_count": counts.get(key, 0),
                    },
                )
            )
            continue

        rollup_occurred_at = _get(aggregate, "last_occurred_at")
        rollup_delivery_id = _get(aggregate, "last_delivery_id")

        # An aggregate ahead of the window is not a window mismatch.
        if rollup_occurred_at is not None and (
            _as_utc_day(rollup_occurred_at) > window.to_date
        ):
            continue

        fields: dict[str, FieldDiff] = {}
        if rollup_delivery_id != canonical_delivery_id:
            fields["last_delivery_id"] = FieldDiff(
                rollup=rollup_delivery_id,
                canonical=canonical_delivery_id,
                delta=None,
            )
        if rollup_occurred_at != canonical_occurred_at:
            fields["last_occurred_at"] = FieldDiff(
                rollup=rollup_occurred_at,
                canonical=canonical_occurred_at,
                delta=_delta(canonical_occurred_at, rollup_occurred_at),
            )
        if fields:
            mismatches.append(
                Mismatch(
                    source=SOURCE_REPORTING_AGGREGATES,
                    day=canonical_day,
                    provider=provider,
                    repository=repository_url,
                    resource_type=resource_type,
                    resource_number=resource_number,
                    fields=fields,
                    context={
                        "reason": "pointer_mismatch",
                        "delivery_count": counts.get(key, 0),
                    },
                )
            )

    # ── Aggregates inside the window must have a backing delivery ──────
    for key, aggregate in aggregates.items():
        if key in latest:
            continue
        rollup_occurred_at = _get(aggregate, "last_occurred_at")
        if rollup_occurred_at is None:
            continue
        rollup_day = _as_utc_day(rollup_occurred_at)
        if not window.contains(rollup_day):
            continue
        provider, repository_url, resource_type, resource_number = key
        mismatches.append(
            Mismatch(
                source=SOURCE_REPORTING_AGGREGATES,
                day=rollup_day,
                provider=provider,
                repository=repository_url,
                resource_type=resource_type,
                resource_number=resource_number,
                fields={
                    "last_delivery_id": FieldDiff(
                        rollup=_get(aggregate, "last_delivery_id"),
                        canonical=None,
                        delta=None,
                    ),
                    "last_occurred_at": FieldDiff(
                        rollup=rollup_occurred_at,
                        canonical=None,
                        delta=None,
                    ),
                },
                context={"reason": "no_backing_delivery"},
            )
        )

    return mismatches


# ---------------------------------------------------------------------------
# Database fetch paths
# ---------------------------------------------------------------------------


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


async def _fetch_afk_dashboard_mismatches(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the ``afk_dashboard_daily`` mismatch query for the window."""
    rows = await conn.fetch(
        AFK_DASHBOARD_DAILY_MISMATCH_SQL, window.from_date, window.to_date,
    )
    return compare_afk_dashboard_daily_rows(rows)


# Backward-compatible alias for the historical fetch helper name.
_fetch_usage_mismatches = _fetch_afk_dashboard_mismatches


async def _fetch_reporting_mismatches(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run the reporting-aggregate comparison for the window."""
    aggregate_rows = await conn.fetch(REPORTING_AGGREGATES_SQL)
    delivery_rows = await conn.fetch(
        REPORTING_DELIVERIES_SQL, window.from_date, window.to_date,
    )
    return compare_reporting_aggregates(aggregate_rows, delivery_rows, window)


async def _run_verification(
    conn: asyncpg.Connection,
    window: VerificationWindow,
) -> list[Mismatch]:
    """Run every rollup-parity comparison and return grouped mismatches."""
    mismatches = await _fetch_afk_dashboard_mismatches(conn, window)
    mismatches.extend(await _fetch_reporting_mismatches(conn, window))
    return mismatches


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def group_mismatches(
    mismatches: Sequence[Mismatch],
) -> dict[tuple[date | None, str | None, str | None], list[Mismatch]]:
    """Group mismatches by ``(day, provider, repository)`` in stable order.

    Both comparison sources share the ``(day, provider, repository)`` grouping
    vocabulary, so one report groups every disagreement the same way.  Groups
    are ordered by the tuple with deterministic tie-breakers, preserving
    insertion order for equal keys.
    """
    ordered = sorted(
        mismatches,
        key=lambda m: (
            m.day or date.min,
            m.provider or "",
            m.repository or "",
            m.resource_type or "",
            m.resource_number or "",
            m.source,
        ),
    )
    groups: dict[tuple[date | None, str | None, str | None], list[Mismatch]] = {}
    for mismatch in ordered:
        groups.setdefault(mismatch.group_key, []).append(mismatch)
    return groups


def _exit_code(mismatches: Sequence[Mismatch]) -> int:
    """``0`` when every rollup matches, ``1`` when any mismatch was found."""
    return 1 if mismatches else 0


def _format_mismatch(mismatch: Mismatch) -> str:
    """Render one mismatch as a single operator-readable line.

    The metric breakdown names every ``afk_dashboard_daily`` metric; differing
    metrics are listed first so the affected metric names are front and centre.
    """
    parts = [f"{mismatch.source} day={mismatch.day} provider={mismatch.provider}"]
    if mismatch.repository is not None:
        parts.append(f"repository={mismatch.repository}")
    if mismatch.resource_type is not None:
        parts.append(f"type={mismatch.resource_type}")
    if mismatch.resource_number is not None:
        parts.append(f"number={mismatch.resource_number}")
    head = " ".join(parts)

    def _order(item: tuple[str, FieldDiff]) -> int:
        name, diff = item
        return 0 if diff.rollup != diff.canonical else 1

    detail = ", ".join(
        f"{name}: rollup={diff.rollup!r} canonical={diff.canonical!r}"
        for name, diff in sorted(mismatch.fields.items(), key=_order)
    )
    reason = mismatch.context.get("reason")
    reason_note = f" [{reason}]" if reason else ""
    return f"{head}{reason_note}: {detail}"


def _emit_report(
    window: VerificationWindow,
    mismatches: Sequence[Mismatch],
    *,
    as_json: bool = False,
) -> None:
    """Log a human-readable report (or emit JSON on stdout)."""
    if as_json:
        print(
            json.dumps(
                {
                    "window": {
                        "from_date": window.from_date.isoformat(),
                        "to_date": window.to_date.isoformat(),
                        "days": window.day_count,
                    },
                    "mismatch_count": len(mismatches),
                    "mismatches": [m.as_dict() for m in mismatches],
                },
                default=str,
            )
        )
        return

    logger.info(
        "AFK dashboard daily parity verification window %s..%s (%d day(s)): "
        "%d mismatch(es).",
        window.from_date,
        window.to_date,
        window.day_count,
        len(mismatches),
    )
    for (day, provider, repository), group in group_mismatches(mismatches).items():
        logger.warning(
            "Mismatch group day=%s provider=%s repository=%s (%d bucket(s)):",
            day,
            provider,
            repository,
            len(group),
        )
        for mismatch in group:
            logger.warning("  %s", _format_mismatch(mismatch))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only afk_dashboard_daily parity verification against "
        "the canonical AFK/usage/engineering source tables.",
    )
    parser.add_argument(
        "--window-days",
        dest="days",
        type=int,
        default=7,
        help="Verify the last N days including today (default: 7).",
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        default=None,
        help="Explicit inclusive window start (YYYY-MM-DD); requires --to-date.",
    )
    parser.add_argument(
        "--to-date",
        type=date.fromisoformat,
        default=None,
        help="Explicit inclusive window end (YYYY-MM-DD); requires --from-date.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON on stdout (for CronJob alerting).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Accepted for CronJob symmetry: this tool is always read-only, "
        "so dry-run only logs an explicit no-write confirmation.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, connect, verify, report; never write."""
    args = _parse_args(argv)
    window = parse_window(
        days=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    if args.dry_run:
        logger.info(
            "Read-only verification (dry-run): no rollup or canonical row "
            "will be modified, deleted, or updated.",
        )

    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            mismatches = await _run_verification(conn, window)
    finally:
        await pool.close()

    _emit_report(window, mismatches, as_json=args.json)
    return _exit_code(mismatches)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
