"""Tests for the metrics registry snapshot/render seam (issue #482)."""

from __future__ import annotations

from app.core.metrics import MetricsRegistry


def test_snapshot_returns_flat_json_safe_shape() -> None:
    """Counters/gauges render as scalars; histograms as {count, sum, buckets}.

    The snapshot is the structured-log ``extra`` / future ``/metrics``
    payload, so it must be flat and JSON-safe: no nested metric objects,
    and histogram bucket keys are strings.
    """
    registry = MetricsRegistry()
    registry.counter("afk_consumer.messages.total").inc(3)
    registry.gauge("afk_consumer.lag.0").set(12.5)
    registry.histogram("afk_consumer.retries.per_message").observe(0.5)
    registry.histogram("afk_consumer.retries.per_message").observe(2.0)

    snapshot = registry.snapshot()

    assert snapshot["afk_consumer.messages.total"] == 3
    assert snapshot["afk_consumer.lag.0"] == 12.5

    histogram = snapshot["afk_consumer.retries.per_message"]
    assert histogram["count"] == 2
    assert histogram["sum"] == 2.5
    # Bucket keys are stable, JSON-safe string labels (default buckets).
    assert histogram["buckets"] == {
        "0": 0,
        "1": 1,
        "2": 1,
        "3": 0,
        "4": 0,
        "5": 0,
        "+Inf": 0,
    }


def test_render_is_snapshot() -> None:
    """``render`` is an alias of ``snapshot`` — same output, same function."""
    registry = MetricsRegistry()
    registry.counter("afk_consumer.messages.total").inc()

    assert MetricsRegistry.render is MetricsRegistry.snapshot
    assert registry.render() == registry.snapshot()
