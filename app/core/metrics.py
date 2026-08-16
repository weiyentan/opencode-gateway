"""Lightweight, stdlib-only metrics registry (issue #482).

The Gateway has no Prometheus/counter infrastructure today —
:mod:`app.core.telemetry` is structured logging + timing only.  This module
introduces the smallest possible operational-metrics surface: process-local,
thread-safe counters, gauges, and histograms that the AFK outcome consumer
instruments, plus a snapshot/render seam for structured logging or a future
exposition endpoint.

Design rules (mirrors :mod:`app.core.telemetry`):

* **Stdlib only.** No Prometheus client dependency; a plain
  :class:`threading.Lock` guards each metric against cross-task races.
* **Stable metric names.** Names are dotted strings (e.g.
  ``afk_consumer.messages.total``); treat them as a contract — do not rename
  existing names.
* **Explicit reset for tests.** :meth:`MetricsRegistry.reset` clears every
  registered metric so tests can inject a fresh registry and assert exact
  counts without cross-test contamination.
* **Snapshot-friendly.** :meth:`MetricsRegistry.snapshot` returns a flat,
  JSON-safe dict (``{name: value}``) suitable for a structured-log ``extra``
  or a future ``/metrics`` exposition endpoint.

Usage::

    registry = MetricsRegistry()
    registry.counter("afk_consumer.messages.total").inc()
    registry.gauge("afk_consumer.lag.0").set(12)
    registry.histogram("afk_consumer.retries.per_message").observe(2)
    registry.snapshot()
"""

from __future__ import annotations

import threading
from typing import Any

# Default histogram bucket upper-bounds (retry counts and similar small
# cardinalities).  The final bucket is +inf so no observation is ever dropped.
_DEFAULT_BUCKETS: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, float("inf"))


def _bucket_label(upper: float) -> str:
    """Render a bucket upper-bound as a stable, JSON-safe string label."""
    if upper == float("inf"):
        return "+Inf"
    if upper.is_integer():
        return str(int(upper))
    return str(upper)


class Counter:
    """A monotonically increasing counter."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    def value(self) -> int:
        with self._lock:
            return self._value


class Gauge:
    """A settable gauge that also supports inc/dec deltas."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    def value(self) -> float:
        with self._lock:
            return self._value


class Histogram:
    """A simple bucket histogram with a running sum and observation count."""

    def __init__(
        self,
        name: str,
        buckets: tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self._buckets = tuple(sorted(buckets))
        self._counts: list[int] = [0] * len(self._buckets)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._count += 1
            self._sum += value
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    self._counts[i] += 1
                    break

    def count(self) -> int:
        with self._lock:
            return self._count

    def sum(self) -> float:
        with self._lock:
            return self._sum

    def buckets(self) -> dict[str, int]:
        with self._lock:
            return {
                _bucket_label(upper): count
                for upper, count in zip(self._buckets, self._counts)
            }


class MetricsRegistry:
    """A namespaced, thread-safe registry of counters, gauges, and histograms.

    ``counter`` / ``gauge`` / ``histogram`` are get-or-create: the first call
    fixes the metric's type, and a later call requesting a different type for
    the same name raises :class:`ValueError`.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, Any] = {}
        self._lock = threading.Lock()

    def counter(self, name: str) -> Counter:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Counter(name)
                self._metrics[name] = metric
            elif not isinstance(metric, Counter):
                raise ValueError(
                    f"metric {name!r} is already a {type(metric).__name__}, not a Counter"
                )
            return metric

    def gauge(self, name: str) -> Gauge:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Gauge(name)
                self._metrics[name] = metric
            elif not isinstance(metric, Gauge):
                raise ValueError(
                    f"metric {name!r} is already a {type(metric).__name__}, not a Gauge"
                )
            return metric

    def histogram(
        self,
        name: str,
        buckets: tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> Histogram:
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = Histogram(name, buckets)
                self._metrics[name] = metric
            elif not isinstance(metric, Histogram):
                raise ValueError(
                    f"metric {name!r} is already a {type(metric).__name__}, not a Histogram"
                )
            return metric

    def get(self, name: str) -> Any | None:
        """Return the metric registered under ``name``, or ``None``."""
        with self._lock:
            return self._metrics.get(name)

    def snapshot(self) -> dict[str, Any]:
        """Return a flat, JSON-safe dict of every registered metric.

        Counters and gauges render as scalars; histograms render as a dict
        with ``count``, ``sum`` and ``buckets`` (string bucket labels).
        """
        out: dict[str, Any] = {}
        with self._lock:
            for name, metric in self._metrics.items():
                if isinstance(metric, Counter):
                    out[name] = metric.value()
                elif isinstance(metric, Gauge):
                    out[name] = metric.value()
                else:
                    out[name] = {
                        "count": metric.count(),
                        "sum": metric.sum(),
                        "buckets": metric.buckets(),
                    }
        return out

    render = snapshot  # alias: the flat dict is the structured-log payload

    def reset(self) -> None:
        """Clear all metrics (primarily for isolated test runs)."""
        with self._lock:
            self._metrics.clear()


# Process-wide default registry, shared by the consumers.  Tests inject their
# own :class:`MetricsRegistry` instance to assert exact counts in isolation.
DEFAULT_REGISTRY = MetricsRegistry()
