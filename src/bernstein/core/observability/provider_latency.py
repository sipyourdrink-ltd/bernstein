"""Provider API latency tracker with historical percentile charts (ROAD-155).

Tracks LLM API response latency per provider per model: p50, p95, p99 over time.
Detects provider degradation before it causes agent timeouts. Alerts when latency
exceeds historical p99 by 2x.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bernstein.core.observability.metric_collector import PercentileTracker

logger = logging.getLogger(__name__)

# Alert when current p99 exceeds baseline p99 by this multiplier
_DEGRADATION_THRESHOLD: float = 2.0

# Number of samples before we trust the baseline p99
_MIN_BASELINE_SAMPLES: int = 10

# Sliding window for in-memory percentile trackers (samples)
_WINDOW_SIZE: int = 500


@dataclass
class LatencyPercentiles:
    """Latency percentiles for a provider+model combination.

    Attributes:
        provider: Provider name (e.g., ``"anthropic"``).
        model: Model identifier (e.g., ``"claude-sonnet-4-6"``).
        p50_ms: 50th percentile latency in milliseconds.
        p95_ms: 95th percentile latency in milliseconds.
        p99_ms: 99th percentile latency in milliseconds.
        sample_count: Number of samples in the current window.
        baseline_p99_ms: Historical p99 baseline (0.0 if not established).
        timestamp: When these percentiles were computed.
    """

    provider: str
    model: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    sample_count: int
    baseline_p99_ms: float
    timestamp: float

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "provider": self.provider,
            "model": self.model,
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
            "sample_count": self.sample_count,
            "baseline_p99_ms": round(self.baseline_p99_ms, 2),
            "timestamp": self.timestamp,
        }


@dataclass
class DegradationAlert:
    """Alert emitted when a provider's latency exceeds its historical p99 by 2x.

    Attributes:
        provider: Affected provider name.
        model: Affected model identifier.
        current_p99_ms: Current p99 latency in milliseconds.
        baseline_p99_ms: Historical p99 baseline in milliseconds.
        ratio: current_p99_ms / baseline_p99_ms.
        timestamp: When the alert was raised.
        message: Human-readable summary.
    """

    provider: str
    model: str
    current_p99_ms: float
    baseline_p99_ms: float
    ratio: float
    timestamp: float
    message: str

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "provider": self.provider,
            "model": self.model,
            "current_p99_ms": round(self.current_p99_ms, 2),
            "baseline_p99_ms": round(self.baseline_p99_ms, 2),
            "ratio": round(self.ratio, 3),
            "timestamp": self.timestamp,
            "message": self.message,
        }


def _read_latency_samples(
    jsonl_file: Path,
    cutoff: float,
    provider: str | None,
    model: str | None,
    samples: list[dict[str, object]],
) -> None:
    """Read and filter latency samples from a single JSONL file."""
    try:
        for line in jsonl_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record: dict[str, object] = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(record.get("timestamp", 0))
            if ts < cutoff:
                continue
            if provider and record.get("provider") != provider:
                continue
            if model and record.get("model") != model:
                continue
            samples.append(record)
    except OSError:
        pass


class ProviderLatencyTracker:
    """Track per-provider-per-model API response latency percentiles over time.

    Persists latency samples to ``{metrics_dir}/provider_latency_{date}.jsonl``
    and computes a rolling p50/p95/p99 from an in-memory sliding window.

    Degradation detection: when current p99 exceeds the historical baseline
    p99 by 2x, a :class:`DegradationAlert` is returned from :meth:`record`.

    Args:
        metrics_dir: Directory to write latency JSONL files.
        window: Sliding window size for in-memory percentile tracking.
        degradation_threshold: Multiplier above baseline p99 that triggers an alert.
    """

    def __init__(
        self,
        metrics_dir: Path | None = None,
        *,
        window: int = _WINDOW_SIZE,
        degradation_threshold: float = _DEGRADATION_THRESHOLD,
    ) -> None:
        self._metrics_dir = metrics_dir or Path.cwd() / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        self._window = window
        self._degradation_threshold = degradation_threshold

        # In-memory trackers keyed by "provider:model"
        self._trackers: dict[str, PercentileTracker] = {}

        # Baseline p99 per key — loaded from persisted history on init
        self._baseline_p99: dict[str, float] = {}

        # Baseline sample counts — we only trust baselines above minimum
        self._baseline_sample_counts: dict[str, int] = {}

        self._lock = threading.Lock()
        self._load_baseline()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        provider: str,
        model: str,
        latency_ms: float,
    ) -> DegradationAlert | None:
        """Record a latency sample and return an alert if degradation is detected.

        Args:
            provider: Provider name (e.g., ``"anthropic"``).
            model: Model identifier (e.g., ``"claude-sonnet-4-6"``).
            latency_ms: Observed response latency in milliseconds.

        Returns:
            :class:`DegradationAlert` if current p99 has degraded ≥ 2x baseline,
            ``None`` otherwise.
        """
        if latency_ms < 0:
            return None

        key = _make_key(provider, model)
        with self._lock:
            if key not in self._trackers:
                self._trackers[key] = PercentileTracker(window=self._window)
            self._trackers[key].add(latency_ms)
            sample_count = self._trackers[key].count()

            # Persist the raw sample
            self._persist_sample(provider, model, latency_ms)

            # Check for degradation BEFORE updating the baseline — otherwise
            # the EMA absorbs any spike before the alert fires (test
            # test_degradation_alert_triggered reproducer). Alert must be
            # compared against the *existing* baseline, not the post-update
            # one.
            alert: DegradationAlert | None = None
            if sample_count >= _MIN_BASELINE_SAMPLES:
                baseline = self._baseline_p99.get(key, 0.0)
                current_p99 = self._trackers[key].p99()
                if baseline > 0.0:
                    ratio = current_p99 / baseline
                    if ratio >= self._degradation_threshold:
                        alert = DegradationAlert(
                            provider=provider,
                            model=model,
                            current_p99_ms=current_p99,
                            baseline_p99_ms=baseline,
                            ratio=ratio,
                            timestamp=time.time(),
                            message=(
                                f"{provider}/{model} latency degraded: "
                                f"p99={current_p99:.0f}ms is {ratio:.1f}x "
                                f"baseline={baseline:.0f}ms"
                            ),
                        )

            # Update baseline only when no degradation alert fired — this
            # keeps the baseline stable during sustained spikes so alerts
            # keep firing instead of the EMA silencing them.
            if sample_count >= _MIN_BASELINE_SAMPLES and alert is None:
                p99 = self._trackers[key].p99()
                existing = self._baseline_p99.get(key, 0.0)
                if not existing:
                    # First baseline
                    self._baseline_p99[key] = p99
                    self._baseline_sample_counts[key] = sample_count
                else:
                    # Exponential moving average to update baseline slowly
                    self._baseline_p99[key] = existing * 0.95 + p99 * 0.05
                    self._baseline_sample_counts[key] = sample_count

            return alert

    def get_percentiles(self, provider: str, model: str) -> LatencyPercentiles:
        """Get current latency percentiles for a specific provider+model.

        Args:
            provider: Provider name.
            model: Model identifier.

        Returns:
            :class:`LatencyPercentiles` with current p50/p95/p99 values.
        """
        key = _make_key(provider, model)
        with self._lock:
            tracker = self._trackers.get(key)
            if tracker is None:
                return LatencyPercentiles(
                    provider=provider,
                    model=model,
                    p50_ms=0.0,
                    p95_ms=0.0,
                    p99_ms=0.0,
                    sample_count=0,
                    baseline_p99_ms=self._baseline_p99.get(key, 0.0),
                    timestamp=time.time(),
                )
            return LatencyPercentiles(
                provider=provider,
                model=model,
                p50_ms=tracker.p50(),
                p95_ms=tracker.p95(),
                p99_ms=tracker.p99(),
                sample_count=tracker.count(),
                baseline_p99_ms=self._baseline_p99.get(key, 0.0),
                timestamp=time.time(),
            )

    def all_percentiles(self) -> list[LatencyPercentiles]:
        """Return current percentiles for all tracked provider+model combinations.

        Returns:
            List of :class:`LatencyPercentiles`, one per provider+model.
        """
        results = []
        with self._lock:
            for key, tracker in self._trackers.items():
                provider, model = _split_key(key)
                results.append(
                    LatencyPercentiles(
                        provider=provider,
                        model=model,
                        p50_ms=tracker.p50(),
                        p95_ms=tracker.p95(),
                        p99_ms=tracker.p99(),
                        sample_count=tracker.count(),
                        baseline_p99_ms=self._baseline_p99.get(key, 0.0),
                        timestamp=time.time(),
                    )
                )
        return results

    def get_history(
        self,
        provider: str | None = None,
        model: str | None = None,
        hours: int = 24,
    ) -> list[dict[str, object]]:
        """Load historical latency samples from JSONL files.

        Args:
            provider: Filter by provider (``None`` for all).
            model: Filter by model (``None`` for all).
            hours: How many hours of history to return (default 24).

        Returns:
            List of raw sample dicts ordered by timestamp ascending.
        """
        cutoff = time.time() - hours * 3600
        samples: list[dict[str, object]] = []

        for jsonl_file in sorted(self._metrics_dir.glob("provider_latency_*.jsonl")):
            _read_latency_samples(jsonl_file, cutoff, provider, model, samples)

        samples.sort(key=lambda r: float(r.get("timestamp", 0)))
        return samples

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_sample(self, provider: str, model: str, latency_ms: float) -> None:
        """Append a latency sample to today's JSONL file.

        Args:
            provider: Provider name.
            model: Model identifier.
            latency_ms: Latency in milliseconds.
        """
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        jsonl_file = self._metrics_dir / f"provider_latency_{date_str}.jsonl"
        record = {
            "timestamp": time.time(),
            "provider": provider,
            "model": model,
            "latency_ms": round(latency_ms, 2),
        }
        try:
            with jsonl_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning("Failed to persist latency sample: %s", exc)

    @staticmethod
    def _parse_latency_record(line: str, cutoff: float) -> tuple[str, float] | None:
        """Parse a single JSONL latency record, returning ``(key, latency_ms)`` or None."""
        line = line.strip()
        if not line:
            return None
        try:
            record: dict[str, object] = json.loads(line)
        except json.JSONDecodeError:
            return None
        ts = float(record.get("timestamp", 0))
        if ts < cutoff:
            return None
        provider = str(record.get("provider", ""))
        model = str(record.get("model", ""))
        latency_ms = float(record.get("latency_ms", 0))
        if not provider or not model or latency_ms <= 0:
            return None
        return _make_key(provider, model), latency_ms

    def _load_baseline(self) -> None:
        """Load historical p99 baselines by replaying persisted JSONL data.

        Reads the last 7 days of latency files and builds per-key
        PercentileTracker instances from which we extract the p99 baseline.
        """
        cutoff = time.time() - 7 * 24 * 3600
        key_samples: dict[str, list[float]] = {}

        for jsonl_file in sorted(self._metrics_dir.glob("provider_latency_*.jsonl")):
            try:
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    parsed = self._parse_latency_record(line, cutoff)
                    if parsed is not None:
                        key, latency_ms = parsed
                        key_samples.setdefault(key, []).append(latency_ms)
            except OSError:
                continue

        for key, samples in key_samples.items():
            if len(samples) >= _MIN_BASELINE_SAMPLES:
                tracker = PercentileTracker(window=len(samples))
                for s in samples:
                    tracker.add(s)
                self._baseline_p99[key] = tracker.p99()
                self._baseline_sample_counts[key] = len(samples)
                logger.debug(
                    "Loaded baseline p99=%.0fms for %s (%d samples)",
                    self._baseline_p99[key],
                    key,
                    len(samples),
                )


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_tracker_instance: ProviderLatencyTracker | None = None
_tracker_lock = threading.Lock()


def get_tracker(metrics_dir: Path | None = None) -> ProviderLatencyTracker:
    """Return the process-global :class:`ProviderLatencyTracker` singleton.

    Args:
        metrics_dir: Override the metrics directory (only applied on first call).

    Returns:
        Singleton :class:`ProviderLatencyTracker` instance.
    """
    global _tracker_instance
    with _tracker_lock:
        if _tracker_instance is None:
            _tracker_instance = ProviderLatencyTracker(metrics_dir)
    return _tracker_instance


# ------------------------------------------------------------------
# Key helpers
# ------------------------------------------------------------------


def _make_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _split_key(key: str) -> tuple[str, str]:
    parts = key.split(":", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return key, ""
