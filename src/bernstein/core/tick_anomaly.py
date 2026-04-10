"""Anomaly detection on orchestrator tick duration distribution.

Maintains a sliding window of recent tick durations and flags ticks
whose duration exceeds the configured percentile threshold.  Designed
to surface slow ticks caused by disk I/O stalls, git locks, or network
latency before they cascade into missed heartbeats or stalled agents.

Usage::

    detector = TickAnomalyDetector(window_size=100, percentile=95.0, min_samples=20)
    detector.record(tick_number=1, duration_ms=120.0)
    alert = detector.check(tick_number=2, duration_ms=5000.0)
    if alert is not None:
        log.warning("Tick anomaly: %s", alert.message)
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TickSample:
    """A single tick duration measurement.

    Attributes:
        tick_number: Sequential tick counter.
        duration_ms: Wall-clock tick duration in milliseconds.
        timestamp: Unix timestamp when the tick was recorded.
    """

    tick_number: int
    duration_ms: float
    timestamp: float


@dataclass(frozen=True)
class AnomalyAlert:
    """Alert raised when a tick duration exceeds the percentile threshold.

    Attributes:
        tick_number: The tick that triggered the alert.
        duration_ms: Observed duration of the anomalous tick.
        threshold_ms: Percentile-derived threshold that was exceeded.
        percentile: The percentile used for the threshold (e.g. 95.0).
        message: Human-readable description of the anomaly.
    """

    tick_number: int
    duration_ms: float
    threshold_ms: float
    percentile: float
    message: str


class TickAnomalyDetector:
    """Sliding-window anomaly detector for orchestrator tick durations.

    Collects tick duration samples in a fixed-size sliding window and
    uses percentile-based thresholds to identify anomalously slow ticks.

    Args:
        window_size: Maximum number of samples retained in the window.
        percentile: Percentile threshold for anomaly detection (0-100).
        min_samples: Minimum samples required before alerts are emitted.
    """

    def __init__(
        self,
        window_size: int = 100,
        percentile: float = 95.0,
        min_samples: int = 20,
    ) -> None:
        self._window_size = window_size
        self._percentile = percentile
        self._min_samples = min_samples
        self._samples: deque[TickSample] = deque(maxlen=window_size)

    def record(
        self,
        tick_number: int,
        duration_ms: float,
        timestamp: float | None = None,
    ) -> None:
        """Add a tick duration sample to the sliding window.

        Args:
            tick_number: Sequential tick counter.
            duration_ms: Wall-clock tick duration in milliseconds.
            timestamp: Unix timestamp; defaults to ``time.time()``.
        """
        ts = timestamp if timestamp is not None else time.time()
        self._samples.append(TickSample(
            tick_number=tick_number,
            duration_ms=duration_ms,
            timestamp=ts,
        ))

    def check(
        self,
        tick_number: int,
        duration_ms: float,
    ) -> AnomalyAlert | None:
        """Check whether a tick duration is anomalous.

        Returns an ``AnomalyAlert`` if the duration exceeds the
        configured percentile threshold and enough samples have been
        collected.  Returns ``None`` otherwise.

        Args:
            tick_number: Sequential tick counter of the tick to check.
            duration_ms: Observed duration of the tick in milliseconds.

        Returns:
            An alert if the tick is anomalous, or None.
        """
        if len(self._samples) < self._min_samples:
            return None

        threshold = self.get_percentile(self._percentile)
        if duration_ms <= threshold:
            return None

        return AnomalyAlert(
            tick_number=tick_number,
            duration_ms=duration_ms,
            threshold_ms=threshold,
            percentile=self._percentile,
            message=(
                f"Tick {tick_number} took {duration_ms:.1f}ms, "
                f"exceeding p{self._percentile:.0f} threshold of {threshold:.1f}ms"
            ),
        )

    def get_percentile(self, p: float) -> float:
        """Compute the *p*-th percentile from the current window.

        Uses ``statistics.quantiles`` for interpolated percentile
        calculation.

        Args:
            p: Percentile value between 0 and 100 (exclusive).

        Returns:
            The percentile value, or 0.0 if the window is empty.
        """
        if not self._samples:
            return 0.0

        durations = [s.duration_ms for s in self._samples]

        if len(durations) == 1:
            return durations[0]

        # statistics.quantiles divides data into n equal intervals;
        # requesting n=100 gives us the 1st through 99th percentiles.
        quantiles = statistics.quantiles(durations, n=100)

        # quantiles has 99 cut-points (indices 0..98) representing
        # percentiles 1..99.  Clamp p into that range.
        idx = max(0, min(int(p) - 1, len(quantiles) - 1))
        return quantiles[idx]

    def stats(self) -> dict[str, float]:
        """Return summary statistics from the current window.

        Returns:
            Dictionary with keys: mean, median, p95, p99, min, max, count.
        """
        if not self._samples:
            return {
                "mean": 0.0,
                "median": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "min": 0.0,
                "max": 0.0,
                "count": 0.0,
            }

        durations = [s.duration_ms for s in self._samples]
        return {
            "mean": statistics.mean(durations),
            "median": statistics.median(durations),
            "p95": self.get_percentile(95),
            "p99": self.get_percentile(99),
            "min": min(durations),
            "max": max(durations),
            "count": float(len(durations)),
        }

    def reset(self) -> None:
        """Clear all samples from the sliding window."""
        self._samples.clear()
