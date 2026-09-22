"""Shared comparison helpers for verification scripts (issue #729).

Extracted from ``verify_afk_dashboard_daily.py`` so both the AFK dashboard
verifier and the reporting resource aggregates verifier can import the common
types and comparison logic without circular dependencies.

Provides:

* **Types**: ``FieldDiff``, ``Mismatch``, ``VerificationWindow``
* **Window parsing**: ``parse_window``
* **Pure comparison helpers**: ``canonical_latest_by_resource``,
  ``compare_reporting_aggregates``, ``compare_afk_dashboard_daily_rows``
* **Row helpers**: ``_get``, ``_delta``, ``_as_utc_day``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from app.core.reporting_aggregates import (  # noqa: E402
    resource_identity_from_payload,
)


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
    *,
    metric_columns: Sequence[str],
    source_label: str,
) -> list[Mismatch]:
    """Map SQL mismatch rows (already filtered) into grouped mismatch records.

    Every row returned by the AFK dashboard daily mismatch query is a genuine
    disagreement, keyed per ``(day, provider, repository)``.  Each of the
    additive metric columns is compared, so the breakdown names every metric —
    the differing ones carry a non-zero delta, the matching ones a zero delta.

    ``metric_columns`` is the list of metric column names to compare
    (imported from the engine module by the caller).
    ``source_label`` identifies the source label for the mismatch
    (e.g. ``"afk_dashboard_daily"``).
    """
    mismatches: list[Mismatch] = []
    for row in rows:
        fields = {
            name: FieldDiff(
                rollup=_get(row, f"rollup_{name}"),
                canonical=_get(row, f"canonical_{name}"),
                delta=_delta(_get(row, f"canonical_{name}"), _get(row, f"rollup_{name}")),
            )
            for name in metric_columns
        }
        mismatched_metrics = [
            name
            for name, diff in fields.items()
            if diff.rollup != diff.canonical
        ]
        mismatches.append(
            Mismatch(
                source=source_label,
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
                    source="reporting_resource_aggregates",
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
                    source="reporting_resource_aggregates",
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
                source="reporting_resource_aggregates",
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
# Reporting helpers
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


def exit_code(mismatches: Sequence[Mismatch]) -> int:
    """``0`` when every rollup matches, ``1`` when any mismatch was found."""
    return 1 if mismatches else 0


def format_mismatch(mismatch: Mismatch) -> str:
    """Render one mismatch as a single operator-readable line.

    The metric breakdown names every metric; differing metrics are listed first
    so the affected metric names are front and centre.
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
